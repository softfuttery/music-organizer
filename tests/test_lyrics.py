from __future__ import annotations

import base64
import json
import sqlite3
import zlib

import pytest

from music_organizer.lyrics import (
    INSTRUMENTAL_LYRIC,
    LyricsProviderError,
    LyricsSearchService,
    _krc_to_lrc,
    _netease_yrc_to_lrc,
    candidate_match,
    combine_lyrics,
    embed_imported_lyrics,
    lyric_is_synced,
    lyric_quality,
    normalize_lrc_timestamps,
    normalize_lyric_decision,
    write_embedded_lyrics,
)
from music_organizer.qrc import qrc_to_lrc
from music_organizer.review import ReviewRepository, utc_now


class FakeResponse:
    def __init__(self, payload=None, text=""):
        self.payload = payload
        self.text = text

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def request(self, method, url, **kwargs):
        del method, kwargs
        if "lyrics.kugou.com/search" in url:
            return FakeResponse(
                {
                    "candidates": [
                        {
                            "id": "123",
                            "accesskey": "ABCDEF",
                            "song": "Hearts Glow",
                            "singer": "Hana Hope",
                            "duration": 220000,
                        }
                    ]
                }
            )
        if "lyrics.kugou.com/download" in url:
            return FakeResponse(
                {
                    "content": "WzAwOjAxLjAwXUhlbGxvClswMDowMi4wMF1Xb3JsZA=="
                }
            )
        raise AssertionError(url)


def test_normalize_lyrics_and_detect_timestamps():
    content = combine_lyrics("[00:01.00]原文\r\n", "[00:01.00]Translation")
    decision = normalize_lyric_decision(
        {
            "status": "selected",
            "source": "kugou",
            "provider_id": "abc",
            "content": content,
        }
    )
    assert decision["synced"] is True
    assert lyric_is_synced(content)
    assert decision["digest"]
    assert decision["quality"]["synced"] is True


def test_legacy_colon_fraction_timestamps_are_normalized_before_save():
    content = "[00:16:40]Original\n[05:03:00]<05:03:25>Ending"

    assert normalize_lrc_timestamps(content) == (
        "[00:16.40]Original\n[05:03.00]<05:03.25>Ending"
    )
    decision = normalize_lyric_decision(
        {
            "status": "selected",
            "source": "kugou",
            "content": content,
        }
    )
    assert decision["content"] == (
        "[00:16.40]Original\n[05:03.00]<05:03.25>Ending"
    )
    assert decision["quality"]["timed_line_count"] == 2


def test_candidate_match_recovers_circle_and_vocalist_role_difference():
    match = candidate_match(
        "Raindrops",
        "FELT",
        241,
        {
            "title": "Raindrops",
            "artist": "舞花",
            "album": "Darkness Brightness",
            "duration": 241,
        },
        query_album="Darkness Brightness",
    )

    assert match["score"] >= 0.9
    assert match["title"] == 1
    assert match["artist"] == 0
    assert match["album"] == 1
    assert match["artist_role_mismatch"] is True


def test_candidate_match_does_not_boost_an_unrelated_album():
    match = candidate_match(
        "Raindrops",
        "FELT",
        241,
        {
            "title": "Raindrops",
            "artist": "舞花",
            "album": "Rebirth Story5",
            "duration": 241,
        },
        query_album="Darkness Brightness",
    )

    assert match["score"] == pytest.approx(0.7)
    assert match["artist_role_mismatch"] is False


def test_lyric_quality_reports_word_timing_and_translation_coverage():
    quality = lyric_quality(
        "[00:20.67]<00:20.67>落ちる<00:21.00>\n"
        "[00:30.59]窓につたい流れ続ける\n"
        "[00:20.670]飘落的雨滴\n"
        "[00:30.590]不断沿窗扉流下"
    )

    assert quality == {
        "synced": True,
        "word_timed": True,
        "bilingual": True,
        "translation_coverage": 1.0,
        "timed_line_count": 4,
        "japanese_line_count": 2,
        "translated_line_count": 2,
    }


