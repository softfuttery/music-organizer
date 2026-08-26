import os
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml
from beets import config, plugins
from beets.library import Item, Library

from music_organizer.naming import PICARD_PRESET3_PATH_FORMAT
from music_organizer.review_importer import existing_album_import_result
from organizer import MusicOrganizer


class BeetsPathIntegrationTests(unittest.TestCase):
    def test_generated_beets_config_renders_picard_preset3_destinations(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            incoming = root / "incoming"
            incoming.mkdir()
            first_path = incoming / "one.flac"
            second_path = incoming / "two.flac"
            first_path.touch()
            second_path.touch()
            organizer = MusicOrganizer(
                str(root / "app.yaml"),
                str(root / "organizer.sqlite3"),
                str(root / "organizer.log"),
                file_logging=False,
            )
            config_path = organizer.write_beets_config(
                {
                    "directory": str(root / "library"),
                    "library": str(root / "beets-library.db"),
                    "config_path": str(root / "beets.yaml"),
                }
            )
            generated = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                generated["paths"]["default"], PICARD_PRESET3_PATH_FORMAT
            )
            self.assertIn("picardpreset", generated["plugins"])

            config.set_file(str(config_path))
            self.assertEqual(
                list(config["plugins"].as_str_seq()),
                ["inline", "musicbrainz", "picardpreset"],
            )
            plugins.load_plugins()
            library = Library(
                str(root / "beets-library.db"),
                directory=str(root / "library"),
            )
            first = Item(
                path=str(first_path),
                albumartist="Various Artists",
                album="合辑",
                artist="艺术家 A",
                title="第一首",
                disc=1,
                disctotal=2,
                track=1,
            )
            second = Item(
                path=str(second_path),
                albumartist="Various Artists",
                album="合辑",
                artist="艺术家 B",
                title="第二首",
                disc=2,
                disctotal=2,
                track=2,
            )
            first["review_recovery_token"] = "same-review-task"
            second["review_recovery_token"] = "same-review-task"
            library.add_album([first, second])
            artists = [item.artist for item in first.get_album().items()]
            plugin_names = [plugin.name for plugin in plugins.find_plugins()]
            first_destination = os.fsdecode(
                first.destination(relative_to_libdir=True)
            ).replace("\\", "/")
            second_destination = os.fsdecode(
                second.destination(relative_to_libdir=True)
            ).replace("\\", "/")
            library._close()
            reopened = Library(
                str(root / "beets-library.db"),
                directory=str(root / "library"),
            )
            recovery_tokens = {
                item.get("review_recovery_token")
                for item in reopened.items()
            }
            reopened._close()

            self.assertEqual(first.disctotal, 2)
            self.assertEqual(first.disc, 1)
            self.assertEqual(artists, ["艺术家 A", "艺术家 B"])
            self.assertIn("picardpreset", plugin_names)
            self.assertEqual(recovery_tokens, {"same-review-task"})

            self.assertEqual(
                first_destination,
                "Various Artists/合辑/1-01 艺术家 A - 第一首.flac",
            )
            self.assertEqual(
                second_destination,
                "Various Artists/合辑/2-02 艺术家 B - 第二首.flac",
            )

    def test_real_beets_library_recovers_a_partially_copied_album(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "source"
            library_root = root / "library"
            source.mkdir()
            library_root.mkdir()
            first_source = source / "01.flac"
            second_source = source / "02.flac"
            first_source.write_bytes(b"first")
            second_source.write_bytes(b"second")
            organizer = MusicOrganizer(
                str(root / "app.yaml"),
                str(root / "organizer.sqlite3"),
                str(root / "organizer.log"),
                file_logging=False,
            )
            config_path = organizer.write_beets_config(
                {
                    "directory": str(library_root),
                    "library": str(root / "beets-library.db"),
                    "config_path": str(root / "beets.yaml"),
                    "import_mode": "copy",
                }
            )
            config.set_file(str(config_path))
            plugins.load_plugins()
            library = Library(
                str(root / "beets-library.db"),
                directory=str(library_root),
            )
            first = Item(
                path=str(first_source),
                albumartist="Artist",
                album="Album",
                artist="Artist",
                title="First",
                track=1,
                mb_trackid="track-1",
            )
            second = Item(
                path=str(second_source),
                albumartist="Artist",
                album="Album",
                artist="Artist",
                title="Second",
                track=2,
                mb_trackid="track-2",
            )
            for item in (first, second):
                item["review_recovery_token"] = "same-task"
            album = library.add_album([first, second])
            album.mb_albumid = "release-1"
            album.store()

            first_destination = Path(os.fsdecode(first.destination()))
            first_destination.parent.mkdir(parents=True)
            shutil.copy2(first_source, first_destination)
            first.path = os.fsencode(first_destination)
            first.store()

            result = existing_album_import_result(
                library,
                "release-1",
                source,
                [
                    {"local_path": "01.flac", "track_key": "track-1"},
                    {"local_path": "02.flac", "track_key": "track-2"},
                ],
                recovery_token="same-task",
                import_mode="copy",
                library_directory=library_root,
            )
            second_destination = Path(os.fsdecode(second.destination()))
            library._close()

            reopened = Library(
                str(root / "beets-library.db"),
                directory=str(library_root),
            )
            persisted_paths = {
                Path(os.fsdecode(item.path)).resolve() for item in reopened.items()
            }
            reopened._close()

            self.assertEqual(result["imported_track_count"], 2)
            self.assertEqual(
                persisted_paths,
                {first_destination.resolve(), second_destination.resolve()},
            )
            self.assertEqual(second_destination.read_bytes(), b"second")


if __name__ == "__main__":
    unittest.main()
