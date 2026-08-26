"""Filename-rule metadata preview for review items without MusicBrainz mappings."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .review import AUDIO_EXTENSIONS, audio_files

MANUAL_CANDIDATE_KEY = "manual:filename-rules"
UNCLASSIFIED_DIRECTORY = "未分类"
_LEADING_BRACKETS = re.compile(r"^(?:\s*\[[^\]]*\])+\s*")
_TRAILING_BRACKETS = re.compile(r"\s*(?:\[[^\]]*\]\s*)+$")
_TRACK_PREFIX = re.compile(
    r"^(?:(?P<disc>\d{1,2})[-.])?(?P<track>\d{1,3})(?:[ ._-]+)"
)


def _text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").strip().split())[:limit]


def _number(value: Any, *, maximum: int = 999) -> int:
    try:
        return max(0, min(int(value or 0), maximum))
    except (TypeError, ValueError):
        return 0


def manual_directory_name(value: str, fallback: str) -> str:
    """Return one safe, readable directory component for manual imports."""
    cleaned = _text(value).replace("/", "／").replace("\\", "＼").strip(" .")
    return cleaned if cleaned not in {"", ".", ".."} else fallback


def manual_artist_directory(value: str) -> str:
    return manual_directory_name(value, "未知艺术家")


def manual_album_directory(value: str) -> str:
    return manual_directory_name(value, "未知专辑")


def manual_destination_relative_directory(
    album: str,
    albumartist: str,
) -> PurePosixPath:
    """Keep manual imports under 未分类/<artist>/<tag album directory>."""
    return PurePosixPath(
        UNCLASSIFIED_DIRECTORY,
        manual_artist_directory(albumartist),
        manual_album_directory(album),
    )


def infer_directory_metadata(name: str) -> tuple[str, str]:
    cleaned = _LEADING_BRACKETS.sub("", str(name or "").strip())
    cleaned = _TRAILING_BRACKETS.sub("", cleaned).strip()
    if " - " in cleaned:
        artist, album = cleaned.split(" - ", 1)
        return _text(artist), _text(album)
    return "", _text(cleaned)


def infer_filename_metadata(
    filename: str,
    *,
    default_artist: str = "",
    default_track: int = 0,
) -> dict[str, Any]:
    stem = Path(filename).stem.strip()
    match = _TRACK_PREFIX.match(stem)
    disc = _number(match.group("disc"), maximum=99) if match else 0
    track = _number(match.group("track")) if match else default_track
    remaining = stem[match.end() :].strip(" -._") if match else stem
    artist = _text(default_artist)
    title = _text(remaining)
    if " - " in remaining:
        parsed_artist, parsed_title = remaining.split(" - ", 1)
        if parsed_artist.strip() and parsed_title.strip():
            artist = _text(parsed_artist)
            title = _text(parsed_title)
    return {
        "artist": artist,
        "title": title or _text(stem),
        "disc": disc or 1,
        "track": track,
    }


def _relative_audio_files(source_root: Path) -> dict[str, Path]:
    return {
        path.relative_to(source_root).as_posix(): path
        for path in audio_files(source_root)
    }


def _auxiliary_files(source_root: Path) -> list[str]:
    return sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() not in AUDIO_EXTENSIONS
    )


def build_manual_candidate(
    source_path: str | Path,
    payload: Mapping[str, Any] | None = None,
    *,
    current_artist: str = "",
    current_album: str = "",
) -> dict[str, Any]:
    root = Path(source_path).resolve(strict=True)
    if not root.is_dir() or Path(source_path).is_symlink():
        raise ValueError("规则入库源目录无效")
    available = _relative_audio_files(root)
    if not available:
        raise ValueError("目录中没有可按规则入库的音频文件")

    directory_artist, directory_album = infer_directory_metadata(root.name)
    payload = dict(payload or {})
    albumartist = _text(
        payload.get("albumartist") or current_artist or directory_artist
    )
    album = _text(payload.get("album") or current_album or directory_album or root.name)
    year = _number(payload.get("year"), maximum=9999)
    raw_tracks = payload.get("tracks")
    supplied: dict[str, Mapping[str, Any]] = {}
    if raw_tracks is not None:
        if not isinstance(raw_tracks, list):
            raise ValueError("规则入库曲目必须是列表")
        for value in raw_tracks:
            if not isinstance(value, Mapping):
                raise ValueError("规则入库曲目必须是对象")
            raw_path = str(value.get("local_path") or "").strip()
            relative = PurePosixPath(raw_path)
            if not raw_path or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("规则入库包含无效文件路径")
            path = relative.as_posix()
            if path not in available:
                raise ValueError(f"规则入库包含未知音频文件: {path}")
            if path in supplied:
                raise ValueError(f"规则入库包含重复音频文件: {path}")
            supplied[path] = value

    tracks = []
    local_items = []
    destination_names: set[str] = set()
    destination_directory = manual_destination_relative_directory(
        album,
        albumartist or directory_artist,
    )
    for index, (local_path, path) in enumerate(sorted(available.items()), 1):
        inferred = infer_filename_metadata(
            path.name,
            default_artist=albumartist or directory_artist,
            default_track=index,
        )
        provided = supplied.get(local_path)
        included = provided is not None if raw_tracks is not None else True
        values = dict(provided or {})
        artist = _text(values.get("artist") or inferred["artist"] or albumartist)
        title = _text(values.get("title") or inferred["title"])
        disc = _number(values.get("disc") or inferred["disc"], maximum=99) or 1
        track_number = _number(values.get("track") or inferred["track"] or index)
        track_key = f"manual:{local_path}"
        local_items.append(
            {
                "local_path": local_path,
                "local_title": title,
                "extension": path.suffix,
                "track_key": track_key if included else "",
            }
        )
        if not included:
            continue
        if not artist or not title:
            raise ValueError(f"规则入库缺少艺术家或标题: {local_path}")
        destination_name = path.name
        normalized_destination_name = destination_name.casefold()
        if normalized_destination_name in destination_names:
            raise ValueError(
                f"规则入库扁平化后存在同名文件，请先调整文件名: {destination_name}"
            )
        destination_names.add(normalized_destination_name)
        tracks.append(
            {
                "key": track_key,
                "track_key": track_key,
                "local_path": local_path,
                "artist": artist,
                "title": title,
                "album": album,
                "albumartist": albumartist or artist,
                "disc": disc,
                "track": track_number,
                "year": year,
                "extension": path.suffix,
                "target_path": (destination_directory / destination_name).as_posix(),
            }
        )
    if not tracks:
        raise ValueError("规则入库至少需要选择一首音频")
    digest = hashlib.sha256(
        json.dumps(tracks, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    mapped = {track["local_path"] for track in tracks}
    return {
        "key": f"{MANUAL_CANDIDATE_KEY}:{digest}",
        "data_source": "manual",
        "album_id": f"manual:{digest}",
        "artist": albumartist or tracks[0]["artist"],
        "album": album,
        "year": year,
        "country": "",
        "media": "File",
        "mediums": max(track["disc"] for track in tracks),
        "score": 1.0,
        "tracks": tracks,
        "local_items": local_items,
        "track_options": tracks,
        "extra_items": [path for path in available if path not in mapped],
        "extra_tracks": [],
        "auxiliary_files": _auxiliary_files(root),
        "unclassified_directory": UNCLASSIFIED_DIRECTORY,
        "destination_relative_directory": destination_directory.as_posix(),
        "artist_directory_name": destination_directory.parts[1],
        "source_directory_name": root.name,
    }
