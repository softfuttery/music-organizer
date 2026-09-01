"""Browser-compatible, on-demand audio previews without changing source files."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from mutagen import File

from .locking import exclusive_file_lock


class AudioPreviewError(RuntimeError):
    """Raised when an incompatible source cannot be transcoded for preview."""


def _is_alac_m4a(path: Path, media_factory: Callable[..., Any]) -> bool:
    if path.suffix.lower() != ".m4a":
        return False
    try:
        media = media_factory(str(path), easy=False)
    except Exception:
        return False
    codec = str(getattr(getattr(media, "info", None), "codec", "") or "")
    return codec.casefold() == "alac"


def browser_compatible_audio(
    source: str | Path,
    cache_root: str | Path,
    *,
    media_factory: Callable[..., Any] = File,
    run: Callable[..., Any] = subprocess.run,
) -> Path:
    """Return *source* or a cached AAC copy when the source is ALAC-in-M4A."""
    path = Path(source)
    if not _is_alac_m4a(path, media_factory):
        return path

    metadata = path.stat(follow_symlinks=False)
    fingerprint = hashlib.sha256(
        f"{path.resolve()}\0{metadata.st_size}\0{metadata.st_mtime_ns}".encode()
    ).hexdigest()
    directory = Path(cache_root) / "audio-preview"
    target = directory / f"{fingerprint}.m4a"
    lock = directory / f".{fingerprint}.lock"
    directory.mkdir(parents=True, exist_ok=True)

    with exclusive_file_lock(lock, timeout=300):
        if target.is_file() and target.stat().st_size > 0:
            return target
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{fingerprint}.",
                suffix=".m4a",
                dir=directory,
                delete=False,
            ) as temporary:
                temporary_name = temporary.name
            result = run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-y",
                    "-i",
                    str(path),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-c:a",
                    "aac",
                    "-b:a",
                    "192k",
                    "-movflags",
                    "+faststart",
                    temporary_name,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,
                check=False,
            )
            temporary_path = Path(temporary_name)
            if result.returncode != 0 or not temporary_path.is_file() or not temporary_path.stat().st_size:
                detail = str(getattr(result, "stderr", "") or "").strip()[-500:]
                raise AudioPreviewError(f"ALAC 试听转码失败: {detail or 'ffmpeg 未生成音频'}")
            os.replace(temporary_path, target)
        except subprocess.TimeoutExpired as exc:
            raise AudioPreviewError("ALAC 试听转码超时") from exc
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)
    return target
