"""Adapter between beets 2.12 autotag candidates and the review database."""

from __future__ import annotations

import os
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, urlsplit, urlunsplit

import requests

from .naming import (
    album_has_multiple_primary_artists,
    picard_preset3_relative_path,
)
from .review import AUDIO_EXTENSIONS, audio_files, source_signature


class MusicBrainzUnavailableError(RuntimeError):
    """Raised when an empty result is caused by unreachable MusicBrainz."""


def configure_http_proxy(url: str, username: str = "", password: str = "") -> str:
    """Configure requests-compatible proxy variables without logging secrets."""
    value = str(url or "").strip().rstrip("/")
    if not value:
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
        ):
            os.environ.pop(name, None)
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("代理地址必须是有效的 http:// 或 https:// 地址")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    user = str(username or parsed.username or "")
    secret = str(password or parsed.password or "")
    credentials = ""
    if user or secret:
        credentials = f"{quote(user, safe='')}:{quote(secret, safe='')}@"
    proxy = urlunsplit((parsed.scheme, credentials + host, "", "", ""))
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ[name] = proxy
    return proxy


def ensure_musicbrainz_reachable(timeout: float = 5.0) -> None:
    """Distinguish a genuine no-match result from a network outage."""
    http_proxy = os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    https_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    proxies = {
        scheme: proxy
        for scheme, proxy in (("http", http_proxy), ("https", https_proxy))
        if proxy
    }
    try:
        response = requests.get(
            "https://musicbrainz.org/ws/2/",
            headers={"User-Agent": "music-organizer-review/1.0"},
            proxies=proxies or None,
            timeout=timeout,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        raise MusicBrainzUnavailableError(
            "无法连接 MusicBrainz，识别任务稍后会自动重试；请检查 NAS 的 DNS、代理或网络设置"
        ) from exc


def _value(obj: Any, name: str, default: Any = "") -> Any:
    value = getattr(obj, name, default)
    return default if value is None else value


def _relative_path(source_root: Path, path: Path) -> str:
    try:
        return path.relative_to(source_root).as_posix()
    except ValueError:
        return path.name


def _track_key(track: Any) -> str:
    release_track_id = str(_value(track, "release_track_id"))
    if release_track_id:
        return release_track_id
    track_id = str(_value(track, "track_id"))
    if track_id:
        return track_id
    return ":".join(
        (
            str(int(_value(track, "medium", 0) or 0)),
            str(int(_value(track, "medium_index", 0) or 0)),
            str(_value(track, "artist")),
            str(_value(track, "title")),
        )
    )


def _track_aliases(track: Any) -> tuple[str, ...]:
    """Return the current release-track key plus legacy recording-ID aliases."""
    aliases = [_track_key(track), str(_value(track, "track_id"))]
    return tuple(dict.fromkeys(alias for alias in aliases if alias))


def _track_payload(track: Any) -> dict[str, Any]:
    return {
        "key": _track_key(track),
        "release_track_id": str(_value(track, "release_track_id")),
        "track_id": str(_value(track, "track_id")),
        "title": str(_value(track, "title")),
        "artist": str(_value(track, "artist")),
        "disc": int(_value(track, "medium", 0) or 0),
        "track": int(_value(track, "medium_index", 0) or 0),
        "length": round(float(_value(track, "length", 0.0) or 0.0), 3),
    }


def _natural_key(value: str) -> tuple[Any, ...]:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    )


def _normalized_match_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(character for character in text if character.isalnum())


def _match_similarity(left: Any, right: Any) -> float:
    first = _normalized_match_text(left)
    second = _normalized_match_text(right)
    if not first or not second:
        return 0.0
    if first == second:
        return 1.0
    return SequenceMatcher(None, first, second, autojunk=False).ratio()


def _item_title(item: Any) -> str:
    title = str(_value(item, "title")).strip()
    if title:
        return title
    stem = Path(_value(item, "filepath")).stem
    return re.sub(r"^\s*(?:\d{1,2}[-_.])?\d{1,3}[ ._-]+", "", stem).strip()


