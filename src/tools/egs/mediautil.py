"""ffmpeg helpers: probing, windowing and re-encoding media into model-friendly pieces."""

import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class MediaError(RuntimeError):
    """ffmpeg/ffprobe failed or is unavailable."""


def have_ffmpeg() -> bool:
    """True if ffmpeg and ffprobe are on PATH."""
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    if not have_ffmpeg():
        raise MediaError("ffmpeg/ffprobe not found on PATH")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode:
        raise MediaError(f"{cmd[0]} failed: {proc.stderr.strip()[-300:]}")
    return proc


def duration(path: Path) -> float:
    """Duration in seconds of an audio/video file."""
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "json", str(path)]).stdout
    try:
        return float(json.loads(out)["format"]["duration"])
    except (KeyError, ValueError) as err:
        raise MediaError(f"cannot determine duration of {path}") from err


def spans(total: float, window: float) -> list[tuple[int, int]]:
    """Whole-second (start, length) windows covering ``total`` seconds."""
    n = max(1, math.ceil(total / window))
    return [(int(i * window), int(min(window, total - i * window)) or 1) for i in range(n)]


@contextmanager
def _tmp(suffix: str) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="egs-media-") as d:
        yield Path(d) / f"piece{suffix}"


@contextmanager
def cut(kind: str, path: Path, start: int, length: int) -> Iterator[Path]:
    """Yield a temp file holding [start, start+length) of audio (16 kHz mono wav) or video (mp4)."""
    with _tmp(".wav" if kind == "audio" else ".mp4") as out:
        pre = ["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(length), "-i", str(path)]
        if kind == "audio":
            _run(pre + ["-vn", "-ac", "1", "-ar", "16000", str(out)])
        else:
            _run(pre + ["-vf", "scale='min(480,iw)':-2", "-c:v", "libx264", "-preset",
                        "ultrafast", "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)])
        yield out


@contextmanager
def image_as(path: Path, accepted: frozenset[str]) -> Iterator[Path]:
    """Yield ``path`` if its suffix is accepted, otherwise a PNG conversion of it."""
    if path.suffix.lower() in accepted:
        yield path
        return
    with _tmp(".png") as out:
        _run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-frames:v", "1", str(out)])
        yield out
