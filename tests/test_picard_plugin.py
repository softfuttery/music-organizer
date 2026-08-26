import unittest

from music_organizer.beetsplug.picardpreset import (
    picard_multiartist_prefix,
)


class FakeAlbum:
    def __init__(self, items):
        self._items = items

    def items(self):
        return iter(self._items)


class FakeItem:
    def __init__(self, artist):
        self.artist = artist
        self.album = None

    def get_album(self):
        return self.album


class PicardPresetPluginTests(unittest.TestCase):
    def test_prefix_is_applied_to_every_track_on_multiartist_album(self):
        first = FakeItem("艺术家 A")
        second = FakeItem("艺术家 B")
        album = FakeAlbum([first, second])
        first.album = second.album = album

        self.assertEqual(picard_multiartist_prefix(first), "艺术家 A - ")
        self.assertEqual(picard_multiartist_prefix(second), "艺术家 B - ")

    def test_prefix_is_empty_when_all_primary_artists_match(self):
        first = FakeItem("艺术家 A")
        second = FakeItem("艺术家 A")
        album = FakeAlbum([first, second])
        first.album = second.album = album

        self.assertEqual(picard_multiartist_prefix(first), "")


if __name__ == "__main__":
    unittest.main()