def _item_track_score(item: Any, track: Any) -> float:
    """Return a Picard-like per-file confidence for one release track."""
    components: list[tuple[float, float]] = [
        (0.72, _match_similarity(_item_title(item), _value(track, "title"))),
    ]
    item_artist = str(_value(item, "artist")).strip()
    track_artist = str(_value(track, "artist")).strip()
    if item_artist and track_artist:
        components.append((0.10, _match_similarity(item_artist, track_artist)))

    item_length = float(_value(item, "length", 0.0) or 0.0)
    track_length = float(_value(track, "length", 0.0) or 0.0)
    if item_length > 0 and track_length > 0:
        delta = abs(item_length - track_length)
        components.append((0.18, max(0.0, 1.0 - delta / 30.0)))

    weight = sum(value[0] for value in components)
    score = sum(component_weight * value for component_weight, value in components)
    score = score / weight if weight else 0.0

    item_disc = int(_value(item, "disc", 0) or 0)
    item_track = int(_value(item, "track", 0) or 0)
    target_disc = int(_value(track, "medium", 0) or 0)
    target_track = int(_value(track, "medium_index", 0) or 0)
    if item_disc and item_track:
        if (item_disc, item_track) == (target_disc, target_track):
            score = min(1.0, score + 0.08)
        elif item_disc != target_disc or item_track != target_track:
            score *= 0.88
    return round(max(0.0, min(score, 1.0)), 4)


def _recover_extra_mappings(
    extra_items: list[Any],
    extra_tracks: list[Any],
) -> tuple[list[tuple[Any, Any, float]], list[Any], list[Any]]:
    """Pair leftovers globally instead of discarding beets' extra items."""
    ranked = sorted(
        (
            (_item_track_score(item, track), item_index, track_index)
            for item_index, item in enumerate(extra_items)
            for track_index, track in enumerate(extra_tracks)
        ),
        key=lambda value: (-value[0], value[1], value[2]),
    )
    used_items: set[int] = set()
    used_tracks: set[int] = set()
    recovered: list[tuple[Any, Any, float]] = []
    for score, item_index, track_index in ranked:
        if score < 0.62:
            break
        if item_index in used_items or track_index in used_tracks:
            continue
        used_items.add(item_index)
        used_tracks.add(track_index)
        recovered.append((extra_items[item_index], extra_tracks[track_index], score))
    return (
        recovered,
        [item for index, item in enumerate(extra_items) if index not in used_items],
        [track for index, track in enumerate(extra_tracks) if index not in used_tracks],
    )


