"""CUE parsing and FFmpeg splitting without logging or persistence concerns."""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from .models import CueSheet, CueTrack
from .pathsafe import resolve_confined, safe_relative_parts

DEFAULT_CUE_AUDIO_EXTS = [".flac", ".wav", ".ape", ".wv", ".tta"]
CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class CueSplitOptions:
    enabled: bool = True
    output_subdir: str = ""
    filename_template: str = "{track:02d} - {title}"
    skip_existing: bool = True
    split_multifile_cues: bool = False
    ffmpeg_path: str = "ffmpeg"
    flac_compression_level: int = 6

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CueSplitOptions":
        return cls(
            enabled=bool(value.get("enabled", True)),
            output_subdir=str(value.get("output_subdir", "") or ""),
            filename_template=str(
                value.get("filename_template") or "{track:02d} - {title}"
            ),
            skip_existing=bool(value.get("skip_existing", True)),
            split_multifile_cues=bool(value.get("split_multifile_cues", False)),
            ffmpeg_path=str(value.get("ffmpeg_path") or "ffmpeg"),
            flac_compression_level=int(value.get("flac_compression_level", 6) or 6),
        )


@dataclass(frozen=True)
class CueCreatedFile:
    marker: Path
    output_path: Path
    track_number: int


@dataclass(frozen=True)
class CueIssue:
    level: Literal["info", "warning", "error"]
    message: str
    error: Exception | None = None
    verbose_only: bool = False


@dataclass
class CueSplitResult:
    created: int = 0
    skipped: int = 0
    failed: int = 0
    files: list[CueCreatedFile] = field(default_factory=list)
    issues: list[CueIssue] = field(default_factory=list)


def parse_cue_value(value: str) -> str:
    value = value.strip()
    quoted = re.match(r'^"(?P<value>.*)"$', value)
    if quoted:
        return quoted.group("value")
    quoted_prefix = re.match(r'^"(?P<value>.*?)"', value)
    if quoted_prefix:
        return quoted_prefix.group("value")
    return value


def cue_time_to_seconds(value: str) -> float:
    match = re.match(r"^(\d+):(\d{2}):(\d{2})$", value.strip())
    if not match:
        raise ValueError(f"Invalid CUE time: {value}")
    minutes, seconds, frames = (int(part) for part in match.groups())
    return minutes * 60 + seconds + frames / 75


def read_cue_text(cue_path: Path) -> str:
    encodings = ["utf-8-sig", "utf-8", "cp932", "shift_jis", "gb18030", "latin-1"]
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            return cue_path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise UnicodeDecodeError(
        "cue",
        b"",
        0,
        1,
        f"Could not decode CUE file {cue_path}: {last_error}",
    )


def parse_cue(cue_path: Path) -> CueSheet:
    sheet = CueSheet(tracks=[])
    current_file = ""
    current_track: CueTrack | None = None
    for raw_line in read_cue_text(cue_path).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        file_match = re.match(r'^FILE\s+"(?P<name>.+?)"\s+\S+', line, re.IGNORECASE)
        if not file_match:
            file_match = re.match(r"^FILE\s+(?P<name>.+?)\s+\S+$", line, re.IGNORECASE)
        if file_match:
            current_file = file_match.group("name").strip()
            continue
        track_match = re.match(r"^TRACK\s+(?P<number>\d+)\s+AUDIO", line, re.IGNORECASE)
        if track_match:
            current_track = CueTrack(
                number=int(track_match.group("number")),
                file_name=current_file,
                indexes={},
            )
            assert sheet.tracks is not None
            sheet.tracks.append(current_track)
            continue
        title_match = re.match(r"^TITLE\s+(?P<value>.+)$", line, re.IGNORECASE)
        if title_match:
            value = parse_cue_value(title_match.group("value"))
            if current_track:
                current_track.title = value
            else:
                sheet.title = value
            continue
        performer_match = re.match(r"^PERFORMER\s+(?P<value>.+)$", line, re.IGNORECASE)
        if performer_match:
            value = parse_cue_value(performer_match.group("value"))
            if current_track:
                current_track.performer = value
            else:
                sheet.performer = value
            continue
        composer_match = re.match(
            r"^REM\s+COMPOSER\s+(?P<value>.+)$", line, re.IGNORECASE
        )
        if composer_match and current_track:
            current_track.composer = parse_cue_value(composer_match.group("value"))
            continue
        isrc_match = re.match(r"^ISRC\s+(?P<value>\S+)", line, re.IGNORECASE)
        if isrc_match and current_track:
            current_track.isrc = isrc_match.group("value")
            continue
        index_match = re.match(
            r"^INDEX\s+(?P<number>\d+)\s+(?P<time>\d+:\d{2}:\d{2})",
            line,
            re.IGNORECASE,
        )
        if index_match and current_track and current_track.indexes is not None:
            current_track.indexes[int(index_match.group("number"))] = cue_time_to_seconds(
                index_match.group("time")
            )
    sheet.tracks = [track for track in sheet.tracks or [] if track.index(1) is not None]
    return sheet


