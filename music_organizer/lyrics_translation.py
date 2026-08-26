"""Structured AI translation for timestamped Japanese lyrics."""

from __future__ import annotations

import json
import re
from typing import Any

import requests

_LINE_TIMESTAMP_RE = re.compile(r"\[(\d{1,3}):([0-5]?\d(?:[.:]\d{1,3})?)\]")
_WORD_TIMESTAMP_RE = re.compile(r"<\d{1,3}:[0-5]?\d(?:[.:]\d{1,3})?>")
_METADATA_RE = re.compile(r"^\[[A-Za-z][A-Za-z0-9_-]*:")
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")


class LyricsTranslationError(RuntimeError):
    """Raised when a translation provider response cannot be used safely."""


def _visible_text(line: str) -> str:
    return _WORD_TIMESTAMP_RE.sub("", _LINE_TIMESTAMP_RE.sub("", line)).strip()


def _timestamps(line: str) -> tuple[str, ...]:
    return tuple(match.group(0) for match in _LINE_TIMESTAMP_RE.finditer(line))


def _translation_lines(content: str) -> list[dict[str, Any]]:
    raw_lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    timestamp_members: dict[str, list[str]] = {}
    for raw_line in raw_lines:
        text = _visible_text(raw_line)
        for timestamp in _timestamps(raw_line):
            timestamp_members.setdefault(timestamp, []).append(text)

    already_bilingual: set[str] = set()
    for timestamp, values in timestamp_members.items():
        nonempty = [value for value in values if value]
        if len(nonempty) < 2:
            continue
        if any(_JAPANESE_RE.search(value) for value in nonempty) and any(
            _CJK_RE.search(value) and not _JAPANESE_RE.search(value)
            for value in nonempty
        ):
            already_bilingual.add(timestamp)

    entries: list[dict[str, Any]] = []
    text_ids: dict[str, int] = {}
    for index, raw_line in enumerate(raw_lines):
        timestamps = _timestamps(raw_line)
        text = _visible_text(raw_line)
        if (
            not timestamps
            or not text
            or _METADATA_RE.match(raw_line.strip())
            or not _JAPANESE_RE.search(text)
            or any(timestamp in already_bilingual for timestamp in timestamps)
        ):
            continue
        translation_id = text_ids.setdefault(text, len(text_ids) + 1)
        entries.append(
            {
                "line_index": index,
                "id": translation_id,
                "text": text,
                "timestamps": timestamps,
            }
        )
    if not entries:
        raise ValueError("没有找到需要翻译的日文时间轴歌词，或歌词已经包含中文翻译")
    if len(text_ids) > 300:
        raise ValueError("单次最多翻译 300 条不同歌词行")
    if sum(len(text) for text in text_ids) > 30000:
        raise ValueError("单次翻译歌词正文不能超过 30000 个字符")
    return entries


def _provider_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def _system_prompt(style: str, target_language: str) -> str:
    style_prompts = {
        "literal": "忠实直译，不补充原文没有的信息。",
        "natural": "在忠实原意的前提下使用自然、简洁的中文。",
        "lyrical": "忠实保留意象和语气，译成自然的歌词中文，但不要擅自押韵或扩写。",
    }
    return (
        "你是日文歌曲歌词翻译器。"
        f"请把输入行翻译为{target_language}。{style_prompts[style]}"
        "专有名词和人名保持一致；不要输出时间戳、罗马音、解释或额外行。"
        "必须输出 JSON 对象，格式为 "
        '{"translations":[{"id":1,"text":"译文"}]}。'
        "每个输入 id 必须且只能出现一次。"
    )


