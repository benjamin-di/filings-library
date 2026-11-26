"""Boundary tests for FilingsService, FastMCP HTTP server, and CLI."""

from __future__ import annotations

import json
from collections.abc import Collection
from pathlib import Path
from typing import Any

import httpx
import pytest

from filings_library import cli
from filings_library.cas import ContentAddressedStore
from filings_library.mcp_server import create_http_app
from filings_library.models import (
    CompanyTarget,
    DocumentRecord,
    ExtractedSection,
    FormFamily,
    SourceAdapter,
)
from filings_library.search import DocumentNotFoundError
from filings_library.service import FilingsService, StaticScopeResolver
from filings_library.store import FilingsStore
from filings_library.targets_client import HubUnavailableError

VALID_BEARER = "a" * 32 + "-valid-backend-bearer-token"


class _FailingScopeResolver:
    async def allowed_source_keys(self, person: str) -> Collection[str]:
        raise HubUnavailableError(f"hub unreachable for {person}")


def _seed_corpus(tmp_path: Path) -> tuple[FilingsStore, ContentAddressedStore, dict[str, str]]:
    """Seed two isolated companies: `alpha` (alice) and `beta` (bob)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    store = FilingsStore(data_dir / "filings.sqlite3")
    cas = ContentAddressedStore(data_dir)

    alpha = CompanyTarget(
        source_key="alpha",
        ticker="ALPH",
        exchange="NASDAQ",
        name="Alpha Holdings Corp",
        sec_cik=100001,
    )
    beta = CompanyTarget(
        source_key="beta",
        ticker="BETA",
        exchange="NYSE",
        name="Beta Systems Inc",
        sec_cik=200002,
    )
    store.upsert_company(alpha)
    store.upsert_company(beta)

    alpha_raw = b"alpha raw filing bytes"
    alpha_sha = cas.put_bytes(alpha_raw)
    alpha_md = "# Alpha 10-K\n\nAlpha exclusive photonics revenue was $22,594 million."
    alpha_md_sha = cas.put_bytes(alpha_md.encode("utf-8"))
    alpha_doc = DocumentRecord(
        document_id="doc-alpha-10k",
        source_key="alpha",
        adapter=SourceAdapter.SEC_EDGAR,
        external_id="000100001-25-000001:alpha10k.htm",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        title="Alpha FY2025 10-K",
        filing_date="2026-02-15",
        period_end="2025-12-31",
        source_url="https://example.invalid/alpha-10k.htm",
        source_sha256=alpha_sha,
        source_mime="text/html",
        source_bytes=len(alpha_raw),
        converter_id="html-ixbrl",
        converter_version="1.0.0",
        markdown_sha256=alpha_md_sha,
    )
    store.upsert_document(alpha_doc)
    store.replace_sections(
        alpha_doc.document_id,
        [
            ExtractedSection(
                locator="section:item-1",
                title="Item 1 Business",
                ordinal=0,
                markdown="Alpha exclusive photonics revenue was $22,594 million.",
            ),
            ExtractedSection(
                locator="section:item-7",
                title="Item 7 MD&A",
                ordinal=1,
                markdown="Alpha operating margin expanded sharply in FY2025.",
            ),
        ],
    )

    beta_raw = b"beta raw filing bytes"
    beta_sha = cas.put_bytes(beta_raw)
    beta_md = "# Beta 10-K\n\nBeta exclusive robotics revenue was $9,100 million."
    beta_md_sha = cas.put_bytes(beta_md.encode("utf-8"))
    beta_doc = DocumentRecord(
        document_id="doc-beta-10k",
        source_key="beta",
        adapter=SourceAdapter.SEC_EDGAR,
        external_id="000200002-25-000001:beta10k.htm",
        form_type="10-K",
        form_family=FormFamily.ANNUAL_REPORT,
        title="Beta FY2025 10-K",
        filing_date="2026-02-20",
        period_end="2025-12-31",
        source_url="https://example.invalid/beta-10k.htm",
        source_sha256=beta_sha,
        source_mime="text/html",
        source_bytes=len(beta_raw),
        converter_id="html-ixbrl",
        converter_version="1.0.0",
        markdown_sha256=beta_md_sha,
    )
    store.upsert_document(beta_doc)
    store.replace_sections(
        beta_doc.document_id,
        [
            ExtractedSection(
                locator="section:item-1",
                title="Item 1 Business",
                ordinal=0,
                markdown="Beta exclusive robotics revenue was $9,100 million.",
            ),
        ],
    )

    alpha_facts = {
        "cik": 100001,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 22594000000,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-15",
                                "accn": "000100001-25-000001",
                            }
                        ]
                    }
                }
            }
        },
    }
    alpha_facts_sha = cas.put_bytes(json.dumps(alpha_facts).encode("utf-8"))
    store.upsert_company_facts("alpha", 100001, alpha_facts_sha)

    beta_facts = {
        "cik": 200002,
        "facts": {
            "us-gaap": {
                "Revenues": {
                    "units": {
                        "USD": [
                            {
                                "val": 9100000000,
                                "start": "2025-01-01",
                                "end": "2025-12-31",
                                "fy": 2025,
                                "fp": "FY",
                                "form": "10-K",
                                "filed": "2026-02-20",
                                "accn": "000200002-25-000001",
                            }
                        ]
                    }
                }
            }
        },
    }
    beta_facts_sha = cas.put_bytes(json.dumps(beta_facts).encode("utf-8"))
    store.upsert_company_facts("beta", 200002, beta_facts_sha)

    return store, cas, {"alpha_doc": alpha_doc.document_id, "beta_doc": beta_doc.document_id}


async def _mcp_call(
    client: httpx.AsyncClient,
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    token: str | None = VALID_BEARER,
    subject: str | None = "bob",
    host: str = "testserver",
) -> httpx.Response:
    headers: dict[str, str] = {
        "Accept": "application/json, text/event-stream",
        "Host": host,
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if subject is not None:
        headers["X-Fleet-Subject"] = subject
    return await client.post(
        "/mcp",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
        },
    )


def _extract_mcp_result(resp: httpx.Response) -> Any:
    payload = resp.json()
    assert "result" in payload, f"Expected result in MCP payload, got: {payload}"
    result = payload["result"]
    assert not result.get("isError"), f"Unexpected MCP tool error: {result}"
    if "structuredContent" in result and "result" in result["structuredContent"]:
        return result["structuredContent"]["result"]
    text = result["content"][0]["text"]
    return json.loads(text)


# ---------------------------------------------------------------------------
# HTTP Auth, Host, Health, and Cache-Control boundaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_health_auth_host_and_cache_control(tmp_path: Path) -> None:
    store, cas, _ids = _seed_corpus(tmp_path)
    try:
        resolver = StaticScopeResolver({"alice": ["alpha"], "bob": ["beta"]})
        service = FilingsService(store, cas, resolver)
        app = create_http_app(
            service,
            backend_bearer_token=VALID_BEARER,
            allowed_hosts=["testserver"],
        )

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # 1. GET /health works with no auth and sets Cache-Control: no-store
                health = await client.get("/health")
                assert health.status_code == 200
                assert health.json() == {"status": "ok"}
                assert health.headers.get("cache-control") == "no-store"

                # 2. Missing bearer token -> 401
                r_no_auth = await _mcp_call(client, "filings_list_companies", token=None)
                assert r_no_auth.status_code == 401
                assert r_no_auth.headers.get("cache-control") == "no-store"

                # 3. Wrong bearer token -> 401
                r_wrong = await _mcp_call(client, "filings_list_companies", token="x" * 40)
                assert r_wrong.status_code == 401
                assert r_wrong.headers.get("cache-control") == "no-store"

                # 4. Missing X-Fleet-Subject -> 403
                r_no_subj = await _mcp_call(client, "filings_list_companies", subject=None)
                assert r_no_subj.status_code == 403
                assert r_no_subj.headers.get("cache-control") == "no-store"

                # 5. Empty X-Fleet-Subject -> 403
                r_empty_subj = await _mcp_call(client, "filings_list_companies", subject="   ")
                assert r_empty_subj.status_code == 403

                # 6. Disallowed Host header -> 400 (TrustedHostMiddleware)
                r_bad_host = await _mcp_call(client, "filings_list_companies", host="evil.example")
                assert r_bad_host.status_code in {400, 403}

                # 7. Valid request -> 200 with Cache-Control: no-store
                r_ok = await _mcp_call(client, "filings_list_companies", subject="bob")
                assert r_ok.status_code == 200
                assert r_ok.headers.get("cache-control") == "no-store"

                # 8. Non-ASCII bearer token -> 401 with Cache-Control: no-store (never 500 TypeError)
                r_non_ascii = await client.post(
                    "/mcp",
                    headers=httpx.Headers(
                        [
                            (b"accept", b"application/json, text/event-stream"),
                            (b"host", b"testserver"),
                            (b"authorization", b"Bearer caf\xe9-" + (b"x" * 32)),
                            (b"x-fleet-subject", b"bob"),
                        ]
                    ),
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "filings_list_companies", "arguments": {}},
                    },
                )
                assert r_non_ascii.status_code == 401
                assert r_non_ascii.headers.get("cache-control") == "no-store"

                # 9. Duplicate Authorization header -> 401 with Cache-Control: no-store
                r_dup_auth = await client.post(
                    "/mcp",
                    headers=[
                        ("Accept", "application/json, text/event-stream"),
                        ("Host", "testserver"),
                        ("Authorization", f"Bearer {VALID_BEARER}"),
                        ("Authorization", f"Bearer {VALID_BEARER}"),
                        ("X-Fleet-Subject", "bob"),
                    ],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "filings_list_companies", "arguments": {}},
                    },
                )
                assert r_dup_auth.status_code == 401
                assert r_dup_auth.headers.get("cache-control") == "no-store"

                # 10. Duplicate X-Fleet-Subject header -> 403 with Cache-Control: no-store
                r_dup_subj = await client.post(
                    "/mcp",
                    headers=[
                        ("Accept", "application/json, text/event-stream"),
                        ("Host", "testserver"),
                        ("Authorization", f"Bearer {VALID_BEARER}"),
                        ("X-Fleet-Subject", "bob"),
                        ("X-Fleet-Subject", "alice"),
                    ],
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {"name": "filings_list_companies", "arguments": {}},
                    },
                )
                assert r_dup_subj.status_code == 403
                assert r_dup_subj.headers.get("cache-control") == "no-store"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_mcp_token_file_error_returns_503_without_path_leak(tmp_path: Path) -> None:
    from filings_library.service import TargetsScopeResolver
    from filings_library.targets_client import LibraryTargetsClient

    store, cas, _ids = _seed_corpus(tmp_path)
    missing_token = tmp_path / "secret-dir" / "missing.token"
    try:
        targets_client = LibraryTargetsClient(
            base_url="https://hub.example.test",
            token_file=missing_token,
        )
        service = FilingsService(store, cas, TargetsScopeResolver(targets_client))
        app = create_http_app(
            service,
            backend_bearer_token=VALID_BEARER,
            allowed_hosts=["testserver"],
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                resp = await _mcp_call(client, "filings_list_companies", subject="bob")
                assert resp.status_code == 503
                assert resp.headers.get("cache-control") == "no-store"
                assert str(missing_token) not in resp.text
                assert resp.json()["detail"] == "targets bearer token unavailable"
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_configured_token", [None, "", "too-short-token-under-32-chars"])
async def test_http_unconfigured_or_short_bearer_fails_closed(
    tmp_path: Path, bad_configured_token: str | None
) -> None:
    store, cas, _ids = _seed_corpus(tmp_path)
    try:
        service = FilingsService(store, cas, StaticScopeResolver({"bob": ["beta"]}))
        app = create_http_app(
            service,
            backend_bearer_token=bad_configured_token,
            allowed_hosts=["testserver"],
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # Even if client echoes the exact short configured token, must fail closed 401.
                resp = await _mcp_call(
                    client,
                    "filings_list_companies",
                    token=bad_configured_token or VALID_BEARER,
                    subject="bob",
                )
                assert resp.status_code == 401
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Strict multi-user isolation across ALL 6 operations
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_multi_user_isolation_all_six_tools(tmp_path: Path) -> None:
    store, cas, ids = _seed_corpus(tmp_path)
    try:
        resolver = StaticScopeResolver({"alice": ["alpha"], "bob": ["beta"]})
        service = FilingsService(store, cas, resolver)
        app = create_http_app(
            service,
            backend_bearer_token=VALID_BEARER,
            allowed_hosts=["testserver"],
        )

        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                # 1. filings_list_companies
                bob_cos = _extract_mcp_result(
                    await _mcp_call(client, "filings_list_companies", subject="bob")
                )
                assert [c["source_key"] for c in bob_cos] == ["beta"]

                alice_cos = _extract_mcp_result(
                    await _mcp_call(client, "filings_list_companies", subject="alice")
                )
                assert [c["source_key"] for c in alice_cos] == ["alpha"]

                # 2. filings_list_documents (unfiltered & explicit cross-tenant filter)
                bob_docs = _extract_mcp_result(
                    await _mcp_call(client, "filings_list_documents", subject="bob")
                )
                assert [d["source_key"] for d in bob_docs] == ["beta"]

                bob_alpha_docs = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_list_documents",
                        {"source_key": "alpha"},
                        subject="bob",
                    )
                )
                assert bob_alpha_docs == []

                # 3. filings_search (alpha-only term invisible to bob; beta term visible)
                bob_search_alpha = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_search",
                        {"query": "photonics"},
                        subject="bob",
                    )
                )
                assert bob_search_alpha == []

                bob_search_beta = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_search",
                        {"query": "robotics"},
                        subject="bob",
                    )
                )
                assert len(bob_search_beta) == 1
                assert bob_search_beta[0]["source_key"] == "beta"

                # 4. filings_read (bob cannot read alpha's document; can read beta's)
                bob_read_alpha = await _mcp_call(
                    client,
                    "filings_read",
                    {"document_id": ids["alpha_doc"]},
                    subject="bob",
                )
                assert bob_read_alpha.status_code == 200
                assert bob_read_alpha.json()["result"]["isError"] is True

                bob_read_beta = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_read",
                        {"document_id": ids["beta_doc"]},
                        subject="bob",
                    )
                )
                assert bob_read_beta["document"]["source_key"] == "beta"

                # 5. filings_facts (unfiltered & explicit cross-tenant source_keys)
                bob_facts = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_facts",
                        {"concepts": ["Revenues"]},
                        subject="bob",
                    )
                )
                assert len(bob_facts) == 1
                assert bob_facts[0]["source_key"] == "beta"
                assert bob_facts[0]["val"] == 9100000000

                bob_facts_alpha = _extract_mcp_result(
                    await _mcp_call(
                        client,
                        "filings_facts",
                        {"concepts": ["Revenues"], "source_keys": ["alpha"]},
                        subject="bob",
                    )
                )
                assert bob_facts_alpha == []

                # 6. filings_status (strictly bob's issuers, no alpha or corpus totals)
                bob_status = _extract_mcp_result(
                    await _mcp_call(client, "filings_status", subject="bob")
                )
                assert bob_status["company_count"] == 1
                assert [c["source_key"] for c in bob_status["companies"]] == ["beta"]
                assert bob_status["companies"][0]["has_company_facts"] is True
                assert bob_status["companies"][0]["counts_by_form_family"] == {"annual_report": 1}
                assert "alpha" not in json.dumps(bob_status)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# Fail-closed HubUnavailableError propagation & HTTP 503 mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hub_unavailable_fails_closed_service_and_http(tmp_path: Path) -> None:
    store, cas, ids = _seed_corpus(tmp_path)
    try:
        service = FilingsService(store, cas, _FailingScopeResolver())

        # Direct service calls must all propagate HubUnavailableError
        with pytest.raises(HubUnavailableError):
            await service.list_companies("bob")
        with pytest.raises(HubUnavailableError):
            await service.list_documents("bob")
        with pytest.raises(HubUnavailableError):
            await service.search("bob", "revenue")
        with pytest.raises(HubUnavailableError):
            await service.read("bob", ids["beta_doc"])
        with pytest.raises(HubUnavailableError):
            await service.facts("bob", ["Revenues"])
        with pytest.raises(HubUnavailableError):
            await service.status("bob")

        # HTTP /mcp must map HubUnavailableError to HTTP 503 with Cache-Control: no-store
        app = create_http_app(
            service,
            backend_bearer_token=VALID_BEARER,
            allowed_hosts=["testserver"],
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                for tool_name, args in [
                    ("filings_list_companies", {}),
                    ("filings_list_documents", {}),
                    ("filings_search", {"query": "revenue"}),
                    ("filings_read", {"document_id": ids["beta_doc"]}),
                    ("filings_facts", {"concepts": ["Revenues"]}),
                    ("filings_status", {}),
                ]:
                    resp = await _mcp_call(client, tool_name, args, subject="bob")
                    assert resp.status_code == 503, f"{tool_name} returned {resp.status_code}"
                    assert resp.headers.get("cache-control") == "no-store"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_service_direct_read_cross_tenant_raises(tmp_path: Path) -> None:
    store, cas, ids = _seed_corpus(tmp_path)
    try:
        service = FilingsService(store, cas, StaticScopeResolver({"bob": ["beta"]}))
        with pytest.raises(DocumentNotFoundError):
            await service.read("bob", ids["alpha_doc"])
    finally:
        store.close()


# ---------------------------------------------------------------------------
# CLI round-trips
# ---------------------------------------------------------------------------


def test_cli_round_trips(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    store, _cas, ids = _seed_corpus(tmp_path)
    store.close()
    data_dir = str(tmp_path / "data")

    # 1. search --all --json
    rc = cli.main(["search", "22,594", "--data-dir", data_dir, "--all", "--json"])
    assert rc == 0
    hits = json.loads(capsys.readouterr().out)
    assert len(hits) == 1
    assert hits[0]["source_key"] == "alpha"

    # 2. read --all --start --end --json
    rc = cli.main(
        [
            "read",
            ids["alpha_doc"],
            "--data-dir",
            data_dir,
            "--all",
            "--start",
            "section:item-7",
            "--end",
            "section:item-7",
            "--json",
        ]
    )
    assert rc == 0
    read_out = json.loads(capsys.readouterr().out)
    assert len(read_out["sections"]) == 1
    assert read_out["sections"][0]["locator"] == "section:item-7"

    # 3. facts --all --source-key --json
    rc = cli.main(
        [
            "facts",
            "Revenues",
            "--data-dir",
            data_dir,
            "--all",
            "--source-key",
            "alpha",
            "--json",
        ]
    )
    assert rc == 0
    facts_out = json.loads(capsys.readouterr().out)
    assert len(facts_out) == 1
    assert facts_out[0]["source_key"] == "alpha"
    assert facts_out[0]["val"] == 22594000000

    # 4. status --all --json
    rc = cli.main(["status", "--data-dir", data_dir, "--all", "--json"])
    assert rc == 0
    status_out = json.loads(capsys.readouterr().out)
    assert status_out["company_count"] == 2
    assert {c["source_key"] for c in status_out["companies"]} == {"alpha", "beta"}

    # 5. export-md
    export_dir = tmp_path / "exported_md"
    rc = cli.main(["export-md", str(export_dir), "--data-dir", data_dir, "--json"])
    assert rc == 0
    export_out = json.loads(capsys.readouterr().out)
    assert export_out["exported"] == 2
    assert all(Path(p).is_file() for p in export_out["files"])

    # 6. rebuild-fts
    rc = cli.main(["rebuild-fts", "--data-dir", data_dir, "--json"])
    assert rc == 0
    rebuild_out = json.loads(capsys.readouterr().out)
    assert rebuild_out["indexed_chunks"] == 3

    # Verify search still works after rebuild-fts
    rc = cli.main(["search", "robotics", "--data-dir", data_dir, "--all", "--json"])
    assert rc == 0
    hits_after = json.loads(capsys.readouterr().out)
    assert len(hits_after) == 1
    assert hits_after[0]["source_key"] == "beta"
