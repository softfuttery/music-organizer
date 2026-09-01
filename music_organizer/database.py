"""SQLite integrity, backup and schema-version maintenance."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Sequence

from .locking import exclusive_file_lock


def _quick_check(connection: sqlite3.Connection) -> None:
    result = str(connection.execute("PRAGMA quick_check").fetchone()[0])
    if result != "ok":
        raise sqlite3.DatabaseError(f"SQLite integrity check failed: {result}")


def _backup_database(database_path: Path, component: str, target_version: int) -> Path:
    backup_root = database_path.parent / "backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = backup_root / (
        f"{database_path.name}.pre-{component}-v{target_version}-{timestamp}.sqlite3"
    )
    source = sqlite3.connect(database_path, timeout=30)
    destination = sqlite3.connect(backup_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    backups = sorted(
        backup_root.glob(f"{database_path.name}.pre-{component}-v*.sqlite3"),
        key=lambda value: value.stat().st_mtime_ns,
        reverse=True,
    )
    for expired in backups[5:]:
        expired.unlink(missing_ok=True)
    return backup_path


@contextmanager
def schema_upgrade(
    database_path: Path,
    component: str,
    target_version: int,
    managed_tables: Sequence[str],
) -> Iterator[None]:
    """Serialize schema work, verify the DB and back up existing component data."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database_path.with_name(f".{database_path.name}.schema.lock")
    with exclusive_file_lock(lock_path, timeout=30):
        connection = sqlite3.connect(database_path, timeout=30)
        try:
            _quick_check(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_versions (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT version FROM schema_versions WHERE component = ?",
                (component,),
            ).fetchone()
            current_version = int(row[0]) if row else 0
            existing_tables = {
                str(value[0])
                for value in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            connection.commit()
        finally:
            connection.close()

        if current_version < target_version and existing_tables.intersection(managed_tables):
            _backup_database(database_path, component, target_version)

        yield

        connection = sqlite3.connect(database_path, timeout=30)
        try:
            connection.execute(
                """
                INSERT INTO schema_versions(component, version, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(component) DO UPDATE SET
                    version = excluded.version,
                    updated_at = excluded.updated_at
                """,
                (
                    component,
                    target_version,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            connection.commit()
        finally:
            connection.close()
