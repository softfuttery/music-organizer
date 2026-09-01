"""Non-interactive beets import using the exact review decision."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

from .beets_review import _track_aliases, _track_key
from .file_compare import stable_regular_files_equal
from .pathsafe import (
    reject_symlink_components,
    resolve_confined,
    resolve_root,
)
from .source_guard import parse_source_guard, source_identity_snapshot


def duplicate_action_for_release(
    album_id: str,
    duplicates: Sequence[Any],
) -> Any:
    """Keep same-name albums unless the exact MusicBrainz release exists."""
    from beets.importer import DuplicateAction

    normalized_id = str(album_id or "").strip()
    duplicate_release_ids = {
        str(getattr(album, "mb_albumid", "") or "").strip()
        for album in duplicates
    }
    if normalized_id and normalized_id in duplicate_release_ids:
        raise ValueError(
            "目标 beets 库已存在同一 MusicBrainz 发行版本；"
            "请先在归档中核对，避免重复入库"
        )
    return DuplicateAction.KEEP


def apply_track_mapping(
    task: Any,
    candidate: Any,
    source_root: Path,
    track_mapping: Sequence[dict[str, str]],
    *,
    resolved_source_paths: dict[Any, str] | None = None,
) -> int:
    """Apply a persisted file-to-track decision to one beets candidate."""
    items_by_path = {}
    for item in task.items:
        path = Path(item.filepath)
        try:
            relative = path.relative_to(source_root).as_posix()
        except ValueError:
            relative = path.name
        if relative in items_by_path:
            raise ValueError(f"待审目录包含重复相对路径: {relative}")
        items_by_path[relative] = item
    candidate_tracks = list(getattr(candidate.info, "tracks", []) or [])
    tracks_by_alias: dict[str, list[Any]] = {}
    for track in candidate_tracks:
        for alias in _track_aliases(track):
            tracks_by_alias.setdefault(alias, []).append(track)
    mapping = {}
    used_track_keys: set[str] = set()
    for decision in track_mapping:
        local_path = str(decision.get("local_path") or "")
        track_key = str(decision.get("track_key") or "")
        parsed_path = PurePosixPath(local_path)
        if not local_path or parsed_path.is_absolute() or ".." in parsed_path.parts:
            raise ValueError(f"已确认的本地文件路径不安全: {local_path}")
        item = items_by_path.get(local_path)
        if item is None:
            suffix_matches = [
                value
                for relative, value in items_by_path.items()
                if local_path.endswith(f"/{relative}")
                or relative.endswith(f"/{local_path}")
            ]
            if len(suffix_matches) > 1:
                raise ValueError(f"已确认的本地文件路径存在歧义: {local_path}")
            if suffix_matches:
                item = suffix_matches[0]
        if item is None:
            raise ValueError(f"已确认的本地文件不存在: {local_path}")
        matches = tracks_by_alias.get(track_key, [])
        if not matches:
            raise ValueError(f"已确认的 MusicBrainz 曲目不存在: {track_key}")
        if len(matches) > 1:
            raise ValueError(f"MusicBrainz 曲目标识不唯一: {track_key}")
        track = matches[0]
        canonical_track_key = _track_key(track)
        if item in mapping or canonical_track_key in used_track_keys:
            raise ValueError("同一文件或 MusicBrainz 曲目不能重复对应")
        mapping[item] = track
        if resolved_source_paths is not None:
            resolved_source_paths[item] = local_path
        used_track_keys.add(canonical_track_key)
    if not mapping:
        raise ValueError("没有可执行的曲目对应")
    candidate.mapping = mapping
    candidate.extra_items = [item for item in task.items if item not in mapping]
    candidate.extra_tracks = [
        track
        for track in candidate_tracks
        if _track_key(track) not in used_track_keys
    ]
    return len(mapping)


class ApprovedImportSession:
    """Factory wrapper that keeps beets imports isolated in a subprocess."""

    @staticmethod
    def create(
        library: Any,
        paths: Sequence[Any],
        source_root: Path,
        album_id: str,
        track_mapping: Sequence[dict[str, str]],
        recovery_token: str,
    ) -> Any:
        from beets import importer

        class Session(importer.ImportSession):
            def __init__(self) -> None:
                super().__init__(library, None, paths, query=None)
                self.selected_track_count = 0
                self.imported_items: list[tuple[str, Any]] = []

            def should_resume(self, _path: Any) -> bool:
                return False

            def choose_match(self, task: Any) -> Any:
                candidate = next(
                    (
                        value
                        for value in task.candidates
                        if str(getattr(value.info, "album_id", "")) == album_id
                    ),
                    None,
                )
                if candidate is None:
                    raise ValueError(
                        f"MusicBrainz 未返回已确认的发行版本: {album_id}"
                    )
                resolved_source_paths: dict[Any, str] = {}
                self.selected_track_count = apply_track_mapping(
                    task,
                    candidate,
                    source_root,
                    track_mapping,
                    resolved_source_paths=resolved_source_paths,
                )
                self.imported_items = [
                    (resolved_source_paths[item], item)
                    for item in candidate.mapping
                ]
                for item in candidate.mapping:
                    item["review_recovery_token"] = recovery_token
                return candidate

            def get_duplicate_action(self, _task: Any, duplicates: list[Any]) -> Any:
                return duplicate_action_for_release(album_id, duplicates)

        return Session()


def _reject_symlink_components(path: Path, label: str) -> None:
    reject_symlink_components(path, label=label)


def _library_root(path: str | Path, *, create: bool = False) -> Path:
    try:
        return resolve_root(path, create=create, label="媒体库目录")
    except ValueError as exc:
        if "symlink" in str(exc):
            raise ValueError("媒体库目录不能包含符号链接") from exc
        raise


def _library_candidate(path: str | Path, library_root: Path) -> Path:
    try:
        return resolve_confined(
            library_root,
            path,
            must_exist=False,
            label="入库目标",
        )
    except ValueError as exc:
        if "symlink" in str(exc):
            raise ValueError("入库目标不能包含符号链接") from exc
        raise ValueError(f"入库目标超出媒体库目录: {path}") from exc


def _validated_library_file(path: str | Path, library_root: Path) -> Path:
    try:
        return resolve_confined(
            library_root,
            path,
            kind="file",
            label="入库目标文件",
        )
    except ValueError as exc:
        if "symlink" in str(exc):
            raise ValueError("入库目标不能包含符号链接") from exc
        if Path(os.fsdecode(path)).is_absolute():
            raise ValueError(f"入库目标超出媒体库目录: {path}") from exc
        raise


def _validated_source_file(path: Path, source_root: Path) -> Path:
    try:
        root = resolve_root(source_root, label="恢复源目录")
        return resolve_confined(root, path, kind="file", label="恢复源文件")
    except ValueError as exc:
        if "symlink" in str(exc):
            raise ValueError("恢复源文件不能包含符号链接") from exc
        raise ValueError(f"恢复源文件超出已确认目录: {path}") from exc


def _atomic_copy(source: Path, destination: Path, library_root: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent = destination.parent.resolve(strict=True)
    if not parent.is_relative_to(library_root):
        raise ValueError(f"入库目标超出媒体库目录: {destination}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.recover-",
        dir=parent,
    )
    temporary = Path(temporary_name)
    try:
        with source.open("rb") as source_handle:
            with os.fdopen(descriptor, "wb") as destination_handle:
                descriptor = -1
                shutil.copyfileobj(source_handle, destination_handle)
                destination_handle.flush()
                os.fsync(destination_handle.fileno())
        shutil.copystat(source, temporary, follow_symlinks=False)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        os.replace(temporary, destination)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _configured_import_mode(config: Any) -> str:
    for mode in ("move", "copy", "hardlink"):
        if config["import"][mode].get(bool):
            return mode
    raise ValueError("beets 导入方式必须是 copy、move 或 hardlink")


def _validate_import_guard(
    source_root: Path,
    import_guard: dict[str, Any],
    *,
    allowed_metadata_changes: set[str] | None = None,
) -> None:
    expected_root, expected_entries = parse_source_guard(import_guard)
    if source_root.is_symlink():
        raise ValueError("入库源保护快照检测到源目录符号链接")
    try:
        root = source_root.expanduser().resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise ValueError("入库源保护快照对应的源目录不存在") from exc
    root_metadata = root.stat()
    current_root = (int(root_metadata.st_dev), int(root_metadata.st_ino))
    if current_root != expected_root:
        raise ValueError("入库源保护快照检测到源目录已被替换")
    allowed = allowed_metadata_changes or set()
    for relative, current in source_identity_snapshot(
        root,
        symlink_policy="reject",
    ).items():
        previous = expected_entries.get(relative)
        if previous is None:
            raise ValueError(
                f"入库源保护快照检测到新增路径: {relative}"
            )
        if previous[:3] != current[:3]:
            raise ValueError(
                f"入库源保护快照检测到路径已被替换: {relative}"
            )
        if previous[3:] != current[3:] and relative not in allowed:
            raise ValueError(
                f"入库源保护快照检测到文件发生变化: {relative}"
            )


def _completed_hardlink_metadata_changes(
    selected_items: Sequence[tuple[str, PurePosixPath, Any]],
    source_root: Path,
    library_root: Path,
) -> set[str]:
    allowed: set[str] = set()
    for _local_path, relative, item in selected_items:
        source_candidate = source_root.joinpath(*relative.parts)
        try:
            source = _validated_source_file(source_candidate, source_root)
        except ValueError:
            continue
        raw_current = getattr(item, "path", None) or getattr(
            item, "filepath", None
        )
        destinations: list[Path] = []
        if raw_current:
            current = Path(os.fsdecode(raw_current)).expanduser()
            try:
                current_resolved = current.resolve(strict=True)
            except (FileNotFoundError, OSError, RuntimeError):
                current_resolved = current
            if current_resolved.is_relative_to(library_root):
                destinations.append(
                    _validated_library_file(current, library_root)
                )
        if not destinations:
            candidate = _library_candidate(item.destination(), library_root)
            if candidate.exists() or candidate.is_symlink():
                destinations.append(
                    _validated_library_file(candidate, library_root)
                )
        for destination in destinations:
            try:
                if os.path.samefile(source, destination):
                    allowed.add(relative.as_posix())
                    break
            except OSError:
                continue
    return allowed


def imported_track_results(
    imported_items: Sequence[tuple[str, Any]],
    *,
    library_directory: str | Path | None = None,
) -> list[dict[str, str]]:
    library_root = (
        _library_root(library_directory)
        if library_directory is not None
        else None
    )
    results = []
    for source, item in imported_items:
        raw_path = getattr(item, "path", None) or getattr(item, "filepath", None)
        if not raw_path:
            raise RuntimeError(f"beets 未返回入库目标路径: {source}")
        raw_destination = Path(os.fsdecode(raw_path))
        if library_root is not None:
            destination = _validated_library_file(raw_destination, library_root)
        else:
            if raw_destination.is_symlink():
                raise ValueError(f"入库目标不能是符号链接: {raw_destination}")
            destination = raw_destination.resolve(strict=True)
            if not destination.is_file():
                raise ValueError(f"入库目标不是普通文件: {destination}")
        results.append({"source": source, "destination": str(destination)})
    return results


def _operation_matches_source(
    source: Path,
    destination: Path,
    import_mode: str,
) -> bool:
    if import_mode == "hardlink":
        return os.path.samefile(source, destination)
    return stable_regular_files_equal(source, destination)


def _remove_moved_source(
    source: Path,
    destination: Path,
    source_root: Path,
) -> None:
    safe_source = _validated_source_file(source, source_root)
    if not stable_regular_files_equal(safe_source, destination):
        raise ValueError(f"move 恢复源文件与入库目标不一致: {source}")
    safe_source.unlink()


def _write_recovered_tags(
    item: Any,
    destination: Path,
    library_root: Path,
) -> Path:
    writer = getattr(item, "try_write", None)
    if not callable(writer):
        raise RuntimeError("beets 曲目不支持恢复标签写入")
    writer()
    return _validated_library_file(destination, library_root)


def _recover_existing_item(
    item: Any,
    source_root: Path,
    local_path: PurePosixPath,
    library_root: Path,
    import_mode: str | None,
    *,
    allow_source_operations: bool,
    write_tags: bool,
) -> Path:
    raw_current = getattr(item, "path", None) or getattr(item, "filepath", None)
    if not raw_current:
        raise RuntimeError("已有 beets 曲目缺少文件路径")
    current = Path(os.fsdecode(raw_current)).expanduser()
    current_exists = current.exists() or current.is_symlink()
    if current_exists:
        if current.is_symlink():
            raise ValueError(f"beets 曲目路径不能是符号链接: {current}")
        current_resolved = current.resolve(strict=True)
        if current_resolved.is_relative_to(library_root):
            destination = _validated_library_file(current, library_root)
            if import_mode == "move" and allow_source_operations:
                mapped_source = source_root.joinpath(*local_path.parts)
                if mapped_source.exists() or mapped_source.is_symlink():
                    _remove_moved_source(
                        mapped_source,
                        destination,
                        source_root,
                    )
            if write_tags:
                destination = _write_recovered_tags(
                    item,
                    destination,
                    library_root,
                )
            return destination
        if not allow_source_operations:
            raise RuntimeError(
                f"源目录已变化，不能采用尚未由 beets 持久化的目标: "
                f"{local_path.as_posix()}"
            )
        source = _validated_source_file(current, source_root)
        mapped_source = _validated_source_file(
            source_root.joinpath(*local_path.parts), source_root
        )
        if source != mapped_source:
            raise ValueError(
                f"beets 曲目路径与已确认源文件不一致: {local_path.as_posix()}"
            )
    else:
        source = None

    raw_destination = item.destination()
    destination_candidate = _library_candidate(raw_destination, library_root)
    destination_exists = (
        destination_candidate.exists() or destination_candidate.is_symlink()
    )
    if not destination_exists:
        if not allow_source_operations:
            raise RuntimeError(
                f"恢复目标尚未生成，不能使用当前源文件: {local_path.as_posix()}"
            )
        if source is None:
            raise RuntimeError(
                f"恢复源文件和入库目标都不存在: {local_path.as_posix()}"
            )
        if import_mode not in {"copy", "move", "hardlink"}:
            raise ValueError("恢复入库缺少有效的 copy、move 或 hardlink 配置")
        destination_candidate.parent.mkdir(parents=True, exist_ok=True)
        parent = destination_candidate.parent.resolve(strict=True)
        if not parent.is_relative_to(library_root):
            raise ValueError(f"入库目标超出媒体库目录: {destination_candidate}")
        try:
            if import_mode == "hardlink":
                os.link(source, destination_candidate)
            else:
                _atomic_copy(source, destination_candidate, library_root)
        except FileExistsError:
            # A previous attempt may have published the complete target just
            # before crashing. Validate it below instead of creating a .1 file.
            pass

    destination = _validated_library_file(
        destination_candidate,
        library_root,
    )
    if source is not None and allow_source_operations:
        if import_mode not in {"copy", "move", "hardlink"}:
            raise ValueError("恢复入库缺少有效的 copy、move 或 hardlink 配置")
        if not _operation_matches_source(source, destination, import_mode):
            raise ValueError(f"入库目标文件冲突: {destination}")

    item.path = str(destination)
    item.store()
    if import_mode == "move" and source is not None and allow_source_operations:
        _remove_moved_source(source, destination, source_root)
    if write_tags:
        destination = _write_recovered_tags(
            item,
            destination,
            library_root,
        )
    return destination


def existing_album_import_result(
    library: Any,
    album_id: str,
    source_root: Path,
    track_mapping: Sequence[dict[str, str]],
    *,
    recovery_token: str | None = None,
    import_mode: str | None = None,
    library_directory: str | Path | None = None,
    allow_source_operations: bool = True,
    import_guard: dict[str, Any] | None = None,
    write_tags: bool = False,
) -> dict[str, Any] | None:
    """Recover a previously completed import for the same persisted task.

    A matching MusicBrainz release/track ID does not prove that source audio is
    identical. Reuse is allowed only when every item in the existing album has
    the persisted token for this exact import attempt.
    """
    matching_albums = [
        album
        for album in library.albums()
        if str(getattr(album, "mb_albumid", "") or "") == album_id
    ]
    if not matching_albums:
        return None
    if len(matching_albums) != 1:
        raise ValueError(f"beets 库中存在多个相同 MusicBrainz 发行版: {album_id}")

    existing_items = list(matching_albums[0].items())
    token = str(recovery_token or "").strip()
    if not token or any(
        str(getattr(item, "review_recovery_token", "") or "") != token
        for item in existing_items
    ):
        raise ValueError(
            "目标 beets 库中发行版已存在；无法确认属于同一导入任务，拒绝复用"
        )

    items_by_release_track: dict[str, list[Any]] = {}
    items_by_recording: dict[str, list[Any]] = {}
    for item in existing_items:
        release_track_id = str(
            getattr(item, "mb_releasetrackid", "") or ""
        ).strip()
        recording_id = str(getattr(item, "mb_trackid", "") or "").strip()
        if release_track_id:
            items_by_release_track.setdefault(release_track_id, []).append(item)
        if recording_id:
            items_by_recording.setdefault(recording_id, []).append(item)
    configured_library = library_directory or getattr(library, "directory", None)
    if not configured_library:
        raise ValueError("beets 未配置媒体库目录")
    library_root = _library_root(
        configured_library,
        create=allow_source_operations,
    )
    selected_items: list[tuple[str, PurePosixPath, Any]] = []
    used_tracks: set[str] = set()
    used_items: set[int] = set()
    for decision in track_mapping:
        local_path = str(decision.get("local_path") or "")
        track_key = str(decision.get("track_key") or "")
        relative = PurePosixPath(local_path)
        if not local_path or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"已确认的本地文件路径不安全: {local_path}")
        if track_key in used_tracks:
            raise ValueError("同一 MusicBrainz 曲目不能重复对应")
        release_track_matches = items_by_release_track.get(track_key, [])
        if len(release_track_matches) > 1:
            raise ValueError(
                f"MusicBrainz 发行曲目标识不唯一，无法安全恢复: {track_key}"
            )
        if release_track_matches:
            item = release_track_matches[0]
        else:
            recording_matches = items_by_recording.get(track_key, [])
            if len(recording_matches) > 1:
                raise ValueError(
                    "旧版 MusicBrainz recording ID 不唯一，无法安全恢复: "
                    f"{track_key}"
                )
            item = recording_matches[0] if recording_matches else None
        if item is None:
            raise ValueError(
                f"已有发行版缺少已确认的 MusicBrainz 曲目: {track_key}"
            )
        item_identity = id(item)
        if item_identity in used_items:
            raise ValueError("同一 beets 曲目不能重复对应")
        selected_items.append((local_path, relative, item))
        used_tracks.add(track_key)
        used_items.add(item_identity)
    if not selected_items:
        raise ValueError("没有可复用的曲目对应")

    effective_write_tags = bool(write_tags and allow_source_operations)
    if allow_source_operations:
        allowed_metadata_changes: set[str] = set()
        if import_mode == "hardlink" and effective_write_tags:
            if import_guard is None:
                raise ValueError("hardlink 标签写入恢复缺少持久化源保护快照")
            allowed_metadata_changes = _completed_hardlink_metadata_changes(
                selected_items,
                source_root,
                library_root,
            )
        if import_guard is not None:
            _validate_import_guard(
                source_root,
                import_guard,
                allowed_metadata_changes=allowed_metadata_changes,
            )

    imported_items: list[tuple[str, Any]] = []
    for local_path, relative, item in selected_items:
        _recover_existing_item(
            item,
            source_root,
            relative,
            library_root,
            import_mode,
            allow_source_operations=allow_source_operations,
            write_tags=effective_write_tags,
        )
        imported_items.append((local_path, item))

    imported_tracks = imported_track_results(
        imported_items,
        library_directory=library_root,
    )
    destination_directory = Path(
        os.path.commonpath(
            [str(Path(track["destination"]).parent) for track in imported_tracks]
        )
    )
    return {
        "album_id": album_id,
        "imported_track_count": len(imported_tracks),
        "imported_tracks": imported_tracks,
        "destination_directory": str(destination_directory),
        "reused_existing_album": True,
        "message": "已复用 beets 库中的同一发行版，未重复入库音轨",
    }


def import_review_album(
    config_path: str | Path,
    source_path: str | Path,
    album_id: str,
    track_mapping: Sequence[dict[str, str]],
    *,
    recovery_token: str | None = None,
    recover_only: bool = False,
    import_guard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from beets import config, plugins, util
    from beets.library import Library

    config.set_file(str(config_path))
    config["threaded"].set(False)
    config["import"]["autotag"].set(True)
    config["import"]["quiet"].set(True)
    config["import"]["timid"].set(False)
    config["import"]["resume"].set(False)
    config["import"]["incremental"].set(False)
    # Identification treats every audio file below the selected review root as
    # one album. Mirror that grouping during import so multi-disc directories
    # such as LACA-9025/ and LACA-9026/ are not split into separate tasks.
    config["import"]["singletons"].set(False)
    config["import"]["flat"].set(True)
    config["import"]["group_albums"].set(False)
    config["import"]["search_ids"].set([album_id])
    plugins.load_plugins()

    source_root = Path(source_path)
    library_directory = config["directory"].get(str)
    import_mode = _configured_import_mode(config)
    write_tags = config["import"]["write"].get(bool)
    library = Library(
        config["library"].get(str),
        directory=library_directory,
    )
    existing_result = existing_album_import_result(
        library,
        album_id,
        source_root,
        track_mapping,
        recovery_token=recovery_token,
        import_mode=import_mode,
        library_directory=library_directory,
        allow_source_operations=not recover_only,
        import_guard=import_guard,
        write_tags=write_tags,
    )
    if existing_result is not None:
        return existing_result
    if recover_only:
        raise RuntimeError("未找到属于同一任务的已入库发行版，拒绝重新导入")
    token = str(recovery_token or "").strip()
    if not token:
        raise ValueError("导入任务缺少恢复令牌")
    if import_guard is not None:
        _validate_import_guard(source_root, import_guard)
    root = source_root.resolve(strict=True)
    session = ApprovedImportSession.create(
        library,
        [util.bytestring_path(str(root))],
        root,
        album_id,
        track_mapping,
        token,
    )
    session.run()
    if not session.selected_track_count:
        raise RuntimeError("beets 没有执行任何曲目入库")
    for _source, item in session.imported_items:
        item["review_recovery_token"] = token
        item.store()
    imported_tracks = imported_track_results(
        session.imported_items,
        library_directory=library_directory,
    )
    destination_directory = Path(
        os.path.commonpath(
            [str(Path(track["destination"]).parent) for track in imported_tracks]
        )
    )
    return {
        "album_id": album_id,
        "imported_track_count": session.selected_track_count,
        "imported_tracks": imported_tracks,
        "destination_directory": str(destination_directory),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--album-id", required=True)
    parser.add_argument("--mapping-json", required=True)
    parser.add_argument("--recovery-token", required=True)
    parser.add_argument("--recover-only", action="store_true")
    parser.add_argument("--import-guard-json")
    args = parser.parse_args()
    mapping = json.loads(args.mapping_json)
    if not isinstance(mapping, list):
        raise ValueError("mapping-json 必须是列表")
    import_guard = None
    if args.import_guard_json:
        import_guard = json.loads(args.import_guard_json)
        if not isinstance(import_guard, dict):
            raise ValueError("import-guard-json 必须是对象")
    result = import_review_album(
        args.config,
        args.source,
        args.album_id,
        mapping,
        recovery_token=args.recovery_token,
        recover_only=args.recover_only,
        import_guard=import_guard,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
