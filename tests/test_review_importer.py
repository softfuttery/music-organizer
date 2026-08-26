import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from music_organizer.review_importer import (
    ApprovedImportSession,
    apply_track_mapping,
    duplicate_action_for_release,
    existing_album_import_result,
    import_review_album,
    imported_track_results,
)


class Box:
    def __init__(self, **values):
        self.__dict__.update(values)


class RecoverableItem(Box):
    def __init__(
        self,
        *,
        destination: Path,
        tag_bytes: bytes | None = None,
        **values,
    ):
        super().__init__(**values)
        self._destination = destination
        self._tag_bytes = tag_bytes
        self.store_calls = 0
        self.write_calls = 0

    def destination(self):
        return os.fsencode(self._destination)

    def store(self):
        self.store_calls += 1

    def try_write(self):
        self.write_calls += 1
        if self._tag_bytes is not None:
            Path(self.path).write_bytes(self._tag_bytes)
        return True


def source_guard(root: Path) -> dict:
    root_metadata = root.stat()
    entries = {}
    for current_root, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(current_root)
        for name in (*dirnames, *filenames):
            candidate = current / name
            metadata = candidate.lstat()
            file_type = stat.S_IFMT(metadata.st_mode)
            entries[candidate.relative_to(root).as_posix()] = [
                int(metadata.st_dev),
                int(metadata.st_ino),
                file_type,
                int(metadata.st_size) if stat.S_ISREG(metadata.st_mode) else 0,
                int(metadata.st_mtime_ns)
                if stat.S_ISREG(metadata.st_mode)
                else 0,
            ]
        dirnames[:] = [
            name for name in dirnames if not (current / name).is_symlink()
        ]
    return {
        "root": [int(root_metadata.st_dev), int(root_metadata.st_ino)],
        "entries": entries,
    }


