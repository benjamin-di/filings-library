"""Tests for v2 ir_assets ingestion, PPTX extraction, Parakeet ASR stdio, and supersession."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from pptx import Presentation

from filings_library.cas import ContentAddressedStore
from filings_library.extractor.app import ExtractorClient
from filings_library.extractor.pptx_extract import extract_pptx
from filings_library.models import ArtifactKind, FormFamily, SourceAdapter
from filings_library.store import FilingsStore
from filings_library.targets_client import LibraryTargetsClient
from filings_library.transcribe import (
    ASR_FORM_TYPE,
    PARAKEET_MODEL_NAME,
    TranscriptSegment,
    parse_parakeet_segments,
    run_remote_parakeet,
    segments_to_extracted_document,
)
from filings_library.web_api import format_display_form_type, transcript_origin_for
from filings_library.worker import SyncConfig, sync_once


def _sample_pptx_bytes() -> bytes:
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "Q3 2026 Financial Highlights"
    slide.placeholders[1].text = "Revenue grew 18% YoY to $4,200 million."
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def test_pptx_extract_produces_slide_locators() -> None:
    extracted = extract_pptx(_sample_pptx_bytes())
    assert extracted.converter_id == "pptx-extract"
    assert extracted.page_count == 1
    assert len(extracted.sections) == 1
    assert extracted.sections[0].locator == "slide:1"
    assert "Q3 2026 Financial Highlights" in extracted.sections[0].markdown
    assert "4,200 million" in extracted.sections[0].markdown


@pytest.mark.asyncio
async def test_run_remote_parakeet_stdio_protocol_and_ts_locators(tmp_path: Path) -> None:
    # Fake Parakeet worker implementing bounded stdio v1 protocol
    worker_script = tmp_path / "fake_parakeet.py"
    worker_script.write_text(
        """
