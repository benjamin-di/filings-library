"""Bounded stdio client for remote Parakeet ASR worker and `ts:MM:SS` Markdown formatter."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

from filings_library.models import ExtractedDocument, ExtractedSection

PARAKEET_MODEL_NAME = "parakeet-unified-en-0.6b-int8"
ASR_FORM_TYPE = "earnings_call_transcript_asr"
_MAX_HEADER = 4096
_MAX_WAV = 768 * 1024 * 1024
_MAX_JSON = 32 * 1024 * 1024
_CHUNK = 256 * 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


class TranscriptionError(RuntimeError):
    """Raised when audio normalization or remote Parakeet transcription fails."""


@dataclass(frozen=True)
class ParakeetProvenance:
    engine_version: str
    model_sha256: str
    threads: int
    source_revision: str


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str


def _encode_header(header: dict[str, object]) -> bytes:
    return json.dumps(header, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\n"


async def _read_header(reader: asyncio.StreamReader) -> dict[str, Any]:
    try:
        line = await reader.readuntil(b"\n")
        if len(line) > _MAX_HEADER:
            raise TranscriptionError("worker header exceeds limit")
        header = json.loads(line)
    except (ValueError, RecursionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        raise TranscriptionError("invalid worker header") from None
    if (
        not isinstance(header, dict)
        or type(header.get("version")) is not int
        or header["version"] != 1
    ):
        raise TranscriptionError("unsupported worker protocol")
    return header


def _bounded_size(header: dict[str, Any], key: str, limit: int) -> int:
    size = header.get(key)
    if type(size) is not int or not 0 < size <= limit:
        raise TranscriptionError(f"invalid worker field {key}")
    return size


async def _read_body(reader: asyncio.StreamReader, dest: BinaryIO, remaining: int) -> None:
    try:
        while remaining:
            chunk = await reader.readexactly(min(remaining, _CHUNK))
            dest.write(chunk)
            remaining -= len(chunk)
    except asyncio.IncompleteReadError:
        raise TranscriptionError("incomplete worker body") from None


def _kill_group(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


async def _drain_and_wait(proc: asyncio.subprocess.Process, *, timeout_s: float) -> None:
    assert proc.stdout is not None
    async with asyncio.timeout(timeout_s):
        while await proc.stdout.read(_CHUNK):
            pass
        await proc.wait()


async def run_remote_parakeet(
    wav_path: Path,
    output_path: Path,
    *,
    command: Sequence[str],
    timeout_s: float = 3700.0,
) -> ParakeetProvenance:
    """Stream normalized 16 kHz mono WAV to the remote Parakeet worker over bounded stdio v1."""
    wav_size = wav_path.stat().st_size
    if not 44 < wav_size <= _MAX_WAV:
        raise TranscriptionError("WAV size outside worker limit")
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=_MAX_HEADER,
        )
    except (OSError, ValueError):
        raise TranscriptionError("unable to start parakeet worker command") from None
    assert proc.stdin is not None and proc.stdout is not None
    try:
        async with asyncio.timeout(timeout_s):
            proc.stdin.write(
                _encode_header({"version": 1, "model": PARAKEET_MODEL_NAME, "wav_size": wav_size})
            )
            with wav_path.open("rb") as media:
                while chunk := media.read(_CHUNK):
                    proc.stdin.write(chunk)
                    await proc.stdin.drain()
            header = await _read_header(proc.stdout)
            if header.get("model") != PARAKEET_MODEL_NAME:
                raise TranscriptionError("worker returned a different model")
            digest = header.get("model_sha256")
            engine_version = header.get("engine_version")
            revision = header.get("source_revision")
            if (
                not isinstance(digest, str)
                or not _SHA256_RE.fullmatch(digest)
                or not isinstance(engine_version, str)
                or not engine_version
                or not re.fullmatch(r"[0-9A-Za-z.+_-]{1,80}", engine_version)
                or not isinstance(revision, str)
                or not re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision)
            ):
                raise TranscriptionError("invalid worker provenance")
            threads = _bounded_size(header, "threads", 32)
            json_size = _bounded_size(header, "json_size", _MAX_JSON)
            with output_path.open("wb") as segments:
                await _read_body(proc.stdout, segments, json_size)
            if await proc.stdout.read(1) or await proc.wait():
                raise TranscriptionError("worker returned an invalid response")
            return ParakeetProvenance(engine_version, digest, threads, revision)
    except TimeoutError:
        raise TranscriptionError("parakeet worker command timed out") from None
    except OSError:
        raise TranscriptionError("parakeet worker transport failed") from None
    finally:
        if not proc.stdin.is_closing():
            proc.stdin.transport.abort()
        try:
            await _drain_and_wait(proc, timeout_s=2.0)
        except TimeoutError:
            _kill_group(proc)
            with contextlib.suppress(TimeoutError):
                await _drain_and_wait(proc, timeout_s=5.0)


def ssh_parakeet_command(ssh_config: Path | str) -> tuple[str, ...]:
    """Build the restricted BatchMode SSH command for the remote Parakeet worker."""
    return (
        "ssh",
        "-F",
        str(ssh_config),
        "-o",
        "BatchMode=yes",
        "-T",
        "parakeet-worker",
    )


async def normalize_audio_to_wav(
    raw_audio_path: Path,
    wav_path: Path,
    *,
    max_duration_s: int = 9000,
    timeout_s: float = 300.0,
) -> None:
    """Convert downloaded audio/video into 16 kHz mono PCM WAV via `ffmpeg`."""
    cmd = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-t",
        str(max_duration_s),
        "-i",
        str(raw_audio_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        raise TranscriptionError(f"ffmpeg unavailable: {exc}") from exc
    try:
        async with asyncio.timeout(timeout_s):
            _, stderr = await proc.communicate()
    except TimeoutError:
        _kill_group(proc)
        raise TranscriptionError("ffmpeg audio normalization timed out") from None
    if proc.returncode != 0 or not wav_path.exists():
        err = (stderr or b"").decode("utf-8", errors="replace")[:300]
        raise TranscriptionError(f"ffmpeg failed ({proc.returncode}): {err}")


def _format_ts(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_parakeet_segments(raw_json: bytes) -> list[TranscriptSegment]:
    """Parse Parakeet worker JSON output into ordered `TranscriptSegment` items."""
    data = json.loads(raw_json.decode("utf-8"))
    items = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise TranscriptionError("unexpected parakeet JSON payload")
    segments: list[TranscriptSegment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        start = float(item.get("start") or 0.0)
        end = float(item.get("end") or start)
        segments.append(TranscriptSegment(start=start, end=end, text=text))
    return segments


def segments_to_extracted_document(
    segments: Sequence[TranscriptSegment],
    *,
    converter_version: str = "1.0.0",
    chunk_seconds: int = 300,
) -> ExtractedDocument:
    """Group timestamped ASR segments into searchable `ts:MM:SS` sections."""
    if not segments:
        raise TranscriptionError("transcription produced zero speech segments")

    buckets: dict[int, list[TranscriptSegment]] = {}
    for seg in segments:
        bucket = max(0, int(seg.start // chunk_seconds))
        buckets.setdefault(bucket, []).append(seg)

    sections: list[ExtractedSection] = []
    md_parts: list[str] = []
    for ordinal, bucket_idx in enumerate(sorted(buckets)):
        group = buckets[bucket_idx]
        start_label = _format_ts(group[0].start)
        end_label = _format_ts(group[-1].end)
        locator = f"ts:{start_label}"
        title = f"Transcript {start_label}–{end_label}"
        lines = [f"[{_format_ts(s.start)}] {s.text}" for s in group]
        section_md = f"## {title}\n\n" + "\n\n".join(lines)
        sections.append(
            ExtractedSection(
                locator=locator,
                title=title,
                ordinal=ordinal,
                markdown=section_md,
                ocr_used=False,
            )
        )
        md_parts.append(section_md)

    return ExtractedDocument(
        converter_id=PARAKEET_MODEL_NAME,
        converter_version=converter_version,
        locator_kind="section",
        markdown="\n\n".join(md_parts) + "\n",
        sections=sections,
        page_count=None,
        ocr_pages=[],
        warnings=[],
    )