def test_kugou_search_and_fetch_with_eslyric_compatible_source():
    service = LyricsSearchService(session=FakeSession())
    result = service.search(
        "Hearts Glow", "Hana Hope", 220, providers=["kugou"]
    )
    candidate = result["candidates"][0]
    assert candidate["provider_id"] == "123"
    assert candidate["score"] == pytest.approx(1.0)
    fetched = service.fetch(candidate)
    assert fetched["synced"] is True
    assert "[00:02.00]World" in fetched["content"]


def test_search_orders_providers_netease_then_qq_then_kugou(monkeypatch):
    service = LyricsSearchService(session=FakeSession())

    def candidate(provider_id, title):
        return [{
            "provider_id": provider_id,
            "title": title,
            "artist": "Artist",
            "album": "Album",
            "duration": 180,
        }]

    monkeypatch.setattr(service, "_search_netease", lambda *_: candidate("n", "Song"))
    monkeypatch.setattr(service, "_search_qqmusic", lambda *_: candidate("q", "Song"))
    monkeypatch.setattr(service, "_search_kugou", lambda *_: candidate("k", "Song"))

    result = service.search(
        "Song", "Artist", 180, ["kugou", "qqmusic", "netease"]
    )

    assert [value["source"] for value in result["candidates"]] == [
        "netease", "qqmusic", "kugou"
    ]


def test_kugou_krc_decode_failure_falls_back_to_lrc():
    class KugouFallbackSession:
        def request(self, method, url, **kwargs):
            del method
            assert "lyrics.kugou.com/download" in url
            if kwargs["params"]["fmt"] == "krc":
                return FakeResponse({"content": base64.b64encode(b"krc1broken").decode()})
            return FakeResponse(
                {"content": base64.b64encode("[00:01.00]普通歌词".encode()).decode()}
            )

    service = LyricsSearchService(session=KugouFallbackSession())
    fetched = service.fetch(
        {
            "source": "kugou",
            "provider_id": "123",
            "access_key": "ABCDEF",
            "title": "Song",
            "artist": "Artist",
        }
    )
    assert fetched["content"] == "[00:01.00]普通歌词"


def test_netease_empty_current_search_falls_back_to_legacy():
    class NeteaseSearchFallbackSession:
        def request(self, method, url, **kwargs):
            del method, kwargs
            if "eapi/batch" in url:
                return FakeResponse({"code": 200, "data": {"resources": []}})
            if "/api/search/get" in url:
                return FakeResponse(
                    {
                        "result": {
                            "songs": [
                                {
                                    "id": 42,
                                    "name": "Song",
                                    "artists": [{"name": "Artist"}],
                                    "album": {"name": "Album"},
                                    "duration": 180000,
                                }
                            ]
                        }
                    }
                )
            raise AssertionError(url)

    candidate = LyricsSearchService(session=NeteaseSearchFallbackSession()).search(
        "Song", "Artist", 180, ["netease"]
    )["candidates"][0]
    assert candidate["provider_id"] == "42"
    assert candidate["netease_mode"] == "lrc"


def test_netease_empty_current_fetch_falls_back_to_legacy():
    class NeteaseFetchFallbackSession:
        def request(self, method, url, **kwargs):
            del method, kwargs
            if "eapi/song/lyric" in url:
                return FakeResponse({"code": 200, "lrc": {"version": 0, "lyric": ""}})
            if "/api/song/lyric" in url:
                return FakeResponse(
                    {"code": 200, "lrc": {"version": 2, "lyric": "[00:01.00]旧接口歌词"}}
                )
            raise AssertionError(url)

    fetched = LyricsSearchService(session=NeteaseFetchFallbackSession()).fetch(
        {
            "source": "netease",
            "provider_id": "42",
            "netease_mode": "yrc",
            "title": "Song",
            "artist": "Artist",
        }
    )
    assert fetched["content"] == "[00:01.00]旧接口歌词"