import json, sys
hdr = json.loads(sys.stdin.buffer.readline().decode('utf-8'))
assert hdr['version'] == 1
assert hdr['model'] == 'parakeet-unified-en-0.6b-int8'
wav = sys.stdin.buffer.read(hdr['wav_size'])
assert len(wav) == hdr['wav_size']
body = json.dumps({
    'source': {'kind': 'parakeet'},
    'transcription': [
        {'offsets': {'from': 0, 'to': 12500}, 'text': 'Welcome to the Q3 2026 earnings call.'},
        {'offsets': {'from': 315000, 'to': 328000}, 'text': 'Operating margin expanded to 24.5 percent.'},
    ]
}).encode('utf-8')
resp_hdr = {
    'version': 1,
    'model': 'parakeet-unified-en-0.6b-int8',
    'engine_version': '0.4.1',
    'model_sha256': 'a' * 64,
    'source_revision': 'b' * 40,
    'threads': 4,
    'json_size': len(body),
}
sys.stdout.buffer.write((json.dumps(resp_hdr) + '\\n').encode('utf-8'))
sys.stdout.buffer.write(body)
sys.stdout.buffer.flush()
""".strip(),
        encoding="utf-8",
    )
    wav_path = tmp_path / "sample.wav"
    wav_path.write_bytes(b"RIFF" + b"\x00" * 100)
    out_json = tmp_path / "segments.json"

    prov = await run_remote_parakeet(
        wav_path,
        out_json,
        command=(sys.executable, str(worker_script)),
        timeout_s=30.0,
    )
    assert prov.engine_version == "0.4.1"
    assert prov.model_sha256 == "a" * 64

    segments = parse_parakeet_segments(out_json.read_bytes())
    extracted = segments_to_extracted_document(segments, converter_version=prov.engine_version)
    assert extracted.converter_id == PARAKEET_MODEL_NAME
    assert [s.locator for s in extracted.sections] == ["ts:00:00", "ts:05:15"]
    assert "Operating margin expanded" in extracted.sections[1].markdown


@pytest.mark.asyncio
async def test_sync_ingests_pptx_webcast_asr_and_official_transcript_supersedes_asr(
    tmp_path: Path,
) -> None:
    store = FilingsStore(tmp_path / "filings.sqlite3")
    cas = ContentAddressedStore(tmp_path)
    pptx_bytes = _sample_pptx_bytes()
    official_html = (
        b"<html><body><h1>Q3 2026 Official Transcript</h1><p>CEO remarks here.</p></body></html>"
    )

    # Pass 1: presentation PPTX + webcast bookmark + audio_replay (no official transcript yet)
    targets_state = {"has_official_transcript": False}

    def targets_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("schema") == "2"
        ir_assets = [
            {
                "id": "pres-1",
                "event_id": "",
                "url": "https://cdn.example.com/q3-deck.pptx",
                "title": "Q3 2026 Presentation",
                "role": "presentation",
                "category": "presentation",
                "published_date": "2026-11-05",
                "period": "2026-Q3",
                "first_seen": "2026-11-05T15:00:00Z",
            },
            {
                "id": "wc-1",
                "event_id": "",
                "url": "https://edge.media-server.com/mmc/p/example",
                "title": "Q3 2026 Webcast",
                "role": "webcast_page",
                "category": "webcast_page",
                "published_date": "2026-11-05",
                "period": "2026-Q3",
                "first_seen": "2026-11-05T15:00:00Z",
            },
            {
                "id": "audio-1",
                "event_id": "",
                "url": "https://cdn.example.com/q3-call.mp3",
                "title": "Q3 2026 Earnings Call",
                "role": "audio_replay",
                "category": "audio_replay",
                "published_date": "2026-11-05",
                "period": "2026-Q3",
                "first_seen": "2026-11-05T16:00:00Z",
            },
        ]
        if targets_state["has_official_transcript"]:
            ir_assets.append(
                {
                    "id": "tr-official-1",
                    "event_id": "",
                    "url": "https://investors.example.com/q3-transcript.html",
                    "title": "Q3 2026 Official Transcript",
                    "role": "transcript",
                    "category": "transcript",
                    "published_date": "2026-11-06",
                    "period": "2026-Q3",
                    "first_seen": "2026-11-06T09:00:00Z",
                }
            )
        return httpx.Response(
            200,
            json={
                "schema": 2,
                "generated_at": "2026-11-05T16:00:00Z",
                "companies": [
                    {
                        "source_key": "example-corp",
                        "ticker": "EXM",
                        "exchange": "XNYS",
                        "name": "Example Corp",
                        "ir_urls": {"presentations": "https://investors.example.com/presentations"},
                        "verified_documents": [],
                        "ir_assets": ir_assets,
                    }
                ],
                "scopes": [{"person": "alice", "source_keys": ["example-corp"]}],
            },
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pptx"):
            return httpx.Response(
                200,
                content=pptx_bytes,
                headers={
                    "content-type": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
                },
            )
        if request.url.path.endswith(".mp3"):
            return httpx.Response(
                200, content=b"ID3" + b"\x00" * 512, headers={"content-type": "audio/mpeg"}
            )
        if request.url.path.endswith(".html"):
            return httpx.Response(200, content=official_html, headers={"content-type": "text/html"})
        return httpx.Response(404)

    async def fake_transcriber(raw_audio: bytes, filename: str):
        assert raw_audio.startswith(b"ID3")
        assert filename == "q3-call.mp3"
        raw_asr = json.dumps(
            {"segments": [{"start": 10.0, "end": 25.0, "text": "Revenue rose 18 percent."}]}
        ).encode("utf-8")
        doc = segments_to_extracted_document(
            [TranscriptSegment(start=10.0, end=25.0, text="Revenue rose 18 percent.")],
            converter_version="0.4.1",
        )
        return raw_asr, doc

    targets_client = LibraryTargetsClient(
        base_url="http://hub.local",
        token="t" * 32,
        transport=httpx.MockTransport(targets_handler),
        ttl_seconds=0.0,
    )
    res1 = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        download_transport=httpx.MockTransport(download_handler),
        url_resolver=lambda _h, _p: ["93.184.216.34"],
        transcriber=fake_transcriber,
    )
    assert res1.errors == []
    assert res1.transcriptions_queued == 1
    assert res1.transcriptions_completed == 1

    docs1 = store.list_documents({"example-corp"})
    by_ext = {d.external_id: d for d in docs1}
    assert by_ext["pres-1"].form_family == FormFamily.PRESENTATION
    assert by_ext["pres-1"].filing_date == "2026-11-05"
    assert by_ext["wc-1"].form_family == FormFamily.WEBCAST
    asr_doc = by_ext["audio-1"]
    assert asr_doc.form_family == FormFamily.TRANSCRIPT
    assert asr_doc.form_type == ASR_FORM_TYPE
    # Raw audio discarded from CAS; ASR_JSON + MARKDOWN retained
    assert not cas.has(asr_doc.source_sha256)
    assert (
        store.get_artifact(
            source_sha256=asr_doc.source_sha256,
            kind=ArtifactKind.ASR_JSON,
            converter_id=PARAKEET_MODEL_NAME,
            converter_version="0.4.1",
        )
        is not None
    )
    assert (
        format_display_form_type(asr_doc.form_type, str(SourceAdapter.EARNINGS_WATCH))
        == "Self-Transcribed (ASR)"
    )
    assert transcript_origin_for(asr_doc.form_type, str(asr_doc.form_family)) == "asr"

    # Pass 2: Official transcript arrives and automatically supersedes the ASR transcript
    targets_state["has_official_transcript"] = True
    res2 = await sync_once(
        store=store,
        cas=cas,
        targets_client=targets_client,
        extractor_client=ExtractorClient.in_process(),
        config=SyncConfig(),
        download_transport=httpx.MockTransport(download_handler),
        url_resolver=lambda _h, _p: ["93.184.216.34"],
        transcriber=fake_transcriber,
    )
    assert res2.errors == []
    docs2 = {d.external_id: d for d in store.list_documents({"example-corp"})}
    official_doc = docs2["tr-official-1"]
    assert official_doc.form_family == FormFamily.TRANSCRIPT
    assert official_doc.supersedes_document_id == asr_doc.document_id
    assert (
        transcript_origin_for(official_doc.form_type, str(official_doc.form_family)) == "official"
    )
    store.close()
