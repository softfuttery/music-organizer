"""Persistence boundary for organizer state and run history."""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, Protocol

from .database import schema_upgrade
from .models import RunResult


class OrganizerRepository(Protocol):
    def initialize(self) -> None: ...
    def recover_interrupted_work(self) -> None: ...
    def is_processed(self, source_path: Path) -> bool: ...
    def processed_sources(self) -> set[str]: ...
    def record_file(
        self,
        source_path: Path,
        target_path: Path,
        mode: str,
        status: str,
        message: str = "",
    ) -> None: ...
    def record_files(
        self,
        records: list[tuple[Path, Path, str, str, str]],
    ) -> None: ...
    def create_run(self) -> int: ...
    def update_run_progress(self, run_id: int, result: RunResult) -> None: ...
    def finish_run(self, run_id: int, result: RunResult) -> None: ...
    def app_state_value(self, key: str, default: str = "") -> str: ...
    def set_app_state_value(self, key: str, value: str) -> None: ...
    def schedule_state(self) -> dict[str, str]: ...
    def configure_schedule(self, cron: str, next_run_time: str) -> bool: ...
    def disable_schedule(self) -> bool: ...
    def advance_schedule_and_enqueue(
        self,
        expected_cron: str,
        expected_next_run_time: str,
        following_next_run_time: str,
    ) -> tuple[bool, bool, dict[str, Any] | None]: ...
    def seen_qb_hashes(self) -> set[str]: ...
    def delayed_qb_hashes(self) -> set[str]: ...
    def record_qb_torrents(
        self,
        torrents: list[dict[str, Any]],
        status: str,
        message: str = "",
    ) -> None: ...
    def record_qb_failures(
        self,
        torrents: list[dict[str, Any]],
        message: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> dict[str, str]: ...
    def reset_qb_torrent_retry(self, torrent_hash: str) -> bool: ...
    def dashboard_snapshot(self) -> dict[str, Any]: ...
    def dashboard_runtime_snapshot(self) -> dict[str, Any]: ...
    def history(self, page: int, per_page: int, query: str) -> dict[str, Any]: ...
    def enqueue_job(self, job_type: str) -> tuple[bool, dict[str, Any]]: ...
    def claim_next_job(self) -> dict[str, Any] | None: ...
    def complete_job(self, job_id: int, result: RunResult) -> None: ...
    def fail_job(self, job_id: int, message: str) -> None: ...
    def request_cancel_active_job(self) -> tuple[bool, dict[str, Any]]: ...
    def job_cancel_requested(self, job_id: int) -> bool: ...
    def job_snapshot(self) -> dict[str, Any]: ...


class SQLiteOrganizerRepository:
    """SQLite implementation used by the web service and scheduler."""

    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.database_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA cache_size = -20000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with (
            schema_upgrade(
                self.database_path,
                "organizer",
                1,
                ("organized_files", "runs", "qb_torrents", "jobs"),
            ),
            self._connection() as conn,
        ):
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS organized_files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL UNIQUE,
                    target_path TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    organized INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS qb_torrents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    torrent_hash TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    save_path TEXT,
                    content_path TEXT,
                    state TEXT,
                    progress REAL,
                    completion_on INTEGER,
                    status TEXT NOT NULL,
                    message TEXT,
                    created_at TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TEXT NOT NULL DEFAULT '',
                    last_attempt_at TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            qb_columns = {
                str(row["name"])
                for row in conn.execute("PRAGMA table_info(qb_torrents)").fetchall()
            }
            qb_migrations = {
                "attempt_count": "INTEGER NOT NULL DEFAULT 0",
                "next_retry_at": "TEXT NOT NULL DEFAULT ''",
                "last_attempt_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in qb_migrations.items():
                if name not in qb_columns:
                    conn.execute(
                        f"ALTER TABLE qb_torrents ADD COLUMN {name} {declaration}"
                    )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS qb_torrents_retry "
                "ON qb_torrents(status, next_retry_at)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    queued_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    scanned INTEGER NOT NULL DEFAULT 0,
                    organized INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    message TEXT NOT NULL DEFAULT ''
                )
                """
            )
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_active
                ON jobs ((1))
                WHERE status IN ('queued', 'running')
                """
            )

    def recover_interrupted_work(self) -> None:
        """Recover work only when the single worker process starts."""
        now = self._now()
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'cancelled', finished_at = ?,
                    message = 'cancelled during worker restart'
                WHERE status = 'running' AND cancel_requested = 1
                """,
                (now,),
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = 'queued', started_at = NULL,
                    message = 'requeued after worker restart'
                WHERE status = 'running' AND cancel_requested = 0
                """
            )
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, message = ?
                WHERE finished_at IS NULL
                """,
                (now, "interrupted before worker restart"),
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def is_processed(self, source_path: Path) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                """
                SELECT 1 FROM organized_files
                WHERE source_path = ? AND status = 'success'
                LIMIT 1
                """,
                (str(source_path),),
            ).fetchone()
        return row is not None

    def processed_sources(self) -> set[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT source_path FROM organized_files WHERE status = 'success'"
            ).fetchall()
        return {str(row["source_path"]) for row in rows}

    def record_file(
        self,
        source_path: Path,
        target_path: Path,
        mode: str,
        status: str,
        message: str = "",
    ) -> None:
        self.record_files([(source_path, target_path, mode, status, message)])

    def record_files(
        self,
        records: list[tuple[Path, Path, str, str, str]],
    ) -> None:
        if not records:
            return
        now = self._now()
        with self._connection() as conn:
            conn.executemany(
                """
                INSERT INTO organized_files
                    (source_path, target_path, mode, status, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    target_path = excluded.target_path,
                    mode = excluded.mode,
                    status = excluded.status,
                    message = excluded.message
                """,
                [
                    (
                        str(source_path),
                        str(target_path),
                        mode,
                        status,
                        message,
                        now,
                    )
                    for source_path, target_path, mode, status, message in records
                ],
            )

    def create_run(self) -> int:
        with self._connection() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at, message) VALUES (?, ?)",
                (self._now(), "running"),
            )
            return int(cursor.lastrowid)

    def update_run_progress(self, run_id: int, result: RunResult) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET scanned = ?, organized = ?, skipped = ?, failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    result.scanned,
                    result.organized,
                    result.skipped,
                    result.failed,
                    "running",
                    run_id,
                ),
            )

    def finish_run(self, run_id: int, result: RunResult) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE runs
                SET finished_at = ?, scanned = ?, organized = ?, skipped = ?,
                    failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    self._now(),
                    result.scanned,
                    result.organized,
                    result.skipped,
                    result.failed,
                    result.message,
                    run_id,
                ),
            )

    def app_state_value(self, key: str, default: str = "") -> str:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
        return str(row["value"]) if row else default

    def set_app_state_value(self, key: str, value: str) -> None:
        with self._connection() as conn:
            self._write_app_state(conn, key, value, self._now())

    @staticmethod
    def _write_app_state(
        conn: sqlite3.Connection, key: str, value: str, now: str
    ) -> None:
        conn.execute(
            """
            INSERT INTO app_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now),
        )

    def schedule_state(self) -> dict[str, str]:
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT key, value FROM app_state
                WHERE key IN ('schedule_cron', 'next_run_time')
                """
            ).fetchall()
        values = {str(row["key"]): str(row["value"]) for row in rows}
        return {
            "cron": values.get("schedule_cron", ""),
            "next_run_time": values.get("next_run_time", ""),
        }

    def configure_schedule(self, cron: str, next_run_time: str) -> bool:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT key, value FROM app_state
                WHERE key IN ('schedule_cron', 'next_run_time')
                """
            ).fetchall()
            values = {str(row["key"]): str(row["value"]) for row in rows}
            changed = (
                values.get("schedule_cron", "") != cron
                or values.get("next_run_time", "") != next_run_time
            )
            if changed:
                now = self._now()
                self._write_app_state(conn, "schedule_cron", cron, now)
                self._write_app_state(conn, "next_run_time", next_run_time, now)
        return changed

    def disable_schedule(self) -> bool:
        return self.configure_schedule("", "")

    def advance_schedule_and_enqueue(
        self,
        expected_cron: str,
        expected_next_run_time: str,
        following_next_run_time: str,
    ) -> tuple[bool, bool, dict[str, Any] | None]:
        """Atomically advance a due schedule and coalesce it into the job queue."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT key, value FROM app_state
                WHERE key IN ('schedule_cron', 'next_run_time')
                """
            ).fetchall()
            values = {str(row["key"]): str(row["value"]) for row in rows}
            if (
                values.get("schedule_cron", "") != expected_cron
                or values.get("next_run_time", "") != expected_next_run_time
            ):
                return False, False, None

            self._write_app_state(
                conn,
                "next_run_time",
                following_next_run_time,
                self._now(),
            )
            active = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1"
            ).fetchone()
            if active:
                return True, False, self._job_payload(active)
            cursor = conn.execute(
                """
                INSERT INTO jobs (job_type, status, queued_at, message)
                VALUES ('qb_poll', 'queued', ?, 'queued')
                """,
                (self._now(),),
            )
            row = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return True, True, self._job_payload(row)

    @staticmethod
    def _job_payload(row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {
                "id": None,
                "job_type": None,
                "status": "idle",
                "running": False,
                "started_at": None,
                "finished_at": None,
                "message": "idle",
                "result": None,
                "cancel_requested": False,
            }
        payload = dict(row)
        status = str(payload["status"])
        terminal = status not in {"queued", "running"}
        return {
            **payload,
            "running": not terminal,
            "cancel_requested": bool(payload["cancel_requested"]),
            "result": (
                {
                    "scanned": int(payload["scanned"]),
                    "organized": int(payload["organized"]),
                    "skipped": int(payload["skipped"]),
                    "failed": int(payload["failed"]),
                    "message": str(payload["message"]),
                }
                if terminal
                else None
            ),
        }

    def enqueue_job(self, job_type: str) -> tuple[bool, dict[str, Any]]:
        if job_type not in {"manual_scan", "qb_poll"}:
            raise ValueError(f"unsupported job type: {job_type}")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1"
            ).fetchone()
            if active:
                return False, self._job_payload(active)
            cursor = conn.execute(
                """
                INSERT INTO jobs (job_type, status, queued_at, message)
                VALUES (?, 'queued', ?, 'queued')
                """,
                (job_type, self._now()),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (cursor.lastrowid,)).fetchone()
        return True, self._job_payload(row)

    def claim_next_job(self) -> dict[str, Any] | None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            now = self._now()
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?, message = 'running'
                WHERE id = ? AND status = 'queued'
                """,
                (now, int(row["id"])),
            )
            claimed = conn.execute(
                "SELECT * FROM jobs WHERE id = ?", (int(row["id"]),)
            ).fetchone()
        return dict(claimed) if claimed else None

    def complete_job(self, job_id: int, result: RunResult) -> None:
        status = "cancelled" if result.message == "stopped by user" else "succeeded"
        if result.failed:
            status = "failed"
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, scanned = ?, organized = ?,
                    skipped = ?, failed = ?, message = ?
                WHERE id = ?
                """,
                (
                    status,
                    self._now(),
                    result.scanned,
                    result.organized,
                    result.skipped,
                    result.failed,
                    result.message,
                    job_id,
                ),
            )

    def fail_job(self, job_id: int, message: str) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = 'failed', finished_at = ?, failed = 1, message = ?
                WHERE id = ?
                """,
                (self._now(), message[:1000], job_id),
            )

    def request_cancel_active_job(self) -> tuple[bool, dict[str, Any]]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                latest = conn.execute(
                    "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
                ).fetchone()
                return False, self._job_payload(latest)
            job_id = int(row["id"])
            if row["status"] == "queued":
                conn.execute(
                    """
                    UPDATE jobs SET status = 'cancelled', finished_at = ?,
                        cancel_requested = 1, message = 'cancelled before start'
                    WHERE id = ?
                    """,
                    (self._now(), job_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE jobs SET cancel_requested = 1, message = 'stopping'
                    WHERE id = ?
                    """,
                    (job_id,),
                )
            updated = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return True, self._job_payload(updated)

    def job_cancel_requested(self, job_id: int) -> bool:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row and row["cancel_requested"])

    def job_snapshot(self) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return self._job_payload(row)

    def seen_qb_hashes(self) -> set[str]:
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT torrent_hash FROM qb_torrents "
                "WHERE status IN ('seen', 'needs_attention')"
            ).fetchall()
        return {str(row["torrent_hash"]).lower() for row in rows}

    def delayed_qb_hashes(self) -> set[str]:
        now = self._now()
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT torrent_hash FROM qb_torrents
                WHERE status = 'retrying' AND next_retry_at > ?
                """,
                (now,),
            ).fetchall()
        return {str(row["torrent_hash"]).lower() for row in rows}

    def record_qb_torrents(
        self,
        torrents: list[dict[str, Any]],
        status: str,
        message: str = "",
    ) -> None:
        if not torrents:
            return
        now = self._now()
        with self._connection() as conn:
            for torrent in torrents:
                torrent_hash = str(torrent.get("hash") or "").lower()
                if not torrent_hash:
                    continue
                conn.execute(
                    """
                    INSERT INTO qb_torrents
                        (torrent_hash, name, save_path, content_path, state, progress,
                         completion_on, status, message, created_at, attempt_count,
                         next_retry_at, last_attempt_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '', ?)
                    ON CONFLICT(torrent_hash) DO UPDATE SET
                        name = excluded.name,
                        save_path = excluded.save_path,
                        content_path = excluded.content_path,
                        state = excluded.state,
                        progress = excluded.progress,
                        completion_on = excluded.completion_on,
                        status = excluded.status,
                        message = excluded.message,
                        created_at = excluded.created_at,
                        attempt_count = 0,
                        next_retry_at = '',
                        last_attempt_at = excluded.last_attempt_at
                    """,
                    (
                        torrent_hash,
                        str(torrent.get("name") or ""),
                        str(torrent.get("save_path") or ""),
                        str(torrent.get("content_path") or ""),
                        str(torrent.get("state") or ""),
                        float(torrent.get("progress") or 0),
                        int(torrent.get("completion_on") or 0),
                        status,
                        message[:500],
                        now,
                        now,
                    ),
                )

    def record_qb_failures(
        self,
        torrents: list[dict[str, Any]],
        message: str,
        max_attempts: int,
        base_delay_seconds: int,
        max_delay_seconds: int,
    ) -> dict[str, str]:
        max_attempts = min(max(1, int(max_attempts)), 100)
        base_delay_seconds = max(1, int(base_delay_seconds))
        max_delay_seconds = max(base_delay_seconds, int(max_delay_seconds))
        now = datetime.now()
        now_value = now.isoformat(timespec="seconds")
        outcomes: dict[str, str] = {}
        with self._connection() as conn:
            for torrent in torrents:
                torrent_hash = str(torrent.get("hash") or "").lower()
                if not torrent_hash:
                    continue
                existing = conn.execute(
                    "SELECT attempt_count FROM qb_torrents WHERE torrent_hash = ?",
                    (torrent_hash,),
                ).fetchone()
                attempt_count = int(existing["attempt_count"] if existing else 0) + 1
                needs_attention = attempt_count >= max_attempts
                status = "needs_attention" if needs_attention else "retrying"
                delay = min(
                    max_delay_seconds,
                    base_delay_seconds * (2 ** (attempt_count - 1)),
                )
                next_retry_at = (
                    ""
                    if needs_attention
                    else (now + timedelta(seconds=delay)).isoformat(timespec="seconds")
                )
                conn.execute(
                    """
                    INSERT INTO qb_torrents
                        (torrent_hash, name, save_path, content_path, state, progress,
                         completion_on, status, message, created_at, attempt_count,
                         next_retry_at, last_attempt_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(torrent_hash) DO UPDATE SET
                        name = excluded.name,
                        save_path = excluded.save_path,
                        content_path = excluded.content_path,
                        state = excluded.state,
                        progress = excluded.progress,
                        completion_on = excluded.completion_on,
                        status = excluded.status,
                        message = excluded.message,
                        attempt_count = excluded.attempt_count,
                        next_retry_at = excluded.next_retry_at,
                        last_attempt_at = excluded.last_attempt_at
                    """,
                    (
                        torrent_hash,
                        str(torrent.get("name") or ""),
                        str(torrent.get("save_path") or ""),
                        str(torrent.get("content_path") or ""),
                        str(torrent.get("state") or ""),
                        float(torrent.get("progress") or 0),
                        int(torrent.get("completion_on") or 0),
                        status,
                        message[:500],
                        now_value,
                        attempt_count,
                        next_retry_at,
                        now_value,
                    ),
                )
                outcomes[torrent_hash] = status
        return outcomes

    def reset_qb_torrent_retry(self, torrent_hash: str) -> bool:
        value = str(torrent_hash or "").strip().lower()
        if not value:
            return False
        with self._connection() as conn:
            changed = conn.execute(
                """
                UPDATE qb_torrents
                SET status = 'retrying', attempt_count = 0,
                    next_retry_at = '', message = 'manual retry requested'
                WHERE torrent_hash = ? AND status = 'needs_attention'
                """,
                (value,),
            ).rowcount
        return changed == 1

    def dashboard_snapshot(self) -> dict[str, Any]:
        with self._connection() as conn:
            return self._dashboard_snapshot(conn)

    @staticmethod
    def _dashboard_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
        total_files = int(
            conn.execute("SELECT COUNT(*) AS count FROM organized_files").fetchone()[
                "count"
            ]
        )
        success_files = int(
            conn.execute(
                "SELECT COUNT(*) AS count FROM organized_files WHERE status = 'success'"
            ).fetchone()["count"]
        )
        last_run = conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        recent = conn.execute(
            "SELECT * FROM organized_files ORDER BY id DESC LIMIT 10"
        ).fetchall()
        recent_qb_torrents = conn.execute(
            "SELECT * FROM qb_torrents ORDER BY id DESC LIMIT 10"
        ).fetchall()
        qb_needs_attention = conn.execute(
            """
            SELECT * FROM qb_torrents
            WHERE status = 'needs_attention'
            ORDER BY id DESC LIMIT 20
            """
        ).fetchall()
        return {
            "total_files": total_files,
            "organized_files": success_files,
            "last_run": dict(last_run) if last_run else None,
            "recent": [dict(row) for row in recent],
            "recent_qb_torrents": [dict(row) for row in recent_qb_torrents],
            "qb_needs_attention": [dict(row) for row in qb_needs_attention],
        }

    def dashboard_runtime_snapshot(self) -> dict[str, Any]:
        """Read the complete dashboard state through one SQLite connection."""
        with self._connection() as conn:
            snapshot = self._dashboard_snapshot(conn)
            state = {
                str(row["key"]): str(row["value"])
                for row in conn.execute("SELECT key, value FROM app_state").fetchall()
            }
            job = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            review_counts = {"active": 0, "archived": 0}
            review_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'review_items'"
            ).fetchone()
            if review_table is not None:
                row = conn.execute(
                    """
                    SELECT
                        SUM(CASE WHEN archived_at = '' THEN 1 ELSE 0 END) AS active,
                        SUM(CASE WHEN archived_at <> '' THEN 1 ELSE 0 END) AS archived
                    FROM review_items
                    """
                ).fetchone()
                review_counts = {
                    "active": int(row["active"] or 0),
                    "archived": int(row["archived"] or 0),
                }
            snapshot["app_state"] = state
            snapshot["job_status"] = self._job_payload(job)
            snapshot["review_counts"] = review_counts
            return snapshot

    def history(self, page: int, per_page: int, query: str) -> dict[str, Any]:
        page = max(page, 1)
        per_page = max(per_page, 1)
        offset = (page - 1) * per_page
        where = ""
        params: list[Any] = []
        if query:
            where = (
                "WHERE source_path LIKE ? OR target_path LIKE ? "
                "OR message LIKE ? OR mode LIKE ?"
            )
            like = f"%{query}%"
            params.extend([like, like, like, like])
        records = """
            SELECT
                'file' AS record_type,
                id,
                created_at,
                mode,
                status,
                source_path,
                target_path,
                COALESCE(message, '') AS message
            FROM organized_files
            UNION ALL
            SELECT
                'job' AS record_type,
                id,
                COALESCE(finished_at, started_at, queued_at) AS created_at,
                job_type AS mode,
                status,
                '' AS source_path,
                '' AS target_path,
                message
            FROM jobs
            WHERE status IN ('failed', 'cancelled')
        """
        with self._connection() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS count FROM ({records}) {where}", params
                ).fetchone()["count"]
            )
            rows = conn.execute(
                f"""
                SELECT * FROM ({records})
                {where}
                ORDER BY created_at DESC, record_type DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, per_page, offset],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "page": page,
            "per_page": per_page,
            "total": total,
            "query": query,
            "has_prev": page > 1,
            "has_next": offset + per_page < total,
        }