def _normalized_credit(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _auxiliary_files(source_root: Path) -> list[str]:
    return sorted(
        path.relative_to(source_root).as_posix()
        for path in source_root.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() not in AUDIO_EXTENSIONS
    )


def serialize_album_candidate(
    candidate: Any,
    source_root: Path,
    current_artist: str = "",
) -> dict[str, Any]:
    info = candidate.info
    data_source = str(_value(info, "data_source", "musicbrainz"))
    album_id = str(_value(info, "album_id"))
    candidate_tracks = list(_value(info, "tracks", []) or [])
    if not candidate_tracks:
        candidate_tracks = list(candidate.mapping.values()) + list(
            candidate.extra_tracks
        )
    multiartist = album_has_multiple_primary_artists(
        str(_value(track, "artist")) for track in candidate_tracks
    )
    mapping = []
    original_pairs = [
        (item, track, _item_track_score(item, track), "beets")
        for item, track in candidate.mapping.items()
    ]
    recovered_pairs, remaining_items, remaining_tracks = _recover_extra_mappings(
        list(candidate.extra_items),
        list(candidate.extra_tracks),
    )
    all_pairs = original_pairs + [
        (item, track, score, "recovered")
        for item, track, score in recovered_pairs
    ]
    for item, track, match_score, match_source in all_pairs:
        item_path = Path(item.filepath)
        local_path = _relative_path(source_root, item_path)
        track_payload = _track_payload(track)
        mapping.append(
            {
                "local_path": local_path,
                "local_title": str(_value(item, "title")),
                "extension": item_path.suffix,
                "track_key": track_payload["key"],
                "match_score": match_score,
                "match_source": match_source,
                **track_payload,
                "target_path": picard_preset3_relative_path(
                    albumartist=str(_value(info, "artist")),
                    artist=str(_value(track, "artist")),
                    album=str(_value(info, "album")),
                    disctotal=int(_value(info, "mediums", 0) or 0),
                    disc=int(_value(track, "medium", 0) or 0),
                    track=int(_value(track, "medium_index", 0) or 0),
                    multiartist=multiartist,
                    title=str(_value(track, "title")),
                    extension=item_path.suffix,
                ),
            }
        )
    extra_items = []
    local_items = [
        {
            "local_path": item["local_path"],
            "local_title": item["local_title"],
            "extension": item["extension"],
            "track_key": item["track_key"],
            "match_score": item["match_score"],
            "match_source": item["match_source"],
            "disc": item["disc"],
            "track": item["track"],
        }
        for item in mapping
    ]
    for item in remaining_items:
        path = Path(item.filepath)
        local_path = _relative_path(source_root, path)
        extra_items.append(local_path)
        local_items.append(
            {
                "local_path": local_path,
                "local_title": str(_value(item, "title")),
                "extension": path.suffix,
                "track_key": "",
                "match_score": 0.0,
                "match_source": "unmatched",
                "disc": 0,
                "track": 0,
            }
        )
    extra_tracks = [
        _track_payload(track)
        for track in remaining_tracks
    ]
    track_options = sorted(
        (_track_payload(track) for track in candidate_tracks),
        key=lambda track: (track["disc"], track["track"], track["title"].casefold()),
    )
    distance = float(candidate.distance.distance)
    penalties = {
        key: round(float(value), 6)
        for key, value in candidate.distance.items()
    }
    raw_score = max(0.0, 1.0 - distance)
    artist_credit_match = bool(
        current_artist
        and _normalized_credit(current_artist)
        == _normalized_credit(_value(info, "artist_credit"))
    )
    # MusicBrainz distinguishes the canonical artist name (for example
    # ``suis``) from the release credit (``suis from ヨルシカ``). Beets scores
    # against the canonical name, even when the file exactly matches the
    # release credit shown to listeners. Do not present that as an artist
    # mismatch in the review UI.
    adjusted_distance = distance
    if artist_credit_match:
        adjusted_distance = max(0.0, distance - penalties.get("artist", 0.0))
    return {
        "key": f"{data_source}:{album_id}",
        "data_source": data_source,
        "album_id": album_id,
        "release_group_id": str(_value(info, "releasegroup_id")),
        "artist": str(_value(info, "artist")),
        "artist_credit": str(_value(info, "artist_credit")),
        "album": str(_value(info, "album")),
        "year": int(_value(info, "year", 0) or 0),
        "country": str(_value(info, "country")),
        "label": str(_value(info, "label")),
        "catalog_number": str(_value(info, "catalognum")),
        "media": str(_value(info, "media")),
        "mediums": int(_value(info, "mediums", 0) or 0),
        "multiartist": multiartist,
        "score": round(max(0.0, 1.0 - adjusted_distance), 6),
        "raw_score": round(raw_score, 6),
        "artist_credit_match": artist_credit_match,
        "penalties": penalties,
        "tracks": mapping,
        "local_items": sorted(
            local_items,
            key=lambda item: (
                1 if not item["track_key"] else 0,
                item["disc"] if item["track_key"] else 0,
                item["track"] if item["track_key"] else 0,
                _natural_key(item["local_path"]),
            ),
        ),
        "track_options": track_options,
        "extra_items": extra_items,
        "extra_tracks": extra_tracks,
        "auxiliary_files": _auxiliary_files(source_root),
    }


class BeetsReviewMatcher:
    """Run one-shot lookups; no beets session object survives the call."""

    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self._configured = False

    def configure(self) -> None:
        if self._configured:
            return
        from beets import config, plugins

        config.set_file(str(self.config_path))
        config["asciify_paths"].set(False)
        configured_plugins = list(config["plugins"].as_str_seq())
        for name in ("musicbrainz", "inline"):
            if name not in configured_plugins:
                configured_plugins.append(name)
        config["plugins"].set(configured_plugins)
        plugins.load_plugins()
        self._configured = True

    @staticmethod
    def _text_search_terms(
        items: Sequence[Any],
        search_artist: str | None,
        search_album: str | None,
    ) -> tuple[str, str] | None:
        """Prefer an explicit artist or a unanimous album artist for lookup.

        beets considers an album a compilation whenever its per-track artists
        differ, even when every file has the same non-VA ``albumartist``. That
        is common for doujin albums credited to a circle such as FELT. In that
        case its MusicBrainz plugin replaces the artist query with Various
        Artists and returns unrelated releases sharing only the album title.
        """
        from beets.autotag.distance import VA_ARTISTS
        from beets.util import get_most_common_tags

        artist = str(search_artist or "").strip()
        album = str(search_album or "").strip()
        if artist and album:
            if artist.casefold() not in VA_ARTISTS:
                return artist, album
            return None

        likelies, consensus = get_most_common_tags(items)
        albumartist = str(likelies.get("albumartist") or "").strip()
        likely_album = str(likelies.get("album") or "").strip()
        if (
            consensus.get("albumartist")
            and albumartist
            and likely_album
            and albumartist.casefold() not in VA_ARTISTS
        ):
            return albumartist, likely_album
        return None

    @staticmethod
    def _musicbrainz_release_ids(
        items: Sequence[Any], artist: str, album: str
    ) -> list[str]:
        """Search MusicBrainz with an artist constraint and return release IDs."""
        from beets import plugins
        from beets.metadata_plugins import SearchParams
        from beetsplug.musicbrainz import MusicBrainzPlugin

        plugin = next(
            (
                candidate
                for candidate in plugins.find_plugins()
                if isinstance(candidate, MusicBrainzPlugin)
            ),
            None,
        )
        if plugin is None:
            raise RuntimeError("MusicBrainz 插件未加载")
        query, filters = plugin.get_search_query_with_filters(
            "album", items, artist, album, False
        )
        limit = plugin.config["search_limit"].get(int)
        response = plugin.get_search_response(
            SearchParams("album", query, filters, limit)
        )
        exact_results = [
            result
            for result in response
            if BeetsReviewMatcher._release_search_result_matches(
                result, artist, album
            )
        ]
        selected_results = exact_results
        if not selected_results:
            selected_results = BeetsReviewMatcher._browse_artist_releases(
                plugin, artist, album
            )
        if not selected_results:
            selected_results = response
        return list(
            dict.fromkeys(
                str(result.get("id") or "").strip()
                for result in selected_results
                if result.get("id")
            )
        )

    @staticmethod
    def _release_search_result_matches(
        result: dict[str, Any], artist: str, album: str
    ) -> bool:
        if _normalized_match_text(result.get("title")) != _normalized_match_text(
            album
        ):
            return False
        credits = result.get("artist_credit") or result.get("artist-credit") or []
        credited_name = "".join(
            f"{credit.get('name', '')}{credit.get('joinphrase', '')}"
            for credit in credits
        )
        canonical_name = "".join(
            f"{credit.get('artist', {}).get('name', '')}"
            f"{credit.get('joinphrase', '')}"
            for credit in credits
        )
        expected = _normalized_credit(artist)
        return expected in {
            _normalized_credit(credited_name),
            _normalized_credit(canonical_name),
        }

    @staticmethod
    def _browse_artist_releases(
        plugin: Any, artist: str, album: str
    ) -> list[dict[str, Any]]:
        """Recover releases whose indexed title tokenization differs.

        MusicBrainz currently stores ``Rebirth Story5`` while the files use
        ``Rebirth Story 5``. A release search does not find that spelling
        variant, but browsing the exact artist's releases does.
        """
        expected_artist = _normalized_credit(artist)
        artist_results = plugin.mb_api.search(
            "artist", {"artist": artist}, limit=5
        )
        artist_ids = []
        for result in artist_results:
            names = {
                _normalized_credit(result.get("name")),
                _normalized_credit(result.get("sort_name")),
                *(
                    _normalized_credit(alias.get("name"))
                    for alias in result.get("aliases", [])
                ),
            }
            if expected_artist in names and result.get("id"):
                artist_ids.append(str(result["id"]))

        matches: list[dict[str, Any]] = []
        for artist_id in dict.fromkeys(artist_ids):
            offset = 0
            while True:
                releases = plugin.mb_api._browse(
                    "release", artist=artist_id, limit=100, offset=offset
                )
                matches.extend(
                    release
                    for release in releases
                    if _normalized_match_text(release.get("title"))
                    == _normalized_match_text(album)
                )
                if len(releases) < 100:
                    break
                offset += 100
        return matches

    def identify(
        self,
        source_path: str | Path,
        *,
        search_artist: str | None = None,
        search_album: str | None = None,
        release_id: str | None = None,
    ) -> dict[str, Any]:
        self.configure()
        from beets import autotag
        from beets.library import Item

        root = Path(source_path).resolve(strict=True)
        files = audio_files(root)
        if not files:
            raise ValueError("目录中没有可识别的音频文件")
        items = [Item.from_path(path) for path in files]
        search_ids = [release_id] if release_id else []
        text_terms = None
        if not search_ids:
            text_terms = self._text_search_terms(
                items, search_artist, search_album
            )
            if text_terms:
                search_ids = self._musicbrainz_release_ids(items, *text_terms)

        if search_ids:
            current_artist, current_album, proposal = autotag.tag_album(
                items,
                search_ids=search_ids,
            )
        elif text_terms:
            from beets.autotag.match import Proposal, Recommendation
            from beets.util import get_most_common_tags

            likelies, _ = get_most_common_tags(items)
            current_artist = str(likelies.get("artist") or "")
            current_album = str(likelies.get("album") or "")
            proposal = Proposal([], Recommendation.none)
        else:
            current_artist, current_album, proposal = autotag.tag_album(
                items,
                search_artist=search_artist,
                search_name=search_album,
            )
        recommendation = (
            proposal.recommendation.name.lower()
            if proposal.recommendation is not None
            else "none"
        )
        candidates = [
            serialize_album_candidate(candidate, root, current_artist or "")
            for candidate in proposal.candidates
        ]
        if not candidates:
            ensure_musicbrainz_reachable()
        if candidates and max(candidate["score"] for candidate in candidates) >= 0.9:
            recommendation = "strong"
        return {
            "signature": source_signature(root, files),
            "audio_count": len(files),
            "current_artist": current_artist or "",
            "current_album": current_album or "",
            "recommendation": recommendation,
            "candidates": candidates,
        }
