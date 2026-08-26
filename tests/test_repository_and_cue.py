import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_organizer.cue import (
    CueProcessor,
    CueSplitOptions,
    cue_output_path,
    resolve_cue_audio,
)
from music_organizer.models import CueSheet, CueTrack, RunResult
from music_organizer.repository import SQLiteOrganizerRepository


class RepositoryTests(unittest.TestCase):
    def test_initialize_migrates_existing_qb_retry_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "organizer.sqlite3"
            conn = sqlite3.connect(database)
            try:
                conn.execute(
                    """
                    CREATE TABLE qb_torrents (
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
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            repository = SQLiteOrganizerRepository(database)
            repository.initialize()

            with repository._connection() as conn:
                columns = {
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(qb_torrents)")
                }
            self.assertTrue(
                {"attempt_count", "next_retry_at", "last_attempt_at"} <= columns
            )

    def test_file_run_and_dashboard_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "organizer.sqlite3"
            repository = SQLiteOrganizerRepository(database)
            repository.initialize()
            source = Path("/source/album.flac")
            target = Path("/target/album.flac")

            repository.record_file(source, target, "hardlink", "success", "created")
            repository.record_file(source, target, "hardlink", "success", "updated")
            run_id = repository.create_run()
            repository.update_run_progress(run_id, RunResult(scanned=3, organized=1))
            repository.finish_run(
                run_id,
                RunResult(scanned=3, organized=1, skipped=2, message="ok"),
            )

            self.assertTrue(repository.is_processed(source))
            self.assertEqual(repository.processed_sources(), {str(source)})
            snapshot = repository.dashboard_snapshot()
            self.assertEqual(snapshot["total_files"], 1)
            self.assertEqual(snapshot["last_run"]["message"], "ok")
            self.assertEqual(repository.history(1, 10, "album")["total"], 1)

            with repository._connection() as conn:
                self.assertEqual(
                    conn.execute("PRAGMA cache_size").fetchone()[0],
                    -20000,
                )

    def test_record_files_commits_a_batch_in_one_repository_call(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            repository.record_files(
                [
                    (
                        Path("/source/01.flac"),
                        Path("/target/01.flac"),
                        "copy",
                        "success",
                        "a",
                    ),
                    (
                        Path("/source/02.flac"),
                        Path("/target/02.flac"),
                        "copy",
                        "success",
                        "b",
                    ),
                ]
            )

            self.assertEqual(
                repository.processed_sources(),
                {str(Path("/source/01.flac")), str(Path("/source/02.flac"))},
            )

    def test_history_includes_job_level_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            repository.enqueue_job("manual_scan")
            claimed = repository.claim_next_job()
            repository.fail_job(int(claimed["id"]), "permission denied")

            history = repository.history(1, 10, "permission")

            self.assertEqual(history["total"], 1)
            self.assertEqual(history["items"][0]["record_type"], "job")
            self.assertEqual(history["items"][0]["mode"], "manual_scan")
            self.assertEqual(history["items"][0]["status"], "failed")
            self.assertEqual(history["items"][0]["message"], "permission denied")

    def test_worker_recovery_marks_unfinished_run_as_interrupted(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            repository.create_run()

            repository.recover_interrupted_work()

            self.assertEqual(
                repository.dashboard_snapshot()["last_run"]["message"],
                "interrupted before worker restart",
            )

    def test_worker_recovery_requeues_persisted_running_job(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            repository.enqueue_job("manual_scan")
            claimed = repository.claim_next_job()
            self.assertEqual(claimed["status"], "running")

            repository.recover_interrupted_work()

            recovered = repository.job_snapshot()
            self.assertEqual(recovered["status"], "queued")
            self.assertEqual(recovered["message"], "requeued after worker restart")

    def test_persistent_job_queue_claim_cancel_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()

            created, queued = repository.enqueue_job("manual_scan")
            duplicate_created, duplicate = repository.enqueue_job("qb_poll")
            claimed = repository.claim_next_job()

            self.assertTrue(created)
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate["id"], queued["id"])
            self.assertEqual(claimed["status"], "running")
            cancelled, stopping = repository.request_cancel_active_job()
            self.assertTrue(cancelled)
            self.assertEqual(stopping["message"], "stopping")
            self.assertTrue(repository.job_cancel_requested(int(claimed["id"])))

            repository.complete_job(
                int(claimed["id"]), RunResult(scanned=3, skipped=3, message="stopped by user")
            )
            snapshot = repository.job_snapshot()
            self.assertEqual(snapshot["status"], "cancelled")
            self.assertFalse(snapshot["running"])

    def test_due_schedule_advance_and_enqueue_is_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            self.assertTrue(
                repository.configure_schedule(
                    "*/30 * * * *",
                    "2026-07-16T10:00:00+08:00",
                )
            )

            advanced, created, queued = repository.advance_schedule_and_enqueue(
                "*/30 * * * *",
                "2026-07-16T10:00:00+08:00",
                "2026-07-16T10:30:00+08:00",
            )
            stale_advanced, stale_created, stale_job = (
                repository.advance_schedule_and_enqueue(
                    "*/30 * * * *",
                    "2026-07-16T10:00:00+08:00",
                    "2026-07-16T10:30:00+08:00",
                )
            )

            self.assertTrue(advanced)
            self.assertTrue(created)
            self.assertEqual(queued["job_type"], "qb_poll")
            self.assertEqual(
                repository.schedule_state()["next_run_time"],
                "2026-07-16T10:30:00+08:00",
            )
            self.assertFalse(stale_advanced)
            self.assertFalse(stale_created)
            self.assertIsNone(stale_job)

    def test_qb_failures_back_off_then_require_attention_and_can_be_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteOrganizerRepository(Path(directory) / "organizer.sqlite3")
            repository.initialize()
            torrent = {"hash": "ABC", "name": "Album", "progress": 1}

            first = repository.record_qb_failures(
                [torrent],
                "target conflict",
                max_attempts=2,
                base_delay_seconds=60,
                max_delay_seconds=300,
            )
            self.assertEqual(first, {"abc": "retrying"})
            self.assertIn("abc", repository.delayed_qb_hashes())
            self.assertNotIn("abc", repository.seen_qb_hashes())

            second = repository.record_qb_failures(
                [torrent],
                "target conflict",
                max_attempts=2,
                base_delay_seconds=60,
                max_delay_seconds=300,
            )
            self.assertEqual(second, {"abc": "needs_attention"})
            self.assertIn("abc", repository.seen_qb_hashes())
            self.assertEqual(
                repository.dashboard_snapshot()["qb_needs_attention"][0]["torrent_hash"],
                "abc",
            )
            self.assertTrue(repository.reset_qb_torrent_retry("ABC"))
            self.assertNotIn("abc", repository.seen_qb_hashes())
            self.assertNotIn("abc", repository.delayed_qb_hashes())


class CuePathSafetyTests(unittest.TestCase):
    def test_resolve_cue_audio_accepts_a_regular_file_beside_the_cue(self):
        with tempfile.TemporaryDirectory() as directory:
            album = Path(directory) / "album"
            album.mkdir()
            cue_path = album / "album.cue"
            cue_path.write_text("", encoding="utf-8")
            audio_path = album / "album.flac"
            audio_path.write_bytes(b"audio")

            self.assertEqual(
                resolve_cue_audio(cue_path, "album.flac"),
                audio_path.resolve(strict=True),
            )

    def test_resolve_cue_audio_rejects_parent_and_absolute_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album = root / "album"
            album.mkdir()
            cue_path = album / "album.cue"
            cue_path.write_text("", encoding="utf-8")
            outside = root / "outside.flac"
            outside.write_bytes(b"outside")

            self.assertIsNone(resolve_cue_audio(cue_path, "../outside.flac"))
            self.assertIsNone(resolve_cue_audio(cue_path, str(outside.resolve())))

    def test_resolve_cue_audio_rejects_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            album = root / "album"
            outside_dir = root / "outside"
            album.mkdir()
            outside_dir.mkdir()
            cue_path = album / "album.cue"
            cue_path.write_text("", encoding="utf-8")
            outside_audio = outside_dir / "outside.flac"
            outside_audio.write_bytes(b"outside")
            inside_audio = album / "inside.flac"
            inside_audio.write_bytes(b"inside")
            linked_file = album / "linked.flac"
            linked_inside = album / "linked-inside.flac"
            linked_dir = album / "linked-dir"
            try:
                linked_file.symlink_to(outside_audio)
                linked_inside.symlink_to(inside_audio)
                linked_dir.symlink_to(outside_dir, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            self.assertIsNone(resolve_cue_audio(cue_path, "linked.flac"))
            self.assertIsNone(resolve_cue_audio(cue_path, "linked-inside.flac"))
            self.assertIsNone(
                resolve_cue_audio(cue_path, "linked-dir/outside.flac")
            )

    def test_cue_output_path_confines_output_subdir_to_target_album(self):
        track = CueTrack(
            number=1,
            file_name="album.flac",
            title="First",
            indexes={1: 0.0},
        )
        sheet = CueSheet(title="Album", tracks=[track])
        with tempfile.TemporaryDirectory() as directory:
            album = Path(directory) / "album"
            album.mkdir()
            target_cue = album / "album.cue"

            legitimate = cue_output_path(
                target_cue,
                track,
                sheet,
                CueSplitOptions(output_subdir="Disc 1"),
                1,
            )
            self.assertEqual(
                legitimate,
                album.resolve(strict=True) / "Disc 1" / "01 - First.flac",
            )
            with self.assertRaises(ValueError):
                cue_output_path(
                    target_cue,
                    track,
                    sheet,
                    CueSplitOptions(output_subdir="../../escaped"),
                    1,
                )
            with self.assertRaises(ValueError):
                cue_output_path(
                    target_cue,
                    track,
                    sheet,
                    CueSplitOptions(output_subdir=str(Path(directory).resolve())),
                    1,
                )

    def test_cue_output_path_rejects_a_symlink_subdirectory(self):
        track = CueTrack(
            number=1,
            file_name="album.flac",
            title="First",
            indexes={1: 0.0},
        )
        sheet = CueSheet(title="Album", tracks=[track])
        with tempfile.TemporaryDirectory() as directory:
            album = Path(directory) / "album"
            album.mkdir()
            target_cue = album / "album.cue"
            actual_output = album / "actual-output"
            actual_output.mkdir()
            linked_output = album / "linked-output"
            try:
                linked_output.symlink_to(actual_output, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            with self.assertRaises(ValueError):
                cue_output_path(
                    target_cue,
                    track,
                    sheet,
                    CueSplitOptions(output_subdir="linked-output"),
                    1,
                )


class CueIdempotencyTests(unittest.TestCase):
    def test_supplied_cue_sheet_avoids_duplicate_parse(self):
        sheet = CueSheet(
            tracks=[
                CueTrack(
                    number=1,
                    file_name="album.flac",
                    title="First",
                    indexes={1: 0.0},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch(
                "music_organizer.cue.parse_cue",
                side_effect=AssertionError("CUE must not be parsed twice"),
            ):
                result = CueProcessor().split(
                    root / "album.cue",
                    root / "output" / "album.cue",
                    CueSplitOptions(),
                    completed_tracks={1},
                    sheet=sheet,
                )

        self.assertEqual(result.created, 0)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.failed, 0)

    def test_recorded_tracks_rebuild_when_output_was_removed(self):
        cue_text = '''FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 01 04:00:00
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cue_path = root / "album.cue"
            cue_path.write_text(cue_text, encoding="utf-8")
            (root / "album.flac").write_bytes(b"source")
            calls = []

            processor = CueProcessor()

            def fake_split(_options, _source, output, *_args):
                calls.append(output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(b"rebuilt")

            processor.run_ffmpeg_split = fake_split
            result = processor.split(
                cue_path,
                root / "output" / "album.cue",
                CueSplitOptions(),
                completed_tracks={1, 2},
            )

        self.assertEqual(result.created, 2)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(len(calls), 2)

    def test_corrupt_existing_output_is_rebuilt(self):
        sheet = CueSheet(
            tracks=[
                CueTrack(
                    number=1,
                    file_name="album.flac",
                    title="First",
                    indexes={1: 0.0},
                )
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cue = root / "album.cue"
            source_cue.write_text("", encoding="utf-8")
            (root / "album.flac").write_bytes(b"source")
            target_cue = root / "output" / "album.cue"
            target_cue.parent.mkdir(parents=True)
            output = cue_output_path(target_cue, sheet.tracks[0], sheet, CueSplitOptions(), 1)
            output.write_bytes(b"not a flac")
            calls = []
            processor = CueProcessor()

            def fake_split(_options, _source, output_file, *_args):
                calls.append(output_file)
                output_file.write_bytes(b"rebuilt")

            processor.run_ffmpeg_split = fake_split
            result = processor.split(
                source_cue,
                target_cue,
                CueSplitOptions(),
                sheet=sheet,
            )

        self.assertEqual(result.created, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.failed, 0)
        self.assertEqual(calls, [output])


if __name__ == "__main__":
    unittest.main()
