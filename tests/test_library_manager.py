import tempfile
import unittest
from pathlib import Path
from unittest import mock

from music_organizer import library_manager
from music_organizer.library_manager import (
    library_file,
    library_root,
    restore_trash,
    save_lyrics,
    scan_folders,
    scan_tracks,
    track_detail,
    trash_entries,
    trash_folder,
    trash_track,
    update_tags,
)


class FakeMedia:
    def __init__(self):
        self.tags = {}
        self.saved = False

    def add_tags(self):
        self.tags = {}

    def save(self):
        self.saved = True


class LibraryManagerTests(unittest.TestCase):
    def test_scan_sidecar_trash_and_restore_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            audio = album / "01 Song.flac"
            audio.write_bytes(b"not-real-audio")

            listing = scan_tracks(root, query="song")
            self.assertEqual(listing["total"], 1)
            self.assertEqual(listing["tracks"][0]["path"], "Artist/Album/01 Song.flac")

            saved = save_lyrics(audio, "[00:01.00]line", "sidecar")
            self.assertEqual(saved["mode"], "sidecar")
            self.assertTrue((album / "01 Song.lrc").is_file())
            self.assertTrue(track_detail(audio, root)["lyrics"]["sidecar"]["synced"])

            trashed = trash_track(root, "Artist/Album/01 Song.flac")
            self.assertFalse(audio.exists())
            self.assertFalse((album / "01 Song.lrc").exists())
            self.assertEqual(trash_entries(root)[0]["token"], trashed["token"])

            restored = restore_trash(root, trashed["token"])
            self.assertEqual(len(restored["restored"]), 2)
            self.assertTrue(audio.is_file())
            self.assertEqual(
                (album / "01 Song.lrc").read_text(encoding="utf-8"),
                "[00:01.00]line",
            )

    def test_unfiltered_scan_reads_only_the_requested_page_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for index in range(3):
                (root / f"{index} song.flac").write_bytes(b"audio")

            with mock.patch(
                "music_organizer.library_manager.track_payload",
                wraps=library_manager.track_payload,
            ) as payload:
                listing = scan_tracks(root, offset=1, limit=1)

            self.assertEqual(listing["total"], 3)
            self.assertEqual(len(listing["tracks"]), 1)
            self.assertEqual(payload.call_count, 1)

    def test_folder_scan_groups_tracks_and_paginates_by_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            for folder, filenames in {
                "Artist A/Album A": ["01 one.flac", "02 two.flac"],
                "Artist B/Album B": ["01 three.flac"],
            }.items():
                target = root / folder
                target.mkdir(parents=True)
                for filename in filenames:
                    (target / filename).write_bytes(b"audio")

            first = scan_folders(root, limit=1)
            self.assertEqual(first["total"], 2)
            self.assertEqual(first["track_total"], 3)
            self.assertEqual(len(first["folders"]), 1)
            self.assertEqual(first["order"], "desc")
            self.assertEqual(first["folders"][0]["path"], "Artist B/Album B")
            self.assertEqual(first["folders"][0]["track_count"], 1)
            self.assertEqual(len(first["folders"][0]["tracks"]), 1)

            ascending = scan_folders(root, limit=1, order="asc")
            self.assertEqual(ascending["folders"][0]["path"], "Artist A/Album A")

            searched = scan_folders(root, query="three")
            self.assertEqual(searched["total"], 1)
            self.assertEqual(searched["folders"][0]["path"], "Artist B/Album B")

    def test_folder_scan_marks_all_tracks_embedded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            (album / "01 one.flac").write_bytes(b"audio")
            (album / "02 two.flac").write_bytes(b"audio")

            def payload(path, _root):
                return {
                    "path": path.relative_to(root).as_posix(),
                    "size": 1,
                    "lyrics": {"embedded": True, "sidecar": False},
                }

            with mock.patch(
                "music_organizer.library_manager.track_payload",
                side_effect=payload,
            ):
                folder = scan_folders(root)["folders"][0]

            self.assertEqual(folder["embedded_count"], 2)
            self.assertTrue(folder["all_embedded"])

    def test_library_path_rejects_traversal_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            audio = root / "song.flac"
            audio.write_bytes(b"audio")
            outside = root.parent / f"{root.name}-outside.flac"
            outside.write_bytes(b"outside")
            try:
                with self.assertRaises(ValueError):
                    library_file(root, "../outside.flac")
                link = root / "linked.flac"
                try:
                    link.symlink_to(outside)
                except OSError:
                    return
                with self.assertRaises(ValueError):
                    library_file(root, "linked.flac")
            finally:
                outside.unlink(missing_ok=True)

    def test_tag_update_is_limited_and_read_back(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "song.flac"
            path.write_bytes(b"audio")
            media = FakeMedia()
            with mock.patch(
                "music_organizer.library_manager._media", return_value=media
            ):
                tags = update_tags(path, {"title": "Song", "artist": "Artist"})
            self.assertTrue(media.saved)
            self.assertEqual(tags["title"], "Song")
            self.assertEqual(tags["artist"], "Artist")
            with self.assertRaises(ValueError):
                update_tags(path, {"path": "escape"})

    def test_library_root_must_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(library_root(root), root.resolve())
            with self.assertRaises(ValueError):
                library_root(root / "missing")

    def test_trash_rolls_back_when_manifest_cannot_be_written(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            audio = root / "song.flac"
            lyrics = root / "song.lrc"
            audio.write_bytes(b"audio")
            lyrics.write_text("lyrics", encoding="utf-8")

            with mock.patch(
                "music_organizer.library_manager._atomic_text",
                side_effect=OSError("disk full"),
            ):
                with self.assertRaises(OSError):
                    trash_track(root, "song.flac")

            self.assertEqual(audio.read_bytes(), b"audio")
            self.assertEqual(lyrics.read_text(encoding="utf-8"), "lyrics")
            self.assertEqual(trash_entries(root), [])

    def test_folder_trash_and_restore_moves_all_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            (album / "song.flac").write_bytes(b"audio")
            (album / "song.lrc").write_text("lyrics", encoding="utf-8")
            (album / "cover.jpg").write_bytes(b"cover")

            trashed = trash_folder(root, "Artist/Album")
            self.assertEqual(trashed["kind"], "folder")
            self.assertEqual(trashed["track_count"], 1)
            self.assertFalse(album.exists())
            self.assertEqual(trash_entries(root)[0]["kind"], "folder")

            restored = restore_trash(root, trashed["token"])
            self.assertEqual(restored["restored"], ["Artist/Album"])
            self.assertEqual((album / "song.flac").read_bytes(), b"audio")
            self.assertEqual((album / "song.lrc").read_text(encoding="utf-8"), "lyrics")
            self.assertEqual((album / "cover.jpg").read_bytes(), b"cover")

    def test_folder_trash_rejects_library_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "song.flac").write_bytes(b"audio")
            with self.assertRaises(ValueError):
                trash_folder(root, ".")

    def test_restore_rejects_a_parent_replaced_by_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            album = root / "Artist" / "Album"
            album.mkdir(parents=True)
            audio = album / "song.flac"
            audio.write_bytes(b"audio")
            trashed = trash_track(root, "Artist/Album/song.flac")
            album.rmdir()
            (root / "Artist").rmdir()

            outside = root.parent / f"{root.name}-restore-outside"
            outside.mkdir()
            link = root / "Artist"
            try:
                try:
                    link.symlink_to(outside, target_is_directory=True)
                except OSError:
                    return
                with self.assertRaises(ValueError):
                    restore_trash(root, trashed["token"])
                self.assertFalse((outside / "Album" / "song.flac").exists())
                self.assertEqual(trash_entries(root)[0]["token"], trashed["token"])
            finally:
                if link.is_symlink():
                    link.unlink()
                outside.rmdir()
