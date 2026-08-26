import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from music_organizer.beets_review import (
    BeetsReviewMatcher,
    MusicBrainzUnavailableError,
    configure_http_proxy,
    ensure_musicbrainz_reachable,
    serialize_album_candidate,
)


class FakeDistance:
    distance = 0.016

    @staticmethod
    def items():
        return [("tracks", 0.01), ("year", 0.006)]


class Box:
    def __init__(self, **values):
        self.__dict__.update(values)


class BeetsReviewSerializationTests(unittest.TestCase):
    def test_unanimous_albumartist_overrides_differing_track_artists(self):
        from beets.library import Item

        items = [
            Item(
                artist=artist,
                albumartist="FELT",
                album="Stand Up",
                title=f"Track {index}",
            )
            for index, artist in enumerate(("NAGI☆", "舞花", "Vivienne"), 1)
        ]

        self.assertEqual(
            BeetsReviewMatcher._text_search_terms(items, None, None),
            ("FELT", "Stand Up"),
        )

    def test_manual_artist_overrides_compilation_inference(self):
        from beets.library import Item

        items = [
            Item(
                artist="Guest A",
                albumartist="Various Artists",
                album="Stand Up",
                title="Track 1",
                comp=True,
            ),
            Item(
                artist="Guest B",
                albumartist="Various Artists",
                album="Stand Up",
                title="Track 2",
                comp=True,
            ),
        ]

        self.assertEqual(
            BeetsReviewMatcher._text_search_terms(items, "FELT", "Stand Up"),
            ("FELT", "Stand Up"),
        )

    def test_explicit_various_artists_keeps_beets_compilation_search(self):
        self.assertIsNone(
            BeetsReviewMatcher._text_search_terms(
                [], "Various Artists", "Stand Up"
            )
        )

    def test_musicbrainz_text_search_always_constrains_artist(self):
        from beets import plugins
        from beetsplug.musicbrainz import MusicBrainzPlugin

        plugin = MusicBrainzPlugin()
        response = [
            {
                "id": "3a5413f6-1bc4-43d6-bbad-5ae46321300b",
                "title": "Stand Up",
                "artist_credit": [
                    {"name": "FELT", "artist": {"name": "FELT"}}
                ],
            },
            {
                "id": "05821285-7e80-438b-b3ec-b5d2c05bbec4",
                "title": "Stand Up",
                "artist_credit": [
                    {
                        "name": "Jethro Tull",
                        "artist": {"name": "Jethro Tull"},
                    }
                ],
            },
        ]
        with (
            mock.patch.object(plugins, "find_plugins", return_value=[plugin]),
            mock.patch.object(
                plugin, "get_search_response", return_value=response
            ) as search,
        ):
            release_ids = BeetsReviewMatcher._musicbrainz_release_ids(
                [], "FELT", "Stand Up"
            )

        self.assertEqual(
            release_ids, ["3a5413f6-1bc4-43d6-bbad-5ae46321300b"]
        )
        params = search.call_args.args[0]
        self.assertEqual(params.filters["artist"], "felt")
        self.assertEqual(params.filters["release"], "stand up")
        self.assertNotIn("arid", params.filters)

    def test_musicbrainz_browses_artist_for_title_spacing_variant(self):
        from beets import plugins
        from beetsplug.musicbrainz import MusicBrainzPlugin

        plugin = MusicBrainzPlugin()
        plugin.mb_api = mock.Mock()
        plugin.mb_api.search.return_value = [
            {
                "id": "174596bf-3906-49a9-a448-bc1c17092634",
                "name": "FELT",
                "sort_name": "FELT",
            }
        ]
        plugin.mb_api._browse.return_value = [
            {
                "id": "87c0a313-094d-470b-9628-0dfa451e5c4c",
                "title": "Rebirth Story5",
            }
        ]
        unrelated = [
            {
                "id": "0d852c54-dd08-4294-bf44-248bec0d1c4b",
                "title": "Die Grobschnitt Story 5",
                "artist_credit": [
                    {"name": "Grobschnitt", "artist": {"name": "Grobschnitt"}}
                ],
            }
        ]
        with (
            mock.patch.object(plugins, "find_plugins", return_value=[plugin]),
            mock.patch.object(
                plugin, "get_search_response", return_value=unrelated
            ),
        ):
            release_ids = BeetsReviewMatcher._musicbrainz_release_ids(
                [], "FELT", "Rebirth Story 5"
            )

        self.assertEqual(
            release_ids, ["87c0a313-094d-470b-9628-0dfa451e5c4c"]
        )
        plugin.mb_api.search.assert_called_once_with(
            "artist", {"artist": "FELT"}, limit=5
        )
        plugin.mb_api._browse.assert_called_once_with(
            "release",
            artist="174596bf-3906-49a9-a448-bc1c17092634",
            limit=100,
            offset=0,
        )

    def test_blank_proxy_configuration_restores_direct_connections(self):
        proxy_variables = {
            "HTTP_PROXY": "http://old-proxy:7890",
            "HTTPS_PROXY": "http://old-proxy:7890",
            "ALL_PROXY": "socks5://old-proxy:1080",
            "http_proxy": "http://old-proxy:7890",
            "https_proxy": "http://old-proxy:7890",
            "all_proxy": "socks5://old-proxy:1080",
        }
        with mock.patch.dict(os.environ, proxy_variables, clear=True):
            self.assertEqual(configure_http_proxy(""), "")
            for name in proxy_variables:
                self.assertNotIn(name, os.environ)

    def test_unreachable_musicbrainz_has_an_actionable_error(self):
        with mock.patch(
            "music_organizer.beets_review.requests.get",
            side_effect=requests.exceptions.ProxyError("proxy timed out"),
        ):
            with self.assertRaisesRegex(
                MusicBrainzUnavailableError, "无法连接 MusicBrainz"
            ):
                ensure_musicbrainz_reachable(timeout=0.01)

    def test_network_diagnostic_uses_the_configured_http_proxy(self):
        response = mock.Mock()
        with (
            mock.patch.dict("os.environ", {}, clear=True),
            mock.patch(
                "music_organizer.beets_review.requests.get",
                return_value=response,
            ) as get,
            mock.patch(
                "socket.create_connection",
                side_effect=AssertionError("must not bypass the proxy"),
            ),
        ):
            proxy = configure_http_proxy("http://proxy.local:7890")
            ensure_musicbrainz_reachable(timeout=1.25)

        get.assert_called_once()
        self.assertEqual(
            get.call_args.kwargs["proxies"],
            {"http": proxy, "https": proxy},
        )
        self.assertEqual(get.call_args.kwargs["timeout"], 1.25)
        response.raise_for_status.assert_called_once_with()

    def test_authenticated_proxy_credentials_are_url_encoded(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            proxy = configure_http_proxy(
                "http://proxy.local:7890", "user@example", "p@ss/word"
            )
            self.assertEqual(
                proxy,
                "http://user%40example:p%40ss%2Fword@proxy.local:7890",
            )

    def test_candidate_is_converted_to_stable_unicode_json_shape(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "01 第一首.flac"
            first.touch()
            (root / "cover.jpg").touch()
            item = Box(filepath=first, title="第一首")
            track = Box(
                track_id="track-id",
                title="第一首",
                artist="中文艺术家",
                medium=1,
                medium_index=1,
                length=185.25,
            )
            info = Box(
                data_source="musicbrainz",
                album_id="release-id",
                releasegroup_id="release-group-id",
                artist="中文艺术家",
                artist_credit="中文艺术家",
                album="中文专辑",
                year=2026,
                country="JP",
                label="示例唱片",
                catalognum="CAT-001",
                media="CD",
                mediums=1,
                tracks=[track],
            )
            candidate = Box(
                info=info,
                mapping={item: track},
                extra_items=[],
                extra_tracks=[],
                distance=FakeDistance(),
            )

            payload = serialize_album_candidate(candidate, root)

        self.assertEqual(payload["key"], "musicbrainz:release-id")
        self.assertEqual(payload["artist"], "中文艺术家")
        self.assertEqual(payload["album"], "中文专辑")
        self.assertEqual(payload["score"], 0.984)
        self.assertEqual(payload["tracks"][0]["local_path"], "01 第一首.flac")
        self.assertEqual(
            payload["tracks"][0]["target_path"],
            "中文艺术家/中文专辑/01 第一首.flac",
        )
        self.assertFalse(payload["multiartist"])
        self.assertEqual(payload["penalties"]["tracks"], 0.01)
        self.assertEqual(payload["local_items"][0]["track_key"], "track-id")
        self.assertEqual(payload["track_options"][0]["key"], "track-id")
        self.assertEqual(payload["auxiliary_files"], ["cover.jpg"])

    def test_release_track_id_distinguishes_repeated_recordings(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            first = root / "01.flac"
            second = root / "02.flac"
            first.touch()
            second.touch()
            first_item = Box(filepath=first, title="Same recording")
            second_item = Box(filepath=second, title="Same recording (encore)")
            first_track = Box(
                track_id="recording-id",
                release_track_id="release-track-1",
                title="Same recording",
                artist="Artist",
                medium=1,
                medium_index=1,
                length=180,
            )
            second_track = Box(
                track_id="recording-id",
                release_track_id="release-track-2",
                title="Same recording (encore)",
                artist="Artist",
                medium=1,
                medium_index=2,
                length=180,
            )
            info = Box(
                data_source="musicbrainz",
                album_id="release-id",
                artist="Artist",
                artist_credit="Artist",
                album="Album",
                mediums=1,
                tracks=[first_track, second_track],
            )
            candidate = Box(
                info=info,
                mapping={first_item: first_track, second_item: second_track},
                extra_items=[],
                extra_tracks=[],
                distance=FakeDistance(),
            )

            payload = serialize_album_candidate(candidate, root)

        self.assertEqual(
            [track["key"] for track in payload["track_options"]],
            ["release-track-1", "release-track-2"],
        )
        self.assertEqual(
            [track["track_key"] for track in payload["tracks"]],
            ["release-track-1", "release-track-2"],
        )
        self.assertEqual(
            [track["track_id"] for track in payload["track_options"]],
            ["recording-id", "recording-id"],
        )

    def test_recovers_extra_items_by_title_and_sorts_by_disc_track(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            disc_two_path = root / "01 Opening.flac"
            disc_one_path = root / "09 Finale.flac"
            disc_two_path.touch()
            disc_one_path.touch()
            disc_two_item = Box(
                filepath=disc_two_path, title="Opening Arrange", artist="Artist",
                length=120,
            )
            disc_one_item = Box(
                filepath=disc_one_path, title="Finale", artist="Artist", length=180,
            )
            disc_one_track = Box(
                track_id="disc-one", title="Finale", artist="Artist",
                medium=1, medium_index=9, length=180,
            )
            disc_two_track = Box(
                track_id="disc-two", title="Opening Arrange", artist="Artist",
                medium=2, medium_index=1, length=120,
            )
            info = Box(
                data_source="musicbrainz", album_id="release-id", artist="Artist",
                artist_credit="Artist", album="Album", mediums=2,
                tracks=[disc_two_track, disc_one_track],
            )
            candidate = Box(
                info=info, mapping={},
                extra_items=[disc_two_item, disc_one_item],
                extra_tracks=[disc_one_track, disc_two_track],
                distance=FakeDistance(),
            )

            payload = serialize_album_candidate(candidate, root)

        self.assertEqual(payload["extra_items"], [])
        self.assertEqual(payload["extra_tracks"], [])
        self.assertEqual(
            [(item["disc"], item["track"]) for item in payload["local_items"]],
            [(1, 9), (2, 1)],
        )
        self.assertTrue(
            all(item["match_source"] == "recovered" for item in payload["local_items"])
        )
        self.assertTrue(
            all(item["match_score"] >= 0.9 for item in payload["local_items"])
        )

    def test_exact_artist_credit_does_not_count_as_artist_mismatch(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            audio = root / "01.flac"
            audio.touch()
            item = Box(filepath=audio, title="猫日")
            track = Box(
                track_id="track-id", title="猫日", artist="suis",
                medium=1, medium_index=1, length=230,
            )
            info = Box(
                data_source="musicbrainz", album_id="release-id",
                releasegroup_id="group-id", artist="suis",
                artist_credit="suis from ヨルシカ", album="猫日",
                year=2026, country="XW", label="", catalognum="",
                media="Digital Media", mediums=1, tracks=[track],
            )
            distance = Box(distance=0.266106)
            distance.items = lambda: [("artist", 0.218487), ("label", 0.047619)]
            candidate = Box(
                info=info, mapping={item: track}, extra_items=[],
                extra_tracks=[], distance=distance,
            )

            payload = serialize_album_candidate(
                candidate, root, current_artist="suis from ヨルシカ"
            )

        self.assertTrue(payload["artist_credit_match"])
        self.assertEqual(payload["raw_score"], 0.733894)
        self.assertEqual(payload["score"], 0.952381)


if __name__ == "__main__":
    unittest.main()
