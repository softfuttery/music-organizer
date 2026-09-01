"""Single background worker for scheduled and user-requested organizer jobs."""

from __future__ import annotations

import argparse
import os
import signal
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

from music_organizer.models import RunResult
from music_organizer.notifications import format_job_notification, send_magicpush
from music_organizer.repository import SQLiteOrganizerRepository
from music_organizer.runtime import runtime_is_ready
from organizer import MusicOrganizer

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "/app/config/config.yaml"))
DATABASE_PATH = Path(os.environ.get("DATABASE_PATH", "/app/data/organizer.sqlite3"))
LOG_PATH = Path(os.environ.get("LOG_PATH", "/app/data/organizer.log"))
POLL_SECONDS = max(float(os.environ.get("WORKER_POLL_SECONDS", "1")), 0.2)
HEARTBEAT_MAX_AGE_SECONDS = max(
    int(os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS", "30")), 5
)
JOB_HEARTBEAT_SECONDS = max(1.0, min(HEARTBEAT_MAX_AGE_SECONDS / 3, 10.0))
HEARTBEAT_WRITE_SECONDS = JOB_HEARTBEAT_SECONDS


class OrganizerWorker:
    def __init__(self) -> None:
        self.repository = SQLiteOrganizerRepository(DATABASE_PATH)
        self.repository.initialize()
        self.repository.recover_interrupted_work()
        self.shutdown_requested = threading.Event()
        self._next_heartbeat_at = 0.0
        self.timezone = ZoneInfo(os.environ.get("TZ", "Asia/Hong_Kong"))
        self.organizer = MusicOrganizer(
            str(CONFIG_PATH),
            str(DATABASE_PATH),
            str(LOG_PATH),
            repository=self.repository,
            cancel_check=self.shutdown_requested.is_set,
        )
    def heartbeat(self) -> None:
        now = time.monotonic()
        if now < self._next_heartbeat_at:
            return
        self.repository.set_app_state_value(
            "worker_heartbeat", datetime.now().isoformat(timespec="seconds")
        )
        self._next_heartbeat_at = now + HEARTBEAT_WRITE_SECONDS

    def _safe_heartbeat(self) -> None:
        try:
            self.heartbeat()
        except Exception as exc:
            self.organizer.logger.warning("Worker heartbeat update failed: %s", exc)

    def _heartbeat_during_job(self, stopped: threading.Event) -> None:
        while not stopped.wait(JOB_HEARTBEAT_SECONDS):
            self._safe_heartbeat()

    def _trigger(self, cron_expr: str) -> CronTrigger:
        return CronTrigger.from_crontab(cron_expr, timezone=self.timezone)

    def _parse_next_run(self, value: str) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self.timezone)
        return parsed.astimezone(self.timezone)

    def configure_schedule(self) -> None:
        config = self.organizer.load_config()
        schedule = config.get("schedule", {})
        if not schedule.get("enabled", True):
            if self.repository.disable_schedule():
                self.organizer.logger.info("Worker schedule disabled by config")
            return

        cron_expr = str(schedule.get("cron") or "*/30 * * * *")
        trigger = self._trigger(cron_expr)
        state = self.repository.schedule_state()
        next_run = None
        if state["cron"] == cron_expr:
            next_run = self._parse_next_run(state["next_run_time"])
        if next_run is None:
            next_run = trigger.get_next_fire_time(None, datetime.now(self.timezone))
        next_value = next_run.isoformat() if next_run else ""
        if self.repository.configure_schedule(cron_expr, next_value):
            self.organizer.logger.info(
                "Worker schedule persisted; cron=%s next=%s",
                cron_expr,
                next_value,
            )

    def enqueue_due_schedule(self) -> None:
        state = self.repository.schedule_state()
        cron_expr = state["cron"]
        expected_next = state["next_run_time"]
        next_run = self._parse_next_run(expected_next)
        if not cron_expr or next_run is None:
            return

        now = datetime.now(self.timezone)
        if next_run > now:
            return
        following = self._trigger(cron_expr).get_next_fire_time(next_run, now)
        following_value = following.isoformat() if following else ""
        advanced, created, job = self.repository.advance_schedule_and_enqueue(
            cron_expr,
            expected_next,
            following_value,
        )
        if not advanced:
            return
        if created and job:
            self.organizer.logger.info(
                "Scheduled qBittorrent poll queued as job %s", job["id"]
            )
        elif job:
            self.organizer.logger.info(
                "Scheduled poll coalesced; job %s is already active", job["id"]
            )

    def _job_organizer(self, job_id: int) -> MusicOrganizer:
        return MusicOrganizer(
            str(CONFIG_PATH),
            str(DATABASE_PATH),
            str(LOG_PATH),
            repository=self.repository,
            cancel_check=lambda: (
                self.shutdown_requested.is_set()
                or self.repository.job_cancel_requested(job_id)
            ),
        )

    def execute_job(self, job: dict) -> None:
        job_id = int(job["id"])
        organizer = self._job_organizer(job_id)
        heartbeat_stopped = threading.Event()
        self._safe_heartbeat()
        heartbeat_thread = threading.Thread(
            target=self._heartbeat_during_job,
            args=(heartbeat_stopped,),
            name=f"organizer-job-{job_id}-heartbeat",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            if job["job_type"] == "manual_scan":
                result = organizer.scan_and_organize()
            elif job["job_type"] == "qb_poll":
                result = organizer.scan_completed_qb_torrents()
            else:
                raise ValueError(f"unsupported job type: {job['job_type']}")
            self.repository.complete_job(job_id, result)
            self.notify_job(job, result)
        except Exception as exc:
            organizer.logger.exception("Worker job %s failed: %s", job_id, exc)
            self.repository.fail_job(job_id, str(exc))
            self.notify_job(job, RunResult(failed=1, message=str(exc)))
        finally:
            heartbeat_stopped.set()
            heartbeat_thread.join(timeout=JOB_HEARTBEAT_SECONDS + 1)
            self._safe_heartbeat()

    def notify_job(self, job: dict, result: RunResult) -> None:
        try:
            config = self.organizer.load_config()
            magicpush = config.get("notifications", {}).get("magicpush", {})
            if not magicpush.get("enabled"):
                return
            if (
                job.get("job_type") == "qb_poll"
                and not magicpush.get("notify_no_changes", False)
                and result.failed == 0
                and result.organized == 0
                and not result.details.get("torrent_names")
            ):
                return
            title, content = format_job_notification(
                job,
                result,
                title_prefix=str(magicpush.get("title") or "Music Organizer"),
            )
            outcome = send_magicpush(magicpush, content, title=title)
            now = datetime.now().isoformat(timespec="seconds")
            self.repository.set_app_state_value("magicpush_last_at", now)
            if outcome and outcome.get("sent"):
                self.repository.set_app_state_value("magicpush_last_status", "sent")
                self.repository.set_app_state_value("magicpush_last_error", "")
                self.organizer.logger.info(
                    "MagicPush notification sent for job %s", job.get("id")
                )
            else:
                error = str((outcome or {}).get("error") or "MagicPush 未发送")
                self.repository.set_app_state_value("magicpush_last_status", "failed")
                self.repository.set_app_state_value("magicpush_last_error", error[:500])
                self.organizer.logger.warning(
                    "MagicPush notification failed for job %s: %s",
                    job.get("id"),
                    error,
                )
        except Exception as exc:
            self.organizer.logger.warning(
                "MagicPush notification crashed for job %s: %s",
                job.get("id"),
                exc,
            )

    def request_shutdown(self, *_args) -> None:
        self.shutdown_requested.set()

    def run_once(self) -> bool:
        self.heartbeat()
        self.configure_schedule()
        self.enqueue_due_schedule()
        job = self.repository.claim_next_job()
        if not job:
            return False
        self.execute_job(job)
        self.heartbeat()
        return True

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)
        self.configure_schedule()
        self.organizer.logger.info("Organizer worker started")
        try:
            while not self.shutdown_requested.is_set():
                self.heartbeat()
                try:
                    self.configure_schedule()
                    self.enqueue_due_schedule()
                except Exception as exc:
                    self.organizer.logger.exception("Schedule evaluation failed: %s", exc)
                job = self.repository.claim_next_job()
                if job:
                    self.execute_job(job)
                    continue
                self.shutdown_requested.wait(POLL_SECONDS)
        finally:
            self.organizer.logger.info("Organizer worker stopped")


def heartbeat_is_fresh(repository: SQLiteOrganizerRepository) -> bool:
    value = repository.app_state_value("worker_heartbeat", "")
    if not value:
        return False
    try:
        age = (datetime.now() - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return False
    return 0 <= age <= HEARTBEAT_MAX_AGE_SECONDS


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args()
    if args.health:
        repository = SQLiteOrganizerRepository(DATABASE_PATH)
        repository.initialize()
        healthy = heartbeat_is_fresh(repository) and runtime_is_ready(
            CONFIG_PATH, DATABASE_PATH
        )
        return 0 if healthy else 1
    worker = OrganizerWorker()
    if args.once:
        worker.run_once()
    else:
        worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
