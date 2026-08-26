"""Deterministic filename-rule import into the library's 未分类 directory."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .manual_review import (
    MANUAL_CANDIDATE_KEY,
    build_manual_candidate,
    manual_destination_relative_directory,
)
from .review_importer import (
    _atomic_copy,
    _configured_import_mode,
    _library_candidate,
    _library_root,
    _validate_import_guard,
    _validated_library_file,
    _validated_source_file,
    imported_track_results,
)


def _existing_token_items(library: Any, token: str) -> list[Any]:
    return [
        item
        for item in library.items()
        if str(getattr(item, "review_recovery_token", "") or "") == token
    ]


def _expected_tracks(
    tracks: Sequence[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected: dict[str, dict[str, Any]] = {}
    for raw_track in tracks:
        if not isinstance(raw_track, dict):
            raise ValueError("规则入库曲目必须是对象")
        local_path = str(raw_track.get("local_path") or "").strip()
        relative = PurePosixPath(local_path)
        if not local_path or relative.is_absolute() or ".." in relative.parts:
            raise ValueError("规则入库包含无效文件路径")
        local_path = relative.as_posix()
        if local_path in expected:
            raise ValueError(f"规则入库包含重复音频文件: {local_path}")
        artist = str(raw_track.get("artist") or "").strip()
        title = str(raw_track.get("title") or "").strip()
        if not artist or not title:
            raise ValueError(f"规则入库缺少艺术家或标题: {local_path}")
        expected[local_path] = {
            "artist": artist,
            "title": title,
            "album": str(raw_track.get("album") or "").strip(),
            "albumartist": str(raw_track.get("albumartist") or artist).strip(),
            "disc": max(int(raw_track.get("disc") or 1), 1),
            "track": max(int(raw_track.get("track") or 0), 0),
            "year": max(int(raw_track.get("year") or 0), 0),
        }
    if not expected:
        raise ValueError("规则入库至少需要选择一首音频")
    return expected


def _write_and_verify_item(
    item: Any,
    expected: dict[str, Any],
    destination: Path,
) -> None:
    item.update(expected)
    item.store()
    item.try_write()
    from beets.library import Item

    verified = Item.from_path(destination)
    for field in ("artist", "title", "album", "albumartist"):
        if str(getattr(verified, field, "") or "") != str(
            expected.get(field, "") or ""
        ):
            raise RuntimeError(
                f"规则入库标签写入后回读不一致: {destination.name} ({field})"
            )


def _recover_existing_items(
    existing: Sequence[Any],
    tracks: Sequence[dict[str, Any]],
    root: Path,
) -> tuple[str, list[dict[str, str]]]:
    expected = _expected_tracks(tracks)
    by_source: dict[str, Any] = {}
    album_ids: set[str] = set()
    for item in existing:
        source = str(getattr(item, "review_source_path", "") or "").strip()
        if not source or source in by_source:
            raise RuntimeError("同一规则任务的 beets 恢复记录不完整")
        by_source[source] = item
        album_id = str(getattr(item, "review_manual_album_id", "") or "").strip()
        if album_id:
            album_ids.add(album_id)
    if set(by_source) != set(expected):
        raise RuntimeError("同一规则任务的 beets 曲目集合与已确认曲目不一致")
    if len(album_ids) > 1:
        raise RuntimeError("同一规则任务包含冲突的恢复标识")

    imported_items = []
    for source, values in expected.items():
        item = by_source[source]
        destination = _validated_library_file(item.path, root)
        _write_and_verify_item(item, values, destination)
        imported_items.append((source, item))
    return (
        next(iter(album_ids), MANUAL_CANDIDATE_KEY),
        imported_track_results(imported_items, library_directory=root),
    )


def import_manual_album(
    config_path: str | Path,
    source_path: str | Path,
    tracks: Sequence[dict[str, Any]],
    *,
    recovery_token: str,
    recover_only: bool = False,
    import_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from beets import config, plugins
    from beets.library import Item, Library

    token = str(recovery_token or "").strip()
    if not token:
        raise ValueError("规则入库任务缺少恢复令牌")
    config.set_file(str(config_path))
    config["threaded"].set(False)
    plugins.load_plugins()
    configured_source = Path(source_path)
    first_track = dict(tracks[0]) if tracks else {}
    relative_destination = manual_destination_relative_directory(
        str(first_track.get("album") or ""),
        str(first_track.get("albumartist") or first_track.get("artist") or ""),
    )
    library_directory = config["directory"].get(str)
    root = _library_root(library_directory, create=True)
    library = Library(config["library"].get(str), directory=library_directory)
    existing = _existing_token_items(library, token)
    if existing:
        album_id, imported = _recover_existing_items(
            existing,
            tracks,
            root,
        )
        return {
            "album_id": album_id,
            "imported_track_count": len(imported),
            "imported_tracks": imported,
            "destination_directory": str(
                root.joinpath(*relative_destination.parts)
            ),
            "reused_existing_album": True,
            "manual": True,
        }
    if recover_only:
        raise RuntimeError("未找到属于同一任务的规则入库记录，拒绝重新导入")
    if configured_source.is_symlink():
        raise ValueError("规则入库源目录不能是符号链接")
    source_root = configured_source.resolve(strict=True)
    candidate = build_manual_candidate(
        source_root,
        {
            "albumartist": first_track.get("albumartist", ""),
            "album": first_track.get("album", ""),
            "year": first_track.get("year", 0),
            "tracks": list(tracks),
        },
    )
    if import_guard is not None:
        _validate_import_guard(source_root, import_guard)

    import_mode = _configured_import_mode(config)
    # Filename-rule import exists specifically to persist the user's manual
    # metadata when no MusicBrainz mapping exists, so tag writing is mandatory.
    write_tags = True
    relative_destination = PurePosixPath(candidate["destination_relative_directory"])
    destination_directory = root.joinpath(*relative_destination.parts)
    created: list[Path] = []
    imported_items: list[tuple[str, Any]] = []
    source_files: list[Path] = []
    album_created = False
    try:
        for track in candidate["tracks"]:
            relative = PurePosixPath(track["local_path"])
            source = _validated_source_file(
                source_root.joinpath(*relative.parts), source_root
            )
            destination = _library_candidate(destination_directory / source.name, root)
            if destination.exists() or destination.is_symlink():
                raise FileExistsError(f"未分类目标已存在: {destination}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            parent = destination.parent.resolve(strict=True)
            if not parent.is_relative_to(root):
                raise ValueError("未分类目标超出音乐库目录")
            if import_mode == "hardlink":
                os.link(source, destination)
            else:
                _atomic_copy(source, destination, root)
            created.append(destination)
            item = Item.from_path(destination)
            item.update(
                {
                    "artist": track["artist"],
                    "title": track["title"],
                    "album": track["album"],
                    "albumartist": track["albumartist"],
                    "disc": int(track["disc"]),
                    "track": int(track["track"]),
                    "year": int(track["year"]),
                    "review_recovery_token": token,
                    "review_source_path": track["local_path"],
                    "review_manual_album_id": candidate["album_id"],
                }
            )
            imported_items.append((track["local_path"], item))
            source_files.append(source)
        library.add_album([item for _source, item in imported_items])
        album_created = True
        for _source, item in imported_items:
            if write_tags:
                destination = _validated_library_file(item.path, root)
                expected = {
                    field: getattr(item, field, "")
                    for field in (
                        "artist",
                        "title",
                        "album",
                        "albumartist",
                        "disc",
                        "track",
                        "year",
                    )
                }
                _write_and_verify_item(item, expected, destination)
        if import_mode == "move":
            for source in source_files:
                source.unlink()
        imported = imported_track_results(
            imported_items,
            library_directory=root,
        )
    except Exception:
        if not album_created:
            for path in reversed(created):
                path.unlink(missing_ok=True)
        raise
    return {
        "album_id": candidate["album_id"],
        "imported_track_count": len(imported),
        "imported_tracks": imported,
        "destination_directory": str(destination_directory),
        "manual": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--tracks-json", required=True)
    parser.add_argument("--recovery-token", required=True)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--import-guard-json")
    args = parser.parse_args()
    tracks = json.loads(args.tracks_json)
    if not isinstance(tracks, list):
        raise ValueError("tracks-json 必须是列表")
    guard = json.loads(args.import_guard_json) if args.import_guard_json else None
    if guard is not None and not isinstance(guard, dict):
        raise ValueError("import-guard-json 必须是对象")
    result = import_manual_album(
        args.config,
        args.source,
        tracks,
        recovery_token=args.recovery_token,
        recover_only=args.recover_only,
        import_guard=guard,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
