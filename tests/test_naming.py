import unittest

from music_organizer.naming import (
    PICARD_PRESET3_PATH_FORMAT,
    album_has_multiple_primary_artists,
    picard_preset3_relative_path,
)


class PicardPreset3Tests(unittest.TestCase):
    def test_standard_album(self):
        self.assertEqual(
            picard_preset3_relative_path(
                albumartist="专辑艺术家",
                artist="专辑艺术家",
                album="专辑",
                disctotal=1,
                disc=1,
                track=3,
                multiartist=False,
                title="标题",
                extension=".flac",
            ),
            "专辑艺术家/专辑/03 标题.flac",
        )

    def test_multi_disc_multiartist_album(self):
        self.assertEqual(
            picard_preset3_relative_path(
                albumartist="Various Artists",
                artist="曲目艺术家",
                album="合辑",
                disctotal=2,
                disc=2,
                track=4,
                multiartist=True,
                title="曲名",
                extension="flac",
            ),
            "Various Artists/合辑/2-04 曲目艺术家 - 曲名.flac",
        )

    def test_missing_albumartist_matches_picard_conditionals(self):
        self.assertEqual(
            picard_preset3_relative_path(
                albumartist="",
                artist="曲目艺术家",
                album="不会成为目录",
                disctotal=1,
                disc=1,
                track=7,
                multiartist=False,
                title="曲名",
            ),
            "曲目艺术家/曲名",
        )

    def test_multiartist_is_album_level_primary_artist_difference(self):
        self.assertTrue(album_has_multiple_primary_artists(["A", "B", "A"]))
        self.assertFalse(album_has_multiple_primary_artists(["A", " a "]))
        self.assertIn("$picard_multiartist_prefix", PICARD_PRESET3_PATH_FORMAT)

if __name__ == "__main__":
    unittest.main()