def sanitize_filename(value: str, fallback: str) -> str:
    name = value.strip() or fallback
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or fallback)[:180]


def _safe_relative_parts(value: str) -> tuple[str, ...]:
    return safe_relative_parts(value)


def _resolve_confined_regular_file(candidate: Path, root: Path) -> Path | None:
    try:
        return resolve_confined(root, candidate, kind="file", label="CUE audio")
    except ValueError:
        return None


def resolve_cue_audio(cue_path: Path, file_name: str) -> Path | None:
    if cue_path.is_symlink() or cue_path.parent.is_symlink():
        return None
    try:
        root = cue_path.parent.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    candidates: list[Path] = []
    if file_name:
        try:
            relative_parts = _safe_relative_parts(file_name)
        except ValueError:
            return None
        if not relative_parts:
            return None
        declared = root.joinpath(*relative_parts)
        candidates.append(declared)
        candidates.extend(declared.with_suffix(ext) for ext in DEFAULT_CUE_AUDIO_EXTS)
    candidates.extend(root / f"{cue_path.stem}{ext}" for ext in DEFAULT_CUE_AUDIO_EXTS)
    for candidate in candidates:
        resolved = _resolve_confined_regular_file(candidate, root)
        if resolved is not None:
            return resolved
    lower_names = {candidate.name.lower() for candidate in candidates}
    try:
        for child in root.iterdir():
            if child.name.lower() not in lower_names:
                continue
            resolved = _resolve_confined_regular_file(child, root)
            if resolved is not None:
                return resolved
    except OSError:
        return None
    return None


def normalized_cue_file_name(file_name: str) -> str:
    return file_name.replace("\\", "/").strip().lower()


def cue_output_path(
    target_cue: Path,
    track: CueTrack,
    sheet: CueSheet,
    options: CueSplitOptions,
    total_tracks: int,
) -> Path:
    if target_cue.parent.is_symlink():
        raise ValueError(f"CUE output root cannot be a symlink: {target_cue.parent}")
    output_root = target_cue.parent.resolve(strict=True)
    if not output_root.is_dir():
        raise ValueError(f"CUE output root is not a directory: {output_root}")
    output_dir = output_root
    output_subdir = options.output_subdir.strip()
    if output_subdir:
        relative_parts = _safe_relative_parts(output_subdir)
        for part in relative_parts:
            candidate = output_dir / part
            if candidate.is_symlink():
                raise ValueError(f"CUE output directory cannot be a symlink: {candidate}")
            try:
                resolved = candidate.resolve(strict=True)
            except FileNotFoundError:
                output_dir = candidate
                continue
            if not resolved.is_dir() or not resolved.is_relative_to(output_root):
                raise ValueError(
                    f"CUE output directory is outside the target album: {candidate}"
                )
            output_dir = resolved
    if not output_dir.resolve(strict=False).is_relative_to(output_root):
        raise ValueError(
            f"CUE output directory is outside the target album: {output_dir}"
        )
    title = track.title or f"Track {track.number:02d}"
    try:
        stem = options.filename_template.format(
            track=track.number,
            total=total_tracks,
            title=title,
            performer=track.performer or sheet.performer,
            album=sheet.title,
        )
    except Exception:
        stem = f"{track.number:02d} - {title}"
    return output_dir / f"{sanitize_filename(stem, f'Track {track.number:02d}')}.flac"


def seconds_arg(value: float) -> str:
    return f"{max(value, 0):.3f}"


