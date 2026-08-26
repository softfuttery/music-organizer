"""beets template field implementing MusicBrainz Picard preset 3."""

from __future__ import annotations

from beets.plugins import BeetsPlugin

from music_organizer.naming import album_has_multiple_primary_artists


def picard_multiartist_prefix(item) -> str:
    album = item.get_album()
    items = list(album.items()) if album is not None else [item]
    if album_has_multiple_primary_artists(track.artist for track in items):
        return f"{item.artist} - " if item.artist else ""
    return ""


class PicardPresetPlugin(BeetsPlugin):
    def __init__(self) -> None:
        super().__init__()
        self.template_fields["picard_multiartist_prefix"] = (
            picard_multiartist_prefix
        )
