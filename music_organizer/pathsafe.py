"""Shared path-confinement primitives for filesystem mutations and reads."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Literal, Sequence

PathKind = Literal["file", "directory", "any"]


def safe_relative_parts(value: str | os.PathLike[str]) -> tuple[str, ...]:
    """Return portable relative parts, rejecting traversal and drive syntax."""
    raw = os.fsdecode(value)
    normalized = raw.replace("\\", "/")
    posix = PurePosixPath(normalized)
    windows = PureWindowsPath(raw)
    if (
        not raw
        or posix.is_absolute()
        or windows.is_absolute()
        or bool(windows.drive)
        or bool(windows.root)
        or ".." in posix.parts
    ):
        raise ValueError(f"Unsafe relative path: {raw}")
    parts = tuple(part for part in posix.parts if part not in {"", "."})
    if not parts:
        raise ValueError(f"Unsafe relative path: {raw}")
    return parts


def reject_symlink_components(path: Path, *, label: str = "Path") -> None:
    """Reject any existing symlink component in an absolute path."""
    absolute = path.expanduser().absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        if part in {"", ".", ".."}:
            raise ValueError(f"{label} contains an unsafe component: {path}")
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} contains a symlink: {current}")


def resolve_root(
    value: str | os.PathLike[str],
    *,
    create: bool = False,
    label: str = "Root",
) -> Path:
    """Resolve a real directory root without accepting redirected components."""
    configured = Path(os.fsdecode(value)).expanduser()
    reject_symlink_components(configured, label=label)
    if create:
        configured.mkdir(parents=True, exist_ok=True)
        reject_symlink_components(configured, label=label)
    try:
        resolved = configured.resolve(strict=True)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise ValueError(f"{label} does not exist: {configured}") from exc
    if resolved != configured.absolute():
        raise ValueError(f"{label} is redirected: {configured}")
    if not resolved.is_dir():
        raise ValueError(f"{label} is not a directory: {resolved}")
    return resolved


def mkdir_confined(
    root: Path,
    parts: Sequence[str],
    *,
    label: str = "Directory",
) -> Path:
    """Create relative directories without following an existing symlink."""
    resolved_root = resolve_root(root, label=label)
    current = resolved_root
    for part in parts:
        if part in {"", ".", ".."}:
            raise ValueError(f"{label} contains an unsafe component")
        candidate = current / part
        if candidate.is_symlink():
            raise ValueError(f"{label} contains a symlink: {candidate}")
        candidate.mkdir(exist_ok=True)
        current = candidate.resolve(strict=True)
        if not current.is_dir() or not current.is_relative_to(resolved_root):
            raise ValueError(f"{label} escapes its configured root")
    return current


def resolve_confined(
    root: Path,
    value: str | os.PathLike[str] | Path,
    *,
    kind: PathKind = "any",
    must_exist: bool = True,
    allow_root: bool = True,
    create_parent: bool = False,
    label: str = "Path",
) -> Path:
    """Resolve a path below *root* without traversing symlink components."""
    resolved_root = resolve_root(root, label=f"{label} root")
    candidate = Path(os.fsdecode(value)).expanduser()
    if not candidate.is_absolute():
        candidate = resolved_root / candidate
    try:
        relative = candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{label} is outside its configured root: {candidate}") from exc
    if not allow_root and not relative.parts:
        raise ValueError(f"{label} cannot be the configured root")
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"{label} contains an unsafe component: {candidate}")

    if create_parent:
        mkdir_confined(
            resolved_root,
            relative.parts[:-1],
            label=f"{label} parent",
        )

    current = resolved_root
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} contains a symlink: {current}")
        if not os.path.lexists(current):
            continue
        resolved = current.resolve(strict=True)
        if resolved != current.absolute() or not resolved.is_relative_to(resolved_root):
            raise ValueError(f"{label} is redirected outside its configured root")
        if index < len(relative.parts) - 1 and not resolved.is_dir():
            raise ValueError(f"{label} parent is not a directory: {current}")
        current = resolved

    try:
        resolved = current.resolve(strict=must_exist)
    except (FileNotFoundError, OSError, RuntimeError) as exc:
        raise ValueError(f"{label} does not exist: {candidate}") from exc
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"{label} is outside its configured root: {candidate}")
    if must_exist:
        if kind == "file" and not resolved.is_file():
            raise ValueError(f"{label} is not a regular file: {candidate}")
        if kind == "directory" and not resolved.is_dir():
            raise ValueError(f"{label} is not a directory: {candidate}")
    return resolved
