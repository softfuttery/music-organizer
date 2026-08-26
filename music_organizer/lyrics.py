"""Lyrics provider adapters, persistence validation and embedded tag writing."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit
from xml.etree import ElementTree

import requests
from Crypto.Cipher import AES

from .qrc import decrypt_qrc_to_lrc

LYRIC_PROVIDERS = {"kugou", "qqmusic", "netease"}
LYRIC_PROVIDER_PRIORITY = {"netease": 0, "qqmusic": 1, "kugou": 2}
MAX_LYRIC_LENGTH = 512_000
MAX_SEARCH_RESULTS_PER_PROVIDER = 12
INSTRUMENTAL_LYRIC = "[00:05.00]纯音乐，请欣赏"
_TIMESTAMP_RE = re.compile(r"\[(?:\d{1,3}:)?\d{1,2}:\d{1,2}(?:[.:]\d{1,3})?\]")
_TIMESTAMP_PARTS_RE = re.compile(
    r"^\[(?:(\d{1,3}):)?(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]$"
)
_WORD_TIMESTAMP_RE = re.compile(r"<(?:(?:\d{1,3}:)?\d{1,2}:\d{1,2})(?:[.:]\d{1,3})?>")
_LEGACY_LINE_TIMESTAMP_RE = re.compile(
    r"\[(\d{1,3}):([0-5]?\d):(\d{1,3})\]"
)
_LEGACY_WORD_TIMESTAMP_RE = re.compile(
    r"<(\d{1,3}):([0-5]?\d):(\d{1,3})>"
)
_JAPANESE_RE = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_KRC_KEY = b"@Gaw^2tGQ61-\xce\xd2ni"
_NETEASE_EAPI_KEY = b"e82ckenh8dichen8"
_NETEASE_EAPI_SEPARATOR = "-36cd479b6b5-"


class LyricsProviderError(RuntimeError):
    """Raised when a remote lyrics provider cannot satisfy a request."""


def _safe_text(value: Any, limit: int = 500) -> str:
    return str(value or "").strip()[:limit]


def _duration_seconds(value: Any) -> float:
    try:
        duration = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(duration, 86_400.0))


def _decode_base64_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return base64.b64decode(text).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""


def _decode_base64_bytes(value: Any) -> bytes:
    text = str(value or "").strip()
    if not text:
        return b""
    try:
        return base64.b64decode(text)
    except (ValueError, TypeError):
        return b""


def _format_lrc_time(milliseconds: int) -> str:
    value = abs(int(milliseconds))
    minutes, remainder = divmod(value, 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{remainder // 10:02d}"


def _word_timed_to_lrc(value: Any, *, yrc: bool) -> str:
    """Convert YRC/KRC word timing syntax into enhanced LRC."""
    result: list[str] = []
    line_pattern = re.compile(r"^\[(\d+),(\d+)\](.*)$")
    word_pattern = re.compile(
        r"\((\d+),(\d+),(\d+)\)(.*?)(?=\(\d+,\d+,\d+\)|$)"
        if yrc
        else r"<(\d+),(\d+),(\d+)>(.*?)(?=<\d+,\d+,\d+>|$)"
    )
    for raw_line in str(value or "").replace("\\n", "\n").splitlines():
        line = raw_line.strip()
        match = line_pattern.match(line)
        if not match:
            if re.match(r"^\[(?:ar|ti|al|by|offset):", line, flags=re.I):
                result.append(line)
            continue
        line_start = int(match.group(1))
        converted = f"[{_format_lrc_time(line_start)}]<{_format_lrc_time(line_start)}>"
        for word in word_pattern.finditer(match.group(3)):
            start = int(word.group(1))
            if not yrc:
                start += line_start
            converted += word.group(4)
            converted += f"<{_format_lrc_time(start + int(word.group(2)))}>"
        if converted.endswith(">") and converted.count(">") > 1:
            result.append(converted)
        elif match.group(3).strip():
            result.append(f"[{_format_lrc_time(line_start)}]{match.group(3).strip()}")
    return "\n".join(result).strip()


def _krc_to_lrc(value: bytes) -> str:
    if len(value) < 4 or value[:4] != b"krc1":
        return value.decode("utf-8", errors="replace").strip()
    decrypted = bytes(
        byte ^ _KRC_KEY[index % len(_KRC_KEY)]
        for index, byte in enumerate(value[4:])
    )
    try:
        text = zlib.decompress(decrypted).decode("utf-8")
    except (UnicodeDecodeError, zlib.error) as exc:
        raise LyricsProviderError("酷狗逐字歌词解码失败") from exc
    plain = _word_timed_to_lrc(text, yrc=False)
    translations: list[list[str]] = []
    language = re.search(r"^\[language:(.+)\]$", text, flags=re.MULTILINE)
    if language:
        try:
            payload = json.loads(base64.b64decode(language.group(1)).decode("utf-8"))
            translations = next(
                (
                    item.get("lyricContent") or []
                    for item in payload.get("content") or []
                    if item.get("type") == 1
                ),
                [],
            )
        except (ValueError, TypeError, UnicodeDecodeError):
            translations = []
    if not translations:
        return plain
    lines = plain.splitlines()
    timed_indexes = [index for index, line in enumerate(lines) if _TIMESTAMP_RE.match(line)]
    additions: dict[int, str] = {}
    for position, values in enumerate(translations):
        if position >= len(timed_indexes) or not values:
            continue
        translated = str(values[0] or "").strip()
        if translated and translated != "//":
            timestamp = _TIMESTAMP_RE.match(lines[timed_indexes[position]])
            if timestamp:
                additions[timed_indexes[position]] = f"{timestamp.group(0)}{translated}"
    merged: list[str] = []
    for index, line in enumerate(lines):
        merged.append(line)
        if index in additions:
            merged.append(additions[index])
    return "\n".join(merged).strip()


def _netease_eapi_params(path: str, payload: Mapping[str, Any]) -> dict[str, str]:
    text = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.md5(
        f"nobody{path}use{text}md5forencrypt".encode("utf-8")
    ).hexdigest()
    data = f"{path}{_NETEASE_EAPI_SEPARATOR}{text}{_NETEASE_EAPI_SEPARATOR}{digest}"
    encoded = data.encode("utf-8")
    padding = 16 - len(encoded) % 16
    encrypted = AES.new(_NETEASE_EAPI_KEY, AES.MODE_ECB).encrypt(
        encoded + bytes([padding]) * padding
    )
    return {"params": encrypted.hex().upper()}


def _netease_yrc_to_lrc(payload: Mapping[str, Any]) -> tuple[str, str]:
    yrc = str((payload.get("yrc") or {}).get("lyric") or "")
    lyric = _word_timed_to_lrc(yrc, yrc=True) if yrc else str(
        (payload.get("lrc") or {}).get("lyric") or ""
    )
    translation = str(
        (payload.get("ytlrc") or {}).get("lyric")
        or (payload.get("tlyric") or {}).get("lyric")
        or ""
    )
    lyric = re.sub(r"^\{[^\r\n]*\}\s*", "", lyric, flags=re.MULTILINE).strip()
    translation = re.sub(
        r"^\{[^\r\n]*\}\s*", "", translation, flags=re.MULTILINE
    ).strip()
    return lyric, translation


def _lyric_has_usable_content(value: str) -> bool:
    credit = re.compile(
        r"^(?:作词|作詞|作曲|编曲|編曲|制作人|製作人|混音|录音|錄音|发行|發行)\s*[:：]"
    )
    for raw_line in str(value or "").splitlines():
        line = re.sub(r"\[[^\]]+\]", "", raw_line)
        line = re.sub(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>", "", line).strip()
        if line and line != "//" and not credit.match(line):
            return True
    return False


def normalize_lrc_timestamps(value: Any) -> str:
    """Convert legacy [mm:ss:cc] timestamps to unambiguous standard LRC."""

    def replace(match: re.Match[str], opening: str, closing: str) -> str:
        minutes, seconds, fraction = match.groups()
        return (
            f"{opening}{int(minutes):02d}:{int(seconds):02d}."
            f"{fraction}{closing}"
        )

    text = str(value or "")
    text = _LEGACY_LINE_TIMESTAMP_RE.sub(
        lambda match: replace(match, "[", "]"), text
    )
    return _LEGACY_WORD_TIMESTAMP_RE.sub(
        lambda match: replace(match, "<", ">"), text
    )


def combine_lyrics(original: Any, translation: Any = "") -> str:
    """Return normalized LRC text while preserving timestamped translations."""
    values = []
    for raw in (original, translation):
        text = normalize_lrc_timestamps(raw)
        text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
        if text and text not in values:
            values.append(text)
    result = "\n".join(values).strip()
    if len(result.encode("utf-8")) > MAX_LYRIC_LENGTH:
        raise ValueError("歌词内容超过 500 KB 限制")
    return result


def lyric_is_synced(content: str) -> bool:
    return bool(_TIMESTAMP_RE.search(str(content or "")))


def lyric_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _timestamp_identity(value: str) -> int | str:
    value = normalize_lrc_timestamps(value)
    match = _TIMESTAMP_PARTS_RE.match(value)
    if not match:
        return value
    hours, minutes, seconds, fraction = match.groups()
    milliseconds = int((fraction or "0").ljust(3, "0"))
    return (
        ((int(hours or 0) * 60 + int(minutes)) * 60 + int(seconds)) * 1000
        + milliseconds
    )


def lyric_quality(content: str) -> dict[str, Any]:
    """Describe usable lyric features without conflating them with track matching."""
    text = normalize_lrc_timestamps(content)
    timestamp_members: dict[int | str, list[str]] = {}
    timed_line_count = 0
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        timestamps = _TIMESTAMP_RE.findall(raw_line)
        if not timestamps:
            continue
        visible = _WORD_TIMESTAMP_RE.sub("", _TIMESTAMP_RE.sub("", raw_line)).strip()
        if not visible:
            continue
        timed_line_count += 1
        for timestamp in timestamps:
            timestamp_members.setdefault(_timestamp_identity(timestamp), []).append(visible)

    japanese_timestamps = 0
    translated_timestamps = 0
    for values in timestamp_members.values():
        has_japanese = any(_JAPANESE_RE.search(value) for value in values)
        if not has_japanese:
            continue
        japanese_timestamps += 1
        if any(
            _CJK_RE.search(value) and not _JAPANESE_RE.search(value)
            for value in values
        ):
            translated_timestamps += 1

    coverage = (
        round(translated_timestamps / japanese_timestamps, 4)
        if japanese_timestamps
        else 0.0
    )
    return {
        "synced": bool(timestamp_members),
        "word_timed": bool(_WORD_TIMESTAMP_RE.search(text)),
        "bilingual": translated_timestamps > 0,
        "translation_coverage": coverage,
        "timed_line_count": timed_line_count,
        "japanese_line_count": japanese_timestamps,
        "translated_line_count": translated_timestamps,
    }


def _similarity(query: str, value: str) -> float:
    left = " ".join(query.casefold().split())
    right = " ".join(value.casefold().split())
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def candidate_match(
    query_title: str,
    query_artist: str,
    query_duration: float,
    candidate: Mapping[str, Any],
    *,
    query_album: str = "",
    artist_aliases: Iterable[str] | None = None,
) -> dict[str, Any]:
    title_score = _similarity(query_title, _safe_text(candidate.get("title")))
    alias_values = (
        artist_aliases
        if isinstance(artist_aliases, Sequence) and not isinstance(artist_aliases, (str, bytes))
        else []
    )
    aliases = [query_artist, *alias_values]
    aliases = [_safe_text(value, 300) for value in aliases if _safe_text(value, 300)]
    artist_score = max(
        (_similarity(value, _safe_text(candidate.get("artist"))) for value in aliases),
        default=0.0,
    )
    album_score = (
        _similarity(query_album, _safe_text(candidate.get("album")))
        if query_album and candidate.get("album")
        else None
    )
    score = title_score * 0.7 + artist_score * 0.3
    candidate_duration = _duration_seconds(candidate.get("duration"))
    duration_score = None
    duration_delta = None
    if query_duration and candidate_duration:
        duration_delta = abs(query_duration - candidate_duration)
        duration_score = max(0.0, 1.0 - min(duration_delta, 30.0) / 30.0)
        score *= max(0.35, 1.0 - min(duration_delta, 30.0) / 40.0)

    artist_role_mismatch = bool(
        title_score >= 0.9
        and album_score is not None
        and album_score >= 0.85
        and artist_score < 0.5
    )
    if artist_role_mismatch:
        # Track artists are often vocalists while local album artists are circles/groups.
        # Exact title+album evidence is strong enough to recover confidence, but the
        # mismatch remains explicit so the UI can ask for a quick manual check.
        if duration_score is None:
            role_adjusted = title_score * 0.75 + album_score * 0.2 + artist_score * 0.05
        else:
            role_adjusted = (
                title_score * 0.65
                + album_score * 0.2
                + duration_score * 0.1
                + artist_score * 0.05
            )
        score = max(score, role_adjusted)

    return {
        "score": round(max(0.0, min(score, 1.0)), 4),
        "title": round(title_score, 4),
        "artist": round(artist_score, 4),
        "album": round(album_score, 4) if album_score is not None else None,
        "duration": round(duration_score, 4) if duration_score is not None else None,
        "duration_delta": round(duration_delta, 2) if duration_delta is not None else None,
        "artist_role_mismatch": artist_role_mismatch,
    }


def candidate_score(
    query_title: str,
    query_artist: str,
    query_duration: float,
    candidate: Mapping[str, Any],
    *,
    query_album: str = "",
    artist_aliases: Iterable[str] | None = None,
) -> float:
    return float(
        candidate_match(
            query_title,
            query_artist,
            query_duration,
            candidate,
            query_album=query_album,
            artist_aliases=artist_aliases,
        )["score"]
    )


def _proxy_url(settings: Mapping[str, Any]) -> str:
    raw = _safe_text(settings.get("proxy_url"), 2_000).rstrip("/")
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("歌词搜索代理必须是有效的 http:// 或 https:// 地址")
    hostname = parsed.hostname
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    username = _safe_text(settings.get("proxy_username"), 256)
    password = _safe_text(settings.get("proxy_password"), 512)
    userinfo = ""
    if username or password:
        userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return urlunsplit((parsed.scheme, userinfo + hostname, parsed.path, "", ""))


class LyricsSearchService:
    """Small server-side adapter for the three ESLyric-compatible sources."""

    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
        *,
        session: requests.Session | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.session = session or requests.Session()
        self.timeout = max(2.0, min(float(timeout), 30.0))
        proxy = _proxy_url(settings or {})
        self.proxies = {"http": proxy, "https": proxy} if proxy else None

    def _json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                data=data,
                headers=dict(headers or {}),
                timeout=self.timeout,
                proxies=self.proxies,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise LyricsProviderError(f"歌词源返回 HTTP {status}") from exc
        except (requests.RequestException, ValueError) as exc:
            # Do not surface proxy URLs because they can contain credentials.
            raise LyricsProviderError(
                f"歌词源请求失败（{type(exc).__name__}）"
            ) from exc
        if not isinstance(payload, dict):
            raise LyricsProviderError("歌词源返回了无效数据")
        return payload

    def _text(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                data=data,
                headers=dict(headers or {}),
                timeout=self.timeout,
                proxies=self.proxies,
            )
            response.raise_for_status()
            return str(response.text or "")
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", "unknown")
            raise LyricsProviderError(f"歌词源返回 HTTP {status}") from exc
        except requests.RequestException as exc:
            raise LyricsProviderError(
                f"歌词源请求失败（{type(exc).__name__}）"
            ) from exc

    def search(
        self,
        title: str,
        artist: str,
        duration: float = 0,
        providers: Iterable[str] | None = None,
        *,
        album: str = "",
        artist_aliases: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        title = _safe_text(title, 300)
        artist = _safe_text(artist, 300)
        album = _safe_text(album, 300)
        if not title:
            raise ValueError("歌词搜索缺少歌曲标题")
        selected = []
        for value in providers or sorted(LYRIC_PROVIDERS):
            provider = _safe_text(value, 30).lower()
            if provider in LYRIC_PROVIDERS and provider not in selected:
                selected.append(provider)
        if not selected:
            raise ValueError("至少选择一个歌词来源")
        duration = _duration_seconds(duration)
        results: list[dict[str, Any]] = []
        warnings: list[str] = []
        with ThreadPoolExecutor(max_workers=len(selected)) as executor:
            pending = {
                executor.submit(getattr(self, f"_search_{provider}"), title, artist): provider
                for provider in selected
            }
            for future in as_completed(pending):
                provider = pending[future]
                try:
                    values = future.result()
                except Exception as exc:
                    warnings.append(f"{provider}: 暂时不可用（{type(exc).__name__}）")
                    continue
                for candidate in values[:MAX_SEARCH_RESULTS_PER_PROVIDER]:
                    candidate["source"] = provider
                    match = candidate_match(
                        title,
                        artist,
                        duration,
                        candidate,
                        query_album=album,
                        artist_aliases=artist_aliases,
                    )
                    candidate["score"] = match.pop("score")
                    candidate["match"] = match
                    if provider == "qqmusic" and candidate.get("qq_mode") == "qrc":
                        candidate["request_duration"] = duration
                    results.append(candidate)
        results.sort(
            key=lambda value: (
                LYRIC_PROVIDER_PRIORITY.get(str(value.get("source") or ""), 99),
                -float(value.get("score") or 0),
            )
        )
        return {"candidates": results[:30], "warnings": warnings}

    def fetch(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        provider = _safe_text(candidate.get("source"), 30).lower()
        if provider not in LYRIC_PROVIDERS:
            raise ValueError("未知歌词来源")
        result = getattr(self, f"_fetch_{provider}")(candidate)
        content = combine_lyrics(result.get("lyric"), result.get("translation"))
        if not content or not _lyric_has_usable_content(content):
            raise LyricsProviderError("该候选只返回歌曲信息，没有可用歌词，请选择其他候选")
        return {
            "source": provider,
            "provider_id": _safe_text(candidate.get("provider_id"), 300),
            "title": _safe_text(candidate.get("title"), 300),
            "artist": _safe_text(candidate.get("artist"), 300),
            "album": _safe_text(candidate.get("album"), 300),
            "duration": _duration_seconds(candidate.get("duration")),
            "content": content,
            "synced": lyric_is_synced(content),
            "quality": lyric_quality(content),
            "digest": lyric_digest(content),
        }

    def _search_kugou(self, title: str, artist: str) -> list[dict[str, Any]]:
        payload = self._json(
            "GET",
            "https://lyrics.kugou.com/search",
            params={
                "ver": 1,
                "man": "yes",
                "client": "pc",
                "keyword": f"{artist}-{title}".strip("-"),
            },
        )
        info = payload.get("candidates") or []
        values = []
        for item in info:
            if not isinstance(item, dict):
                continue
            lyric_id = _safe_text(item.get("id"), 200)
            access_key = _safe_text(item.get("accesskey"), 200)
            if not lyric_id or not access_key:
                continue
            values.append(
                {
                    "provider_id": lyric_id,
                    "access_key": access_key,
                    "title": _safe_text(item.get("song")),
                    "artist": _safe_text(item.get("singer")),
                    "album": "",
                    "duration": _duration_seconds((item.get("duration") or 0) / 1000),
                }
            )
        return values

    def _fetch_kugou(self, candidate: Mapping[str, Any]) -> dict[str, str]:
        lyric_id = _safe_text(candidate.get("provider_id"), 200)
        access_key = _safe_text(candidate.get("access_key"), 200)
        if not lyric_id.isdigit() or not re.fullmatch(r"[A-Fa-f0-9]+", access_key):
            raise ValueError("酷狗歌词候选标识无效")
        params = {
            "ver": 1,
            "client": "pc",
            "id": lyric_id,
            "accesskey": access_key,
            "fmt": "krc",
            "charset": "utf8",
        }
        try:
            payload = self._json(
                "GET", "https://lyrics.kugou.com/download", params=params
            )
            content = _decode_base64_bytes(payload.get("content"))
            lyric = _krc_to_lrc(content) if content else ""
            if lyric and _lyric_has_usable_content(lyric):
                return {"lyric": lyric}
        except LyricsProviderError:
            pass
        params["fmt"] = "lrc"
        payload = self._json(
            "GET", "https://lyrics.kugou.com/download", params=params
        )
        return {"lyric": _decode_base64_text(payload.get("content"))}

    def _search_netease_legacy(self, title: str, artist: str) -> list[dict[str, Any]]:
        payload = self._json(
            "POST",
            "https://music.163.com/api/search/get",
            data={
                "s": f"{title} {artist}".strip(),
                "type": 1,
                "limit": MAX_SEARCH_RESULTS_PER_PROVIDER,
                "offset": 0,
            },
            headers={"Referer": "https://music.163.com/"},
        )
        return (payload.get("result") or {}).get("songs") or []

    def _search_netease(self, title: str, artist: str) -> list[dict[str, Any]]:
        path = "/api/search/song/list/page"
        try:
            payload = self._json(
                "POST",
                "https://interface.music.163.com/eapi/batch",
                data=_netease_eapi_params(
                    path,
                    {
                        "keyword": f"{title} {artist}".strip().lower(),
                        "needCorrect": "1",
                        "channel": "typing",
                        "offset": 0,
                        "scene": "normal",
                        "total": True,
                        "limit": MAX_SEARCH_RESULTS_PER_PROVIDER,
                    },
                ),
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                    "Origin": "https://music.163.com",
                    "Referer": "https://music.163.com/",
                },
            )
            resources = (payload.get("data") or {}).get("resources") or []
            songs = [
                ((item.get("baseInfo") or {}).get("simpleSongData") or {})
                for item in resources
                if isinstance(item, dict)
            ]
            if not any(isinstance(item, dict) and item.get("id") for item in songs):
                raise LyricsProviderError("网易云逐字歌词搜索没有返回候选")
            mode = "yrc"
        except LyricsProviderError:
            songs = self._search_netease_legacy(title, artist)
            mode = "lrc"
        values = []
        for item in songs:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            artists = item.get("artists") or item.get("ar") or []
            artist_names = [
                _safe_text(value.get("name"))
                for value in artists
                if isinstance(value, dict) and value.get("name")
            ]
            album = item.get("album") or item.get("al") or {}
            values.append(
                {
                    "provider_id": str(item["id"]),
                    "title": _safe_text(item.get("name")),
                    "artist": " / ".join(artist_names),
                    "album": _safe_text(album.get("name") if isinstance(album, dict) else ""),
                    "duration": _duration_seconds((item.get("duration") or item.get("dt") or 0) / 1000),
                    "netease_mode": mode,
                }
            )
        return values

    def _fetch_netease(self, candidate: Mapping[str, Any]) -> dict[str, str]:
        provider_id = _safe_text(candidate.get("provider_id"), 100)
        if not provider_id.isdigit():
            raise ValueError("网易云歌词候选 ID 无效")
        payload: Mapping[str, Any] = {}
        if candidate.get("netease_mode") != "lrc":
            path = "/api/song/lyric/v1"
            try:
                payload = self._json(
                    "POST",
                    "https://interface3.music.163.com/eapi/song/lyric/v1",
                    data=_netease_eapi_params(
                        path,
                        {
                            "id": int(provider_id),
                            "cp": False,
                            "tv": 0,
                            "lv": 0,
                            "rv": 0,
                            "kv": 0,
                            "yv": 0,
                            "ytv": 0,
                            "yrv": 0,
                        },
                    ),
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                        "Origin": "https://music.163.com",
                        "Referer": "https://music.163.com/",
                    },
                )
                lyric, translation = _netease_yrc_to_lrc(payload)
                if _lyric_has_usable_content(combine_lyrics(lyric, translation)):
                    return {"lyric": lyric, "translation": translation}
            except LyricsProviderError:
                pass
        payload = self._json(
            "GET",
            "https://music.163.com/api/song/lyric",
            params={"id": provider_id, "lv": -1, "tv": -1, "rv": -1},
            headers={"Referer": "https://music.163.com/"},
        )
        if int((payload.get("lrc") or {}).get("version") or 0) == 1:
            raise LyricsProviderError(
                "网易云该候选只有作词作曲信息，没有正文歌词，请选择其他候选"
            )
        lyric, translation = _netease_yrc_to_lrc(payload)
        return {"lyric": lyric, "translation": translation}

    def _search_qqmusic_legacy(self, title: str, artist: str) -> list[dict[str, Any]]:
        payload = self._json(
            "GET",
            "https://c.y.qq.com/soso/fcgi-bin/client_search_cp",
            params={
                "p": 1,
                "n": MAX_SEARCH_RESULTS_PER_PROVIDER,
                "w": f"{title} {artist}".strip(),
                "format": "json",
            },
            headers={"Referer": "https://y.qq.com/"},
        )
        songs = (((payload.get("data") or {}).get("song") or {}).get("list")) or []
        values = []
        for item in songs:
            if not isinstance(item, dict):
                continue
            songmid = _safe_text(item.get("songmid"), 200)
            if not songmid:
                continue
            artists = item.get("singer") or []
            artist_names = [
                _safe_text(value.get("name"))
                for value in artists
                if isinstance(value, dict) and value.get("name")
            ]
            values.append(
                {
                    "provider_id": songmid,
                    "title": _safe_text(item.get("songname")),
                    "artist": " / ".join(artist_names),
                    "album": _safe_text(item.get("albumname")),
                    "duration": _duration_seconds(item.get("interval")),
                    "qq_mode": "lrc",
                }
            )
        return values

    def _search_qqmusic(self, title: str, artist: str) -> list[dict[str, Any]]:
        try:
            text = self._text(
                "GET",
                "https://c.y.qq.com/lyric/fcgi-bin/fcg_search_pc_lrc.fcg",
                params={
                    "SONGNAME": title,
                    "SINGERNAME": artist,
                    "TYPE": 2,
                    "RANGE_MIN": 1,
                    "RANGE_MAX": 20,
                },
                headers={"Referer": "https://y.qq.com/"},
            )
            root = ElementTree.fromstring(text.lstrip("\ufeff"))
            values = []
            for item in root.findall(".//songinfo"):
                provider_id = _safe_text(item.get("id"), 100)
                if not provider_id.isdigit():
                    continue

                def read(name: str) -> str:
                    return unquote_plus(_safe_text(item.findtext(name), 500))

                values.append(
                    {
                        "provider_id": provider_id,
                        "title": read("name") or unquote_plus(_safe_text(root.findtext(".//songname"))),
                        "artist": read("singername") or unquote_plus(_safe_text(root.findtext(".//singer"))),
                        "album": read("albumname"),
                        "duration": 0.0,
                        "qq_mode": "qrc",
                    }
                )
            if values:
                return values
        except (ElementTree.ParseError, LyricsProviderError):
            pass
        return self._search_qqmusic_legacy(title, artist)

    def _fetch_qqmusic_legacy(self, provider_id: str) -> dict[str, str]:
        payload = self._json(
            "GET",
            "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg",
            params={"songmid": provider_id, "format": "json", "nobase64": 0},
            headers={"Referer": "https://y.qq.com/"},
        )
        return {
            "lyric": _decode_base64_text(payload.get("lyric")),
            "translation": _decode_base64_text(payload.get("trans")),
        }

    def _fetch_qqmusic(self, candidate: Mapping[str, Any]) -> dict[str, str]:
        provider_id = _safe_text(candidate.get("provider_id"), 200)
        if not provider_id or not re.fullmatch(r"[\w-]+", provider_id):
            raise ValueError("QQ 音乐歌词候选 ID 无效")
        if candidate.get("qq_mode") == "qrc" or provider_id.isdigit():
            module = "music.musichallSong.PlayLyricInfo.GetPlayLyricInfo"

            def encode(value: Any) -> str:
                return base64.b64encode(
                    _safe_text(value, 500).encode("utf-8")
                ).decode("ascii")

            request_body = {
                "comm": {
                    "_channelid": "0",
                    "_os_version": "6.2.9200-2",
                    "ct": "19",
                    "cv": "1873",
                    "patch": "118",
                    "tmeAppID": "qqmusic",
                    "tmeLoginType": 2,
                    "uin": "0",
                    "wid": "0",
                },
                module: {
                    "method": "GetPlayLyricInfo",
                    "module": "music.musichallSong.PlayLyricInfo",
                    "param": {
                        "albumName": encode(candidate.get("album")),
                        "crypt": 1,
                        "ct": 19,
                        "cv": 1873,
                        "interval": int(_duration_seconds(candidate.get("request_duration"))),
                        "lrc_t": 0,
                        "qrc": 1,
                        "qrc_t": 0,
                        "roma": 1,
                        "roma_t": 0,
                        "singerName": encode(candidate.get("artist")),
                        "songID": int(provider_id),
                        "songName": encode(candidate.get("title")),
                        "trans": 1,
                        "trans_t": 0,
                        "type": -1,
                    },
                },
            }
            try:
                payload = self._json(
                    "POST",
                    "https://u.y.qq.com/cgi-bin/musicu.fcg",
                    data=json.dumps(request_body, ensure_ascii=False, separators=(",", ":")),
                    headers={
                        "Content-Type": "application/json",
                        "Host": "u.y.qq.com",
                        "Referer": "https://y.qq.com/",
                    },
                )
                response = payload.get(module) or {}
                data = response.get("data") or {}
                if payload.get("code") != 0 or response.get("code") != 0:
                    raise LyricsProviderError("QQ 音乐逐字歌词接口返回失败")
                if str(data.get("songID") or "") != provider_id:
                    raise LyricsProviderError("QQ 音乐逐字歌词返回了其他歌曲")
                result = {
                    "lyric": decrypt_qrc_to_lrc(str(data.get("lyric") or "")),
                    "translation": decrypt_qrc_to_lrc(str(data.get("trans") or "")),
                }
                if _lyric_has_usable_content(
                    combine_lyrics(result["lyric"], result["translation"])
                ):
                    return result
                raise LyricsProviderError("QQ 音乐逐字歌词没有正文")
            except (LyricsProviderError, TypeError, ValueError):
                legacy = self._search_qqmusic_legacy(
                    _safe_text(candidate.get("title")),
                    _safe_text(candidate.get("artist")),
                )
                if not legacy:
                    raise LyricsProviderError("QQ 音乐逐字与普通歌词接口均未返回结果")
                duration = _duration_seconds(candidate.get("request_duration"))
                best = max(
                    legacy,
                    key=lambda value: candidate_score(
                        _safe_text(candidate.get("title")),
                        _safe_text(candidate.get("artist")),
                        duration,
                        value,
                    ),
                )
                return self._fetch_qqmusic_legacy(str(best["provider_id"]))
        return self._fetch_qqmusic_legacy(provider_id)


def normalize_lyric_decision(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a selected lyric before it is persisted in the review DB."""
    status = _safe_text(value.get("status"), 30).lower() or "selected"
    if status not in {"selected", "instrumental", "skipped"}:
        raise ValueError("歌词状态必须是 selected、instrumental 或 skipped")
    result: dict[str, Any] = {"status": status}
    if status == "skipped":
        return result
    if status == "instrumental":
        result.update(
            {
                "content": INSTRUMENTAL_LYRIC,
                "synced": True,
                "digest": lyric_digest(INSTRUMENTAL_LYRIC),
                "quality": lyric_quality(INSTRUMENTAL_LYRIC),
            }
        )
        return result
    source = _safe_text(value.get("source"), 30).lower()
    if source not in LYRIC_PROVIDERS:
        raise ValueError("未知歌词来源")
    content = combine_lyrics(value.get("content"))
    if not content:
        raise ValueError("所选歌词内容为空")
    result.update(
        {
            "source": source,
            "provider_id": _safe_text(value.get("provider_id"), 300),
            "title": _safe_text(value.get("title"), 300),
            "artist": _safe_text(value.get("artist"), 300),
            "album": _safe_text(value.get("album"), 300),
            "content": content,
            "synced": lyric_is_synced(content),
            "quality": lyric_quality(content),
            "digest": lyric_digest(content),
        }
    )
    return result