def cue_output_is_valid(path: Path) -> bool:
    """Return whether an existing split is a readable audio file with duration."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size <= 4:
            return False
        from mutagen import File, MutagenError

        media = File(str(path), easy=False)
        return bool(media and media.info and float(media.info.length or 0) > 0)
    except (OSError, TypeError, ValueError, MutagenError):
        return False


class CueProcessor:
    def __init__(
        self,
        cancel_requested: CancelCheck | None = None,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
    ):
        self.cancel_requested = cancel_requested or (lambda: False)
        self.popen_factory = popen_factory

    def run_ffmpeg_split(
        self,
        options: CueSplitOptions,
        audio_source: Path,
        output_file: Path,
        start: float,
        end: float | None,
        track: CueTrack,
        sheet: CueSheet,
        total_tracks: int,
    ) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = output_file.with_name(f"{output_file.stem}.tmp{output_file.suffix}")
        if temp_file.exists():
            temp_file.unlink()
        cmd = [
            options.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(audio_source),
            "-map",
            "0:a:0",
            "-ss",
            seconds_arg(start),
        ]
        if end is not None:
            duration = end - start
            if duration <= 0:
                raise ValueError(f"Track {track.number:02d} has invalid duration")
            cmd.extend(["-t", seconds_arg(duration)])
        metadata = {
            "title": track.title or f"Track {track.number:02d}",
            "artist": track.performer or sheet.performer,
            "album": sheet.title,
            "track": f"{track.number:02d}/{total_tracks}",
            "TRACKNUMBER": f"{track.number:02d}",
            "TOTALTRACKS": str(total_tracks),
            "composer": track.composer,
            "ISRC": track.isrc,
        }
        for key, value in metadata.items():
            if value:
                cmd.extend(["-metadata", f"{key}={value}"])
        cmd.extend(
            [
                "-compression_level",
                str(max(0, min(options.flac_compression_level, 12))),
                "-c:a",
                "flac",
                "-y",
                str(temp_file),
            ]
        )
        process = self.popen_factory(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                if not self.cancel_requested():
                    continue
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                if temp_file.exists():
                    temp_file.unlink()
                raise InterruptedError("stopped by user")
        if process.returncode != 0:
            if temp_file.exists():
                temp_file.unlink()
            raise RuntimeError((stderr or stdout or "ffmpeg failed").strip())
        temp_file.replace(output_file)

    def split(
        self,
        source_cue: Path,
        target_cue: Path,
        options: CueSplitOptions,
        completed_tracks: set[int] | None = None,
        sheet: CueSheet | None = None,
    ) -> CueSplitResult:
        result = CueSplitResult()
        if not options.enabled or source_cue.suffix.lower() != ".cue":
            return result
        completed_tracks = completed_tracks or set()
        try:
            sheet = sheet or parse_cue(source_cue)
            tracks = sheet.tracks or []
            if not tracks:
                result.skipped = 1
                result.issues.append(
                    CueIssue("warning", f"CUE has no audio tracks with INDEX 01: {source_cue}")
                )
                return result
            cue_files = {
                normalized_cue_file_name(track.file_name)
                for track in tracks
                if track.file_name.strip()
            }
            if len(cue_files) > 1 and not options.split_multifile_cues:
                result.skipped = len(tracks)
                result.issues.append(
                    CueIssue(
                        "info",
                        "Skipped multi-file CUE because tracks already reference "
                        f"separate files: {source_cue}",
                        verbose_only=True,
                    )
                )
                return result
            target_cue.parent.mkdir(parents=True, exist_ok=True)
            expected_outputs = [
                cue_output_path(target_cue, track, sheet, options, len(tracks))
                for track in tracks
            ]
            if options.skip_existing and all(
                cue_output_is_valid(output)
                for output in expected_outputs
            ):
                result.skipped = len(expected_outputs)
                result.issues.append(
                    CueIssue(
                        "info", f"CUE split already exists: {source_cue}", verbose_only=True
                    )
                )
                return result
            for index, track in enumerate(tracks):
                output_file = expected_outputs[index]
                if cue_output_is_valid(output_file):
                    result.skipped += 1
                    continue
                if track.number in completed_tracks:
                    result.issues.append(
                        CueIssue(
                            "warning",
                            "Rebuilding a recorded CUE track because its output is "
                            f"missing or invalid: {output_file}",
                            verbose_only=True,
                        )
                    )
                audio_source = resolve_cue_audio(source_cue, track.file_name)
                if not audio_source:
                    result.skipped += 1
                    result.issues.append(
                        CueIssue(
                            "warning",
                            "Skipped CUE track because audio file was not found: "
                            f"{source_cue} references {track.file_name}",
                        )
                    )
                    continue
                next_track = tracks[index + 1] if index + 1 < len(tracks) else None
                end = None
                if next_track and next_track.file_name == track.file_name:
                    end = (
                        next_track.index(0)
                        if next_track.index(0) is not None
                        else next_track.index(1)
                    )
                try:
                    self.run_ffmpeg_split(
                        options,
                        audio_source,
                        output_file,
                        track.index(1) or 0,
                        end,
                        track,
                        sheet,
                        len(tracks),
                    )
                    result.created += 1
                    result.files.append(
                        CueCreatedFile(
                            marker=Path(f"{source_cue}#track-{track.number:02d}"),
                            output_path=output_file,
                            track_number=track.number,
                        )
                    )
                except InterruptedError:
                    raise
                except Exception as exc:
                    result.failed += 1
                    result.issues.append(
                        CueIssue(
                            "error",
                            f"Failed to split {source_cue} track {track.number:02d}: {exc}",
                            error=exc,
                        )
                    )
            return result
        except InterruptedError:
            raise
        except Exception as exc:
            result.failed += 1
            result.issues.append(
                CueIssue("error", f"Failed to parse CUE {source_cue}: {exc}", error=exc)
            )
            return result
