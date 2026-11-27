"""Command-line interface (`filings-library`) for sync, query, export, and servers."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import uvicorn

from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient, create_extractor_app
from filings_library.mcp_server import create_http_app
from filings_library.service import (
    AllCompaniesScopeResolver,
    FilingsService,
    ScopeResolver,
    TargetsScopeResolver,
)
from filings_library.store import DEFAULT_GLOBAL_CAP_BYTES, FilingsStore
from filings_library.targets_client import LibraryTargetsClient
from filings_library.worker import (
    DEFAULT_PER_ISSUER_CAP_BYTES,
    DEFAULT_PER_ISSUER_MAX_DOCS,
    SyncConfig,
    export_markdown,
    sync_once,
)

DEFAULT_DATA_DIR = "data"
DB_FILENAME = "filings.sqlite3"


def _open_store_and_cas(data_dir: Path | str) -> tuple[FilingsStore, ContentAddressedStore]:
    root = Path(data_dir)
    root.mkdir(parents=True, exist_ok=True)
    store = FilingsStore(root / DB_FILENAME)
    cas = ContentAddressedStore(root)
    return store, cas


def _build_targets_client(args: argparse.Namespace) -> LibraryTargetsClient:
    targets_url = getattr(args, "targets_url", None) or os.environ.get("FILINGS_TARGETS_URL", "")
    if not targets_url:
        raise ValueError(
            "Targets hub URL is required (pass --targets-url or set FILINGS_TARGETS_URL)"
        )
    token = getattr(args, "targets_token", None) or os.environ.get("FILINGS_TARGETS_TOKEN")
    token_file = getattr(args, "targets_token_file", None) or os.environ.get(
        "FILINGS_TARGETS_TOKEN_FILE"
    )
    return LibraryTargetsClient(
        base_url=targets_url,
        token=token,
        token_file=token_file,
    )


def _build_scope_resolver(
    args: argparse.Namespace, store: FilingsStore
) -> tuple[ScopeResolver, str]:
    if getattr(args, "all", False):
        return AllCompaniesScopeResolver(store), "__all__"
    person = getattr(args, "person", None)
    if not person:
        raise ValueError("Specify --person ID or --all")
    return TargetsScopeResolver(_build_targets_client(args)), str(person)


def _add_common_data_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("FILINGS_DATA_DIR", DEFAULT_DATA_DIR),
        help="Directory containing filings.sqlite3 and objects/sha256 CAS",
    )


def _add_targets_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--targets-url",
        default=None,
        help="Base URL of targets hub (env: FILINGS_TARGETS_URL)",
    )
    parser.add_argument(
        "--targets-token",
        default=None,
        help="Bearer token for targets hub (env: FILINGS_TARGETS_TOKEN)",
    )
    parser.add_argument(
        "--targets-token-file",
        default=None,
        help="Path to 0600 bearer token file (env: FILINGS_TARGETS_TOKEN_FILE)",
    )


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--person", help="Person/subject identifier scoped via targets hub")
    group.add_argument(
        "--all",
        action="store_true",
        help="Local operator mode across all companies in the local store",
    )
    _add_targets_args(parser)


def _print_output(data: Any, *, as_json: bool, text_formatter: Any = None) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True))
    elif text_formatter is not None:
        print(text_formatter(data))
    else:
        print(json.dumps(data, indent=2, sort_keys=True))


def _format_search(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No matching filings found."
    lines: list[str] = []
    for hit in hits:
        lines.append(
            f"[{hit['score']:.3f}] {hit['source_key']} {hit['form_type']} "
            f"({hit['filing_date']}) {hit['document_id']}#{hit['locator']}"
        )
        if hit.get("snippet"):
            lines.append(f"  {hit['snippet']}")
    return "\n".join(lines)


def _format_read(payload: dict[str, Any]) -> str:
    doc = payload["document"]
    lines = [
        f"# {doc['title']} ({doc['source_key']} {doc['form_type']} {doc['filing_date']})",
        f"document_id: {doc['document_id']} | truncated: {payload['truncated']}",
        "",
    ]
    for sec in payload["sections"]:
        lines.append(f"## [{sec['locator']}] {sec['title']}")
        lines.append(sec["markdown"])
        lines.append("")
    return "\n".join(lines).rstrip()


def _format_facts(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No matching XBRL facts found."
    lines: list[str] = []
    for r in rows:
        lines.append(
            f"{r['source_key']} ({r['ticker']}) {r['concept']} = {r['val']} {r['unit']} "
            f"[end={r['end']} fy={r['fy']} fp={r['fp']} form={r['form']}]"
        )
    return "\n".join(lines)


def _format_status(report: dict[str, Any]) -> str:
    lines = [f"Person: {report['person']} | Companies: {report['company_count']}"]
    for c in report["companies"]:
        families = ", ".join(f"{k}={v}" for k, v in c["counts_by_form_family"].items()) or "none"
        adapters = ",".join(c["adapters"]) or "none"
        lines.append(
            f"- {c['source_key']} ({c['ticker']}): {c['document_count']} docs "
            f"[{families}] adapters=[{adapters}] latest={c['latest_filing_date'] or 'n/a'} "
            f"facts={'yes' if c['has_company_facts'] else 'no'}"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="filings-library",
        description="Multi-user company financial filings library CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # sync
    p_sync = sub.add_parser("sync", help="Run one bounded sync pass against targets hub")
    _add_common_data_args(p_sync)
    _add_targets_args(p_sync)
    p_sync.add_argument(
        "--extractor-url",
        default=os.environ.get("FILINGS_EXTRACTOR_URL"),
        help="Extractor service URL (defaults to in-process extractor when omitted)",
    )
    p_sync.add_argument(
        "--sec-user-agent",
        default=os.environ.get("FILINGS_SEC_USER_AGENT"),
        help="SEC EDGAR User-Agent with contact email (env: FILINGS_SEC_USER_AGENT)",
    )
    p_sync.add_argument("--backfill-years", type=int, default=3)
    p_sync.add_argument("--global-cap-bytes", type=int, default=DEFAULT_GLOBAL_CAP_BYTES)
    p_sync.add_argument("--per-issuer-cap-bytes", type=int, default=DEFAULT_PER_ISSUER_CAP_BYTES)
    p_sync.add_argument("--per-issuer-max-docs", type=int, default=DEFAULT_PER_ISSUER_MAX_DOCS)
    p_sync.add_argument("--json", action="store_true", dest="as_json")

    # search
    p_search = sub.add_parser("search", help="Search filings with FTS5 financial ranking")
    p_search.add_argument("query", help="Search query")
    _add_common_data_args(p_search)
    _add_scope_args(p_search)
    p_search.add_argument("--source-key", default=None)
    p_search.add_argument("--form-family", default=None)
    p_search.add_argument("--since", default=None)
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--json", action="store_true", dest="as_json")

    # read
    p_read = sub.add_parser("read", help="Read bounded sections of a filing")
    p_read.add_argument("document_id", help="Document ID to read")
    _add_common_data_args(p_read)
    _add_scope_args(p_read)
    p_read.add_argument("--start", default=None, help="Start locator (e.g. page:1)")
    p_read.add_argument("--end", default=None, help="End locator (e.g. page:3)")
    p_read.add_argument("--max-chars", type=int, default=24000)
    p_read.add_argument("--json", action="store_true", dest="as_json")

    # facts
    p_facts = sub.add_parser("facts", help="Query cached SEC XBRL companyfacts")
    p_facts.add_argument("concepts", nargs="+", help="XBRL concept names (e.g. Revenues)")
    _add_common_data_args(p_facts)
    _add_scope_args(p_facts)
    p_facts.add_argument(
        "--source-key",
        action="append",
        dest="source_keys",
        default=None,
        help="Restrict to specific source_key (repeatable)",
    )
    p_facts.add_argument("--limit-per-company", type=int, default=8)
    p_facts.add_argument("--json", action="store_true", dest="as_json")

    # status
    p_status = sub.add_parser("status", help="Show per-issuer coverage report")
    _add_common_data_args(p_status)
    _add_scope_args(p_status)
    p_status.add_argument("--json", action="store_true", dest="as_json")

    # export-md
    p_export = sub.add_parser("export-md", help="One-way disposable Markdown export")
    p_export.add_argument("out_dir", help="Destination directory for Markdown files")
    _add_common_data_args(p_export)
    p_export.add_argument("--json", action="store_true", dest="as_json")

    # rebuild-fts
    p_rebuild = sub.add_parser("rebuild-fts", help="Rebuild FTS5 index from stored sections")
    _add_common_data_args(p_rebuild)
    p_rebuild.add_argument("--json", action="store_true", dest="as_json")

    # backup
    p_backup = sub.add_parser("backup", help="Create a verified SQLite online backup")
    p_backup.add_argument("out_dir", help="Destination directory for dated online backup")
    _add_common_data_args(p_backup)
    p_backup.add_argument("--json", action="store_true", dest="as_json")

    # serve
    p_serve = sub.add_parser("serve", help="Run hosted HTTP/MCP server via uvicorn")
    _add_common_data_args(p_serve)
    _add_targets_args(p_serve)
    p_serve.add_argument("--host", default=os.environ.get("FILINGS_HOST", "0.0.0.0"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("FILINGS_PORT", "8080")))
    p_serve.add_argument(
        "--bearer-token",
        default=os.environ.get("FILINGS_BACKEND_BEARER_TOKEN"),
        help="Backend bearer token (>=32 chars; env: FILINGS_BACKEND_BEARER_TOKEN)",
    )
    p_serve.add_argument(
        "--allowed-hosts",
        default=os.environ.get("FILINGS_ALLOWED_HOSTS", "localhost,127.0.0.1"),
        help="Comma-separated allowed Host header values (env: FILINGS_ALLOWED_HOSTS)",
    )
    p_serve.add_argument(
        "--public-url",
        default=os.environ.get("FILINGS_PUBLIC_URL", ""),
        help="Canonical public web URL for OIDC and MCP citation deep-links (env: FILINGS_PUBLIC_URL)",
    )
    p_serve.add_argument(
        "--oidc-issuer",
        default=os.environ.get("FILINGS_OIDC_ISSUER", ""),
        help="OIDC issuer URL for browser workspace authentication (env: FILINGS_OIDC_ISSUER)",
    )
    p_serve.add_argument(
        "--oidc-client-id",
        default=os.environ.get("FILINGS_OIDC_CLIENT_ID", "filings-web"),
        help="Public PKCE OIDC client ID (env: FILINGS_OIDC_CLIENT_ID)",
    )
    p_serve.add_argument(
        "--oidc-required-group",
        default=os.environ.get("FILINGS_OIDC_REQUIRED_GROUP", ""),
        help="Required OIDC group claim for browser workspace access (env: FILINGS_OIDC_REQUIRED_GROUP)",
    )

    # extractor
    p_ext = sub.add_parser("extractor", help="Run stateless filings-extractor HTTP service")
    p_ext.add_argument("--host", default=os.environ.get("FILINGS_EXTRACTOR_HOST", "0.0.0.0"))
    p_ext.add_argument(
        "--port", type=int, default=int(os.environ.get("FILINGS_EXTRACTOR_PORT", "8081"))
    )

    return parser


def _cmd_sync(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        targets_client = _build_targets_client(args)
        extractor_client = (
            ExtractorClient(base_url=args.extractor_url)
            if args.extractor_url
            else ExtractorClient.in_process()
        )
        cfg = SyncConfig(
            backfill_years=args.backfill_years,
            global_cap_bytes=args.global_cap_bytes,
            per_issuer_cap_bytes=args.per_issuer_cap_bytes,
            per_issuer_max_docs=args.per_issuer_max_docs,
            sec_user_agent=args.sec_user_agent,
        )
        result = asyncio.run(
            sync_once(
                store=store,
                cas=cas,
                targets_client=targets_client,
                extractor_client=extractor_client,
                config=cfg,
            )
        )
        payload = {
            "companies_synced": result.companies_synced,
            "documents_ingested": result.documents_ingested,
            "documents_skipped_existing": result.documents_skipped_existing,
            "documents_skipped_quota": result.documents_skipped_quota,
            "facts_updated": result.facts_updated,
            "errors": result.errors,
        }
        _print_output(payload, as_json=args.as_json)
        return 0
    finally:
        store.close()


def _cmd_search(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        resolver, person = _build_scope_resolver(args, store)
        svc = FilingsService(store, cas, resolver)
        hits = asyncio.run(
            svc.search(
                person,
                args.query,
                source_key=args.source_key,
                form_family=args.form_family,
                since=args.since,
                limit=args.limit,
            )
        )
        _print_output(hits, as_json=args.as_json, text_formatter=_format_search)
        return 0
    finally:
        store.close()


def _cmd_read(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        resolver, person = _build_scope_resolver(args, store)
        svc = FilingsService(store, cas, resolver)
        payload = asyncio.run(
            svc.read(
                person,
                args.document_id,
                start_locator=args.start,
                end_locator=args.end,
                max_chars=args.max_chars,
            )
        )
        _print_output(payload, as_json=args.as_json, text_formatter=_format_read)
        return 0
    finally:
        store.close()


def _cmd_facts(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        resolver, person = _build_scope_resolver(args, store)
        svc = FilingsService(store, cas, resolver)
        rows = asyncio.run(
            svc.facts(
                person,
                args.concepts,
                source_keys=args.source_keys,
                limit_per_company=args.limit_per_company,
            )
        )
        _print_output(rows, as_json=args.as_json, text_formatter=_format_facts)
        return 0
    finally:
        store.close()


def _cmd_status(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        resolver, person = _build_scope_resolver(args, store)
        svc = FilingsService(store, cas, resolver)
        report = asyncio.run(svc.status(person))
        _print_output(report, as_json=args.as_json, text_formatter=_format_status)
        return 0
    finally:
        store.close()


def _cmd_export_md(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        written = export_markdown(store, cas, args.out_dir)
        payload = {"exported": len(written), "files": [str(p) for p in written]}
        _print_output(
            payload,
            as_json=args.as_json,
            text_formatter=lambda d: f"Exported {d['exported']} Markdown file(s) to {args.out_dir}",
        )
        return 0
    finally:
        store.close()


def _cmd_rebuild_fts(args: argparse.Namespace) -> int:
    store, _cas = _open_store_and_cas(args.data_dir)
    try:
        count = store.rebuild_fts()
        payload = {"indexed_chunks": count}
        _print_output(
            payload,
            as_json=args.as_json,
            text_formatter=lambda d: f"Rebuilt FTS5 index with {d['indexed_chunks']} chunk(s).",
        )
        return 0
    finally:
        store.close()


def _cmd_backup(args: argparse.Namespace) -> int:
    import fcntl
    import hashlib
    import shutil
    import sqlite3
    import tempfile
    from datetime import UTC, datetime

    data_dir = Path(args.data_dir)
    src_db = data_dir / "filings.sqlite3"
    if not src_db.is_file():
        raise FileNotFoundError(f"Database not found: {src_db}")
    out_root = Path(args.out_dir)
    if out_root.is_symlink():
        raise ValueError("backup out_dir must not be a symlink")
    out_root.mkdir(parents=True, exist_ok=True)
    lock_fd = open(out_root / ".backup.lock", "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        final_dir = out_root / day
        if final_dir.is_symlink():
            raise ValueError(f"backup target must not be a symlink: {final_dir}")
        stage_dir = Path(tempfile.mkdtemp(prefix=f".backup-{day}-", dir=out_root))
        try:
            dst_db = stage_dir / "filings.sqlite3"
            src_conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=10.0)
            try:
                dst_conn = sqlite3.connect(dst_db)
                try:
                    src_conn.backup(dst_conn)
                    check = dst_conn.execute("PRAGMA quick_check").fetchone()
                    if not check or check[0] != "ok":
                        raise RuntimeError(f"Backup integrity check failed: {check}")
                finally:
                    dst_conn.close()
            finally:
                src_conn.close()
            with dst_db.open("rb") as fh:
                sha = hashlib.file_digest(fh, "sha256").hexdigest()
            size_bytes = dst_db.stat().st_size
            manifest = {
                "schema": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "day": day,
                "files": {"filings.sqlite3": {"sha256": sha, "bytes": size_bytes}},
            }
            (stage_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            old_dir: Path | None = None
            if final_dir.is_symlink():
                raise ValueError(f"backup target must not be a symlink: {final_dir}")
            if final_dir.exists():
                old_dir = out_root / f".backup-old-{day}-{os.getpid()}"
                if old_dir.exists():
                    shutil.rmtree(old_dir)
                os.replace(final_dir, old_dir)
            try:
                os.replace(stage_dir, final_dir)
            except Exception:
                if old_dir is not None and old_dir.exists() and not final_dir.exists():
                    os.replace(old_dir, final_dir)
                raise
            if old_dir is not None and old_dir.exists():
                shutil.rmtree(old_dir, ignore_errors=True)
            payload = {
                "backup_dir": str(final_dir),
                "sha256": sha,
                "bytes": size_bytes,
            }
            _print_output(
                payload,
                as_json=args.as_json,
                text_formatter=lambda d: (
                    f"Backed up SQLite DB to {d['backup_dir']} ({d['bytes']} bytes)"
                ),
            )
            return 0
        except Exception:
            shutil.rmtree(stage_dir, ignore_errors=True)
            raise
    finally:
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_UN)
        lock_fd.close()


def _cmd_serve(args: argparse.Namespace) -> int:
    store, cas = _open_store_and_cas(args.data_dir)
    try:
        resolver = TargetsScopeResolver(_build_targets_client(args))
        svc = FilingsService(store, cas, resolver)
        hosts = [h.strip() for h in str(args.allowed_hosts).split(",") if h.strip()]
        public_url = str(args.public_url or "").strip().rstrip("/") or None
        oidc_issuer = str(args.oidc_issuer or "").strip()
        browser_auth = None
        if oidc_issuer and public_url:
            from signin_window.oidc import BrowserAuth, OIDCConfig

            browser_auth = BrowserAuth(
                OIDCConfig(
                    issuer=oidc_issuer,
                    client_id=str(args.oidc_client_id or "filings-web").strip(),
                    public_url=public_url,
                    state_dir=Path(args.data_dir) / "browser",
                    required_group=str(args.oidc_required_group or "").strip(),
                    cookie_name="__Host-filings",
                    claim_names=("person_id",),
                ),
                home="/",
            )
        app = create_http_app(
            svc,
            backend_bearer_token=args.bearer_token,
            allowed_hosts=hosts,
            browser_auth=browser_auth,
            public_base_url=public_url,
        )
        uvicorn.run(app, host=args.host, port=args.port, timeout_keep_alive=65)
        return 0
    finally:
        store.close()


def _cmd_extractor(args: argparse.Namespace) -> int:
    app = create_extractor_app()
    uvicorn.run(app, host=args.host, port=args.port, timeout_keep_alive=65)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    dispatch = {
        "sync": _cmd_sync,
        "search": _cmd_search,
        "read": _cmd_read,
        "facts": _cmd_facts,
        "status": _cmd_status,
        "export-md": _cmd_export_md,
        "rebuild-fts": _cmd_rebuild_fts,
        "backup": _cmd_backup,
        "serve": _cmd_serve,
        "extractor": _cmd_extractor,
    }
    try:
        return dispatch[args.command](args)
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
