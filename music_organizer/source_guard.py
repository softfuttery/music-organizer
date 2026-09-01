"""Shared source-tree identity snapshots for approved review imports."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import Literal

SourceIdentity = tuple[int, int, int, int, int]
SymlinkPolicy = Literal["record_and_skip", "reject"]


def source_identity_snapshot(
    root: Path,
    *,
    symlink_policy: SymlinkPolicy,
) -> dict[str, SourceIdentity]:
    """Capture path identities without following links or hashing media."""
    if symlink_policy not in {"record_and_skip", "reject"}:
        raise ValueError(f"Unsupported symlink policy: {symlink_policy}")
    snapshot: dict[str, SourceIdentity] = {}
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in (*dirnames, *filenames):
            candidate = current / name
            metadata = candidate.lstat()
            relative = candidate.relative_to(root).as_posix()
            if stat.S_ISLNK(metadata.st_mode) and symlink_policy == "reject":
                raise ValueError(f"入库源保护快照检测到符号链接: {relative}")
            file_type = stat.S_IFMT(metadata.st_mode)
            snapshot[relative] = (
                int(metadata.st_dev),
                int(metadata.st_ino),
                file_type,
                int(metadata.st_size) if stat.S_ISREG(metadata.st_mode) else 0,
                int(metadata.st_mtime_ns) if stat.S_ISREG(metadata.st_mode) else 0,
            )
        dirnames[:] = [
            name for name in dirnames if not (current / name).is_symlink()
        ]
    return snapshot


def serialize_source_guard(
    root_identity: tuple[int, int],
    snapshot: dict[str, SourceIdentity],
) -> dict:
    return {
        "root": list(root_identity),
        "entries": {path: list(identity) for path, identity in snapshot.items()},
    }


def parse_source_guard(
    value: object,
) -> tuple[tuple[int, int], dict[str, SourceIdentity]]:
    if not isinstance(value, dict):
        raise ValueError("持久化入库源保护快照无效")
    raw_root = value.get("root")
    raw_entries = value.get("entries")
    if not isinstance(raw_root, list) or len(raw_root) != 2 or not isinstance(
        raw_entries, dict
    ):
        raise ValueError("持久化入库源保护快照不完整")
    try:
        root_identity = (int(raw_root[0]), int(raw_root[1]))
        entries: dict[str, SourceIdentity] = {}
        for raw_path, raw_identity in raw_entries.items():
            if not isinstance(raw_path, str) or not isinstance(
                raw_identity, list
            ) or len(raw_identity) != 5:
                raise ValueError
            relative = PurePosixPath(raw_path)
            if not raw_path or relative.is_absolute() or ".." in relative.parts:
                raise ValueError
            normalized = relative.as_posix()
            if normalized in entries:
                raise KeyError(normalized)
            entries[normalized] = tuple(
                int(part) for part in raw_identity
            )  # type: ignore[assignment]
    except KeyError as exc:
        raise ValueError("持久化入库源保护快照包含重复路径") from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("持久化入库源保护快照无效") from exc
    return root_identity, entries


def snapshot_has_new_or_replaced_paths(
    before: dict[str, SourceIdentity],
    after: dict[str, SourceIdentity],
    *,
    allowed_metadata_changes: set[str] | None = None,
) -> bool:
    """Missing paths are expected for move mode; additions/replacements are not."""
    allowed_metadata_changes = allowed_metadata_changes or set()
    for path, identity in after.items():
        previous = before.get(path)
        if previous is None or previous[:3] != identity[:3]:
            return True
        if previous[3:] != identity[3:] and path not in allowed_metadata_changes:
            return True
    return False
