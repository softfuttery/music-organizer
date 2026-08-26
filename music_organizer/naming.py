"""Shared naming rules for beets imports and review previews."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Iterable

PICARD_PRESET3_PATH_FORMAT = (
    "$album_dir/%if{$albumartist,$album/}$disc_prefix$track_prefix"
    "$picard_multiartist_prefix$title"
)
LEGACY_PATH_FORMAT = (
    "$album_dir/$album_part$disc_prefix$track_prefix$multiartist_prefix$title"
)


def album_has_multiple_primary_artists(artists: Iterable[str]) -> bool:
    """Match Picard's _multiartist album-level meaning."""
    normalized = {
        " ".join(str(artist or "").split()).casefold()
        for artist in artists
        if str(artist or "").strip()
    }
    return len(normalized) > 1


def picard_preset3_relative_path(
    *,
    albumartist: str,
    artist: str,
    album: str,
    disctotal: int,
    disc: int,
    track: int,
    multiartist: bool,
    title: str,
    extension: str = "",
) -> str:
    """Render the logical path produced by Picard system preset 3."""
    directories = [albumartist or artist]
    if albumartist and album:
        directories.append(album)

    disc_prefix = f"{disc}-" if disctotal > 1 else ""
    track_prefix = f"{track:02d} " if albumartist and track else ""
    artist_prefix = f"{artist} - " if multiartist else ""
    suffix = extension if not extension or extension.startswith(".") else f".{extension}"
    filename = f"{disc_prefix}{track_prefix}{artist_prefix}{title}{suffix}"
    return PurePosixPath(*directories, filename).as_posix()
