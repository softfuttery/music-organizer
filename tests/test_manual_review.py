import tempfile
import unittest
from pathlib import Path

from music_organizer.manual_review import (
    build_manual_candidate,
    infer_directory_metadata,
    infer_filename_metadata,
    manual_album_directory,
    manual_artist_directory,
)


class ManualReviewTests(unittest.TestCase):
    def test_filename_and_directory_rules(self):
        self.assertEqual(
            infer_directory_metadata("[260722] 7co - 猫じゃらし [BVCL-1536]"),
            ("7co", "猫じゃらし"),
        )
        parsed = infer_filename_metadata("01 Hana Hope - Hearts Glow.flac")
        self.assertEqual(parsed["track"], 1)
        self.assertEqual(parsed["artist"], "Hana Hope")
        self.assertEqual(parsed["title"], "Hearts Glow")

    def test_candidate_always_targets_unclassified_and_can_be_edited(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Artist - Album"
            source.mkdir()
            (source / "01 Artist - First.flac").write_bytes(b"audio")
            (source / "02 Second.flac").write_bytes(b"audio")

            preview = build_manual_candidate(source)
            self.assertTrue(
                all(
                    track["target_path"].startswith(
                        "未分类/Artist/Album/"
                    )
                    for track in preview["tracks"]
                )
            )
            edited = build_manual_candidate(
                source,
                {
                    "albumartist": "Edited Artist",
                    "album": "Edited Album",
                    "year": 2026,
                    "tracks": [
                        {
                            "local_path": "02 Second.flac",
                            "artist": "Edited Artist",
                            "title": "Edited Second",
                            "disc": 1,
                            "track": 2,
                        }
                    ],
                },
            )
            self.assertEqual(len(edited["tracks"]), 1)
            self.assertEqual(edited["tracks"][0]["album"], "Edited Album")
            self.assertEqual(
                edited["tracks"][0]["target_path"],
                "未分类/Edited Artist/Edited Album/02 Second.flac",
            )
            self.assertEqual(
                edited["destination_relative_directory"],
                "未分类/Edited Artist/Edited Album",
            )
            self.assertEqual(edited["extra_items"], ["01 Artist - First.flac"])
            self.assertNotEqual(edited["key"], preview["key"])

    def test_manual_artist_directory_stays_one_safe_component(self):
        self.assertEqual(
            manual_artist_directory("Artist/Unit\\Name"),
            "Artist／Unit＼Name",
        )
        self.assertEqual(manual_artist_directory(".."), "未知艺术家")
        self.assertEqual(manual_album_directory("Album/Disc"), "Album／Disc")
        self.assertEqual(manual_album_directory("."), "未知专辑")

    def test_nested_source_files_are_flattened_into_the_album_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "Artist - Album"
            nested = source / "Disc 1"
            nested.mkdir(parents=True)
            (nested / "01 Song.flac").write_bytes(b"audio")

            preview = build_manual_candidate(source)

            self.assertEqual(
                preview["tracks"][0]["target_path"],
                "未分类/Artist/Album/01 Song.flac",
            )