def test_qqmusic_current_search_fetches_qrc_and_translation(monkeypatch):
    module = "music.musichallSong.PlayLyricInfo.GetPlayLyricInfo"

    class QQSession:
        def request(self, method, url, **kwargs):
            del method
            if "fcg_search_pc_lrc" in url:
                return FakeResponse(
                    text=(
                        "<root><songname><![CDATA[Song]]></songname>"
                        "<singer><![CDATA[Artist]]></singer>"
                        '<songinfo id="123"><name><![CDATA[Song]]></name>'
                        "<singername><![CDATA[Artist]]></singername>"
                        "<albumname><![CDATA[Album]]></albumname></songinfo></root>"
                    )
                )
            if "musicu.fcg" in url:
                assert '"trans":1' in kwargs["data"]
                return FakeResponse(
                    {
                        "code": 0,
                        module: {
                            "code": 0,
                            "data": {"songID": 123, "lyric": "original", "trans": "translated"},
                        },
                    }
                )
            raise AssertionError(url)

    monkeypatch.setattr(
        "music_organizer.lyrics.decrypt_qrc_to_lrc",
        lambda value: {
            "original": "[00:01.00]原文",
            "translated": "[00:01.00]翻译",
        }.get(value, ""),
    )
    service = LyricsSearchService(session=QQSession())
    candidate = service.search("Song", "Artist", 180, ["qqmusic"])["candidates"][0]
    assert candidate["provider_id"] == "123"
    assert candidate["qq_mode"] == "qrc"
    fetched = service.fetch(candidate)
    assert "[00:01.00]原文" in fetched["content"]
    assert "[00:01.00]翻译" in fetched["content"]


def test_qqmusic_qrc_failure_falls_back_to_best_legacy_candidate():
    module = "music.musichallSong.PlayLyricInfo.GetPlayLyricInfo"

    class QQFallbackSession:
        def request(self, method, url, **kwargs):
            del method, kwargs
            if "musicu.fcg" in url:
                return FakeResponse({"code": 0, module: {"code": 1, "data": {}}})
            if "client_search_cp" in url:
                return FakeResponse(
                    {
                        "data": {
                            "song": {
                                "list": [
                                    {
                                        "songmid": "legacy-mid",
                                        "songname": "Song",
                                        "singer": [{"name": "Artist"}],
                                        "albumname": "Album",
                                        "interval": 180,
                                    }
                                ]
                            }
                        }
                    }
                )
            if "fcg_query_lyric_new" in url:
                return FakeResponse(
                    {"lyric": base64.b64encode("[00:01.00]普通歌词".encode()).decode()}
                )
            raise AssertionError(url)

    fetched = LyricsSearchService(session=QQFallbackSession()).fetch(
        {
            "source": "qqmusic",
            "provider_id": "123",
            "qq_mode": "qrc",
            "title": "Song",
            "artist": "Artist",
            "album": "Album",
            "request_duration": 180,
        }
    )
    assert fetched["content"] == "[00:01.00]普通歌词"


def test_netease_version_one_metadata_stub_is_rejected():
    class NeteaseSession:
        def request(self, method, url, **kwargs):
            del method, url, kwargs
            return FakeResponse(
                {"code": 200, "lrc": {"version": 1, "lyric": "[00:00.00]作曲：某人"}}
            )

    service = LyricsSearchService(session=NeteaseSession())
    with pytest.raises(LyricsProviderError, match="没有正文歌词"):
        service.fetch({"source": "netease", "provider_id": "123"})


def test_qrc_word_timing_is_converted_to_enhanced_lrc():
    converted = qrc_to_lrc(
        '<QrcInfos><Lyric_1 LyricType="1" '
        'LyricContent="[1000,2000]你(1000,500)好(1500,500)"/></QrcInfos>'
    )
    assert converted == "[00:01.00]<00:01.00>你<00:01.50>好<00:02.00>"


def test_krc_word_timing_and_translation_are_converted_to_enhanced_lrc():
    language = base64.b64encode(
        json.dumps(
            {"content": [{"type": 1, "lyricContent": [["翻译"]]}]},
            ensure_ascii=False,
        ).encode("utf-8")
    ).decode("ascii")
    text = f"[language:{language}]\n[1000,1000]<0,500,0>原<500,500,0>文"
    key = b"@Gaw^2tGQ61-\xce\xd2ni"
    compressed = zlib.compress(text.encode("utf-8"))
    encrypted = bytes(byte ^ key[index % len(key)] for index, byte in enumerate(compressed))
    converted = _krc_to_lrc(b"krc1" + encrypted)
    assert "[00:01.00]<00:01.00>原<00:01.50>文<00:02.00>" in converted
    assert "[00:01.00]翻译" in converted


