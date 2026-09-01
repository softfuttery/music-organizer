"""Persistent music review workflow primitives.

Review work uses a queue separate from organizer jobs so metadata lookups do
not block qBittorrent polling or the existing organizer worker.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Sequence

from .database import schema_upgrade
from .file_compare import stable_regular_files_equal
from .lyrics import normalize_lyric_decision
from .pathsafe import mkdir_confined, resolve_confined

AUDIO_EXTENSIONS = {
    ".aac", ".aiff", ".alac", ".ape", ".dff", ".dsf", ".flac",
    ".m4a", ".mp3", ".ogg", ".opus", ".tta", ".wav", ".wv",
}

AUTO_DISCOVERY_MAX_BATCH_ITEMS = 100
AUTO_DISCOVERY_MAX_PENDING_ITEMS = 100


class ActiveReviewOverlapError(ValueError):
    """Raised when a requested directory overlaps an active review item."""


def _review_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _paths_overlap(left: Path, right: Path) -> bool:
    return (
        left == right
        or left.is_relative_to(right)
        or right.is_relative_to(left)
    )


def _collapse_review_paths(paths: Sequence[Path]) -> list[Path]:
    """Deduplicate paths and keep only the outermost selected directories."""
    collapsed: list[Path] = []
    for raw_path in paths:
        path = _review_path(raw_path)
        if any(
            path == existing or path.is_relative_to(existing)
            for existing in collapsed
        ):
            continue
        collapsed = [
            existing for existing in collapsed if not existing.is_relative_to(path)
        ]
        collapsed.append(path)
    return collapsed


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def ensure_within_roots(path: str | Path, roots: Sequence[str | Path]) -> Path:
    """Resolve *path* and reject values outside configured review roots."""
    for root in roots:
        try:
            return resolve_confined(
                Path(root).expanduser(),
                Path(path).expanduser(),
                kind="directory",
                label="预审路径",
            )
        except ValueError:
            continue
    raise ValueError(f"预审路径不在允许范围内: {path}")


def _mkdir_confined(root: Path, parts: Sequence[str]) -> Path:
    """Create a directory path without following an existing symlink component."""
    try:
        return mkdir_confined(root, parts, label="隔离目录")
    except ValueError as exc:
        if "symlink" in str(exc):
            raise ValueError("隔离目录不能包含符号链接") from exc
        raise


def _confined_regular_file(root: Path, candidate: Path) -> Path | None:
    """Resolve a regular file below *root* without traversing symlinks."""
    try:
        return resolve_confined(root, candidate, kind="file", label="文件")
    except ValueError:
        if not os.path.lexists(candidate):
            return None
        raise ValueError("文件路径不能包含符号链接或超出允许范围")


FileIdentity = tuple[int, int, int, int, int, int]


def _identity_from_stat(metadata: os.stat_result) -> FileIdentity:
    file_type = stat.S_IFMT(metadata.st_mode)
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("文件不是普通文件")
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        file_type,
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _regular_file_identity(path: Path) -> FileIdentity:
    try:
        return _identity_from_stat(path.lstat())
    except ValueError as exc:
        raise ValueError(f"文件不是普通文件: {path}") from exc


def _same_confined_regular_file(
    root: Path,
    candidate: Path,
    expected: FileIdentity,
) -> Path | None:
    """Return the file only while its complete captured identity is unchanged."""
    safe = _confined_regular_file(root, candidate)
    if safe is None or _regular_file_identity(safe) != expected:
        return None
    return safe


def _matches_guard_identity(
    current: FileIdentity,
    expected: Sequence[int],
    *,
    allow_metadata_change: bool = False,
) -> bool:
    try:
        normalized = tuple(int(part) for part in expected)
    except (TypeError, ValueError):
        return False
    if len(normalized) != 5 or current[:3] != normalized[:3]:
        return False
    return allow_metadata_change or current[3:5] == normalized[3:5]


def _confined_output_file(root: Path, relative: Path) -> Path:
    """Return an output path whose existing components stay below *root*."""
    try:
        output = resolve_confined(
            root,
            relative,
            must_exist=False,
            create_parent=True,
            label="输出文件",
        )
    except ValueError as exc:
        raise ValueError(
            "输出文件不能包含符号链接或超出入库目标范围"
        ) from exc
    if output.exists():
        return resolve_confined(root, output, kind="file", label="输出文件")
    return output


def audio_files(path: Path) -> list[Path]:
    if path.is_symlink():
        return []
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if not root.is_dir():
        return []

    files: list[Path] = []
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        safe_dirnames: list[str] = []
        for dirname in dirnames:
            directory = current / dirname
            if directory.is_symlink():
                continue
            try:
                resolved_directory = directory.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved_directory.is_dir() and resolved_directory.is_relative_to(root):
                safe_dirnames.append(dirname)
        dirnames[:] = safe_dirnames

        for filename in filenames:
            candidate = current / filename
            if candidate.suffix.lower() not in AUDIO_EXTENSIONS:
                continue
            if candidate.is_symlink():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_file() and resolved.is_relative_to(root):
                files.append(candidate)
    return sorted(files)


def auxiliary_files(path: Path) -> list[str]:
    """Return portable non-audio file paths without following symlinks."""
    if path.is_symlink():
        return []
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return []
    if not root.is_dir():
        return []

    files: list[str] = []
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        dirnames[:] = [
            name for name in dirnames if not (current / name).is_symlink()
        ]
        for filename in filenames:
            candidate = current / filename
            if (
                candidate.is_symlink()
                or candidate.suffix.lower() in AUDIO_EXTENSIONS
            ):
                continue
            files.append(candidate.relative_to(root).as_posix())
    return sorted(files)


def source_signature(path: Path, files: Sequence[Path] | None = None) -> str:
    """Return a cheap change detector without reading complete media files."""
    digest = hashlib.sha256()
    for candidate in files if files is not None else audio_files(path):
        stat = candidate.stat()
        digest.update(candidate.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b":")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def quarantine_files(
    source_path: str | Path,
    roots: Sequence[str | Path],
    relative_paths: Sequence[str],
    item_id: int,
    *,
    expected_source_identities: dict[str, Sequence[int]] | None = None,
) -> list[dict[str, str]]:
    """Move explicitly selected surplus files into a reversible hidden area."""
    source = ensure_within_roots(source_path, roots)
    allowed_roots = [
        Path(root).expanduser().resolve(strict=True)
        for root in roots
        if source == Path(root).expanduser().resolve(strict=True)
        or source.is_relative_to(Path(root).expanduser().resolve(strict=True))
    ]
    if not allowed_roots:
        raise ValueError("预审路径不在允许范围内")
    allowed_root = max(allowed_roots, key=lambda path: len(path.parts))
    source_relative = source.relative_to(allowed_root)
    quarantine_root = _mkdir_confined(
        allowed_root,
        (
            ".music-organizer-quarantine",
            f"review-{int(item_id)}",
            *source_relative.parts,
        ),
    )
    results: list[dict[str, str]] = []
    for raw_path in relative_paths:
        relative = PurePosixPath(str(raw_path))
        entry = {"source": relative.as_posix(), "status": "quarantined"}
        try:
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("不是安全的相对路径")
            original = source.joinpath(*relative.parts)
            destination_parent = _mkdir_confined(
                quarantine_root, relative.parts[:-1]
            )
            destination = destination_parent / relative.name
            if not original.exists() and destination.exists():
                if destination.is_symlink():
                    raise ValueError("隔离区目标不能是符号链接")
                resolved_destination = destination.resolve(strict=True)
                resolved_quarantine = quarantine_root.resolve(strict=True)
                if not resolved_destination.is_file() or not (
                    resolved_destination.is_relative_to(resolved_quarantine)
                ):
                    raise ValueError("隔离区中的既有目标无效")
                entry["destination"] = destination.relative_to(
                    allowed_root
                ).as_posix()
                results.append(entry)
                continue
            if original.is_symlink():
                raise ValueError("拒绝移动符号链接")
            resolved = original.resolve(strict=True)
            if not resolved.is_file() or not resolved.is_relative_to(source):
                raise ValueError("文件不在待审目录内")
            if expected_source_identities is not None:
                expected_identity = expected_source_identities.get(
                    relative.as_posix()
                )
                if expected_identity is None or not _matches_guard_identity(
                    _regular_file_identity(resolved),
                    expected_identity,
                ):
                    raise ValueError("隔离文件在入库期间发生变化，已保留")
            if destination.exists():
                suffix = 1
                while destination.with_name(
                    f"{destination.name}.{suffix}"
                ).exists():
                    suffix += 1
                destination = destination.with_name(f"{destination.name}.{suffix}")
            shutil.move(str(resolved), str(destination))
            entry["destination"] = destination.relative_to(allowed_root).as_posix()
        except Exception as exc:
            entry["status"] = "failed"
            entry["error"] = str(exc)
        results.append(entry)
    return results


def finalize_review_import(
    source_path: str | Path,
    roots: Sequence[str | Path],
    target_root: str | Path,
    destination_directory: str | Path,
    imported_tracks: Sequence[dict[str, str]],
    *,
    extra_file_patterns: Sequence[str] = (),
    extra_file_paths: Sequence[str] = (),
    flatten_extra_files: bool = False,
    move_extra_files: bool = False,
    cleanup_source_after_import: bool = False,
    expected_source_identities: dict[str, Sequence[int]] | None = None,
    allowed_source_metadata_changes: Sequence[str] = (),
) -> dict[str, Any]:
    """Move Picard-style auxiliary files and safely prune an imported source."""
    source = ensure_within_roots(source_path, roots)
    target = Path(target_root).expanduser().resolve(strict=True)
    destination = Path(destination_directory).expanduser().resolve(strict=True)
    if not destination.is_dir() or not (
        destination == target or destination.is_relative_to(target)
    ):
        raise ValueError(f"入库目标目录超出配置范围: {destination}")

    warnings: list[str] = []
    additional_files: list[dict[str, str]] = []
    allowed_metadata_changes = set(allowed_source_metadata_changes)
    imported_sources: list[tuple[Path, FileIdentity | None]] = []
    imported_source_names: set[str] = set()
    for entry in imported_tracks:
        relative = PurePosixPath(
            ReviewRepository._relative_decision_path(entry.get("source", ""))
        )
        source_file = source.joinpath(*relative.parts)
        imported_source_names.add(relative.as_posix())
        target_file = Path(str(entry.get("destination") or "")).resolve(strict=True)
        if not target_file.is_file() or not target_file.is_relative_to(target):
            raise ValueError(f"入库文件目标无效: {target_file}")
        safe_source = _confined_regular_file(source, source_file)
        safe_identity = (
            _regular_file_identity(safe_source)
            if safe_source is not None
            else None
        )
        if expected_source_identities is not None and safe_identity is not None:
            expected_identity = expected_source_identities.get(
                relative.as_posix()
            )
            if expected_identity is None or not _matches_guard_identity(
                safe_identity,
                expected_identity,
                allow_metadata_change=(
                    relative.as_posix() in allowed_metadata_changes
                ),
            ):
                warnings.append(
                    "入库音频在收尾开始前发生变化，当前文件已保留: "
                    + relative.as_posix()
                )
                safe_identity = None
        imported_sources.append(
            (
                source_file,
                safe_identity,
            )
        )

    patterns = [
        str(value).strip().casefold()
        for value in extra_file_patterns
        if str(value).strip()
    ]
    explicit_extra_files = {
        ReviewRepository._relative_decision_path(value)
        for value in extra_file_paths
    }
    if move_extra_files and (patterns or explicit_extra_files):
        candidates: list[tuple[Path, Path, FileIdentity]] = []
        for path in source.rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or path.relative_to(source).as_posix() in imported_source_names
            ):
                continue
            relative = path.relative_to(source)
            relative_name = relative.as_posix()
            if relative_name not in explicit_extra_files and not any(
                fnmatch.fnmatchcase(path.name.casefold(), pattern)
                for pattern in patterns
            ):
                continue
            safe_candidate = _confined_regular_file(source, path)
            if safe_candidate is None:
                continue
            candidate_identity = _regular_file_identity(safe_candidate)
            if expected_source_identities is not None:
                expected_identity = expected_source_identities.get(
                    relative.as_posix()
                )
                if expected_identity is None or not _matches_guard_identity(
                    candidate_identity,
                    expected_identity,
                ):
                    warnings.append(
                        "附加文件在收尾开始前发生变化，已保留: "
                        + relative.as_posix()
                    )
                    continue
            candidates.append(
                (
                    path,
                    relative,
                    candidate_identity,
                )
            )
        for original, relative, original_identity in candidates:
            safe_original = _same_confined_regular_file(
                source,
                original,
                original_identity,
            )
            if safe_original is None:
                warnings.append(
                    "附加文件在收尾期间发生变化，已保留: "
                    + relative.as_posix()
                )
                continue
            output_relative = Path(relative.name) if flatten_extra_files else relative
            output = _confined_output_file(destination, output_relative)
            if output.exists() and not stable_regular_files_equal(
                safe_original, output
            ):
                suffix = 1
                while True:
                    candidate = _confined_output_file(
                        destination,
                        relative.with_name(
                            f"{relative.stem}.{suffix}{relative.suffix}"
                        ),
                    )
                    if not candidate.exists():
                        output = candidate
                        break
                    suffix += 1
            if not output.exists():
                output = _confined_output_file(
                    destination, output.relative_to(destination)
                )
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{output.name}.part-",
                    dir=output.parent,
                )
                temporary = Path(temporary_name)
                source_changed_during_copy = False
                try:
                    with safe_original.open("rb") as source_handle:
                        opened_identity = _identity_from_stat(
                            os.fstat(source_handle.fileno())
                        )
                        # Windows may report timestamp fields from an open
                        # handle at a slightly different precision than lstat.
                        # Pin the opened object by inode/type/size here; the
                        # full path identity (including mtime/ctime) is checked
                        # again after copying and immediately before deletion.
                        if opened_identity[:4] != original_identity[:4]:
                            source_changed_during_copy = True
                        with os.fdopen(descriptor, "wb") as destination_handle:
                            descriptor = -1
                            if not source_changed_during_copy:
                                shutil.copyfileobj(
                                    source_handle,
                                    destination_handle,
                                )
                    if not source_changed_during_copy:
                        shutil.copystat(
                            safe_original,
                            temporary,
                            follow_symlinks=False,
                        )
                        source_changed_during_copy = (
                            _same_confined_regular_file(
                                source,
                                original,
                                original_identity,
                            )
                            is None
                        )
                    if source_changed_during_copy:
                        warnings.append(
                            "附加文件在复制期间发生变化，已保留: "
                            + relative.as_posix()
                        )
                        continue
                    output = _confined_output_file(
                        destination, output.relative_to(destination)
                    )
                    os.replace(temporary, output)
                finally:
                    if descriptor >= 0:
                        os.close(descriptor)
                    temporary.unlink(missing_ok=True)
            safe_original = _same_confined_regular_file(
                source,
                original,
                original_identity,
            )
            if safe_original is None:
                warnings.append(
                    "附加文件在删除前发生变化，当前文件已保留: "
                    + relative.as_posix()
                )
                continue
            safe_original.unlink()
            additional_files.append(
                {
                    "source": relative.as_posix(),
                    "destination": str(output),
                    "status": "moved",
                }
            )

    removed_source_files: list[str] = []
    if cleanup_source_after_import:
        for source_file, expected_identity in imported_sources:
            relative = source_file.relative_to(source).as_posix()
            if expected_identity is None:
                if source_file.exists() or source_file.is_symlink():
                    warnings.append(
                        "入库音频在收尾期间重新出现，已保留: " + relative
                    )
                continue
            safe_source_file = _same_confined_regular_file(
                source,
                source_file,
                expected_identity,
            )
            if safe_source_file is None:
                if source_file.exists() or source_file.is_symlink():
                    warnings.append(
                        "入库音频在删除前发生变化，当前文件已保留: "
                        + relative
                    )
                continue
            safe_source_file.unlink()
            removed_source_files.append(relative)

    removed_directories: list[str] = []
    if cleanup_source_after_import or additional_files:
        directories = [path for path in source.rglob("*") if path.is_dir()]
        for directory in sorted(directories, key=lambda value: len(value.parts), reverse=True):
            try:
                directory.rmdir()
                removed_directories.append(directory.relative_to(source).as_posix())
            except OSError:
                pass
        try:
            source.rmdir()
            source_removed = True
        except OSError:
            source_removed = False
    else:
        source_removed = False

    remaining_files = []
    if source.exists():
        remaining_files = [
            path.relative_to(source).as_posix()
            for path in source.rglob("*")
            if path.is_file()
        ]
        if cleanup_source_after_import and remaining_files:
            warnings.append(
                "源目录包含未匹配文件，已保留: " + ", ".join(remaining_files[:10])
            )
    return {
        "additional_files": additional_files,
        "removed_source_files": removed_source_files,
        "removed_directories": removed_directories,
        "source_removed": source_removed,
        "remaining_files": remaining_files,
        "warnings": warnings,
    }


class ReviewRepository:
    """SQLite-backed source of truth for review batches and worker claims."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA cache_size = -20000")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        with (
            schema_upgrade(
                self.database_path,
                "review",
                1,
                ("review_batches", "review_items", "review_queue"),
            ),
            self._connection() as conn,
        ):
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS review_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS review_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES review_batches(id)
                        ON DELETE CASCADE,
                    source_path TEXT NOT NULL,
                    source_signature TEXT NOT NULL DEFAULT '',
                    audio_count INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    current_artist TEXT NOT NULL DEFAULT '',
                    current_album TEXT NOT NULL DEFAULT '',
                    recommendation TEXT NOT NULL DEFAULT '',
                    candidates_json TEXT NOT NULL DEFAULT '[]',
                    selected_candidate_key TEXT NOT NULL DEFAULT '',
                    import_token TEXT NOT NULL DEFAULT '',
                    import_stage TEXT NOT NULL DEFAULT '',
                    import_guard_json TEXT NOT NULL DEFAULT '{}',
                    import_checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    import_started_at TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_id, source_path)
                );
                CREATE TABLE IF NOT EXISTS review_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES review_items(id)
                        ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    queued_at TEXT NOT NULL,
                    claimed_at TEXT,
                    finished_at TEXT,
                    error TEXT NOT NULL DEFAULT ''
                );
                CREATE UNIQUE INDEX IF NOT EXISTS review_queue_one_active_action
                ON review_queue(item_id, action)
                WHERE status IN ('queued', 'running');
                CREATE INDEX IF NOT EXISTS review_items_batch_status
                ON review_items(batch_id, status);
                CREATE INDEX IF NOT EXISTS review_queue_claim
                ON review_queue(status, action, id);
                """
            )
            # Web and review-worker are recreated as one release cohort. Hold
            # the write lock across migration introspection and ALTER TABLE so
            # two fresh containers cannot both try to add the same column.
            conn.execute("BEGIN IMMEDIATE")
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(review_queue)").fetchall()
            }
            if "payload_json" not in columns:
                conn.execute(
                    "ALTER TABLE review_queue "
                    "ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'"
                )
            item_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(review_items)").fetchall()
            }
            item_migrations = {
                "decision_json": "TEXT NOT NULL DEFAULT '{}'",
                "lyrics_json": "TEXT NOT NULL DEFAULT '{}'",
                "import_result_json": "TEXT NOT NULL DEFAULT '{}'",
                "archived_at": "TEXT NOT NULL DEFAULT ''",
                "import_token": "TEXT NOT NULL DEFAULT ''",
                "import_stage": "TEXT NOT NULL DEFAULT ''",
                "import_guard_json": "TEXT NOT NULL DEFAULT '{}'",
                "import_checkpoint_json": "TEXT NOT NULL DEFAULT '{}'",
                "import_started_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in item_migrations.items():
                if name not in item_columns:
                    conn.execute(
                        f"ALTER TABLE review_items ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS review_items_archive "
                "ON review_items(archived_at, batch_id, status)"
            )
            conn.execute(
                """
                UPDATE review_items
                SET archived_at = CASE
                        WHEN updated_at <> '' THEN updated_at
                        ELSE created_at
                    END
                WHERE archived_at = '' AND status IN ('done', 'skipped')
                """
            )

    @staticmethod
    def _item_payload(row: sqlite3.Row) -> dict[str, Any]:
        payload = dict(row)
        payload["candidates"] = json.loads(payload.pop("candidates_json") or "[]")
        payload["decision"] = json.loads(payload.pop("decision_json", "{}") or "{}")
        payload["lyrics"] = json.loads(payload.pop("lyrics_json", "{}") or "{}")
        payload["import_result"] = json.loads(
            payload.pop("import_result_json", "{}") or "{}"
        )
        payload["import_checkpoint"] = json.loads(
            payload.pop("import_checkpoint_json", "{}") or "{}"
        )
        payload["import_guard"] = json.loads(
            payload.pop("import_guard_json", "{}") or "{}"
        )
        payload["archived"] = bool(payload.get("archived_at"))
        return payload

    def recover_interrupted(self) -> None:
        now = utc_now()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE review_queue
                SET status = 'queued', claimed_at = NULL,
                    error = 'review worker restarted'
                WHERE status = 'running'
                """
            )
            conn.execute(
                """
                UPDATE review_items
                SET status = 'queued', updated_at = ?,
                    error = '识别进程重启，任务已重新排队'
                WHERE status = 'identifying'
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE review_items
                SET status = 'approved', updated_at = ?,
                    error = '入库进程重启，任务已重新排队'
                WHERE status = 'importing'
                """,
                (now,),
            )

    def create_batch(self, paths: Sequence[Path], label: str = "") -> dict[str, Any]:
        collapsed_paths = _collapse_review_paths(paths)
        if not collapsed_paths:
            raise ValueError("至少选择一个预审目录")
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active_paths = [
                _review_path(row["source_path"])
                for row in conn.execute(
                    "SELECT source_path FROM review_items WHERE archived_at = ''"
                ).fetchall()
            ]
            for path in collapsed_paths:
                conflict = next(
                    (
                        active_path
                        for active_path in active_paths
                        if _paths_overlap(path, active_path)
                    ),
                    None,
                )
                if conflict is not None:
                    raise ActiveReviewOverlapError(
                        f"目录已有活跃预审任务或与其范围重叠: {conflict}"
                    )
            cursor = conn.execute(
                """
                INSERT INTO review_batches (status, label, created_at, updated_at)
                VALUES ('queued', ?, ?, ?)
                """,
                (label.strip()[:200], now, now),
            )
            batch_id = int(cursor.lastrowid)
            for path in collapsed_paths:
                cursor = conn.execute(
                    """
                    INSERT INTO review_items
                        (batch_id, source_path, status, created_at, updated_at)
                    VALUES (?, ?, 'queued', ?, ?)
                    """,
                    (batch_id, str(path), now, now),
                )
                conn.execute(
                    """
                    INSERT INTO review_queue
                        (item_id, action, status, queued_at, payload_json)
                    VALUES (?, 'identify', 'queued', ?, '{}')
                    """,
                    (int(cursor.lastrowid), now),
                )
        return self.batch(batch_id)

    def create_discovered_batch(
        self,
        entries: Sequence[tuple[Path, str]],
        label: str = "自动发现新音乐",
        *,
        max_batch_items: int = AUTO_DISCOVERY_MAX_BATCH_ITEMS,
        max_pending_items: int = AUTO_DISCOVERY_MAX_PENDING_ITEMS,
    ) -> dict[str, Any] | None:
        """Atomically enqueue a bounded set of new or changed album directories."""
        if not entries:
            return None
        max_batch_items = max(1, int(max_batch_items))
        max_pending_items = max(1, int(max_pending_items))
        collapsed_entries: list[tuple[Path, str]] = []
        for raw_path, signature in entries:
            path = _review_path(raw_path)
            if any(
                path == existing or path.is_relative_to(existing)
                for existing, _ in collapsed_entries
            ):
                continue
            collapsed_entries = [
                (existing, existing_signature)
                for existing, existing_signature in collapsed_entries
                if not existing.is_relative_to(path)
            ]
            collapsed_entries.append((path, signature))
        now = utc_now()
        batch_id = 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending_count = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM review_queue
                    WHERE action = 'identify' AND status IN ('queued', 'running')
                    """
                ).fetchone()[0]
            )
            available_slots = min(
                max_batch_items,
                max_pending_items - pending_count,
            )
            if available_slots <= 0:
                return None
            eligible: list[tuple[Path, str]] = []
            active_paths = [
                _review_path(row["source_path"])
                for row in conn.execute(
                    "SELECT source_path FROM review_items WHERE archived_at = ''"
                ).fetchall()
            ]
            for path, signature in collapsed_entries:
                source_path = str(path)
                if any(_paths_overlap(path, active) for active in active_paths):
                    continue
                latest = conn.execute(
                    """
                    SELECT source_signature FROM review_items
                    WHERE source_path = ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (source_path,),
                ).fetchone()
                if latest is not None and latest["source_signature"] == signature:
                    continue
                eligible.append((path, signature))
                if len(eligible) >= available_slots:
                    break
            if not eligible:
                return None
            cursor = conn.execute(
                """
                INSERT INTO review_batches (status, label, created_at, updated_at)
                VALUES ('queued', ?, ?, ?)
                """,
                (label.strip()[:200], now, now),
            )
            batch_id = int(cursor.lastrowid)
            for path, signature in eligible:
                cursor = conn.execute(
                    """
                    INSERT INTO review_items
                        (batch_id, source_path, source_signature, status,
                         created_at, updated_at)
                    VALUES (?, ?, ?, 'queued', ?, ?)
                    """,
                    (batch_id, str(path), signature, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO review_queue
                        (item_id, action, status, queued_at, payload_json)
                    VALUES (?, 'identify', 'queued', ?, '{}')
                    """,
                    (int(cursor.lastrowid), now),
                )
        return self.batch(batch_id)

    @staticmethod
    def _scope_filter(scope: str, alias: str = "i") -> str:
        if scope == "active":
            return f"{alias}.archived_at = ''"
        if scope == "archived":
            return f"{alias}.archived_at <> ''"
        if scope == "all":
            return "1 = 1"
        raise ValueError("scope 必须是 active、archived 或 all")

    def scope_counts(self) -> dict[str, int]:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN archived_at = '' THEN 1 ELSE 0 END) AS active,
                    SUM(CASE WHEN archived_at <> '' THEN 1 ELSE 0 END) AS archived
                FROM review_items
                """
            ).fetchone()
        return {
            "active": int(row["active"] or 0),
            "archived": int(row["archived"] or 0),
        }

    @staticmethod
    def _search_filter(query: str, alias: str = "i") -> tuple[str, list[str]]:
        value = str(query or "").strip()[:200]
        if not value:
            return "1 = 1", []
        escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        fields = (
            f"{alias}.source_path",
            f"{alias}.current_artist",
            f"{alias}.current_album",
            f"{alias}.import_result_json",
        )
        return (
            "(" + " OR ".join(f"{field} LIKE ? ESCAPE '\\'" for field in fields) + ")",
            [pattern] * len(fields),
        )

    def batches(
        self,
        limit: int = 30,
        scope: str = "active",
        query: str = "",
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        scope_filter = self._scope_filter(scope)
        search_filter, search_params = self._search_filter(query)
        with self._connection() as conn:
            rows = conn.execute(
                f"""
                SELECT b.*,
                       COUNT(i.id) AS item_count,
                       SUM(CASE WHEN i.status = 'done' THEN 1 ELSE 0 END) AS done_count,
                       SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) AS failed_count,
                       SUM(CASE WHEN i.status IN ('needs_review', 'ready') THEN 1 ELSE 0 END)
                           AS review_count
                FROM review_batches b
                JOIN review_items i ON i.batch_id = b.id
                    AND {scope_filter} AND {search_filter}
                GROUP BY b.id
                ORDER BY b.id DESC
                LIMIT ? OFFSET ?
                """,
                (
                    *search_params,
                    max(1, min(limit, 100)),
                    max(0, int(offset)),
                ),
            ).fetchall()
        return [dict(row) for row in rows]

    def batch_count(self, scope: str = "active", query: str = "") -> int:
        scope_filter = self._scope_filter(scope)
        search_filter, search_params = self._search_filter(query)
        with self._connection() as conn:
            return int(
                conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT b.id)
                    FROM review_batches b
                    JOIN review_items i ON i.batch_id = b.id
                        AND {scope_filter} AND {search_filter}
                    """,
                    search_params,
                ).fetchone()[0]
            )

    def batch(
        self,
        batch_id: int,
        scope: str = "all",
        query: str = "",
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> dict[str, Any]:
        scope_filter = self._scope_filter(scope, alias="review_items")
        search_filter, search_params = self._search_filter(
            query, alias="review_items"
        )
        with self._connection() as conn:
            batch = conn.execute(
                "SELECT * FROM review_batches WHERE id = ?", (batch_id,)
            ).fetchone()
            if batch is None:
                raise KeyError(f"预审批次不存在: {batch_id}")
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM review_items "
                    f"WHERE batch_id = ? AND {scope_filter} AND {search_filter}",
                    (batch_id, *search_params),
                ).fetchone()[0]
            )
            effective_limit = None if limit is None else max(1, min(int(limit), 100))
            effective_offset = max(0, int(offset))
            if effective_limit is not None and total:
                effective_offset = min(
                    effective_offset,
                    ((total - 1) // effective_limit) * effective_limit,
                )
            pagination = ""
            pagination_params: tuple[int, ...] = ()
            if effective_limit is not None:
                pagination = " LIMIT ? OFFSET ?"
                pagination_params = (effective_limit, effective_offset)
            items = conn.execute(
                f"SELECT * FROM review_items "
                f"WHERE batch_id = ? AND {scope_filter} AND {search_filter} "
                f"ORDER BY id{pagination}",
                (batch_id, *search_params, *pagination_params),
            ).fetchall()
        payload = dict(batch)
        payload["scope"] = scope
        payload["items"] = [self._item_payload(row) for row in items]
        returned_limit = effective_limit if effective_limit is not None else max(total, 1)
        payload["pagination"] = {
            "total": total,
            "offset": effective_offset,
            "limit": returned_limit,
            "has_previous": effective_offset > 0,
            "has_next": effective_offset + len(items) < total,
        }
        return payload

    def delete_archived_item(self, item_id: int) -> dict[str, int | bool]:
        """Delete review history only; imported and source files are untouched."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT batch_id, archived_at FROM review_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if not row["archived_at"]:
                raise ValueError("只能删除已归档的预审记录")
            batch_id = int(row["batch_id"])
            conn.execute("DELETE FROM review_items WHERE id = ?", (item_id,))
            batch_deleted = conn.execute(
                "DELETE FROM review_batches WHERE id = ? "
                "AND NOT EXISTS (SELECT 1 FROM review_items WHERE batch_id = ?)",
                (batch_id, batch_id),
            ).rowcount == 1
        return {
            "item_id": item_id,
            "batch_id": batch_id,
            "batch_deleted": batch_deleted,
        }

    def claim_next(self, action: str = "identify") -> dict[str, Any] | None:
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT q.id AS queue_id, q.item_id, q.action, q.attempts,
                       q.payload_json,
                       i.batch_id, i.source_path, i.source_signature,
                       i.selected_candidate_key, i.candidates_json,
                       i.decision_json, i.lyrics_json,
                       i.import_token, i.import_stage,
                       i.import_guard_json, i.import_checkpoint_json,
                       i.import_started_at
                FROM review_queue q
                JOIN review_items i ON i.id = q.item_id
                WHERE q.status = 'queued' AND q.action = ?
                ORDER BY q.id
                LIMIT 1
                """,
                (action,),
            ).fetchone()
            if row is None:
                return None
            changed = conn.execute(
                """
                UPDATE review_queue
                SET status = 'running', claimed_at = ?, attempts = attempts + 1,
                    error = ''
                WHERE id = ? AND status = 'queued'
                """,
                (now, int(row["queue_id"])),
            ).rowcount
            if changed != 1:
                return None
            import_token = str(row["import_token"] or "")
            import_stage = str(row["import_stage"] or "")
            import_started_at = str(row["import_started_at"] or "")
            if action == "import":
                import_token = import_token or secrets.token_hex(16)
                if import_stage != "beets_committed":
                    import_stage = "beets_running"
                import_started_at = import_started_at or now
            conn.execute(
                """
                UPDATE review_items
                SET status = ?, import_token = ?, import_stage = ?,
                    import_started_at = ?, updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    "identifying" if action == "identify" else "importing",
                    import_token,
                    import_stage,
                    import_started_at,
                    now,
                    int(row["item_id"]),
                ),
            )
        payload = dict(row)
        payload.update(
            {
                "import_token": import_token,
                "import_stage": import_stage,
                "import_started_at": import_started_at,
            }
        )
        return payload

    def save_lyric_decision(
        self,
        item_id: int,
        local_path: str,
        decision: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist one manual lyric decision before the album is approved."""
        relative = self._relative_decision_path(local_path)
        normalized = normalize_lyric_decision(decision)
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, lyrics_json FROM review_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能修改歌词选择")
            lyrics = json.loads(row["lyrics_json"] or "{}")
            lyrics[relative] = normalized
            serialized = json.dumps(lyrics, ensure_ascii=False, sort_keys=True)
            if len(serialized.encode("utf-8")) > 8 * 1024 * 1024:
                raise ValueError("单个预审项目的歌词决定超过 8 MB 限制")
            conn.execute(
                "UPDATE review_items SET lyrics_json = ?, updated_at = ? WHERE id = ?",
                (serialized, now, item_id),
            )
        return self.item(item_id)

    @staticmethod
    def _relative_decision_path(value: Any) -> str:
        path = str(value or "").strip()
        parsed = PurePosixPath(path)
        if not path or parsed.is_absolute() or ".." in parsed.parts:
            raise ValueError(f"无效的相对文件路径: {path}")
        return parsed.as_posix()

    @classmethod
    def _validate_decision(
        cls,
        candidate: dict[str, Any],
        track_mapping: Sequence[dict[str, Any]] | None,
        quarantine_paths: Sequence[str] | None,
    ) -> dict[str, Any]:
        local_items = candidate.get("local_items") or [
            {
                "local_path": item.get("local_path"),
                "track_key": item.get("track_key") or item.get("track_id"),
            }
            for item in candidate.get("tracks", [])
        ] + [
            {"local_path": path, "track_key": ""}
            for path in candidate.get("extra_items", [])
        ]
        track_options = candidate.get("track_options") or [
            {
                "key": item.get("track_key") or item.get("key") or item.get("track_id"),
            }
            for item in candidate.get("tracks", []) + candidate.get("extra_tracks", [])
        ]
        allowed_local = {
            cls._relative_decision_path(item.get("local_path"))
            for item in local_items
        }
        allowed_tracks = {
            str(item.get("key") or item.get("track_key") or item.get("track_id") or "")
            for item in track_options
        }
        allowed_tracks.discard("")
        if track_mapping is None:
            track_mapping = [
                {
                    "local_path": item.get("local_path"),
                    "track_key": item.get("track_key") or item.get("track_id"),
                }
                for item in candidate.get("tracks", [])
            ]
        normalized_mapping = []
        used_local: set[str] = set()
        used_tracks: set[str] = set()
        for item in track_mapping:
            if not isinstance(item, dict):
                raise ValueError("曲目对应必须是对象列表")
            local_path = cls._relative_decision_path(item.get("local_path"))
            track_key = str(item.get("track_key") or item.get("track_id") or "").strip()
            if local_path not in allowed_local:
                raise ValueError(f"曲目对应包含未知文件: {local_path}")
            if track_key not in allowed_tracks:
                raise ValueError(f"曲目对应包含未知曲目: {track_key}")
            if local_path in used_local or track_key in used_tracks:
                raise ValueError("同一文件或 MusicBrainz 曲目不能重复对应")
            used_local.add(local_path)
            used_tracks.add(track_key)
            normalized_mapping.append(
                {"local_path": local_path, "track_key": track_key}
            )
        if not normalized_mapping:
            raise ValueError("至少需要保留一首已对应的曲目")

        allowed_cleanup = allowed_local | {
            cls._relative_decision_path(path)
            for path in candidate.get("auxiliary_files", [])
        }
        normalized_cleanup = []
        for value in quarantine_paths or []:
            path = cls._relative_decision_path(value)
            if path not in allowed_cleanup:
                raise ValueError(f"清理列表包含未知文件: {path}")
            if path in used_local:
                raise ValueError(f"已对应入库的文件不能移入隔离区: {path}")
            if path not in normalized_cleanup:
                normalized_cleanup.append(path)
        return {
            "track_mapping": normalized_mapping,
            "quarantine_paths": normalized_cleanup,
        }

    def approve(
        self,
        item_id: int,
        candidate_key: str,
        *,
        track_mapping: Sequence[dict[str, Any]] | None = None,
        quarantine_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM review_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能确认入库")
            candidates = json.loads(row["candidates_json"] or "[]")
            candidate = next(
                (item for item in candidates if item.get("key") == candidate_key),
                None,
            )
            if candidate is None:
                raise ValueError("选择的候选已失效，请重新识别")
            decision = self._validate_decision(
                candidate, track_mapping, quarantine_paths
            )
            decision["candidate_key"] = candidate_key
            decision_json = json.dumps(decision, ensure_ascii=False)
            same_attempt = (
                str(row["selected_candidate_key"] or "") == candidate_key
                and str(row["decision_json"] or "{}") == decision_json
            )
            conn.execute(
                """
                UPDATE review_items
                SET status = 'approved', selected_candidate_key = ?,
                    decision_json = ?,
                    import_token = CASE WHEN ? THEN import_token ELSE '' END,
                    import_stage = CASE WHEN ? THEN import_stage ELSE '' END,
                    import_guard_json = CASE
                        WHEN ? THEN import_guard_json ELSE '{}'
                    END,
                    import_checkpoint_json = CASE
                        WHEN ? THEN import_checkpoint_json ELSE '{}'
                    END,
                    import_started_at = CASE
                        WHEN ? THEN import_started_at ELSE ''
                    END,
                    updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    candidate_key,
                    decision_json,
                    same_attempt,
                    same_attempt,
                    same_attempt,
                    same_attempt,
                    same_attempt,
                    now,
                    item_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO review_queue (item_id, action, status, queued_at)
                VALUES (?, 'import', 'queued', ?)
                """,
                (item_id, now),
            )
            self._refresh_batch(conn, item_id, now)
        return self.item(item_id)

    def approve_manual(
        self,
        item_id: int,
        candidate: dict[str, Any],
        *,
        quarantine_paths: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Persist a validated filename-rule candidate and queue its import."""
        candidate_key = str(candidate.get("key") or "")
        if candidate.get("data_source") != "manual" or not candidate_key:
            raise ValueError("规则入库候选无效")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status, candidates_json FROM review_items WHERE id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能确认规则入库")
            candidates = [
                value
                for value in json.loads(row["candidates_json"] or "[]")
                if value.get("data_source") != "manual"
            ]
            candidates.append(candidate)
            conn.execute(
                "UPDATE review_items SET candidates_json = ? WHERE id = ?",
                (json.dumps(candidates, ensure_ascii=False), item_id),
            )
        return self.approve(
            item_id,
            candidate_key,
            track_mapping=[
                {
                    "local_path": track["local_path"],
                    "track_key": track["track_key"],
                }
                for track in candidate.get("tracks", [])
            ],
            quarantine_paths=quarantine_paths,
        )

    def reidentify(
        self,
        item_id: int,
        *,
        search_artist: str = "",
        search_album: str = "",
        release_id: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        payload = {
            "search_artist": search_artist.strip()[:300],
            "search_album": search_album.strip()[:300],
            "release_id": release_id.strip()[:200],
        }
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM review_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能重新识别")
            conn.execute(
                """
                UPDATE review_items
                SET status = 'queued', recommendation = '',
                    candidates_json = '[]', selected_candidate_key = '',
                    decision_json = '{}', import_result_json = '{}',
                    import_token = '', import_stage = '',
                    import_guard_json = '{}', import_checkpoint_json = '{}',
                    import_started_at = '',
                    updated_at = ?, error = ''
                WHERE id = ?
                """,
                (now, item_id),
            )
            conn.execute(
                """
                INSERT INTO review_queue
                    (item_id, action, status, queued_at, payload_json)
                VALUES (?, 'identify', 'queued', ?, ?)
                """,
                (item_id, now, json.dumps(payload, ensure_ascii=False)),
            )
            self._refresh_batch(conn, item_id, now)
        return self.item(item_id)

    def skip(self, item_id: int) -> dict[str, Any]:
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM review_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能跳过")
            conn.execute(
                """
                UPDATE review_items
                SET status = 'skipped', archived_at = ?,
                    import_result_json = ?, updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    now,
                    json.dumps(
                        {"outcome": "skipped", "message": "用户选择跳过，源文件未修改"},
                        ensure_ascii=False,
                    ),
                    now,
                    item_id,
                ),
            )
            self._refresh_batch(conn, item_id, now)
        return self.item(item_id)

    def record_source_recycled(
        self, item_id: int, destination: str | Path
    ) -> dict[str, Any]:
        """Archive a review item after moving its source album to a recycle area."""
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT status FROM review_items WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"预审项目不存在: {item_id}")
            if row["status"] not in ("ready", "needs_review", "failed"):
                raise ValueError("当前状态不能移动源目录")
            conn.execute(
                """
                UPDATE review_items
                SET status = 'skipped', archived_at = ?,
                    import_result_json = ?, updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    now,
                    json.dumps(
                        {
                            "outcome": "source_recycled",
                            "message": "用户选择不入库，源专辑目录已移入预审回收站",
                            "recycle_destination": str(destination),
                        },
                        ensure_ascii=False,
                    ),
                    now,
                    item_id,
                ),
            )
            self._refresh_batch(conn, item_id, now)
        return self.item(item_id)

    def item(self, item_id: int) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM review_items WHERE id = ?", (item_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"预审项目不存在: {item_id}")
        return self._item_payload(row)

    def checkpoint_import(
        self,
        queue_id: int,
        item_id: int,
        result: dict[str, Any],
    ) -> None:
        """Persist the beets commit before any source cleanup is attempted."""
        if not result.get("album_id") or not result.get("imported_tracks"):
            raise ValueError("beets 导入结果缺少发行版或已入库曲目")
        now = utc_now()
        serialized = json.dumps(dict(result), ensure_ascii=False)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """
                SELECT 1
                FROM review_queue q
                JOIN review_items i ON i.id = q.item_id
                WHERE q.id = ? AND q.item_id = ? AND q.action = 'import'
                  AND q.status = 'running' AND i.status = 'importing'
                """,
                (queue_id, item_id),
            ).fetchone()
            if active is None:
                raise ValueError("入库任务已不在运行状态，拒绝写入检查点")
            conn.execute(
                """
                UPDATE review_items
                SET import_stage = 'beets_committed',
                    import_checkpoint_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (serialized, now, item_id),
            )

    def checkpoint_import_guard(
        self,
        queue_id: int,
        item_id: int,
        guard: dict[str, Any],
    ) -> None:
        """Persist the immutable source identity guard before spawning beets."""
        root_identity = guard.get("root")
        entries = guard.get("entries")
        if (
            not isinstance(root_identity, list)
            or len(root_identity) != 2
            or not isinstance(entries, dict)
        ):
            raise ValueError("入库源保护快照不完整")
        payload = dict(guard)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        now = utc_now()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """
                SELECT i.import_guard_json
                FROM review_queue q
                JOIN review_items i ON i.id = q.item_id
                WHERE q.id = ? AND q.item_id = ? AND q.action = 'import'
                  AND q.status = 'running' AND i.status = 'importing'
                """,
                (queue_id, item_id),
            ).fetchone()
            if active is None:
                raise ValueError("入库任务已不在运行状态，拒绝写入源保护快照")
            existing = json.loads(active["import_guard_json"] or "{}")
            if existing and existing != payload:
                raise ValueError("入库源保护快照已存在且不一致")
            conn.execute(
                """
                UPDATE review_items
                SET import_guard_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (serialized, now, item_id),
            )

    def complete_import(
        self,
        queue_id: int,
        item_id: int,
        result: dict[str, Any] | None = None,
        source_signature_after_import: str = "",
    ) -> None:
        now = utc_now()
        payload = dict(result or {})
        payload.setdefault("outcome", "imported")
        payload.setdefault("completed_at", now)
        serialized_payload = json.dumps(payload, ensure_ascii=False)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                """
                SELECT 1
                FROM review_queue q
                JOIN review_items i ON i.id = q.item_id
                WHERE q.id = ? AND q.item_id = ? AND q.action = 'import'
                  AND q.status = 'running' AND i.status = 'importing'
                  AND i.import_stage = 'beets_committed'
                """,
                (queue_id, item_id),
            ).fetchone()
            if active is None:
                raise ValueError("入库结果尚未持久化检查点，拒绝完成任务")
            conn.execute(
                """
                UPDATE review_items
                SET status = 'done', archived_at = ?,
                    import_result_json = ?, import_stage = 'done',
                    import_checkpoint_json = ?,
                    source_signature = CASE
                        WHEN ? <> '' THEN ? ELSE source_signature
                    END,
                    updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    now,
                    serialized_payload,
                    serialized_payload,
                    source_signature_after_import,
                    source_signature_after_import,
                    now,
                    item_id,
                ),
            )
            conn.execute(
                """
                UPDATE review_queue
                SET status = 'done', finished_at = ?, error = ''
                WHERE id = ?
                """,
                (now, queue_id),
            )
            self._refresh_batch(conn, item_id, now)

    def complete_identification(
        self,
        queue_id: int,
        item_id: int,
        *,
        signature: str,
        audio_count: int,
        current_artist: str,
        current_album: str,
        recommendation: str,
        candidates: Sequence[dict[str, Any]],
    ) -> None:
        now = utc_now()
        status = "ready" if recommendation == "strong" and candidates else "needs_review"
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE review_items
                SET source_signature = ?, audio_count = ?, status = ?,
                    current_artist = ?, current_album = ?, recommendation = ?,
                    candidates_json = ?, selected_candidate_key = '',
                    decision_json = '{}', import_result_json = '{}',
                    import_token = '', import_stage = '',
                    import_guard_json = '{}', import_checkpoint_json = '{}',
                    import_started_at = '',
                    archived_at = '',
                    updated_at = ?, error = ''
                WHERE id = ?
                """,
                (
                    signature,
                    audio_count,
                    status,
                    current_artist,
                    current_album,
                    recommendation,
                    json.dumps(list(candidates), ensure_ascii=False),
                    now,
                    item_id,
                ),
            )
            conn.execute(
                """
                UPDATE review_queue
                SET status = 'done', finished_at = ?, error = ''
                WHERE id = ?
                """,
                (now, queue_id),
            )
            self._refresh_batch(conn, item_id, now)

    def fail(
        self,
        queue_id: int,
        item_id: int,
        error: str,
        *,
        max_attempts: int = 3,
    ) -> bool:
        now = utc_now()
        message = error.strip()[:2000]
        with self._connection() as conn:
            queue = conn.execute(
                "SELECT action, attempts FROM review_queue WHERE id = ?",
                (queue_id,),
            ).fetchone()
            if queue is None:
                return False
            attempts = int(queue["attempts"])
            if attempts < max(1, max_attempts):
                item_status = (
                    "queued" if str(queue["action"]) == "identify" else "approved"
                )
                conn.execute(
                    """
                    UPDATE review_items
                    SET status = ?, error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (item_status, message, now, item_id),
                )
                conn.execute(
                    """
                    UPDATE review_queue
                    SET status = 'queued', claimed_at = NULL, finished_at = NULL,
                        error = ?
                    WHERE id = ?
                    """,
                    (message, queue_id),
                )
                self._refresh_batch(conn, item_id, now)
                return True
            conn.execute(
                """
                UPDATE review_items
                SET status = 'failed', error = ?, updated_at = ?
                WHERE id = ?
                """,
                (message, now, item_id),
            )
            conn.execute(
                """
                UPDATE review_queue
                SET status = 'failed', finished_at = ?, error = ?
                WHERE id = ?
                """,
                (now, message, queue_id),
            )
            self._refresh_batch(conn, item_id, now)
            return False

    @staticmethod
    def _refresh_batch(conn: sqlite3.Connection, item_id: int, now: str) -> None:
        conn.execute(
            """
            UPDATE review_batches
            SET status = CASE
                    WHEN EXISTS (
                        SELECT 1 FROM review_items
                        WHERE batch_id = review_batches.id
                          AND status IN ('queued', 'identifying', 'approved', 'importing')
                    ) THEN 'running'
                    WHEN EXISTS (
                        SELECT 1 FROM review_items
                        WHERE batch_id = review_batches.id AND status = 'failed'
                    ) THEN 'needs_attention'
                    WHEN EXISTS (
                        SELECT 1 FROM review_items
                        WHERE batch_id = review_batches.id
                          AND status IN ('needs_review', 'ready')
                    ) THEN 'needs_review'
                    ELSE 'done'
                END,
                updated_at = ?
            WHERE id = (SELECT batch_id FROM review_items WHERE id = ?)
            """,
            (now, item_id),
        )
