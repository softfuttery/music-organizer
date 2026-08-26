import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from music_organizer.config import DEFAULT_INCLUDE_EXTS, load_config, normalize_exts
from music_organizer.cue import CueSplitResult
from music_organizer.models import RunResult
from music_organizer.qbittorrent import QBittorrentClient
from organizer import MusicOrganizer


class ConfigTests(unittest.TestCase):
    def test_load_config_applies_defaults_without_sharing_extension_list(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("paths_mapping: {}\n", encoding="utf-8")

            first = load_config(path)
            first["include"]["exts"].append(".changed")
            second = load_config(path)

            self.assertEqual(second["include"]["exts"], DEFAULT_INCLUDE_EXTS)
            self.assertTrue(second["cue_split"]["enabled"])
            self.assertEqual(second["qbittorrent"]["scan_mode"], "torrent_paths")

    def test_config_root_must_be_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(["invalid"]), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "root must be a mapping"):
                load_config(path)

    def test_normalize_extensions(self):
        self.assertEqual(normalize_exts([" FLAC ", ".WAV", ""]), {".flac", ".wav"})


class CueParsingTests(unittest.TestCase):
    def test_parse_single_file_cue(self):
        cue = '''PERFORMER "Artist"
TITLE "Album"
FILE "album.flac" WAVE
  TRACK 01 AUDIO
    TITLE "First"
    INDEX 01 00:00:00
  TRACK 02 AUDIO
    TITLE "Second"
    INDEX 00 03:59:00
    INDEX 01 04:00:00
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "album.cue"
            path.write_text(cue, encoding="utf-8")
            organizer = MusicOrganizer.__new__(MusicOrganizer)

            sheet = organizer.parse_cue(path)

        self.assertEqual(sheet.performer, "Artist")
        self.assertEqual(sheet.title, "Album")
        self.assertEqual([track.title for track in sheet.tracks or []], ["First", "Second"])
        self.assertEqual((sheet.tracks or [])[1].index(1), 240.0)


class QBittorrentClientTests(unittest.TestCase):
    def test_api_key_is_exposed_as_bearer_header(self):
        client = QBittorrentClient("http://qb/", "", "", api_key="test-api-key")

        self.assertEqual(client.url("/api/v2/torrents/info"), "http://qb/api/v2/torrents/info")
        self.assertEqual(client.headers()["Authorization"], "Bearer test-api-key")

    def test_login_accepts_qbittorrent_204_response(self):
        client = QBittorrentClient("http://qb/", "admin", "password")
        response = mock.MagicMock()
        response.status = 204
        response.read.return_value = b""
        response.__enter__.return_value = response
        client.opener.open = mock.Mock(return_value=response)

        client.login()

        client.opener.open.assert_called_once()


class QBittorrentPollingTests(unittest.TestCase):
    def test_failed_scan_records_retry_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                """
paths_mapping: {}
qbittorrent:
  enabled: true
  retry_max_attempts: 3
  retry_base_seconds: 10
  retry_max_seconds: 30
""".lstrip(),
                encoding="utf-8",
            )
            organizer = MusicOrganizer(
                str(config_path),
                str(root / "organizer.sqlite3"),
                str(root / "organizer.log"),
                file_logging=False,
            )
            completed = [{"hash": "abc", "name": "completed"}]

            with mock.patch.object(
                organizer,
                "pending_qb_torrents",
                return_value=(completed, 11, False),
            ), mock.patch.object(
                organizer,
                "scan_and_organize",
                return_value=RunResult(failed=1, message="target conflict"),
            ):
                result = organizer.scan_completed_qb_torrents()

            self.assertEqual(
                result.details["torrent_retry_states"],
                {"abc": "retrying"},
            )
            self.assertIn("abc", organizer.repository.delayed_qb_hashes())

    def test_sync_rid_is_not_advanced_while_a_changed_torrent_is_deferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(
                """
paths_mapping: {}
qbittorrent:
  enabled: true
  poll_mode: sync
  scan_mode: torrent_paths
""".lstrip(),
                encoding="utf-8",
            )
            organizer = MusicOrganizer(
                str(config_path),
                str(root / "organizer.sqlite3"),
                str(root / "organizer.log"),
                file_logging=False,
            )
            organizer.set_app_state_value("qb_sync_rid", "10")
            completed = [{"hash": "abc", "name": "completed", "content_path": "/music"}]

            with mock.patch.object(
                organizer,
                "pending_qb_torrents",
                return_value=(completed, 11, True),
            ), mock.patch.object(
                organizer,
                "scan_and_organize",
                return_value=RunResult(message="ok"),
            ):
                result = organizer.scan_completed_qb_torrents()

            self.assertEqual(result.message, "ok")
            self.assertEqual(organizer.app_state_value("qb_sync_rid"), "10")
            self.assertNotIn("_run_lock", organizer.__dict__)
            self.assertNotIn("_stop_event", organizer.__dict__)

            for handler in list(organizer.logger.handlers):
                organizer.logger.removeHandler(handler)
                handler.close()


class TransferSafetyTests(unittest.TestCase):
    def setUp(self):
        self.organizer = MusicOrganizer.__new__(MusicOrganizer)

    def test_copy_publishes_only_after_complete_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            source.write_bytes(b"audio-data")

            self.assertEqual(self.organizer.transfer(source, target, "copy"), "copied")
            self.assertEqual(target.read_bytes(), b"audio-data")
            self.assertEqual(list(target.parent.glob("*.part-*")), [])

    def test_existing_different_target_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            source.write_bytes(b"new-audio")
            target.parent.mkdir()
            target.write_bytes(b"old-audio")

            with self.assertRaises(FileExistsError):
                self.organizer.transfer(source, target, "copy")

    def test_existing_identical_copy_is_recovered_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            source.write_bytes(b"complete-audio")
            target.parent.mkdir()
            target.write_bytes(b"complete-audio")
            target_inode = target.stat().st_ino

            self.assertEqual(
                self.organizer.transfer(source, target, "copy"),
                "recovered existing copy",
            )
            self.assertEqual(target.read_bytes(), b"complete-audio")
            self.assertEqual(target.stat().st_ino, target_inode)

    def test_copy_recovery_tolerates_platform_specific_fstat_ctime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            source.write_bytes(b"complete-audio")
            target.parent.mkdir()
            target.write_bytes(b"complete-audio")
            real_fstat = os.fstat

            def platform_fstat(descriptor):
                value = real_fstat(descriptor)
                return SimpleNamespace(
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                    st_ctime_ns=value.st_ctime_ns + 1,
                )

            with mock.patch("organizer.os.fstat", side_effect=platform_fstat):
                result = self.organizer.transfer(source, target, "copy")

            self.assertEqual(result, "recovered existing copy")

    def test_existing_identical_file_is_not_recovered_for_hardlink_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            source.write_bytes(b"same-audio")
            target.parent.mkdir()
            target.write_bytes(b"same-audio")

            with self.assertRaises(FileExistsError):
                self.organizer.transfer(source, target, "hardlink")

    def test_target_nested_under_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertTrue(
                MusicOrganizer.path_is_same_or_under(root / "library", root)
            )
            self.assertFalse(
                MusicOrganizer.path_is_same_or_under(root / "library", root / "source")
            )

    def test_copy_rejects_a_preexisting_temporary_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target = root / "library" / "source.flac"
            outside = root / "outside.flac"
            source.write_bytes(b"new-audio")
            outside.write_bytes(b"outside-audio")
            target.parent.mkdir()
            temporary = target.with_name(
                f".{target.name}.part-{os.getpid()}-123"
            )
            try:
                temporary.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with mock.patch("organizer.time.time_ns", return_value=123):
                with self.assertRaises(ValueError):
                    self.organizer.transfer(source, target, "copy")

            self.assertEqual(outside.read_bytes(), b"outside-audio")
            self.assertFalse(target.exists())

    def test_copy_accepts_regular_directories_below_explicit_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target_root = root / "library"
            target = target_root / "Artist" / "source.flac"
            source.write_bytes(b"audio-data")
            target.parent.mkdir(parents=True)

            self.assertEqual(
                self.organizer.transfer(
                    source,
                    target,
                    "copy",
                    target_root,
                ),
                "copied",
            )
            self.assertEqual(target.read_bytes(), b"audio-data")

    def test_copy_rejects_a_symlink_directory_inside_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.flac"
            target_root = root / "library"
            actual = target_root / "actual"
            linked = target_root / "Artist"
            source.write_bytes(b"audio-data")
            actual.mkdir(parents=True)
            try:
                linked.symlink_to(actual, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            with self.assertRaises(ValueError):
                self.organizer.transfer(
                    source,
                    linked / "source.flac",
                    "copy",
                    target_root,
                )

            self.assertFalse((actual / "source.flac").exists())


class TargetBoundaryIntegrationTests(unittest.TestCase):
    @staticmethod
    def make_organizer(root: Path, source: Path, target: Path) -> MusicOrganizer:
        config_path = root / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "paths_mapping": {str(source): str(target)},
                    "mode": "copy",
                    "keep_dir_struct": True,
                    "mkdir_if_single": False,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return MusicOrganizer(
            str(config_path),
            str(root / "organizer.sqlite3"),
            str(root / "organizer.log"),
            file_logging=False,
        )

    @staticmethod
    def close_organizer(organizer: MusicOrganizer) -> None:
        for handler in list(organizer.logger.handlers):
            organizer.logger.removeHandler(handler)
            handler.close()

    def test_scan_rejects_a_symlink_directory_below_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "library"
            outside = root / "outside"
            (source / "Artist").mkdir(parents=True)
            target.mkdir()
            outside.mkdir()
            (source / "Artist" / "track.flac").write_bytes(b"audio")
            try:
                (target / "Artist").symlink_to(
                    outside,
                    target_is_directory=True,
                )
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            organizer = self.make_organizer(root, source, target)
            try:
                result = organizer.scan_and_organize()
            finally:
                self.close_organizer(organizer)

            self.assertGreaterEqual(result.failed, 1)
            self.assertFalse((outside / "track.flac").exists())

    def test_scan_records_an_identical_copy_left_before_database_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "library"
            source_file = source / "Artist" / "track.flac"
            target_file = target / "Artist" / "track.flac"
            source_file.parent.mkdir(parents=True)
            target_file.parent.mkdir(parents=True)
            source_file.write_bytes(b"complete-audio")
            target_file.write_bytes(b"complete-audio")
            organizer = self.make_organizer(root, source, target)
            try:
                result = organizer.scan_and_organize()
                self.assertTrue(organizer.repository.is_processed(source_file))
            finally:
                self.close_organizer(organizer)

            self.assertEqual(result.failed, 0)
            self.assertEqual(result.organized, 1)
            self.assertEqual(target_file.read_bytes(), b"complete-audio")

    def test_a_cue_arriving_after_audio_is_sent_to_the_split_stage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "library"
            album = source / "Artist" / "Album"
            audio = album / "track.flac"
            album.mkdir(parents=True)
            audio.write_bytes(b"audio")
            organizer = self.make_organizer(root, source, target)
            try:
                first = organizer.scan_and_organize()
                self.assertEqual(first.failed, 0)
                cue = album / "album.cue"
                cue.write_text(
                    'FILE "track.flac" WAVE\n'
                    '  TRACK 01 AUDIO\n'
                    '    TITLE "Track"\n'
                    '    INDEX 01 00:00:00\n',
                    encoding="utf-8",
                )
                split_sources: list[Path] = []

                def observe_split(source_file, *_args, **_kwargs):
                    split_sources.append(source_file)
                    return (0, 0, 0)

                with mock.patch.object(
                    organizer,
                    "split_cue_if_needed",
                    side_effect=observe_split,
                ):
                    second = organizer.scan_and_organize()
            finally:
                self.close_organizer(organizer)

            self.assertEqual(second.failed, 0)
            self.assertIn(cue, split_sources)

    def test_scan_rejects_a_symlink_mapping_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            outside = root / "outside"
            target = root / "library-link"
            (source / "Artist").mkdir(parents=True)
            outside.mkdir()
            (source / "Artist" / "track.flac").write_bytes(b"audio")
            try:
                target.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            organizer = self.make_organizer(root, source, target)
            try:
                result = organizer.scan_and_organize()
            finally:
                self.close_organizer(organizer)

            self.assertGreaterEqual(result.failed, 1)
            self.assertFalse((outside / "Artist" / "track.flac").exists())

    def test_cue_split_rejects_a_target_below_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cue = root / "source" / "album.cue"
            target_root = root / "library"
            actual_album = target_root / "actual-album"
            source_cue.parent.mkdir()
            actual_album.mkdir(parents=True)
            source_cue.write_text("", encoding="utf-8")
            linked_album = target_root / "Album"
            try:
                linked_album.symlink_to(actual_album, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")
            organizer = MusicOrganizer.__new__(MusicOrganizer)
            organizer.cue_processor = mock.Mock()
            organizer._cue_sheet_cache = {}

            with self.assertRaises(ValueError):
                organizer.split_cue_if_needed(
                    source_cue,
                    linked_album / "album.cue",
                    {"cue_split": {"enabled": True}},
                    set(),
                    False,
                    target_root,
                )

            organizer.cue_processor.split.assert_not_called()

    def test_cue_split_accepts_a_regular_target_below_target_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_cue = root / "source" / "album.cue"
            target_root = root / "library"
            target_cue = target_root / "Album" / "album.cue"
            source_cue.parent.mkdir()
            target_root.mkdir()
            source_cue.write_text("", encoding="utf-8")
            organizer = MusicOrganizer.__new__(MusicOrganizer)
            organizer.cue_processor = mock.Mock()
            organizer.cue_processor.split.return_value = CueSplitResult()
            organizer._cue_sheet_cache = {}

            result = organizer.split_cue_if_needed(
                source_cue,
                target_cue,
                {"cue_split": {"enabled": True}},
                set(),
                False,
                target_root,
            )

            self.assertEqual(result, (0, 0, 0))
            self.assertEqual(
                organizer.cue_processor.split.call_args.args[1],
                target_cue,
            )


class ProcessControlTests(unittest.TestCase):
    def test_interruptible_process_honors_cancellation(self):
        organizer = MusicOrganizer.__new__(MusicOrganizer)
        cancelled = threading.Event()
        organizer._cancel_check = cancelled.is_set
        timer = threading.Timer(0.1, cancelled.set)
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(InterruptedError, "stopped by user"):
                organizer.run_interruptible_process(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout=10,
                )
        finally:
            timer.cancel()

        self.assertLess(time.monotonic() - started, 5)

    def test_interruptible_process_enforces_timeout(self):
        organizer = MusicOrganizer.__new__(MusicOrganizer)
        organizer._cancel_check = lambda: False
        started = time.monotonic()

        with self.assertRaisesRegex(TimeoutError, "Process timed out"):
            organizer.run_interruptible_process(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                timeout=0.1,
            )

        self.assertLess(time.monotonic() - started, 5)


class CandidateScannerSafetyTests(unittest.TestCase):
    def setUp(self):
        self.organizer = MusicOrganizer.__new__(MusicOrganizer)

    def test_candidate_scanner_rejects_file_and_directory_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan_root = root / "inbox"
            outside = root / "outside"
            scan_root.mkdir()
            outside.mkdir()
            legitimate = scan_root / "inside.flac"
            legitimate.write_bytes(b"inside")
            outside_audio = outside / "outside.flac"
            outside_audio.write_bytes(b"outside")
            linked_file = scan_root / "linked.flac"
            linked_inside = scan_root / "linked-inside.flac"
            linked_dir = scan_root / "linked-dir"
            try:
                linked_file.symlink_to(outside_audio)
                linked_inside.symlink_to(legitimate)
                linked_dir.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            config = {
                "include": {"exts": [".flac"], "globs": []},
                "exclude": {"exts": [], "globs": []},
            }
            candidates = list(
                self.organizer.iter_candidate_files(scan_root, config)
            )

            self.assertEqual(candidates, [legitimate])

    def test_candidate_scanner_rejects_a_symlink_scan_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "outside.flac").write_bytes(b"outside")
            linked_root = root / "linked-root"
            try:
                linked_root.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            config = {
                "include": {"exts": [".flac"], "globs": []},
                "exclude": {"exts": [], "globs": []},
            }

            self.assertEqual(
                list(self.organizer.iter_candidate_files(linked_root, config)),
                [],
            )

    def test_candidate_scanner_rejects_a_scan_root_below_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scan_root = root / "inbox"
            actual = scan_root / "actual"
            nested = actual / "nested"
            nested.mkdir(parents=True)
            (nested / "inside.flac").write_bytes(b"inside")
            linked_dir = scan_root / "linked-dir"
            try:
                linked_dir.symlink_to(actual, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            config = {
                "include": {"exts": [".flac"], "globs": []},
                "exclude": {"exts": [], "globs": []},
            }

            self.assertEqual(
                list(
                    self.organizer.iter_candidate_files(
                        linked_dir / "nested",
                        config,
                        rules_root=scan_root,
                    )
                ),
                [],
            )


if __name__ == "__main__":
    unittest.main()