def _validated_destination(path: str | Path, library_root: str | Path) -> Path:
    root = Path(library_root).resolve(strict=True)
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError(f"歌词写入目标不能是符号链接: {candidate}")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise ValueError(f"歌词写入目标不在媒体库内: {resolved}")
    return resolved


def _read_embedded_lyrics(audio: Any) -> str:
    tags = getattr(audio, "tags", None)
    if tags is None:
        return ""
    try:
        from mutagen.id3 import USLT
        from mutagen.mp4 import MP4Tags

        if isinstance(tags, MP4Tags):
            values = tags.get("\xa9lyr") or []
            return str(values[0] if values else "")
        values = tags.getall("USLT") if hasattr(tags, "getall") else []
        if values and isinstance(values[0], USLT):
            return str(values[0].text or "")
    except (ImportError, KeyError, TypeError):
        pass
    for key in ("LYRICS", "lyrics", "UNSYNCEDLYRICS", "unsyncedlyrics"):
        try:
            value = tags.get(key)
        except (AttributeError, KeyError):
            continue
        if isinstance(value, (list, tuple)):
            value = value[0] if value else ""
        if value:
            return str(value)
    return ""


def write_embedded_lyrics(path: str | Path, content: str) -> dict[str, Any]:
    """Write normalized LRC to the container appropriate for *path* and verify it."""
    from mutagen import File
    from mutagen.id3 import ID3, USLT
    from mutagen.mp4 import MP4Tags

    content = combine_lyrics(content)
    if not content:
        raise ValueError("不能写入空歌词")
    audio = File(str(path), easy=False)
    if audio is None:
        raise ValueError(f"不支持写入歌词的音频格式: {path}")
    if audio.tags is None:
        audio.add_tags()
    tags = audio.tags
    if isinstance(tags, ID3):
        tags.delall("USLT")
        tags.add(USLT(encoding=3, lang="und", desc="", text=content))
        tag_name = "USLT"
    elif isinstance(tags, MP4Tags):
        tags["\xa9lyr"] = [content]
        tag_name = "©lyr"
    else:
        try:
            tags["LYRICS"] = [content]
        except TypeError:
            tags["LYRICS"] = content
        tag_name = "LYRICS"
    audio.save()
    verified_audio = File(str(path), easy=False)
    verified = _read_embedded_lyrics(verified_audio)
    if verified != content:
        raise RuntimeError(f"歌词标签写入后回读不一致: {path}")
    return {
        "tag": tag_name,
        "synced": lyric_is_synced(content),
        "digest": lyric_digest(content),
    }