def _parse_provider_content(raw: str, expected_ids: set[int]) -> dict[int, str]:
    value = raw.strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise LyricsTranslationError("AI 翻译没有返回有效 JSON") from exc
    rows = payload.get("translations") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise LyricsTranslationError("AI 翻译结果缺少 translations 列表")
    translations: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise LyricsTranslationError("AI 翻译结果包含无效行")
        try:
            translation_id = int(row.get("id"))
        except (TypeError, ValueError) as exc:
            raise LyricsTranslationError("AI 翻译结果包含无效 ID") from exc
        text = str(row.get("text") or "").strip()
        if (
            translation_id in translations
            or not text
            or "\n" in text
            or "\r" in text
            or len(text) > 2000
            or _LINE_TIMESTAMP_RE.search(text)
            or _WORD_TIMESTAMP_RE.search(text)
        ):
            raise LyricsTranslationError("AI 翻译结果行数、内容或时间戳无效")
        translations[translation_id] = text
    if set(translations) != expected_ids:
        raise LyricsTranslationError("AI 翻译返回的歌词行不完整")
    return translations


class LyricsTranslationService:
    """Translate lyric text through an OpenAI-compatible chat endpoint."""

    def __init__(self, settings: dict[str, Any], session: Any = requests):
        self.settings = settings
        self.session = session

    def translate(
        self,
        content: str,
        *,
        title: str = "",
        artist: str = "",
    ) -> dict[str, Any]:
        if not bool(self.settings.get("enabled", False)):
            raise ValueError("AI 歌词翻译尚未在配置页启用")
        api_key = str(self.settings.get("api_key") or "")
        base_url = str(self.settings.get("base_url") or "").strip()
        model = str(self.settings.get("model") or "").strip()
        if not api_key:
            raise ValueError("AI 歌词翻译 API Key 尚未配置")
        if not base_url or not model:
            raise ValueError("AI 歌词翻译接口地址或模型尚未配置")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("没有可翻译的歌词内容")
        if len(content) > 100000:
            raise ValueError("歌词内容过长，单次最多处理 100000 个字符")

        entries = _translation_lines(content)
        unique_lines = {entry["id"]: entry["text"] for entry in entries}
        requested = {
            "title": str(title or "")[:300],
            "artist": str(artist or "")[:300],
            "lines": [
                {"id": translation_id, "text": text}
                for translation_id, text in unique_lines.items()
            ],
        }
        style = str(self.settings.get("style") or "natural").lower()
        if style not in {"literal", "natural", "lyrical"}:
            style = "natural"
        target_language = str(
            self.settings.get("target_language") or "简体中文"
        )[:50]
        request_payload = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": _system_prompt(style, target_language),
                },
                {
                    "role": "user",
                    "content": "请按要求翻译以下 JSON：\n"
                    + json.dumps(requested, ensure_ascii=False),
                },
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "max_tokens": 8192,
        }
        timeout = min(max(int(self.settings.get("timeout", 120) or 120), 10), 300)
        translations: dict[int, str] | None = None
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                response = self.session.post(
                    _provider_url(base_url),
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                    timeout=timeout,
                )
                response.raise_for_status()
                body = response.json()
                raw = str(body["choices"][0]["message"]["content"] or "")
                translations = _parse_provider_content(raw, set(unique_lines))
                break
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = LyricsTranslationError("AI 翻译响应结构无效")
                last_error.__cause__ = exc
            except (requests.RequestException, LyricsTranslationError) as exc:
                last_error = exc
        if translations is None:
            if isinstance(last_error, requests.RequestException):
                status = getattr(getattr(last_error, "response", None), "status_code", None)
                suffix = f"（HTTP {status}）" if status else ""
                raise LyricsTranslationError(f"无法连接 AI 翻译服务{suffix}") from last_error
            raise LyricsTranslationError(str(last_error or "AI 翻译失败")) from last_error

        additions: dict[int, list[str]] = {}
        for entry in entries:
            translated = translations[entry["id"]]
            prefix = "".join(entry["timestamps"])
            additions.setdefault(entry["line_index"], []).append(prefix + translated)
        output: list[str] = []
        for index, raw_line in enumerate(
            content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        ):
            output.append(raw_line)
            output.extend(additions.get(index, []))
        return {
            "content": "\n".join(output),
            "translated_lines": len(entries),
            "unique_lines": len(unique_lines),
            "model": model,
            "style": style,
            "target_language": target_language,
        }
