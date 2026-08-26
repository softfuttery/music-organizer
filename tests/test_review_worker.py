import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest import mock

import review_worker
from music_organizer.repository import SQLiteOrganizerRepository
from music_organizer.review import ReviewRepository


class ReviewWorkerTests(unittest.TestCase):
    @staticmethod
    def import_job(
        source: Path,
        *,
        checkpoint: dict | None = None,
        guard: dict | None = None,
    ) -> dict:
        return {
            "queue_id": 1,
            "item_id": 2,
            "attempts": 1,
            "source_path": str(source),
            "source_signature": "signature",
            "selected_candidate_key": "candidate",
            "candidates_json": json.dumps(
                [
                    {
                        "key": "candidate",
                        "album_id": "release-id",
                        "artist": "Artist",
                        "album": "Album",
                    }
                ]
            ),
            "decision_json": json.dumps({"track_mapping": []}),
            "import_token": "persistent-token",
            "import_guard_json": json.dumps(guard or {}),
            "import_checkpoint_json": json.dumps(checkpoint or {}),
        }

    def test_new_music_waits_until_directory_contents_are_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inbox = root / "inbox"
            album = inbox / "Artist - Album"
            album.mkdir(parents=True)
            (album / "01.flac").write_bytes(b"audio")
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.review_config = {"source_roots": [str(inbox)]}
            worker.discovery_stable_seconds = 60
            worker.discovery_observations = {}
            worker.repository = ReviewRepository(root / "organizer.sqlite3")
            worker.repository.initialize()
            worker.organizer = mock.Mock()

            self.assertIsNone(worker.discover_new_music(now=0))
            self.assertIsNone(worker.discover_new_music(now=59))
            batch = worker.discover_new_music(now=60)

            self.assertIsNotNone(batch)
            self.assertEqual(batch["label"], "自动发现新音乐")
            self.assertEqual(batch["items"][0]["source_path"], str(album.resolve()))

    def test_beets_imports_are_serialized(self):
        worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
        worker.import_lock = threading.Lock()
        state_lock = threading.Lock()
        release_first = threading.Event()
        first_started = threading.Event()
        active = 0
        maximum_active = 0

        def fake_import(_job):
            nonlocal active, maximum_active
            with state_lock:
                active += 1
                maximum_active = max(maximum_active, active)
                if active == 1:
                    first_started.set()
            release_first.wait(timeout=2)
            with state_lock:
                active -= 1

        worker._import_approved = fake_import
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(worker.import_approved, {"item_id": 1})
            self.assertTrue(first_started.wait(timeout=1))
            second = executor.submit(worker.import_approved, {"item_id": 2})
            self.assertFalse(second.done())
            release_first.set()
            first.result(timeout=2)
            second.result(timeout=2)

        self.assertEqual(maximum_active, 1)

    def test_manual_candidate_uses_filename_rule_importer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "Artist - Album"
            source.mkdir()
            (source / "01 Artist - Song.flac").write_bytes(b"audio")
            (source / "cover.jpg").write_bytes(b"cover")
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)
            job["candidates_json"] = json.dumps(
                [
                    {
                        "key": "candidate",
                        "album_id": "manual:test",
                        "data_source": "manual",
                        "artist": "Artist",
                        "album": "Album",
                        "auxiliary_files": ["cover.jpg"],
                        "tracks": [
                            {
                                "local_path": "01 Artist - Song.flac",
                                "artist": "Artist",
                                "title": "Song",
                                "album": "Album",
                                "albumartist": "Artist",
                                "disc": 1,
                                "track": 1,
                                "year": 2026,
                            }
                        ],
                    }
                ]
            )
            imported = {
                "album_id": "manual:test",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01 Artist - Song.flac",
                        "destination": str(root / "library" / "song.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
                "manual": True,
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "directory": str(root / "library"),
            }

            with mock.patch(
                "review_worker.finalize_review_import",
                return_value={"additional_files": [], "warnings": []},
            ) as finalizer:
                worker._import_approved(job)

            command = worker.organizer.run_interruptible_process.call_args.args[0]
            self.assertIn("music_organizer.manual_importer", command)
            self.assertIn("--tracks-json", command)
            self.assertNotIn("--album-id", command)
            self.assertEqual(
                finalizer.call_args.kwargs["extra_file_paths"],
                ["cover.jpg"],
            )
            self.assertTrue(finalizer.call_args.kwargs["move_extra_files"])

    def test_missing_source_recovers_only_the_same_tokenized_beets_import(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "already-moved-album"
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
                "reused_existing_album": True,
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {"source_roots": [str(root)]}

            worker._import_approved(self.import_job(source))

            command = worker.organizer.run_interruptible_process.call_args.args[0]
            self.assertIn("--recover-only", command)
            self.assertIn("--recovery-token", command)
            worker.repository.checkpoint_import.assert_called_once_with(
                1, 2, imported
            )
            worker.repository.complete_import.assert_called_once()
            worker.repository.fail.assert_not_called()

    def test_checkpoint_completes_after_source_was_removed_without_rerunning_beets(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {"source_roots": [str(root)]}

            worker._import_approved(
                self.import_job(root / "removed-source", checkpoint=checkpoint)
            )

            worker.organizer.run_interruptible_process.assert_not_called()
            worker.repository.checkpoint_import.assert_not_called()
            worker.repository.complete_import.assert_called_once()
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("持久化检查点恢复", " ".join(result["warnings"]))
            worker.repository.fail.assert_not_called()

    def test_partial_move_restart_uses_persisted_guard_without_recover_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            first = source / "01.flac"
            second = source / "02.flac"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            signature_before = review_worker.source_signature(source)
            root_stat = source.stat()
            guard = review_worker.source_guard(
                (int(root_stat.st_dev), int(root_stat.st_ino)),
                review_worker.source_identity_snapshot(source),
            )
            first.unlink()
            imported = {
                "album_id": "release-id",
                "imported_track_count": 2,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    },
                    {
                        "source": "02.flac",
                        "destination": str(root / "library" / "02.flac"),
                    },
                ],
                "destination_directory": str(root / "library"),
                "reused_existing_album": True,
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "move",
            }
            job = self.import_job(source, guard=guard)
            job["source_signature"] = signature_before

            worker._import_approved(job)

            command = worker.organizer.run_interruptible_process.call_args.args[0]
            self.assertIn("--import-guard-json", command)
            self.assertNotIn("--recover-only", command)
            worker.repository.checkpoint_import_guard.assert_not_called()
            worker.repository.checkpoint_import.assert_called_once()
            worker.repository.complete_import.assert_called_once()
            worker.repository.fail.assert_not_called()

    def test_checkpoint_recovery_never_cleans_files_seen_only_after_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            (source / "01.flac").write_bytes(b"audio")
            new_cover = source / "cover.jpg"
            new_cover.write_bytes(b"new after restart")
            checkpoint = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "move_extra_files": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source, checkpoint=checkpoint)
            job["source_signature"] = review_worker.source_signature(source)
            job["decision_json"] = json.dumps(
                {"track_mapping": [], "quarantine_paths": ["cover.jpg"]}
            )

            with (
                mock.patch("review_worker.quarantine_files") as quarantine,
                mock.patch("review_worker.finalize_review_import") as finalizer,
            ):
                worker._import_approved(job)

            quarantine.assert_not_called()
            finalizer.assert_not_called()
            self.assertTrue(new_cover.is_file())
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("重启后同步的新文件", " ".join(result["warnings"]))
            worker.repository.fail.assert_not_called()

    def test_same_token_beets_recovery_skips_destructive_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            (source / "01.flac").write_bytes(b"audio")
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
                "reused_existing_album": True,
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "move_extra_files": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)

            with mock.patch("review_worker.finalize_review_import") as finalizer:
                worker._import_approved(job)

            finalizer.assert_not_called()
            worker.repository.checkpoint_import.assert_called_once()
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("重启后同步的新文件", " ".join(result["warnings"]))
            worker.repository.fail.assert_not_called()

    def test_source_is_rechecked_after_beets_before_destructive_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "move_extra_files": True,
                "cleanup_source_after_import": True,
            }

            def finish_after_replacement(*_args, **_kwargs):
                (source / "new-arrival.flac").write_bytes(b"new")
                return 0, json.dumps(imported)

            worker.organizer.run_interruptible_process.side_effect = (
                finish_after_replacement
            )

            with (
                mock.patch(
                    "review_worker.source_signature",
                    return_value="signature",
                ),
                mock.patch("review_worker.finalize_review_import") as finalizer,
            ):
                worker._import_approved(self.import_job(source))

            finalizer.assert_not_called()
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("源目录内容已变化", " ".join(result["warnings"]))
            self.assertEqual(
                worker.repository.complete_import.call_args.kwargs[
                    "source_signature_after_import"
                ],
                "",
            )
            worker.repository.fail.assert_not_called()

    def test_source_is_rechecked_after_quarantine_before_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"approved audio")
            (source / "cover.jpg").write_bytes(b"cover")
            destination = root / "library" / "01.flac"
            destination.parent.mkdir()
            destination.write_bytes(b"approved audio")
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(destination),
                    }
                ],
                "destination_directory": str(destination.parent),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "directory": str(root / "library"),
                "import_mode": "copy",
                "move_extra_files": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)
            job["decision_json"] = json.dumps(
                {
                    "track_mapping": [],
                    "quarantine_paths": ["cover.jpg"],
                }
            )

            def replace_audio_after_quarantine(*_args, **_kwargs):
                replacement = source / ".new-audio"
                replacement.write_bytes(b"new synchronized audio")
                replacement.replace(source_audio)
                return [
                    {
                        "source": "cover.jpg",
                        "destination": ".quarantine/cover.jpg",
                        "status": "quarantined",
                    }
                ]

            with (
                mock.patch(
                    "review_worker.quarantine_files",
                    side_effect=replace_audio_after_quarantine,
                ),
                mock.patch("review_worker.finalize_review_import") as finalizer,
            ):
                worker._import_approved(job)

            finalizer.assert_not_called()
            self.assertEqual(source_audio.read_bytes(), b"new synchronized audio")
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("源目录内容已变化", " ".join(result["warnings"]))
            self.assertEqual(
                worker.repository.complete_import.call_args.kwargs[
                    "source_signature_after_import"
                ],
                "",
            )
            worker.repository.fail.assert_not_called()

    def test_source_change_while_computing_final_signature_is_not_archived(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            (source / "01.flac").write_bytes(b"approved audio")
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "copy",
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)
            real_signature = review_worker.source_signature
            call_count = 0

            def change_during_final_signature(path, files=None):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    (source / "new.flac").write_bytes(b"new synchronized audio")
                return real_signature(path, files)

            with mock.patch(
                "review_worker.source_signature",
                side_effect=change_during_final_signature,
            ):
                worker._import_approved(job)

            self.assertTrue((source / "new.flac").is_file())
            self.assertEqual(
                worker.repository.complete_import.call_args.kwargs[
                    "source_signature_after_import"
                ],
                "",
            )
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("完成记录前检测到源目录再次变化", " ".join(result["warnings"]))
            worker.repository.fail.assert_not_called()

    def test_move_mode_removals_do_not_look_like_replacement_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"audio")
            (source / "cover.jpg").write_bytes(b"cover")
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()

            def finish_after_move(*_args, **_kwargs):
                source_audio.unlink()
                return 0, json.dumps(imported)

            worker.organizer.run_interruptible_process.side_effect = finish_after_move
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "move_extra_files": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)

            with mock.patch(
                "review_worker.finalize_review_import",
                return_value={
                    "additional_files": [],
                    "removed_source_files": [],
                    "removed_directories": [],
                    "source_removed": False,
                    "remaining_files": [],
                    "warnings": [],
                },
            ) as finalizer:
                worker._import_approved(job)

            finalizer.assert_called_once()
            worker.repository.complete_import.assert_called_once()
            worker.repository.fail.assert_not_called()

    def test_in_place_source_rewrite_skips_destructive_finalization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"old")
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(root / "library" / "01.flac"),
                    }
                ],
                "destination_directory": str(root / "library"),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()

            def finish_after_rewrite(*_args, **_kwargs):
                source_audio.write_bytes(b"replacement audio")
                return 0, json.dumps(imported)

            worker.organizer.run_interruptible_process.side_effect = (
                finish_after_rewrite
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "copy",
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)

            with mock.patch("review_worker.finalize_review_import") as finalizer:
                worker._import_approved(job)

            finalizer.assert_not_called()
            result = worker.repository.complete_import.call_args.args[2]
            self.assertIn("源目录内容已变化", " ".join(result["warnings"]))
            self.assertTrue(source_audio.is_file())
            worker.repository.fail.assert_not_called()

    def test_expected_hardlink_tag_write_can_still_finalize(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"untagged")
            destination = root / "library" / "01.flac"
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(destination),
                    }
                ],
                "destination_directory": str(destination.parent),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()

            def finish_after_tag_write(*_args, **_kwargs):
                destination.parent.mkdir(parents=True)
                os.link(source_audio, destination)
                destination.write_bytes(b"tagged audio")
                return 0, json.dumps(imported)

            worker.organizer.run_interruptible_process.side_effect = (
                finish_after_tag_write
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "hardlink",
                "write_tags": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source)
            job["source_signature"] = review_worker.source_signature(source)

            with mock.patch(
                "review_worker.finalize_review_import",
                return_value={
                    "additional_files": [],
                    "removed_source_files": [],
                    "removed_directories": [],
                    "source_removed": False,
                    "remaining_files": [],
                    "warnings": [],
                },
            ) as finalizer:
                worker._import_approved(job)

            finalizer.assert_called_once()
            worker.repository.complete_import.assert_called_once()
            worker.repository.fail.assert_not_called()

    def test_hardlink_tag_restart_persists_new_stable_source_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            destination = root / "library" / "01.flac"
            source.mkdir()
            destination.parent.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"before tags")
            signature_before = review_worker.source_signature(source)
            root_stat = source.stat()
            guard = review_worker.source_guard(
                (int(root_stat.st_dev), int(root_stat.st_ino)),
                review_worker.source_identity_snapshot(source),
            )
            os.link(source_audio, destination)
            destination.write_bytes(b"after a larger tag write")
            signature_after = review_worker.source_signature(source)
            self.assertNotEqual(signature_before, signature_after)
            imported = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(destination),
                    }
                ],
                "destination_directory": str(destination.parent),
                "reused_existing_album": True,
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(imported),
            )
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "hardlink",
                "write_tags": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source, guard=guard)
            job["source_signature"] = signature_before

            with mock.patch("review_worker.finalize_review_import") as finalizer:
                worker._import_approved(job)

            command = worker.organizer.run_interruptible_process.call_args.args[0]
            self.assertIn("--import-guard-json", command)
            self.assertNotIn("--recover-only", command)
            finalizer.assert_not_called()
            worker.repository.complete_import.assert_called_once()
            self.assertEqual(
                worker.repository.complete_import.call_args.kwargs[
                    "source_signature_after_import"
                ],
                signature_after,
            )
            worker.repository.fail.assert_not_called()

    def test_hardlink_tag_checkpoint_persists_new_stable_source_signature(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            destination = root / "library" / "01.flac"
            source.mkdir()
            destination.parent.mkdir()
            source_audio = source / "01.flac"
            source_audio.write_bytes(b"before tags")
            signature_before = review_worker.source_signature(source)
            root_stat = source.stat()
            guard = review_worker.source_guard(
                (int(root_stat.st_dev), int(root_stat.st_ino)),
                review_worker.source_identity_snapshot(source),
            )
            os.link(source_audio, destination)
            destination.write_bytes(b"after a larger tag write")
            signature_after = review_worker.source_signature(source)
            checkpoint = {
                "album_id": "release-id",
                "imported_track_count": 1,
                "imported_tracks": [
                    {
                        "source": "01.flac",
                        "destination": str(destination),
                    }
                ],
                "destination_directory": str(destination.parent),
            }
            worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
            worker.organizer = mock.Mock()
            worker.repository = mock.Mock()
            worker.max_attempts = 3
            worker.import_timeout_seconds = 60
            worker.beets_config_path = root / "beets.yaml"
            worker.review_config = {
                "source_roots": [str(root)],
                "import_mode": "hardlink",
                "write_tags": True,
                "cleanup_source_after_import": True,
            }
            job = self.import_job(source, checkpoint=checkpoint, guard=guard)
            job["source_signature"] = signature_before

            with mock.patch("review_worker.finalize_review_import") as finalizer:
                worker._import_approved(job)

            worker.organizer.run_interruptible_process.assert_not_called()
            worker.repository.checkpoint_import.assert_not_called()
            finalizer.assert_not_called()
            worker.repository.complete_import.assert_called_once()
            self.assertEqual(
                worker.repository.complete_import.call_args.kwargs[
                    "source_signature_after_import"
                ],
                signature_after,
            )
            worker.repository.fail.assert_not_called()

    @mock.patch("review_worker.BeetsReviewMatcher")
    @mock.patch("review_worker.ReviewRepository")
    @mock.patch("review_worker.MusicOrganizer")
    def test_import_reuses_beets_config_written_during_initialization(
        self,
        organizer_class,
        repository_class,
        matcher_class,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "album"
            source.mkdir()
            organizer = organizer_class.return_value
            organizer.load_config.return_value = {
                "review": {
                    "enabled": False,
                    "max_attempts": 3,
                    "source_roots": [str(root)],
                    "directory": str(root / "library"),
                    "move_extra_files": True,
                    "extra_file_patterns": ["*.jpg", "*.png"],
                    "cleanup_source_after_import": True,
                    "import_timeout_seconds": 7200,
                }
            }
            organizer.write_beets_config.return_value = root / "beets.yaml"
            organizer.run_interruptible_process.return_value = (
                0,
                json.dumps(
                    {
                        "album_id": "release-id",
                        "imported_track_count": 1,
                        "imported_tracks": [
                            {
                                "source": "01.flac",
                                "destination": str(root / "library" / "01.flac"),
                            }
                        ],
                        "destination_directory": str(root / "library"),
                    }
                ),
            )
            repository = repository_class.return_value
            with mock.patch.dict(
                review_worker.os.environ,
                {"REVIEW_IMPORT_TIMEOUT_SECONDS": ""},
            ):
                worker = review_worker.ReviewWorker()
            cancel_check = organizer_class.call_args.kwargs["cancel_check"]
            self.assertFalse(cancel_check())
            job = {
                "queue_id": 1,
                "item_id": 2,
                "attempts": 0,
                "source_path": str(source),
                "source_signature": "signature",
                "selected_candidate_key": "candidate",
                "candidates_json": json.dumps(
                    [
                        {
                            "key": "candidate",
                            "album_id": "release-id",
                            "artist": "Artist",
                            "album": "Album",
                        }
                    ]
                ),
                "decision_json": "{}",
                "import_token": "persistent-token",
                "import_checkpoint_json": "{}",
            }

            with (
                mock.patch(
                    "review_worker.source_signature",
                    return_value="signature",
                ),
                mock.patch(
                    "review_worker.finalize_review_import",
                    return_value={
                        "additional_files": [],
                        "source_removed": True,
                        "remaining_files": [],
                        "warnings": [],
                    },
                ) as finalizer,
            ):
                worker.import_approved(job)

            organizer.write_beets_config.assert_called_once()
            command = organizer.run_interruptible_process.call_args.args[0]
            self.assertIn("--recovery-token", command)
            self.assertIn("persistent-token", command)
            self.assertEqual(
                organizer.run_interruptible_process.call_args.kwargs["timeout"],
                7200,
            )
            repository.checkpoint_import.assert_called_once()
            repository.complete_import.assert_called_once()
            repository.fail.assert_not_called()
            finalizer.assert_called_once()
            worker.request_shutdown()
            self.assertTrue(cancel_check())

    @mock.patch("review_worker.configure_http_proxy")
    @mock.patch("review_worker.BeetsReviewMatcher")
    @mock.patch("review_worker.ReviewRepository")
    @mock.patch("review_worker.MusicOrganizer")
    def test_runtime_config_reload_rebuilds_matcher_and_updates_discovery_settings(
        self,
        organizer_class,
        repository_class,
        matcher_class,
        configure_proxy,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text("revision: one\n", encoding="utf-8")
            first = {
                "review": {
                    "enabled": True,
                    "identify_workers": 2,
                    "auto_discover": False,
                    "source_roots": [str(root / "inbox-one")],
                    "proxy_url": "http://proxy-one:7890",
                    "proxy_username": "first-user",
                    "proxy_password": "first-secret",
                }
            }
            second = {
                "review": {
                    "enabled": True,
                    "identify_workers": 5,
                    "auto_discover": True,
                    "discovery_interval_seconds": 45,
                    "discovery_stable_seconds": 120,
                    "source_roots": [str(root / "inbox-two")],
                    "proxy_url": "http://proxy-two:7890",
                    "proxy_username": "second-user",
                    "proxy_password": "second-secret",
                }
            }
            organizer = organizer_class.return_value
            organizer.load_config.side_effect = [first, second]
            organizer.write_beets_config.side_effect = [
                root / "beets-one.yaml",
                root / "beets-two.yaml",
            ]

            with mock.patch.object(review_worker, "CONFIG_PATH", config_path):
                worker = review_worker.ReviewWorker()
                config_path.write_text("revision: two and changed\n", encoding="utf-8")
                self.assertTrue(worker.runtime_config_changed())
                self.assertTrue(worker.reload_runtime_config())

            self.assertEqual(worker.worker_count, 5)
            self.assertTrue(worker.auto_discover)
            self.assertEqual(worker.discovery_interval_seconds, 45)
            self.assertEqual(worker.discovery_stable_seconds, 120)
            self.assertEqual(worker.review_config["source_roots"], [str(root / "inbox-two")])
            self.assertEqual(worker.discovery_observations, {})
            self.assertEqual(worker.next_discovery_at, 0.0)
            self.assertEqual(matcher_class.call_count, 2)
            matcher_class.return_value.configure.assert_called()
            configure_proxy.assert_called_with(
                "http://proxy-two:7890", "second-user", "second-secret"
            )

    @mock.patch("review_worker.configure_http_proxy")
    @mock.patch("review_worker.BeetsReviewMatcher")
    @mock.patch("review_worker.ReviewRepository")
    @mock.patch("review_worker.MusicOrganizer")
    def test_bad_runtime_config_keeps_last_good_settings_until_file_changes_again(
        self,
        organizer_class,
        repository_class,
        matcher_class,
        _configure_proxy,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text("revision: good-one\n", encoding="utf-8")
            first = {
                "review": {
                    "enabled": True,
                    "identify_workers": 2,
                    "source_roots": [str(root / "inbox-one")],
                }
            }
            second = {
                "review": {
                    "enabled": False,
                    "identify_workers": 4,
                    "source_roots": [str(root / "inbox-two")],
                }
            }
            organizer = organizer_class.return_value
            organizer.load_config.side_effect = [
                first,
                ValueError("invalid yaml"),
                second,
            ]
            organizer.write_beets_config.side_effect = [
                root / "beets-one.yaml",
                root / "beets-two.yaml",
            ]

            with mock.patch.object(review_worker, "CONFIG_PATH", config_path):
                worker = review_worker.ReviewWorker()
                original_matcher = worker.matcher
                config_path.write_text("review: [broken\n", encoding="utf-8")

                self.assertFalse(worker.reload_runtime_config())
                self.assertTrue(worker.enabled)
                self.assertEqual(worker.worker_count, 2)
                self.assertIs(worker.matcher, original_matcher)
                self.assertFalse(worker.runtime_config_changed())

                config_path.write_text("revision: good-two-and-new\n", encoding="utf-8")
                self.assertTrue(worker.runtime_config_changed())
                self.assertTrue(worker.reload_runtime_config())

            self.assertFalse(worker.enabled)
            self.assertEqual(worker.worker_count, 4)
            self.assertEqual(
                worker.review_config["source_roots"],
                [str(root / "inbox-two")],
            )
            self.assertEqual(matcher_class.call_count, 2)

    @mock.patch("review_worker.configure_http_proxy")
    @mock.patch("review_worker.BeetsReviewMatcher")
    @mock.patch("review_worker.ReviewRepository")
    @mock.patch("review_worker.MusicOrganizer")
    def test_runtime_reload_with_empty_proxy_switches_to_direct_connection(
        self,
        organizer_class,
        _repository_class,
        _matcher_class,
        configure_proxy,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text("revision: proxy\n", encoding="utf-8")
            organizer = organizer_class.return_value
            organizer.load_config.side_effect = [
                {
                    "review": {
                        "enabled": False,
                        "proxy_url": "http://proxy.local:7890",
                        "proxy_username": "user",
                        "proxy_password": "secret",
                    }
                },
                {"review": {"enabled": False, "proxy_url": ""}},
            ]
            organizer.write_beets_config.return_value = root / "beets.yaml"

            with mock.patch.object(review_worker, "CONFIG_PATH", config_path):
                worker = review_worker.ReviewWorker()
                config_path.write_text("revision: direct-now\n", encoding="utf-8")
                self.assertTrue(worker.reload_runtime_config())

            self.assertEqual(
                configure_proxy.call_args_list,
                [
                    mock.call("http://proxy.local:7890", "user", "secret"),
                    mock.call("", "", ""),
                ],
            )

    def test_run_forever_checks_for_runtime_config_changes(self):
        worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
        worker.shutdown_requested = review_worker.threading.Event()
        worker.worker_count = 1
        worker.poll_seconds = 0.2
        worker.enabled = False
        worker.organizer = mock.Mock()
        worker.heartbeat = mock.Mock()
        worker.runtime_config_changed = mock.Mock(return_value=True)

        def reload_and_stop():
            worker.shutdown_requested.set()
            return True

        worker.reload_runtime_config = mock.Mock(side_effect=reload_and_stop)

        worker.run_forever()

        worker.runtime_config_changed.assert_called()
        worker.reload_runtime_config.assert_called_once_with()

    def test_run_forever_drains_backlog_before_reloading_configuration(self):
        worker = review_worker.ReviewWorker.__new__(review_worker.ReviewWorker)
        worker.shutdown_requested = review_worker.threading.Event()
        worker.worker_count = 1
        worker.poll_seconds = 0.01
        worker.enabled = True
        worker.auto_discover = False
        worker.organizer = mock.Mock()
        worker.repository = mock.Mock()
        release_job = review_worker.threading.Event()
        job = {"action": "identify", "queue_id": 1, "item_id": 2}
        worker.repository.claim_next.return_value = job

        def identify(_job):
            release_job.wait(1)

        worker.identify = mock.Mock(side_effect=identify)
        worker.import_approved = mock.Mock()
        worker.heartbeat = mock.Mock()
        checks = 0

        def config_changed():
            nonlocal checks
            checks += 1
            if checks >= 2:
                release_job.set()
                return True
            return False

        worker.runtime_config_changed = mock.Mock(side_effect=config_changed)

        def reload_and_stop():
            worker.shutdown_requested.set()
            return True

        worker.reload_runtime_config = mock.Mock(side_effect=reload_and_stop)

        worker.run_forever()

        worker.repository.claim_next.assert_called_once_with("identify")
        worker.reload_runtime_config.assert_called_once_with()

    def test_healthcheck_reads_heartbeat_without_organizer_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "organizer.sqlite3"
            repository = SQLiteOrganizerRepository(database_path)
            repository.initialize()
            repository.set_app_state_value(
                "review_worker_heartbeat",
                review_worker.datetime.now().isoformat(timespec="seconds"),
            )

            with (
                mock.patch.object(review_worker, "DATABASE_PATH", database_path),
                mock.patch("review_worker.MusicOrganizer") as organizer_class,
            ):
                self.assertTrue(review_worker.heartbeat_is_fresh())

            organizer_class.assert_not_called()

    def test_healthcheck_without_database_schema_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "missing.sqlite3"
            with mock.patch.object(review_worker, "DATABASE_PATH", database_path):
                self.assertFalse(review_worker.heartbeat_is_fresh())

    def test_healthcheck_rejects_stuck_import(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "organizer.sqlite3"
            repository = SQLiteOrganizerRepository(database_path)
            repository.initialize()
            repository.set_app_state_value(
                "review_worker_heartbeat",
                review_worker.datetime.now().isoformat(timespec="seconds"),
            )
            repository.set_app_state_value(
                "review_import_active_at",
                (review_worker.datetime.now() - timedelta(minutes=5)).isoformat(
                    timespec="seconds"
                ),
            )
            repository.set_app_state_value(
                "review_import_timeout_seconds",
                "60",
            )

            with mock.patch.object(review_worker, "DATABASE_PATH", database_path):
                self.assertFalse(review_worker.heartbeat_is_fresh())


if __name__ == "__main__":
    unittest.main()