def embed_imported_lyrics(
    imported_tracks: Sequence[Mapping[str, Any]],
    decisions: Mapping[str, Any],
    library_root: str | Path,
) -> list[dict[str, Any]]:
    """Apply selected decisions to beets destinations, safely and idempotently."""
    tracks = {
        PurePosixPath(_safe_text(item.get("source"), 2_000)).as_posix(): item
        for item in imported_tracks
        if _safe_text(item.get("source"), 2_000)
    }
    results = []
    for raw_path, raw_decision in decisions.items():
        local_path = PurePosixPath(_safe_text(raw_path, 2_000))
        if not local_path.parts or local_path.is_absolute() or ".." in local_path.parts:
            raise ValueError(f"歌词决定包含无效相对路径: {raw_path}")
        decision = normalize_lyric_decision(raw_decision)
        if decision["status"] == "skipped":
            results.append({"source": local_path.as_posix(), "status": decision["status"]})
            continue
        track = tracks.get(local_path.as_posix())
        if track is None:
            results.append(
                {"source": local_path.as_posix(), "status": "not_imported"}
            )
            continue
        destination = _validated_destination(track.get("destination", ""), library_root)
        metadata = write_embedded_lyrics(destination, decision["content"])
        results.append(
            {
                "source": local_path.as_posix(),
                "destination": str(destination),
                "status": "embedded",
                "provider": decision.get("source", decision["status"]),
                **metadata,
            }
        )
    return results
