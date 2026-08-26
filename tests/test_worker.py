import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import worker as worker_module
from music_organizer.repository import SQLiteOrganizerRepository


class WorkerIntegrationTests(unittest.TestCase):
    @staticmethod
    def _patch_worker_paths(environment: dict[str, str]):
        return mock.patch.multiple(
            worker_module,
            CONFIG_PATH=Path(environment["CONFIG_PATH"]),
            DATABASE_PATH=Path(environment["DATABASE_PATH"]),
            LOG_PATH=Path(environment["LOG_PATH"]),
        )

    def test_worker_does_not_run_legacy_automatic_beets_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            generated_path = root / "generated-beets.yaml"
            config_path.write_text(
                "\n".join(
                    (
                        "paths_mapping: {}",
                        "schedule:",
                        "  enabled: false",
                        "beets:",
                        "  enabled: true",
                        f"  config_path: '{generated_path.as_posix()}'",
                        "",
                    )
                ),
                encoding="utf-8",
            )
            environment = {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(root / "organizer.sqlite3"),
                "LOG_PATH": str(root / "organizer.log"),
            }
            with (
                mock.patch.dict(os.environ, environment),
                self._patch_worker_paths(environment),
            ):
                worker = worker_module.OrganizerWorker()

                self.assertFalse(generated_path.exists())

                for handler in list(worker.organizer.logger.handlers):
                    worker.organizer.logger.removeHandler(handler)
                    handler.close()

    def test_worker_claims_and_completes_persisted_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "paths_mapping: {}\nschedule:\n  enabled: false\n",
                encoding="utf-8",
            )
            environment = {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(root / "organizer.sqlite3"),
                "LOG_PATH": str(root / "organizer.log"),
            }
            with (
                mock.patch.dict(os.environ, environment),
                self._patch_worker_paths(environment),
            ):
                repository = SQLiteOrganizerRepository(Path(environment["DATABASE_PATH"]))
                repository.initialize()
                created, _ = repository.enqueue_job("manual_scan")
                self.assertTrue(created)

                worker = worker_module.OrganizerWorker()
                self.assertFalse(hasattr(worker, "scheduler"))
                self.assertTrue(worker.run_once())
                self.assertEqual(repository.job_snapshot()["status"], "succeeded")

                for handler in list(worker.organizer.logger.handlers):
                    worker.organizer.logger.removeHandler(handler)
                    handler.close()

    def test_worker_keeps_heartbeat_fresh_during_long_job(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "paths_mapping: {}\nschedule:\n  enabled: false\n",
                encoding="utf-8",
            )
            environment = {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(root / "organizer.sqlite3"),
                "LOG_PATH": str(root / "organizer.log"),
            }
            with (
                mock.patch.dict(os.environ, environment),
                self._patch_worker_paths(environment),
                mock.patch.object(worker_module, "JOB_HEARTBEAT_SECONDS", 0.01),
            ):
                worker = worker_module.OrganizerWorker()
                worker.repository.enqueue_job("manual_scan")
                claimed = worker.repository.claim_next_job()
                background_heartbeat = threading.Event()
                heartbeat_calls = 0

                def record_heartbeat():
                    nonlocal heartbeat_calls
                    heartbeat_calls += 1
                    if heartbeat_calls >= 2:
                        background_heartbeat.set()

                job_organizer = mock.Mock()

                def run_long_job():
                    self.assertTrue(background_heartbeat.wait(0.5))
                    return worker_module.RunResult(message="ok")

                job_organizer.scan_and_organize.side_effect = run_long_job
                with (
                    mock.patch.object(worker, "heartbeat", side_effect=record_heartbeat),
                    mock.patch.object(worker, "_job_organizer", return_value=job_organizer),
                    mock.patch.object(worker, "notify_job"),
                ):
                    worker.execute_job(claimed)

                self.assertGreaterEqual(heartbeat_calls, 2)
                self.assertEqual(worker.repository.job_snapshot()["status"], "succeeded")

                for handler in list(worker.organizer.logger.handlers):
                    worker.organizer.logger.removeHandler(handler)
                    handler.close()

    def test_schedule_deadline_survives_worker_reconstruction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                "paths_mapping: {}\nschedule:\n  enabled: true\n  cron: '* * * * *'\n",
                encoding="utf-8",
            )
            environment = {
                "CONFIG_PATH": str(config_path),
                "DATABASE_PATH": str(root / "organizer.sqlite3"),
                "LOG_PATH": str(root / "organizer.log"),
                "TZ": "Asia/Hong_Kong",
            }
            with (
                mock.patch.dict(os.environ, environment),
                self._patch_worker_paths(environment),
            ):
                worker = worker_module.OrganizerWorker()
                worker.configure_schedule()
                repository = worker.repository
                persisted = repository.schedule_state()["next_run_time"]

                reconstructed = worker_module.OrganizerWorker()
                reconstructed.configure_schedule()
                self.assertEqual(
                    reconstructed.repository.schedule_state()["next_run_time"],
                    persisted,
                )

                past = datetime.now(ZoneInfo("Asia/Hong_Kong")) - timedelta(minutes=1)
                repository.configure_schedule("* * * * *", past.isoformat())
                reconstructed.enqueue_due_schedule()
                self.assertEqual(repository.job_snapshot()["job_type"], "qb_poll")
                self.assertNotEqual(
                    repository.schedule_state()["next_run_time"],
                    past.isoformat(),
                )

                for organizer in (worker.organizer, reconstructed.organizer):
                    for handler in list(organizer.logger.handlers):
                        organizer.logger.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()
