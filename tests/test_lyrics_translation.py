import json

import pytest

from music_organizer.lyrics_translation import (
    LyricsTranslationError,
    LyricsTranslationService,
)


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": json.dumps(self.content, ensure_ascii=False)}}]
        }


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(self.responses.pop(0))


def settings(**overrides):
    value = {
        "enabled": True,
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "api_key": "secret-key",
        "target_language": "简体中文",
        "style": "natural",
        "timeout": 120,
    }
    value.update(overrides)
    return value


def test_translation_preserves_metadata_word_timing_and_existing_bilingual_lines():
    session = FakeSession(
        [{"translations": [{"id": 1, "text": "已经不想再见到你"}]}]
    )
    content = "\n".join(
        [
            "[ti:テスト]",
            "[00:01.00]<00:01.00>もう君のことを<00:02.00>見たくない",
            "[00:03.00]こんにちは",
            "[00:03.00]你好",
            "[00:04.00]instrumental",
        ]
    )

    result = LyricsTranslationService(settings(), session=session).translate(
        content, title="曲名", artist="歌手"
    )

    assert result["translated_lines"] == 1
    assert "[ti:テスト]" in result["content"]
    assert "[00:01.00]<00:01.00>もう君のことを<00:02.00>見たくない" in result["content"]
    assert "[00:01.00]已经不想再见到你" in result["content"]
    assert result["content"].count("[00:03.00]你好") == 1
    url, request = session.calls[0]
    assert url == "https://api.deepseek.com/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret-key"
    assert request["json"]["response_format"] == {"type": "json_object"}
    assert "<00:01.00>" not in request["json"]["messages"][1]["content"]


def test_translation_retries_invalid_json_and_rejects_incomplete_ids():
    incomplete = {"translations": []}
    session = FakeSession([incomplete, incomplete])

    with pytest.raises(LyricsTranslationError, match="不完整"):
        LyricsTranslationService(settings(), session=session).translate(
            "[00:01.00]こんにちは"
        )

    assert len(session.calls) == 2


def test_translation_requires_enabled_configuration_and_japanese_timed_lines():
    with pytest.raises(ValueError, match="尚未在配置页启用"):
        LyricsTranslationService(settings(enabled=False)).translate(
            "[00:01.00]こんにちは"
        )
    with pytest.raises(ValueError, match="没有找到需要翻译"):
        LyricsTranslationService(settings()).translate("[00:01.00]hello")