def test_yrc_word_timing_prefers_yrc_translation():
    lyric, translation = _netease_yrc_to_lrc(
        {
            "yrc": {"lyric": "[1000,1000](1000,500,0)原(1500,500,0)文"},
            "ytlrc": {"lyric": "[00:01.000]翻译"},
            "tlyric": {"lyric": "[00:01.000]旧翻译"},
        }
    )
    assert lyric == "[00:01.00]<00:01.00>原<00:01.50>文<00:02.00>"
    assert translation == "[00:01.000]翻译"


def test_write_embedded_lyrics_verifies_round_trip(monkeypatch, tmp_path):
    import mutagen

    class FakeAudio:
        def __init__(self):
            self.tags = {}
            self.saved = False

        def add_tags(self):
            self.tags = {}

        def save(self):
            self.saved = True

    audio = FakeAudio()
    monkeypatch.setattr(mutagen, "File", lambda *_args, **_kwargs: audio)
    result = write_embedded_lyrics(
        tmp_path / "track.flac", "[00:01.00]verified"
    )
    assert audio.saved is True
    assert audio.tags["LYRICS"] == ["[00:01.00]verified"]
    assert result["tag"] == "LYRICS"


def test_embed_imported_lyrics_stays_inside_library(monkeypatch, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    target = library / "song.flac"
    target.write_bytes(b"test")
    monkeypatch.setattr(
        "music_organizer.lyrics.write_embedded_lyrics",
        lambda path, content: {"tag": "LYRICS", "synced": True, "digest": "ok"},
    )
    result = embed_imported_lyrics(
        [{"source": "disc/song.flac", "destination": str(target)}],
        {
            "disc/song.flac": {
                "status": "selected",
                "source": "netease",
                "provider_id": "1",
                "content": "[00:01.00]line",
            }
        },
        library,
    )
    assert result[0]["status"] == "embedded"
    assert result[0]["destination"] == str(target.resolve())


def test_instrumental_decision_embeds_fixed_synced_lyric(monkeypatch, tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    target = library / "song.flac"
    target.write_bytes(b"test")
    written = {}

    def fake_write(path, content):
        written["path"] = path
        written["content"] = content
        return {"tag": "LYRICS", "synced": True, "digest": "ok"}

    monkeypatch.setattr("music_organizer.lyrics.write_embedded_lyrics", fake_write)
    normalized = normalize_lyric_decision({"status": "instrumental"})
    assert normalized["content"] == INSTRUMENTAL_LYRIC
    result = embed_imported_lyrics(
        [{"source": "song.flac", "destination": str(target)}],
        {"song.flac": {"status": "instrumental"}},
        library,
    )
    assert written["content"] == "[00:05.00]纯音乐，请欣赏"
    assert result[0]["status"] == "embedded"
    assert result[0]["provider"] == "instrumental"


def test_repository_persists_manual_lyric_decision(tmp_path):
    database = tmp_path / "review.sqlite3"
    repository = ReviewRepository(database)
    repository.initialize()
    now = utc_now()
    with sqlite3.connect(database) as conn:
        batch_id = conn.execute(
            "INSERT INTO review_batches(status, label, created_at, updated_at) "
            "VALUES ('needs_review', '', ?, ?)",
            (now, now),
        ).lastrowid
        item_id = conn.execute(
            "INSERT INTO review_items(batch_id, source_path, status, created_at, updated_at) "
            "VALUES (?, ?, 'needs_review', ?, ?)",
            (batch_id, str(tmp_path), now, now),
        ).lastrowid
    item = repository.save_lyric_decision(
        item_id,
        "disc/song.flac",
        {
            "status": "selected",
            "source": "qqmusic",
            "provider_id": "mid",
            "content": "[00:01.00]line",
        },
    )
    assert item["lyrics"]["disc/song.flac"]["source"] == "qqmusic"
