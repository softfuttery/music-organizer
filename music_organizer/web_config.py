"""Validation and normalization for the HTML configuration form."""

import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from apscheduler.triggers.cron import CronTrigger

from .config import DEFAULT_BEETS_PATH_FORMAT, normalize_review_source_profiles
from .notifications import resolve_magicpush_token


def _lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _checked(form: Any, name: str) -> bool:
    return form.get(name) == "on"


def build_web_config(
    existing_config: dict[str, Any],
    form: Any,
    magicpush_token_file: Path,
) -> dict[str, Any]:
    """Build a complete persisted config from a submitted Flask form."""
    existing_review = existing_config.get("review", {})
    existing_translation = existing_config.get("translation", {})
    mappings = {}
    for line in _lines(form.get("paths_mapping", "")):
        if "=>" in line:
            source, target = line.split("=>", 1)
        elif "|" in line:
            source, target = line.split("|", 1)
        else:
            raise ValueError(f"路径映射格式错误：{line}")
        source = source.strip()
        target = target.strip()
        if not source.startswith("/") or not target.startswith("/"):
            raise ValueError("源路径和目标路径必须使用绝对路径")
        mappings[source] = target
    if not mappings:
        raise ValueError("至少需要一条路径映射")

    mode = form.get("mode", "hardlink")
    if mode not in {"hardlink", "copy"}:
        raise ValueError("转移方式只能是 hardlink 或 copy")
    qb_base_url = form.get("qb_base_url", "").strip().rstrip("/")
    if qb_base_url:
        parsed_qb_url = urlsplit(qb_base_url)
        if not parsed_qb_url.hostname:
            raise ValueError("qBittorrent 地址必须包含有效主机名")
        try:
            parsed_qb_url.port
        except ValueError as exc:
            raise ValueError("qBittorrent 地址端口无效") from exc
        if parsed_qb_url.username is not None or parsed_qb_url.password is not None:
            raise ValueError("qBittorrent 账号密码请使用独立字段配置")
    if qb_base_url and not qb_base_url.startswith(("http://", "https://")):
        raise ValueError("qBittorrent 地址必须以 http:// 或 https:// 开头")

    existing_qb = existing_config.get("qbittorrent", {})
    qb_password = form.get("qb_password", "") or existing_qb.get("password", "")
    qb_api_key = form.get("qb_api_key", "") or existing_qb.get("api_key", "")
    qb_scan_mode = form.get("qb_scan_mode", "torrent_paths")
    if qb_scan_mode not in {"torrent_paths", "full"}:
        raise ValueError("qBittorrent 扫描范围只能是 torrent_paths 或 full")
    qb_poll_mode = form.get("qb_poll_mode", "sync")
    if qb_poll_mode not in {"sync", "completed_list"}:
        raise ValueError("qBittorrent 检查模式只能是 sync 或 completed_list")

    review_import_mode = form.get(
        "review_import_mode", str(existing_review.get("import_mode") or "hardlink")
    )
    if review_import_mode not in {"copy", "hardlink", "move"}:
        raise ValueError("预审入库方式只能是 copy、hardlink 或 move")
    schedule_cron = form.get("schedule_cron", "*/30 * * * *").strip()
    schedule_cron = schedule_cron or "*/30 * * * *"
    CronTrigger.from_crontab(schedule_cron)

    magicpush_enabled = _checked(form, "magicpush_enabled")
    magicpush_base_url = form.get("magicpush_base_url", "").strip().rstrip("/")
    if magicpush_base_url and not magicpush_base_url.startswith(
        ("http://", "https://")
    ):
        raise ValueError("MagicPush 地址必须以 http:// 或 https:// 开头")
    magicpush_token = form.get("magicpush_token", "").strip()
    if len(magicpush_token) > 512:
        raise ValueError("MagicPush token 长度异常")
    saved_magicpush_token, _ = resolve_magicpush_token(
        {"token_file": str(magicpush_token_file)}
    )
    if magicpush_enabled and not magicpush_base_url:
        raise ValueError("启用 MagicPush 时必须填写服务地址")
    if magicpush_enabled and not (magicpush_token or saved_magicpush_token):
        raise ValueError("启用 MagicPush 时必须先保存 token")

    profiles_payload = form.get("review_source_profiles")
    if profiles_payload is not None:
        try:
            submitted_profiles = json.loads(str(profiles_payload) or "[]")
        except json.JSONDecodeError as exc:
            raise ValueError("目录方案数据格式错误") from exc
        if not isinstance(submitted_profiles, list) or len(submitted_profiles) > 20:
            raise ValueError("目录方案必须是最多 20 项的列表")
        for value in submitted_profiles:
            if not isinstance(value, dict):
                raise ValueError("每个目录方案必须是对象")
            path = str(value.get("path") or "").strip()
            if not path.startswith("/"):
                raise ValueError("目录方案路径必须使用容器内绝对路径")
            if str(value.get("discovery_mode") or "direct") not in {
                "direct",
                "artist_album",
            }:
                raise ValueError("目录方案发现方式无效")
            if str(value.get("import_mode") or "hardlink") not in {
                "copy",
                "hardlink",
                "move",
            }:
                raise ValueError("目录方案入库方式无效")
        profile_seed = dict(existing_review)
        profile_seed["source_profiles"] = submitted_profiles
        source_profiles = normalize_review_source_profiles(profile_seed)
        if len(source_profiles) != len(submitted_profiles):
            raise ValueError("目录方案路径不能为空或重复")
    else:
        review_roots_legacy = _lines(form.get("review_source_roots", ""))
        existing_profiles = {
            value["path"]: value
            for value in normalize_review_source_profiles(existing_review)
        }
        profile_seed = dict(existing_review)
        profile_seed["source_profiles"] = []
        for path in review_roots_legacy:
            profile = dict(
                existing_profiles.get(path, {"path": path, "name": Path(path).name})
            )
            profile["auto_discover"] = _checked(form, "review_auto_discover")
            profile_seed["source_profiles"].append(profile)
        source_profiles = normalize_review_source_profiles(profile_seed)
    review_roots = [profile["path"] for profile in source_profiles]
    if any(not value.startswith("/") for value in review_roots):
        raise ValueError("音乐预审允许目录必须使用容器内绝对路径")
    review_enabled = _checked(form, "review_enabled")
    if review_enabled and not review_roots:
        raise ValueError("启用音乐预审时至少需要一个允许选择的 Inbox 目录")
    review_proxy_url = form.get(
        "review_proxy_url", str(existing_review.get("proxy_url") or "")
    ).strip().rstrip("/")
    if review_proxy_url:
        parsed_proxy = urlsplit(review_proxy_url)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
            raise ValueError("音乐预审代理地址必须是有效的 http:// 或 https:// 地址")
        try:
            parsed_proxy.port
        except ValueError as exc:
            raise ValueError("音乐预审代理地址端口无效") from exc
        if parsed_proxy.path not in {"", "/"} or parsed_proxy.query or parsed_proxy.fragment:
            raise ValueError("音乐预审代理地址不能包含路径、查询参数或片段")
    if review_proxy_url and (
        parsed_proxy.username is not None or parsed_proxy.password is not None
    ):
        raise ValueError("代理账号密码请使用独立字段配置")
    review_proxy_username = form.get(
        "review_proxy_username", str(existing_review.get("proxy_username") or "")
    ).strip()
    review_proxy_password = (
        form.get("review_proxy_password", "")
        or str(existing_review.get("proxy_password") or "")
    )
    if len(review_proxy_username) > 256 or len(review_proxy_password) > 512:
        raise ValueError("音乐预审代理账号或密码长度异常")
    review_directory = form.get(
        "review_directory", str(existing_review.get("directory") or "")
    ).strip() or "/media/library/music"
    review_recycle_directory = form.get(
        "review_recycle_directory",
        str(existing_review.get("recycle_directory") or ""),
    ).strip()
    review_library = form.get(
        "review_library", str(existing_review.get("library") or "")
    ).strip()
    review_config_path = form.get(
        "review_config_path", str(existing_review.get("config_path") or "")
    ).strip()
    for label, value in (
        ("预审入库目标目录", review_directory),
        ("预审回收站目录", review_recycle_directory),
        ("预审 Library DB", review_library),
        ("预审 beets Config", review_config_path),
    ):
        if value and not value.startswith("/"):
            raise ValueError(f"{label}必须使用容器内绝对路径")
    review_path_format = form.get(
        "review_path_format",
        str(existing_review.get("path_format") or DEFAULT_BEETS_PATH_FORMAT),
    ).strip() or DEFAULT_BEETS_PATH_FORMAT
    review_extra_file_patterns = [
        value
        for value in form.get("review_extra_file_patterns", "")
        .replace(",", " ")
        .replace(";", " ")
        .split()
        if value
    ]
    if len(review_extra_file_patterns) > 50 or any(
        len(value) > 100 or "/" in value or "\\" in value
        for value in review_extra_file_patterns
    ):
        raise ValueError("额外文件匹配仅支持最多 50 个文件名通配符")

    translation_enabled = _checked(form, "translation_enabled")
    translation_base_url = form.get(
        "translation_base_url",
        str(existing_translation.get("base_url") or "https://api.deepseek.com"),
    ).strip().rstrip("/")
    if translation_base_url:
        parsed_translation_url = urlsplit(translation_base_url)
        if (
            parsed_translation_url.scheme not in {"http", "https"}
            or not parsed_translation_url.hostname
        ):
            raise ValueError("AI 翻译接口必须是有效的 http:// 或 https:// 地址")
        if (
            parsed_translation_url.username is not None
            or parsed_translation_url.password is not None
        ):
            raise ValueError("AI 翻译接口地址不能包含账号密码")
        try:
            parsed_translation_url.port
        except ValueError as exc:
            raise ValueError("AI 翻译接口端口无效") from exc
    translation_model = form.get(
        "translation_model",
        str(existing_translation.get("model") or "deepseek-v4-flash"),
    ).strip()
    translation_api_key = (
        form.get("translation_api_key", "")
        or str(existing_translation.get("api_key") or "")
    )
    translation_style = form.get(
        "translation_style",
        str(existing_translation.get("style") or "natural"),
    ).strip().lower()
    if translation_style not in {"literal", "natural", "lyrical"}:
        raise ValueError("AI 翻译风格无效")
    if len(translation_model) > 200 or len(translation_api_key) > 1000:
        raise ValueError("AI 翻译模型名称或 API Key 长度异常")
    if translation_enabled and (
        not translation_base_url or not translation_model or not translation_api_key
    ):
        raise ValueError("启用 AI 歌词翻译时必须填写接口地址、模型和 API Key")

    return {
        "paths_mapping": mappings,
        "mode": mode,
        "keep_dir_struct": _checked(form, "keep_dir_struct"),
        "mkdir_if_single": _checked(form, "mkdir_if_single"),
        "include": {
            "globs": _lines(form.get("include_globs", "")),
            "exts": _lines(form.get("include_exts", "")),
        },
        "exclude": {
            "globs": _lines(form.get("exclude_globs", "")),
            "exts": _lines(form.get("exclude_exts", "")),
        },
        "cue_split": {
            "enabled": _checked(form, "cue_split_enabled"),
            "output_subdir": form.get("cue_output_subdir", "").strip(),
            "filename_template": form.get(
                "cue_filename_template", "{track:02d} - {title}"
            ).strip() or "{track:02d} - {title}",
            "skip_existing": _checked(form, "cue_skip_existing"),
            "split_multifile_cues": _checked(form, "cue_split_multifile_cues"),
            "skip_source_audio": _checked(form, "cue_skip_source_audio"),
            "ffmpeg_path": form.get("cue_ffmpeg_path", "ffmpeg").strip() or "ffmpeg",
            "flac_compression_level": min(
                max(int(form.get("cue_flac_compression_level", "6") or "6"), 0),
                12,
            ),
        },
        "qbittorrent": {
            "enabled": _checked(form, "qb_enabled"),
            "base_url": qb_base_url,
            "username": form.get("qb_username", "").strip(),
            "password": qb_password,
            "password_file": str(existing_qb.get("password_file") or ""),
            "api_key": qb_api_key,
            "api_key_file": str(existing_qb.get("api_key_file") or ""),
            "timeout": min(max(int(form.get("qb_timeout", "10") or "10"), 3), 120),
            "network_max_attempts": int(existing_qb.get("network_max_attempts", 3)),
            "network_retry_seconds": float(
                existing_qb.get("network_retry_seconds", 1)
            ),
            "network_retry_max_seconds": float(
                existing_qb.get("network_retry_max_seconds", 5)
            ),
            "min_completion_age_seconds": min(
                max(int(form.get("qb_min_completion_age_seconds", "60") or "60"), 0),
                3600,
            ),
            "scan_mode": qb_scan_mode,
            "poll_mode": qb_poll_mode,
            "category": form.get("qb_category", "").strip(),
            "tag": form.get("qb_tag", "").strip(),
            "retry_max_attempts": int(existing_qb.get("retry_max_attempts", 5)),
            "retry_base_seconds": int(existing_qb.get("retry_base_seconds", 60)),
            "retry_max_seconds": int(existing_qb.get("retry_max_seconds", 3600)),
        },
        "review": {
            "enabled": review_enabled,
            "source_roots": review_roots,
            "source_profiles": source_profiles,
            "proxy_url": review_proxy_url,
            "proxy_username": review_proxy_username,
            "proxy_password": review_proxy_password,
            "proxy_password_file": str(
                existing_review.get("proxy_password_file") or ""
            ),
            "directory": review_directory,
            "recycle_directory": review_recycle_directory,
            "library": review_library,
            "config_path": review_config_path,
            "path_format": review_path_format,
            "import_mode": review_import_mode,
            "write_tags": _checked(form, "review_write_tags"),
            "move_extra_files": _checked(form, "review_move_extra_files"),
            "extra_file_patterns": review_extra_file_patterns,
            "cleanup_source_after_import": _checked(
                form, "review_cleanup_source_after_import"
            ),
            "identify_workers": min(
                max(int(form.get("review_identify_workers", "3") or "3"), 1),
                8,
            ),
            "import_timeout_seconds": min(
                max(int(existing_review.get("import_timeout_seconds", 3600) or 3600), 60),
                86400,
            ),
            "auto_discover": (
                any(profile["auto_discover"] for profile in source_profiles)
                if profiles_payload is not None
                else _checked(form, "review_auto_discover")
            ),
            "discovery_interval_seconds": min(
                max(
                    int(form.get("review_discovery_interval_seconds", "60") or "60"),
                    30,
                ),
                3600,
            ),
            "discovery_stable_seconds": min(
                max(
                    int(form.get("review_discovery_stable_seconds", "60") or "60"),
                    10,
                ),
                86400,
            ),
            "poll_seconds": 1,
        },
        "translation": {
            "enabled": translation_enabled,
            "base_url": translation_base_url,
            "model": translation_model,
            "api_key": translation_api_key,
            "api_key_file": str(existing_translation.get("api_key_file") or ""),
            "target_language": "简体中文",
            "style": translation_style,
            "timeout": min(
                max(int(form.get("translation_timeout", "120") or "120"), 10),
                300,
            ),
        },
        "notifications": {
            "magicpush": {
                "enabled": magicpush_enabled,
                "base_url": magicpush_base_url,
                "token_file": str(magicpush_token_file),
                "title": form.get("magicpush_title", "Music Organizer").strip()
                or "Music Organizer",
                "type": "text",
                "timeout": min(
                    max(int(form.get("magicpush_timeout", "10") or "10"), 3),
                    30,
                ),
                "notify_no_changes": _checked(form, "magicpush_notify_no_changes"),
            }
        },
        "schedule": {
            "cron": schedule_cron,
            "enabled": _checked(form, "schedule_enabled"),
        },
        "logging": {
            "verbose_file_actions": _checked(form, "verbose_file_actions"),
            "progress_interval": max(
                int(form.get("progress_interval", "500") or "500"), 1
            ),
        },
    }
