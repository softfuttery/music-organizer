"""Direct, path-confined management of files already inside the music library."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .lyrics import (
    _read_embedded_lyrics,
    combine_lyrics,
    lyric_digest,
    lyric_is_synced,
    write_embedded_lyrics,
)
from .pathsafe import resolve_confined, resolve_root, safe_relative_parts
from .review import AUDIO_EXTENSIONS

TRASH_DIRECTORY = ".music-organizer-trash"
MAX_LIBRARY_TRACKS = 20_000
EDITABLE_TAGS = (
    "title",
    "artist",
    "album",
    "albumartist",
    "tracknumber",
    "discnumber",
    "date",
    "genre",
)
_TAG_ALIASES = {
    "title": ("title", "TIT2", "\xa9nam"),
    "artist": ("artist", "TPE1", "\xa9ART"),
    "album": ("album", "TALB", "\xa9alb"),
    "albumartist": ("albumartist", "album artist", "TPE2", "aART"),
    "tracknumber": ("tracknumber", "track", "TRCK", "trkn"),
    "discnumber": ("discnumber", "disc", "TPOS", "disk"),
    "date": ("date", "year", "TDRC", "TYER", "\xa9day"),
    "genre": ("genre", "TCON", "\xa9gen", "gnre"),
}
_MEDIA_UNSET = object()


def _one(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else ""
    if isinstance(value, tuple):
        current = int(value[0] or 0) if value else 0
        total = int(value[1] or 0) if len(value) > 1 else 0
        value = f"{current}/{total}" if total else str(current)
    text = getattr(value, "text", value)
    if isinstance(text, (list, tuple)):
        text = text[0] if text else ""
    value = text
    return str(value or "").strip()


def library_root(value: str | Path) -> Path:
    return resolve_root(value, label="音乐库目录")


def relative_library_path(value: Any) -> PurePosixPath:
    raw = str(value or "").strip()
    try:
        path = PurePosixPath(*safe_relative_parts(raw))
    except ValueError as exc:
        raise ValueError("音乐库相对路径无效") from exc
    if TRASH_DIRECTORY in path.parts:
        raise ValueError("音乐库相对路径无效")
    return path


def library_file(
    root: Path,
    value: Any,
    *,
    audio_only: bool = True,
) -> Path:
    relative = relative_library_path(value)
    resolved = resolve_confined(
        root,
        Path(*relative.parts),
        kind="file",
        label="音乐库文件",
    )
    if audio_only and resolved.suffix.lower() not in AUDIO_EXTENSIONS:
        raise ValueError("目标不是支持的音频文件")
    return resolved


def library_directory(root: Path, value: Any) -> Path:
    relative = relative_library_path(value)
    if not relative.parts:
        raise ValueError("不能操作音乐库根目录")
    return resolve_confined(
        root,
        Path(*relative.parts),
        kind="directory",
        allow_root=False,
        label="音乐库文件夹",
    )


def _media(path: Path, *, easy: bool = False) -> Any:
    from mutagen import File, MutagenError

    try:
        media = File(str(path), easy=easy)
    except (MutagenError, OSError, ValueError) as exc:
        raise ValueError(f"无法读取音频标签: {path.name}") from exc
    if media is None:
        raise ValueError(f"不支持读取该音频格式: {path.suffix}")
    return media


def _tags_from_media(media: Any | None) -> dict[str, str]:
    if media is None:
        return {name: "" for name in EDITABLE_TAGS}
    tags = getattr(media, "tags", None) or {}
    values: dict[str, str] = {}
    for name in EDITABLE_TAGS:
        value = ""
        for key in _TAG_ALIASES[name]:
            try:
                value = _one(tags.get(key))
            except (AttributeError, KeyError, TypeError, ValueError):
                value = ""
            if value:
                break
        values[name] = value
    return values


def read_tags(path: Path, *, media: Any = _MEDIA_UNSET) -> dict[str, str]:
    if media is _MEDIA_UNSET:
        try:
            media = _media(path)
        except ValueError:
            media = None
    return _tags_from_media(media)


def audio_duration(path: Path, *, media: Any = _MEDIA_UNSET) -> float:
    try:
        if media is _MEDIA_UNSET:
            media = _media(path)
        return round(float(getattr(getattr(media, "info", None), "length", 0) or 0), 3)
    except (TypeError, ValueError):
        return 0.0


def sidecar_path(path: Path) -> Path:
    return path.with_suffix(".lrc")


def lyric_state(path: Path, *, media: Any = _MEDIA_UNSET) -> dict[str, Any]:
    embedded = ""
    try:
        if media is _MEDIA_UNSET:
            media = _media(path)
        embedded = _read_embedded_lyrics(media) if media is not None else ""
    except ValueError:
        pass
    sidecar = ""
    lyric_file = sidecar_path(path)
    if lyric_file.is_file() and not lyric_file.is_symlink():
        try:
            sidecar = lyric_file.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            sidecar = ""
    return {
        "embedded": {
            "exists": bool(embedded),
            "content": embedded,
            "synced": lyric_is_synced(embedded),
        },
        "sidecar": {
            "exists": bool(sidecar),
            "content": sidecar,
            "synced": lyric_is_synced(sidecar),
            "path": lyric_file.name,
        },
    }


def track_payload(
    path: Path,
    root: Path,
    *,
    media: Any = _MEDIA_UNSET,
) -> dict[str, Any]:
    relative = path.relative_to(root).as_posix()
    stat = path.stat()
    if media is _MEDIA_UNSET:
        try:
            media = _media(path)
        except ValueError:
            media = None
    tags = read_tags(path, media=media)
    embedded = bool(
        _read_embedded_lyrics(media) if media is not None else ""
    )
    lyric_file = sidecar_path(path)
    try:
        sidecar = (
            not lyric_file.is_symlink()
            and lyric_file.is_file()
            and lyric_file.stat().st_size > 0
        )
    except OSError:
        sidecar = False
    return {
        "path": relative,
        "name": path.name,
        "directory": path.parent.relative_to(root).as_posix(),
        "extension": path.suffix.lower(),
        "size": stat.st_size,
        "modified_at": datetime.fromtimestamp(
            stat.st_mtime, timezone.utc
        ).isoformat(),
        "duration": audio_duration(path, media=media),
        "tags": tags,
        "lyrics": {
            "embedded": embedded,
            "sidecar": sidecar,
        },
    }


def iter_audio_files(root: Path) -> Iterable[Path]:
    seen = 0
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name != TRASH_DIRECTORY and not (current / name).is_symlink()
        )
        for name in sorted(filenames):
            candidate = current / name
            if candidate.is_symlink() or candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            yield candidate
            seen += 1
            if seen >= MAX_LIBRARY_TRACKS:
                return


def scan_tracks(
    root: Path,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    needle = " ".join(str(query or "").casefold().split())
    paths = sorted(
        iter_audio_files(root),
        key=lambda path: path.relative_to(root).as_posix().casefold(),
    )
    truncated = len(paths) >= MAX_LIBRARY_TRACKS
    offset = max(int(offset or 0), 0)
    limit = min(max(int(limit or 100), 1), 500)
    if not needle:
        values = []
        for path in paths[offset : offset + limit]:
            try:
                values.append(track_payload(path, root))
            except OSError:
                continue
        return {
            "root": str(root),
            "total": len(paths),
            "offset": offset,
            "limit": limit,
            "tracks": values,
            "truncated": truncated,
        }

    values = []
    for path in paths:
        try:
            payload = track_payload(path, root)
        except OSError:
            continue
        searchable = " ".join(
            [payload["path"], *payload["tags"].values()]
        ).casefold()
        if needle and needle not in searchable:
            continue
        values.append(payload)
    values.sort(
        key=lambda item: (
            item["directory"].casefold(),
            item["name"].casefold(),
        )
    )
    total = len(values)
    return {
        "root": str(root),
        "total": total,
        "offset": offset,
        "limit": limit,
        "tracks": values[offset : offset + limit],
        "truncated": truncated,
    }


def _folder_payload(
    directory: str,
    tracks: list[dict[str, Any]],
    *,
    track_count: int | None = None,
) -> dict[str, Any]:
    resolved_track_count = len(tracks) if track_count is None else track_count
    embedded_count = sum(
        1 for track in tracks if bool(track.get("lyrics", {}).get("embedded"))
    )
    return {
        "path": directory,
        "name": "根目录" if directory == "." else PurePosixPath(directory).name,
        "track_count": resolved_track_count,
        "size": sum(int(track.get("size") or 0) for track in tracks),
        "embedded_count": embedded_count,
        "all_embedded": (
            resolved_track_count > 0 and embedded_count == resolved_track_count
        ),
        "tracks": tracks,
        "deletable": directory != ".",
    }


def scan_folders(
    root: Path,
    *,
    query: str = "",
    offset: int = 0,
    limit: int = 20,
    order: str = "desc",
) -> dict[str, Any]:
    needle = " ".join(str(query or "").casefold().split())
    paths = list(iter_audio_files(root))
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        directory = path.parent.relative_to(root).as_posix()
        grouped.setdefault(directory, []).append(path)
    for values in grouped.values():
        values.sort(key=lambda path: path.name.casefold())

    offset = max(int(offset or 0), 0)
    limit = min(max(int(limit or 20), 1), 100)
    folders: list[dict[str, Any]] = []
    if needle:
        for directory, folder_paths in grouped.items():
            directory_match = needle in directory.casefold()
            matched_tracks = []
            for path in folder_paths:
                try:
                    payload = track_payload(path, root)
                except OSError:
                    continue
                searchable = " ".join(
                    [payload["path"], *payload["tags"].values()]
                ).casefold()
                if directory_match or needle in searchable:
                    matched_tracks.append(payload)
            if matched_tracks:
                folders.append(_folder_payload(directory, matched_tracks))
    else:
        selected_directories = sorted(
            grouped,
            key=str.casefold,
            reverse=str(order).casefold() != "asc",
        )[
            offset : offset + limit
        ]
        for directory in selected_directories:
            tracks = []
            for path in grouped[directory]:
                try:
                    tracks.append(track_payload(path, root))
                except OSError:
                    continue
            folders.append(
                _folder_payload(
                    directory,
                    tracks,
                    track_count=len(grouped[directory]),
                )
            )

    folders.sort(
        key=lambda value: str(value["path"]).casefold(),
        reverse=str(order).casefold() != "asc",
    )
    total = len(folders) if needle else len(grouped)
    matched_track_total = sum(folder["track_count"] for folder in folders)
    if needle:
        folders = folders[offset : offset + limit]
    return {
        "root": str(root),
        "total": total,
        "track_total": (
            matched_track_total if needle else len(paths)
        ),
        "offset": offset,
        "limit": limit,
        "order": "asc" if str(order).casefold() == "asc" else "desc",
        "folders": folders,
        "truncated": len(paths) >= MAX_LIBRARY_TRACKS,
    }


def track_detail(path: Path, root: Path) -> dict[str, Any]:
    try:
        media = _media(path)
    except ValueError:
        media = None
    payload = track_payload(path, root, media=media)
    payload["lyrics"] = lyric_state(path, media=media)
    return payload


def update_tags(path: Path, values: Mapping[str, Any]) -> dict[str, str]:
    unknown = set(values) - set(EDITABLE_TAGS)
    if unknown:
        raise ValueError(f"不支持修改的标签: {', '.join(sorted(unknown))}")
    media = _media(path, easy=True)
    if getattr(media, "tags", None) is None:
        try:
            media.add_tags()
        except (AttributeError, NotImplementedError) as exc:
            raise ValueError(f"该音频格式不支持添加标签: {path.suffix}") from exc
    tags = media.tags
    for name in EDITABLE_TAGS:
        if name not in values:
            continue
        value = str(values.get(name) or "").strip()[:1_000]
        if value:
            try:
                tags[name] = [value]
            except TypeError:
                tags[name] = value
        else:
            try:
                del tags[name]
            except KeyError:
                pass
    try:
        media.save()
    except (OSError, ValueError) as exc:
        raise ValueError(f"保存音频标签失败: {path.name}") from exc
    return read_tags(path)


def _atomic_text(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_lyrics(path: Path, content: Any, mode: str) -> dict[str, Any]:
    normalized = combine_lyrics(content)
    if not normalized:
        raise ValueError("歌词内容不能为空")
    mode = str(mode or "").strip().lower()
    if mode == "embedded":
        metadata = write_embedded_lyrics(path, normalized)
    elif mode == "sidecar":
        lyric_file = sidecar_path(path)
        if lyric_file.is_symlink():
            raise ValueError("同名歌词文件不能是符号链接")
        _atomic_text(lyric_file, normalized)
        verified = lyric_file.read_text(encoding="utf-8")
        if verified != normalized:
            raise RuntimeError("本地歌词文件写入后回读不一致")
        metadata = {
            "tag": "sidecar",
            "path": lyric_file.name,
            "synced": lyric_is_synced(normalized),
            "digest": lyric_digest(normalized),
        }
    else:
        raise ValueError("歌词保存位置必须是 embedded 或 sidecar")
    return {"mode": mode, **metadata, "lyrics": lyric_state(path)}


def _trash_root(root: Path) -> Path:
    target = root / TRASH_DIRECTORY
    if target.is_symlink():
        raise ValueError("音乐库回收区不能是符号链接")
    target.mkdir(mode=0o700, exist_ok=True)
    return target.resolve(strict=True)


def _remove_empty_tree(record_root: Path) -> None:
    if not record_root.exists() or record_root.is_symlink():
        return
    for directory in sorted(
        (path for path in record_root.rglob("*") if path.is_dir()),
        key=lambda value: len(value.parts),
        reverse=True,
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    try:
        record_root.rmdir()
    except OSError:
        pass


def _restore_destination(root: Path, relative: PurePosixPath) -> Path:
    parent = root
    for part in relative.parts[:-1]:
        parent = parent / part
        if parent.is_symlink():
            raise ValueError("恢复目标路径不能包含符号链接")
        if parent.exists() and not parent.is_dir():
            raise ValueError("恢复目标的父路径不是目录")
        parent.mkdir(exist_ok=True)
    resolved_parent = parent.resolve(strict=True)
    if not resolved_parent.is_relative_to(root):
        raise ValueError("恢复目标超出音乐库目录")
    destination = resolved_parent / relative.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"原位置已存在文件: {relative.as_posix()}")
    return destination


def trash_track(root: Path, relative_value: Any) -> dict[str, Any]:
    source = library_file(root, relative_value)
    relative = source.relative_to(root)
    token = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    record_root = _trash_root(root) / token
    destination = record_root / relative
    rollback: list[tuple[Path, Path]] = []
    try:
        destination.parent.mkdir(parents=True, exist_ok=False)
        os.replace(source, destination)
        rollback.append((destination, source))
        moved = [relative.as_posix()]
        lyric_file = sidecar_path(source)
        if lyric_file.is_file() and not lyric_file.is_symlink():
            lyric_destination = record_root / lyric_file.relative_to(root)
            lyric_destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(lyric_file, lyric_destination)
            rollback.append((lyric_destination, lyric_file))
            moved.append(lyric_file.relative_to(root).as_posix())
        manifest = {
            "token": token,
            "kind": "file",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "primary": relative.as_posix(),
            "paths": moved,
        }
        _atomic_text(
            record_root / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        return manifest
    except Exception:
        for moved_path, original_path in reversed(rollback):
            if moved_path.exists() and not original_path.exists():
                original_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(moved_path, original_path)
        _remove_empty_tree(record_root)
        raise


def trash_folder(root: Path, relative_value: Any) -> dict[str, Any]:
    source = library_directory(root, relative_value)
    relative = source.relative_to(root)
    track_count = sum(1 for _ in iter_audio_files(source))
    if not track_count:
        raise ValueError("文件夹中没有可管理的音频文件")
    token = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(4)
    record_root = _trash_root(root) / token
    destination = record_root / relative
    moved = False
    try:
        destination.parent.mkdir(parents=True, exist_ok=False)
        os.replace(source, destination)
        moved = True
        manifest = {
            "token": token,
            "kind": "folder",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "primary": relative.as_posix(),
            "paths": [relative.as_posix()],
            "track_count": track_count,
        }
        _atomic_text(
            record_root / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
        return manifest
    except Exception:
        if moved and destination.exists() and not source.exists():
            source.parent.mkdir(parents=True, exist_ok=True)
            os.replace(destination, source)
        _remove_empty_tree(record_root)
        raise


def trash_entries(root: Path) -> list[dict[str, Any]]:
    trash = root / TRASH_DIRECTORY
    if not trash.is_dir() or trash.is_symlink():
        return []
    values = []
    for record_root in sorted(trash.iterdir(), reverse=True):
        manifest_path = record_root / "manifest.json"
        if not record_root.is_dir() or record_root.is_symlink() or not manifest_path.is_file():
            continue
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(value, dict) and value.get("token") == record_root.name:
            values.append(value)
    return values


def restore_trash(root: Path, token: Any) -> dict[str, Any]:
    token = str(token or "").strip()
    if not token or not all(char.isalnum() or char in "-_" for char in token):
        raise ValueError("回收记录标识无效")
    trash = _trash_root(root)
    record_root = (trash / token).resolve(strict=True)
    if not record_root.is_dir() or record_root.parent != trash:
        raise ValueError("回收记录不存在")
    manifest_path = record_root / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("回收记录清单无效")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("回收记录清单无效") from exc
    if not isinstance(manifest, dict) or manifest.get("token") != token:
        raise ValueError("回收记录清单无效")
    kind = str(manifest.get("kind") or "file")
    if kind not in {"file", "folder"}:
        raise ValueError("回收记录类型无效")
    paths = [relative_library_path(value) for value in manifest.get("paths", [])]
    if not paths or len(paths) != len(set(paths)):
        raise ValueError("回收记录没有可恢复文件")
    if kind == "folder" and len(paths) != 1:
        raise ValueError("文件夹回收记录无效")
    destinations: dict[PurePosixPath, Path] = {}
    for relative in paths:
        try:
            if kind == "folder":
                library_directory(record_root, relative.as_posix())
            else:
                library_file(record_root, relative.as_posix(), audio_only=False)
        except ValueError as exc:
            raise ValueError(
                f"回收记录缺少文件: {relative.as_posix()}"
            ) from exc
        destinations[relative] = _restore_destination(root, relative)
    restored = []
    rollback: list[tuple[Path, Path]] = []
    try:
        for relative in paths:
            source = (
                library_directory(record_root, relative.as_posix())
                if kind == "folder"
                else library_file(
                    record_root,
                    relative.as_posix(),
                    audio_only=False,
                )
            )
            destination = destinations[relative]
            os.replace(source, destination)
            rollback.append((destination, source))
            restored.append(relative.as_posix())
    except Exception:
        for restored_path, trash_path in reversed(rollback):
            if restored_path.exists() and not trash_path.exists():
                trash_path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(restored_path, trash_path)
        raise
    manifest_path.unlink(missing_ok=True)
    _remove_empty_tree(record_root)
    return {"token": token, "restored": restored}