class ReviewImporterDecisionTests(unittest.TestCase):
    def test_same_name_different_musicbrainz_release_is_kept(self):
        from beets.importer import DuplicateAction

        duplicate = Box(mb_albumid="older-release-id")

        self.assertEqual(
            duplicate_action_for_release("new-release-id", [duplicate]),
            DuplicateAction.KEEP,
        )

    def test_exact_musicbrainz_release_duplicate_is_rejected(self):
        duplicate = Box(mb_albumid="same-release-id")

        with self.assertRaisesRegex(ValueError, "同一 MusicBrainz 发行版本"):
            duplicate_action_for_release("same-release-id", [duplicate])

    def test_hardlink_tag_write_recovers_remaining_tracks_with_guard(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            target = root / "library" / "Artist" / "Album"
            source.mkdir()
            target.mkdir(parents=True)
            first_source = source / "01.flac"
            second_source = source / "02.flac"
            first_source.write_bytes(b"first track before tags")
            second_source.write_bytes(b"second track unchanged")
            guard = source_guard(source)
            first_target = target / "01 First.flac"
            second_target = target / "02 Second.flac"
            first_target.hardlink_to(first_source)
            first_target.write_bytes(b"first track after a larger metadata write")
            items = [
                RecoverableItem(
                    destination=first_target,
                    tag_bytes=b"first finalized tags",
                    path=str(first_source),
                    mb_trackid="track-1",
                    review_recovery_token="same-task",
                ),
                RecoverableItem(
                    destination=second_target,
                    tag_bytes=b"second finalized tags",
                    path=str(second_source),
                    mb_trackid="track-2",
                    review_recovery_token="same-task",
                ),
            ]
            album = Box(mb_albumid="release-1")
            album.items = lambda: items
            library = Box(directory=str(root / "library"))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                source,
                [
                    {"local_path": "01.flac", "track_key": "track-1"},
                    {"local_path": "02.flac", "track_key": "track-2"},
                ],
                recovery_token="same-task",
                import_mode="hardlink",
                library_directory=root / "library",
                import_guard=guard,
                write_tags=True,
            )

            self.assertEqual(result["imported_track_count"], 2)
            self.assertTrue(os.path.samefile(first_source, first_target))
            self.assertTrue(os.path.samefile(second_source, second_target))
            self.assertEqual(Path(items[0].path).resolve(), first_target)
            self.assertEqual(Path(items[1].path).resolve(), second_target)
            self.assertEqual(items[0].store_calls, 1)
            self.assertEqual(items[1].store_calls, 1)
            self.assertEqual(items[0].write_calls, 1)
            self.assertEqual(items[1].write_calls, 1)
            self.assertEqual(first_source.read_bytes(), b"first finalized tags")
            self.assertEqual(second_source.read_bytes(), b"second finalized tags")

    def test_hardlink_tag_write_guard_rejects_new_or_replaced_paths(self):
        for mutation in ("new", "replaced"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as tempdir,
            ):
                root = Path(tempdir)
                source = root / "source"
                target = root / "library" / "Artist" / "Album"
                source.mkdir()
                target.mkdir(parents=True)
                first_source = source / "01.flac"
                second_source = source / "02.flac"
                first_source.write_bytes(b"first before tags")
                second_source.write_bytes(b"second original")
                guard = source_guard(source)
                first_target = target / "01 First.flac"
                second_target = target / "02 Second.flac"
                first_target.hardlink_to(first_source)
                first_target.write_bytes(b"first after metadata write")
                if mutation == "new":
                    (source / "unexpected.flac").write_bytes(b"new audio")
                else:
                    second_source.unlink()
                    second_source.write_bytes(b"replacement audio")
                items = [
                    RecoverableItem(
                        destination=first_target,
                        path=str(first_source),
                        mb_trackid="track-1",
                        review_recovery_token="same-task",
                    ),
                    RecoverableItem(
                        destination=second_target,
                        path=str(second_source),
                        mb_trackid="track-2",
                        review_recovery_token="same-task",
                    ),
                ]
                album = Box(mb_albumid="release-1")
                album.items = lambda: items
                library = Box(directory=str(root / "library"))
                library.albums = lambda: [album]

                with self.assertRaisesRegex(ValueError, "源保护快照"):
                    existing_album_import_result(
                        library,
                        "release-1",
                        source,
                        [
                            {"local_path": "01.flac", "track_key": "track-1"},
                            {"local_path": "02.flac", "track_key": "track-2"},
                        ],
                        recovery_token="same-task",
                        import_mode="hardlink",
                        library_directory=root / "library",
                        import_guard=guard,
                        write_tags=True,
                    )

                self.assertFalse(second_target.exists())
                self.assertEqual(items[0].store_calls, 0)
                self.assertEqual(items[1].store_calls, 0)

    def test_same_task_recovery_creates_a_missing_library_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "new-library"
            source.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"audio")
            destination = library_root / "Artist" / "Album" / "01.flac"
            item = RecoverableItem(
                destination=destination,
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                source,
                [{"local_path": "01.flac", "track_key": "track-1"}],
                recovery_token="same-task",
                import_mode="copy",
                library_directory=library_root,
            )

            self.assertEqual(result["imported_track_count"], 1)
            self.assertEqual(destination.read_bytes(), b"audio")
            self.assertTrue(destination.resolve().is_relative_to(library_root))

    def test_same_task_recovery_finishes_partial_file_operations(self):
        for mode in ("copy", "move", "hardlink"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tempdir:
                root = Path(tempdir)
                source = root / "source"
                target = root / "library" / "Artist" / "Album"
                source.mkdir()
                target.mkdir(parents=True)

                completed_target = target / "01 Completed.flac"
                completed_target.write_bytes(b"completed")
                pending_source = source / "02.flac"
                pending_source.write_bytes(b"pending")
                pending_target = target / "02 Pending.flac"
                landed_source = source / "03.flac"
                landed_source.write_bytes(b"landed")
                landed_target = target / "03 Landed.flac"
                if mode == "hardlink":
                    landed_target.hardlink_to(landed_source)
                else:
                    landed_target.write_bytes(b"landed")

                items = [
                    RecoverableItem(
                        destination=completed_target,
                        path=str(completed_target),
                        mb_trackid="track-1",
                        review_recovery_token="same-task",
                    ),
                    RecoverableItem(
                        destination=pending_target,
                        path=str(pending_source),
                        mb_trackid="track-2",
                        review_recovery_token="same-task",
                    ),
                    RecoverableItem(
                        destination=landed_target,
                        path=str(landed_source),
                        mb_trackid="track-3",
                        review_recovery_token="same-task",
                    ),
                ]
                album = Box(mb_albumid="release-1")
                album.items = lambda: items
                library = Box(directory=str(root / "library"))
                library.albums = lambda: [album]

                result = existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [
                        {"local_path": "01.flac", "track_key": "track-1"},
                        {"local_path": "02.flac", "track_key": "track-2"},
                        {"local_path": "03.flac", "track_key": "track-3"},
                    ],
                    recovery_token="same-task",
                    import_mode=mode,
                    library_directory=root / "library",
                )

                self.assertEqual(result["imported_track_count"], 3)
                self.assertEqual(Path(items[1].path).resolve(), pending_target)
                self.assertEqual(Path(items[2].path).resolve(), landed_target)
                self.assertEqual(pending_target.read_bytes(), b"pending")
                self.assertEqual(landed_target.read_bytes(), b"landed")
                self.assertFalse(pending_target.is_symlink())
                self.assertFalse(landed_target.is_symlink())
                self.assertTrue(
                    pending_target.resolve().is_relative_to(
                        (root / "library").resolve()
                    )
                )
                self.assertEqual(items[1].store_calls, 1)
                self.assertEqual(items[2].store_calls, 1)
                if mode == "move":
                    self.assertFalse(pending_source.exists())
                    self.assertFalse(landed_source.exists())
                else:
                    self.assertTrue(pending_source.exists())
                    self.assertTrue(landed_source.exists())
                if mode == "hardlink":
                    self.assertTrue(os.path.samefile(pending_source, pending_target))
                    self.assertTrue(os.path.samefile(landed_source, landed_target))
                self.assertFalse((target / "03 Landed.1.flac").exists())

    def test_move_recovery_removes_source_before_writing_tags(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"untagged audio")
            destination = library_root / "01.flac"
            item = RecoverableItem(
                destination=destination,
                tag_bytes=b"tagged library audio",
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                source,
                [{"local_path": "01.flac", "track_key": "track-1"}],
                recovery_token="same-task",
                import_mode="move",
                library_directory=library_root,
                write_tags=True,
            )

            self.assertEqual(result["imported_track_count"], 1)
            self.assertFalse(source_file.exists())
            self.assertEqual(destination.read_bytes(), b"tagged library audio")
            self.assertEqual(item.store_calls, 1)
            self.assertEqual(item.write_calls, 1)

    def test_recovery_rejects_conflicting_existing_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"approved audio")
            destination = library_root / "01.flac"
            destination.write_bytes(b"different audio")
            item = RecoverableItem(
                destination=destination,
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(ValueError, "目标文件冲突"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                )

            self.assertEqual(source_file.read_bytes(), b"approved audio")
            self.assertEqual(destination.read_bytes(), b"different audio")
            self.assertEqual(item.store_calls, 0)

    def test_recovery_rejects_destination_outside_library(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            outside = root / "outside" / "01.flac"
            source.mkdir()
            library_root.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"audio")
            item = RecoverableItem(
                destination=outside,
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(ValueError, "超出媒体库目录"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                )

            self.assertFalse(outside.exists())

    def test_recovery_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"approved audio")
            outside = root / "outside.flac"
            outside.write_bytes(b"outside audio")
            destination = library_root / "01.flac"
            try:
                destination.symlink_to(outside)
            except OSError as exc:
                self.skipTest(f"当前文件系统不能创建符号链接: {exc}")
            item = RecoverableItem(
                destination=destination,
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(ValueError, "符号链接"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                )

            self.assertEqual(outside.read_bytes(), b"outside audio")
            self.assertEqual(source_file.read_bytes(), b"approved audio")

    def test_recovery_rejects_symlinked_destination_parent_inside_library(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            real_target = library_root / "real"
            source.mkdir()
            real_target.mkdir(parents=True)
            source_file = source / "01.flac"
            source_file.write_bytes(b"audio")
            linked_target = library_root / "linked"
            try:
                linked_target.symlink_to(real_target, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f"当前文件系统不能创建符号链接: {exc}")
            destination = linked_target / "01.flac"
            item = RecoverableItem(
                destination=destination,
                path=str(source_file),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(ValueError, "符号链接"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                )

            self.assertFalse((real_target / "01.flac").exists())
            self.assertTrue(source_file.is_file())

    def test_recover_only_does_not_consume_changed_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            replacement = source / "01.flac"
            replacement.write_bytes(b"replacement")
            destination = library_root / "01.flac"
            item = RecoverableItem(
                destination=destination,
                path=str(replacement),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(RuntimeError, "源目录已变化"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                    allow_source_operations=False,
                )

            self.assertEqual(replacement.read_bytes(), b"replacement")
            self.assertFalse(destination.exists())

    def test_recover_only_does_not_adopt_ambiguous_existing_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            replacement = source / "01.flac"
            replacement.write_bytes(b"replacement")
            destination = library_root / "01.flac"
            destination.write_bytes(b"unrelated existing file")
            item = RecoverableItem(
                destination=destination,
                path=str(replacement),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(RuntimeError, "不能采用"):
                existing_album_import_result(
                    library,
                    "release-1",
                    source,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-task",
                    import_mode="copy",
                    library_directory=library_root,
                    allow_source_operations=False,
                )

            self.assertEqual(replacement.read_bytes(), b"replacement")
            self.assertEqual(destination.read_bytes(), b"unrelated existing file")
            self.assertEqual(item.store_calls, 0)

    def test_recover_only_reuses_completed_album_without_touching_new_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            original = source / "01.flac"
            original.write_bytes(b"original source")
            guard = source_guard(source)
            new_audio = source / "new.flac"
            new_audio.write_bytes(b"newly synchronized")
            destination = library_root / "01.flac"
            destination.write_bytes(b"imported library audio")
            item = RecoverableItem(
                destination=destination,
                tag_bytes=b"must not be written",
                path=str(destination),
                mb_trackid="track-1",
                review_recovery_token="same-task",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                source,
                [{"local_path": "01.flac", "track_key": "track-1"}],
                recovery_token="same-task",
                import_mode="hardlink",
                library_directory=library_root,
                allow_source_operations=False,
                import_guard=guard,
                write_tags=True,
            )

            self.assertTrue(result["reused_existing_album"])
            self.assertEqual(original.read_bytes(), b"original source")
            self.assertEqual(new_audio.read_bytes(), b"newly synchronized")
            self.assertEqual(destination.read_bytes(), b"imported library audio")
            self.assertEqual(item.store_calls, 0)
            self.assertEqual(item.write_calls, 0)

    def test_normal_import_does_not_reuse_existing_release_without_same_task_proof(
        self,
    ):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            destination = root / "library" / "Artist" / "Album" / "01.flac"
            source.mkdir()
            source_file = source / "01.flac"
            source_file.write_bytes(b"new high-resolution audio")
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"existing lower-resolution audio")
            config_path = root / "beets.yaml"
            config_path.write_text(
                "library: ignored.db\ndirectory: ignored\nplugins: []\n",
                encoding="utf-8",
            )
            item = Box(
                mb_trackid="track-1",
                path=str(destination),
                review_recovery_token="an-older-import",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(Path(tempdir) / "library"))
            library.albums = lambda: [album]
            session = Mock()

            with (
                patch("beets.library.Library", return_value=library),
                patch("beets.plugins.load_plugins"),
                patch.object(
                    ApprovedImportSession,
                    "create",
                    return_value=session,
                ),
            ):
                with self.assertRaisesRegex(ValueError, "发行版已存在"):
                    import_review_album(
                        config_path,
                        source,
                        "release-1",
                        [{"local_path": "01.flac", "track_key": "track-1"}],
                    )

            self.assertTrue(source_file.is_file())
            self.assertEqual(
                source_file.read_bytes(), b"new high-resolution audio"
            )
            session.run.assert_not_called()

    def test_exact_existing_release_is_reused_for_finalization(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir) / "source"
            target = Path(tempdir) / "library" / "Artist" / "Album"
            root.mkdir()
            target.mkdir(parents=True)
            (root / "01.flac").write_bytes(b"source audio")
            destination = target / "01 Track.flac"
            destination.write_bytes(b"library audio")
            item = Box(
                mb_trackid="track-1",
                path=str(destination),
                review_recovery_token="same-import-token",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(Path(tempdir) / "library"))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                root,
                [{"local_path": "01.flac", "track_key": "track-1"}],
                recovery_token="same-import-token",
            )

        self.assertTrue(result["reused_existing_album"])
        self.assertEqual(result["imported_track_count"], 1)
        self.assertEqual(result["imported_tracks"][0]["source"], "01.flac")
        self.assertEqual(
            result["destination_directory"], str(target.resolve())
        )

    def test_completed_import_recovery_uses_distinct_release_track_ids(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            library_root = root / "library"
            target = library_root / "Artist" / "Album"
            target.mkdir(parents=True)
            first_destination = target / "01 First.flac"
            second_destination = target / "02 Encore.flac"
            first_destination.write_bytes(b"first appearance")
            second_destination.write_bytes(b"second appearance")
            first = Box(
                mb_releasetrackid="release-track-1",
                mb_trackid="shared-recording",
                path=str(first_destination),
                review_recovery_token="same-import-token",
            )
            second = Box(
                mb_releasetrackid="release-track-2",
                mb_trackid="shared-recording",
                path=str(second_destination),
                review_recovery_token="same-import-token",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [first, second]
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                root / "removed-source",
                [
                    {"local_path": "01.flac", "track_key": "release-track-1"},
                    {"local_path": "02.flac", "track_key": "release-track-2"},
                ],
                recovery_token="same-import-token",
                allow_source_operations=False,
            )

        self.assertEqual(
            [track["destination"] for track in result["imported_tracks"]],
            [str(first_destination.resolve()), str(second_destination.resolve())],
        )

    def test_completed_import_recovery_rejects_ambiguous_recording_alias(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            library_root = root / "library"
            target = library_root / "Artist" / "Album"
            target.mkdir(parents=True)
            items = []
            for index in (1, 2):
                destination = target / f"0{index}.flac"
                destination.write_bytes(f"appearance {index}".encode())
                items.append(
                    Box(
                        mb_releasetrackid=f"release-track-{index}",
                        mb_trackid="shared-recording",
                        path=str(destination),
                        review_recovery_token="same-import-token",
                    )
                )
            album = Box(mb_albumid="release-1")
            album.items = lambda: items
            library = Box(directory=str(library_root))
            library.albums = lambda: [album]

            with self.assertRaisesRegex(ValueError, "旧版.*不唯一"):
                existing_album_import_result(
                    library,
                    "release-1",
                    root / "removed-source",
                    [{"local_path": "01.flac", "track_key": "shared-recording"}],
                    recovery_token="same-import-token",
                    allow_source_operations=False,
                )

    def test_existing_release_with_missing_track_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "01.flac").write_bytes(b"audio")
            album = Box(mb_albumid="release-1")
            album.items = lambda: []
            library = Box(directory=str(root))
            library.albums = lambda: [album]
            with self.assertRaisesRegex(ValueError, "缺少已确认"):
                existing_album_import_result(
                    library,
                    "release-1",
                    root,
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-import-token",
                )

    def test_existing_release_reuse_requires_same_task_confirmation(self):
        item = Box(
            mb_trackid="track-1",
            review_recovery_token="another-import-token",
        )
        album = Box(mb_albumid="release-1")
        album.items = lambda: [item]
        library = Box()
        library.albums = lambda: [album]

        with self.assertRaisesRegex(ValueError, "发行版已存在"):
            existing_album_import_result(
                library,
                "release-1",
                Path("unused"),
                [],
            )

    def test_same_task_recovery_does_not_require_removed_source_files(self):
        with tempfile.TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "library" / "Artist" / "Album"
            target.mkdir(parents=True)
            destination = target / "01 Track.flac"
            destination.write_bytes(b"already imported audio")
            item = Box(
                mb_trackid="track-1",
                path=str(destination),
                review_recovery_token="same-import-token",
            )
            album = Box(mb_albumid="release-1")
            album.items = lambda: [item]
            library = Box(directory=str(Path(tempdir) / "library"))
            library.albums = lambda: [album]

            result = existing_album_import_result(
                library,
                "release-1",
                Path(tempdir) / "removed-source",
                [{"local_path": "01.flac", "track_key": "track-1"}],
                recovery_token="same-import-token",
            )

        self.assertTrue(result["reused_existing_album"])
        self.assertEqual(result["imported_tracks"][0]["source"], "01.flac")

    def test_recover_only_never_imports_replacement_source(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "replacement"
            source.mkdir()
            (source / "01.flac").write_bytes(b"replacement audio")
            config_path = root / "beets.yaml"
            config_path.write_text(
                "library: ignored.db\ndirectory: ignored\nplugins: []\n",
                encoding="utf-8",
            )
            library = Box()
            library.albums = lambda: []
            session = Mock()

            with (
                patch("beets.library.Library", return_value=library),
                patch("beets.plugins.load_plugins"),
                patch.object(
                    ApprovedImportSession,
                    "create",
                    return_value=session,
                ),
                self.assertRaisesRegex(RuntimeError, "拒绝重新导入"),
            ):
                import_review_album(
                    config_path,
                    source,
                    "release-1",
                    [{"local_path": "01.flac", "track_key": "track-1"}],
                    recovery_token="same-import-token",
                    recover_only=True,
                )

            session.run.assert_not_called()

    def test_imported_track_results_exposes_final_beets_destination(self):
        with tempfile.TemporaryDirectory() as tempdir:
            destination = Path(tempdir) / "Artist" / "Album" / "01 Track.flac"
            destination.parent.mkdir(parents=True)
            destination.write_bytes(b"audio")

            result = imported_track_results(
                [("disc/track.flac", Box(path=str(destination)))]
            )

        self.assertEqual(result[0]["source"], "disc/track.flac")
        self.assertEqual(result[0]["destination"], str(destination.resolve()))

    def test_persisted_mapping_replaces_beets_candidate_mapping(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = Box(filepath=root / "01.flac")
            second = Box(filepath=root / "02.flac")
            track_one = Box(track_id="track-1")
            track_two = Box(track_id="track-2")
            task = Box(items=[first, second])
            candidate = Box(
                info=Box(tracks=[track_one, track_two]),
                mapping={first: track_one, second: track_two},
                extra_items=[],
                extra_tracks=[],
            )

            count = apply_track_mapping(
                task,
                candidate,
                root,
                [{"local_path": "02.flac", "track_key": "track-1"}],
            )

        self.assertEqual(count, 1)
        self.assertEqual(candidate.mapping, {second: track_one})
        self.assertEqual(candidate.extra_items, [first])
        self.assertEqual(candidate.extra_tracks, [track_two])

    def test_release_track_ids_map_two_appearances_of_one_recording(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = Box(filepath=root / "01.flac")
            second = Box(filepath=root / "02.flac")
            track_one = Box(
                track_id="recording-id",
                release_track_id="release-track-1",
            )
            track_two = Box(
                track_id="recording-id",
                release_track_id="release-track-2",
            )
            task = Box(items=[first, second])
            candidate = Box(info=Box(tracks=[track_one, track_two]))

            count = apply_track_mapping(
                task,
                candidate,
                root,
                [
                    {"local_path": "01.flac", "track_key": "release-track-1"},
                    {"local_path": "02.flac", "track_key": "release-track-2"},
                ],
            )

        self.assertEqual(count, 2)
        self.assertEqual(candidate.mapping, {first: track_one, second: track_two})

    def test_legacy_recording_id_mapping_remains_supported_when_unambiguous(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            item = Box(filepath=root / "01.flac")
            track = Box(
                track_id="legacy-recording-id",
                release_track_id="release-track-id",
            )
            task = Box(items=[item])
            candidate = Box(info=Box(tracks=[track]))

            count = apply_track_mapping(
                task,
                candidate,
                root,
                [{"local_path": "01.flac", "track_key": "legacy-recording-id"}],
            )

        self.assertEqual(count, 1)
        self.assertEqual(candidate.mapping, {item: track})

    def test_ambiguous_legacy_recording_id_mapping_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            item = Box(filepath=root / "01.flac")
            first_track = Box(
                track_id="same-recording",
                release_track_id="release-track-1",
            )
            second_track = Box(
                track_id="same-recording",
                release_track_id="release-track-2",
            )
            task = Box(items=[item])
            candidate = Box(info=Box(tracks=[first_track, second_track]))

            with self.assertRaisesRegex(ValueError, "曲目标识不唯一"):
                apply_track_mapping(
                    task,
                    candidate,
                    root,
                    [{"local_path": "01.flac", "track_key": "same-recording"}],
                )

    def test_nested_review_path_matches_unique_beets_task_suffix(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            item = Box(filepath=root / "01 Track.flac")
            track = Box(track_id="track-1")
            task = Box(items=[item])
            candidate = Box(
                info=Box(tracks=[track]),
                mapping={item: track},
                extra_items=[],
                extra_tracks=[],
            )
            resolved_source_paths = {}

            count = apply_track_mapping(
                task,
                candidate,
                root,
                [
                    {
                        "local_path": "LACA-9026/01 Track.flac",
                        "track_key": "track-1",
                    }
                ],
                resolved_source_paths=resolved_source_paths,
            )

        self.assertEqual(count, 1)
        self.assertEqual(candidate.mapping, {item: track})
        self.assertEqual(
            resolved_source_paths,
            {item: "LACA-9026/01 Track.flac"},
        )

    def test_ambiguous_beets_task_suffix_is_rejected(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = Box(filepath=root / "disc-1" / "01 Track.flac")
            second = Box(filepath=root / "disc-2" / "01 Track.flac")
            track = Box(track_id="track-1")
            task = Box(items=[first, second])
            candidate = Box(info=Box(tracks=[track]))

            with self.assertRaisesRegex(ValueError, "路径存在歧义"):
                apply_track_mapping(
                    task,
                    candidate,
                    root,
                    [
                        {
                            "local_path": "01 Track.flac",
                            "track_key": "track-1",
                        }
                    ],
                )

    def test_duplicate_track_mapping_is_rejected_again_in_import_subprocess(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = Box(filepath=root / "01.flac")
            second = Box(filepath=root / "02.flac")
            track = Box(track_id="track-1")
            task = Box(items=[first, second])
            candidate = Box(info=Box(tracks=[track]))
            with self.assertRaisesRegex(ValueError, "不能重复对应"):
                apply_track_mapping(
                    task,
                    candidate,
                    root,
                    [
                        {"local_path": "01.flac", "track_key": "track-1"},
                        {"local_path": "02.flac", "track_key": "track-1"},
                    ],
                )


if __name__ == "__main__":
    unittest.main()
