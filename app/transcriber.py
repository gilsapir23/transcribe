"""Core transcription logic: split long audio with ffmpeg, call the OpenAI
transcription API per chunk, and assemble the final text.

Deliberately dependency-light (uses `requests` + the bundled ffmpeg binary
from `imageio-ffmpeg`, no `openai` SDK) so PyInstaller builds stay small and
predictable across Windows/macOS.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import imageio_ffmpeg
import requests

API_URL = "https://api.openai.com/v1/audio/transcriptions"
MAX_CHUNK_BYTES = 24 * 1024 * 1024  # stay safely under OpenAI's 25MB cap
MIN_CHUNK_SECONDS = 60
DEFAULT_CHUNK_SECONDS = 15 * 60


class TranscriptionCancelled(Exception):
    pass


class TranscriptionError(Exception):
    pass


@dataclass
class JobSettings:
    api_key: str
    organization_id: str = ""
    project_id: str = ""
    model: str = "whisper-1"
    language: str = ""  # "" = auto-detect
    chunk_minutes: int = 15


def ffmpeg_path() -> str:
    # When frozen by PyInstaller, build.py places the ffmpeg binary in a
    # dedicated "ffmpeg_bin" folder next to the app rather than relying on
    # imageio_ffmpeg's own (package-relative) lookup, which is fragile once
    # the package has been extracted into a PyInstaller bundle.
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        for candidate in (base / "ffmpeg_bin").glob("ffmpeg*"):
            if candidate.is_file():
                return str(candidate)
    return imageio_ffmpeg.get_ffmpeg_exe()


def probe_duration_seconds(input_path: str) -> float:
    """Read duration by asking ffmpeg to decode nothing and parsing stderr,
    avoiding a dependency on a separate ffprobe binary."""
    exe = ffmpeg_path()
    proc = subprocess.run(
        [exe, "-i", input_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    out = proc.stdout or ""
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", out)
    if not m:
        return 0.0
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def _pick_chunk_seconds(file_bytes: int, duration_sec: float, requested_minutes: int) -> int:
    requested = max(MIN_CHUNK_SECONDS, requested_minutes * 60)
    if duration_sec <= 0 or file_bytes <= 0:
        return requested
    bytes_per_sec = file_bytes / duration_sec
    max_seconds_for_size = int(MAX_CHUNK_BYTES / bytes_per_sec) if bytes_per_sec > 0 else requested
    return max(MIN_CHUNK_SECONDS, min(requested, max_seconds_for_size))


def split_audio(input_path: str, chunk_seconds: int, out_dir: Path) -> list[Path]:
    exe = ffmpeg_path()
    out_dir.mkdir(parents=True, exist_ok=True)
    pattern = str(out_dir / "chunk_%04d.m4a")
    cmd = [
        exe, "-v", "error", "-y",
        "-i", input_path,
        "-f", "segment", "-segment_time", str(chunk_seconds),
        "-c", "copy", "-map", "0:a",
        pattern,
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        # Some containers/codecs don't support stream copy into segments; retry with re-encode.
        cmd = [
            exe, "-v", "error", "-y",
            "-i", input_path,
            "-f", "segment", "-segment_time", str(chunk_seconds),
            "-c:a", "aac", "-b:a", "96k", "-map", "0:a",
            pattern,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        if proc.returncode != 0:
            raise TranscriptionError(f"ffmpeg נכשל בפיצול הקובץ:\n{proc.stdout}")
    return sorted(out_dir.glob("chunk_*.m4a"))


def transcribe_chunk(chunk_path: Path, settings: JobSettings, attempt_retries: int = 2) -> str:
    headers = {"Authorization": f"Bearer {settings.api_key}"}
    if settings.organization_id:
        headers["OpenAI-Organization"] = settings.organization_id
    if settings.project_id:
        headers["OpenAI-Project"] = settings.project_id

    last_error = None
    for attempt in range(attempt_retries + 1):
        try:
            with open(chunk_path, "rb") as f:
                files = {"file": (chunk_path.name, f, "audio/m4a")}
                data = {"model": settings.model, "response_format": "text"}
                if settings.language:
                    data["language"] = settings.language
                resp = requests.post(API_URL, headers=headers, files=files, data=data, timeout=600)
            if resp.status_code == 200:
                return resp.text
            last_error = f"HTTP {resp.status_code}: {resp.text[:500]}"
        except requests.RequestException as e:
            last_error = str(e)
        if attempt < attempt_retries:
            continue
    raise TranscriptionError(last_error or "שגיאה לא ידועה בקריאה ל-API")


ProgressCallback = Callable[[str, float], None]  # (status_message, fraction 0..1)
ShouldStopCallback = Callable[[], bool]


def transcribe_file(
    input_path: str,
    output_path: str,
    settings: JobSettings,
    on_progress: Optional[ProgressCallback] = None,
    should_stop: Optional[ShouldStopCallback] = None,
) -> str:
    """Transcribe one audio/video file end to end. Returns the full text and
    also writes it to `output_path`. Raises TranscriptionCancelled/Error."""

    def report(msg: str, frac: float):
        if on_progress:
            on_progress(msg, frac)

    def check_cancel():
        if should_stop and should_stop():
            raise TranscriptionCancelled()

    src = Path(input_path)
    if not src.exists():
        raise TranscriptionError(f"הקובץ לא נמצא: {input_path}")

    report("בודק את הקובץ...", 0.02)
    check_cancel()
    duration = probe_duration_seconds(str(src))
    file_bytes = src.stat().st_size

    chunk_seconds = _pick_chunk_seconds(file_bytes, duration, settings.chunk_minutes)
    needs_split = file_bytes > MAX_CHUNK_BYTES

    tmp_dir = Path(tempfile.mkdtemp(prefix="transcribeapp_"))
    try:
        if needs_split:
            report("מפצל את הקובץ לחלקים...", 0.05)
            chunk_paths = split_audio(str(src), chunk_seconds, tmp_dir)
        else:
            chunk_paths = [src]

        if not chunk_paths:
            raise TranscriptionError("פיצול הקובץ לא הניב חלקים לתמלול")

        texts = []
        total = len(chunk_paths)
        for i, chunk in enumerate(chunk_paths):
            check_cancel()
            pct = 0.1 + 0.85 * (i / total)
            report(f"מתמלל חלק {i + 1} מתוך {total}...", pct)
            text = transcribe_chunk(chunk, settings)
            texts.append(text.strip())

        check_cancel()
        report("שומר תוצאה...", 0.97)
        full_text = "\n\n".join(t for t in texts if t)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(full_text, encoding="utf-8")
        report("הושלם", 1.0)
        return full_text
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
