"""MagicPush delivery and organizer job message formatting."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import requests

from music_organizer.models import RunResult


def resolve_magicpush_token(config: dict[str, Any]) -> tuple[str, str]:
    """Read a MagicPush token without ever returning it in status payloads."""
    raw_path = str(config.get("token_file") or "").strip()
    if not raw_path:
        return "", "missing"
    path = Path(raw_path).expanduser()
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "", str(path)
    return token, str(path)


def magicpush_endpoint(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    if value.endswith("/api/push"):
        return f"{value}/"
    return f"{value}/api/push/"


def send_magicpush(
    config: dict[str, Any],
    content: str,
    *,
    title: str,
) -> dict[str, Any] | None:
    if not config.get("enabled"):
        return None
    token, token_source = resolve_magicpush_token(config)
    base_url = str(config.get("base_url") or "").strip()
    if not token:
        return {
            "ok": False,
            "sent": False,
            "error": "MagicPush token 未配置或不可读取",
            "token_source": token_source,
        }
    if not base_url:
        return {
            "ok": False,
            "sent": False,
            "error": "MagicPush 服务地址未配置",
            "token_source": token_source,
        }

    endpoint = magicpush_endpoint(base_url)
    timeout = min(max(float(config.get("timeout", 10) or 10), 3), 30)
    payload = {"title": title, "content": content, "type": "text"}
    last_error = ""
    response: requests.Response | None = None
    for attempt in range(2):
        try:
            response = requests.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout,
            )
            if response.status_code < 500 or attempt == 1:
                break
        except requests.RequestException as exc:
            last_error = str(exc).replace(token, "********")
            if attempt == 1 or not isinstance(
                exc, (requests.ConnectionError, requests.Timeout)
            ):
                break
        time.sleep(1)

    if response is None:
        return {
            "ok": False,
            "sent": False,
            "error": last_error or "MagicPush 请求失败",
            "endpoint": endpoint,
            "token_source": token_source,
        }

    try:
        body: Any = response.json()
    except ValueError:
        body = None
    api_success = body.get("success", True) if isinstance(body, dict) else True
    sent = bool(response.ok and api_success)
    result = {
        "ok": sent,
        "sent": sent,
        "status_code": response.status_code,
        "endpoint": endpoint,
        "token_source": token_source,
    }
    if not sent:
        error = (
            str(body.get("message") or body.get("error") or "")
            if isinstance(body, dict)
            else response.text[:300]
        ) or f"MagicPush 返回 HTTP {response.status_code}"
        result["error"] = error.replace(token, "********")
    return result


def format_job_notification(
    job: dict[str, Any],
    result: RunResult,
    *,
    title_prefix: str = "Music Organizer",
) -> tuple[str, str]:
    cancelled = result.message == "stopped by user"
    if cancelled:
        state = "已取消"
        marker = "[取消]"
    elif result.failed:
        state = "失败"
        marker = "[失败]"
    else:
        state = "成功"
        marker = "[成功]"

    job_type = str(job.get("job_type") or "")
    job_label = "qBittorrent 增量整理" if job_type == "qb_poll" else "手动全量整理"
    details = result.details or {}
    torrent_names = [str(value) for value in details.get("torrent_names", []) if value]
    album_names = [str(value) for value in details.get("album_names", []) if value]

    lines = [f"状态：{state}", f"任务：{job_label}"]
    if torrent_names:
        lines.append(f"种子名称：{'、'.join(torrent_names[:10])}")
        if len(torrent_names) > 10:
            lines.append(f"种子数量：{len(torrent_names)}")
    if album_names:
        lines.append(f"专辑名称：{'、'.join(album_names[:10])}")
        if len(album_names) > 10:
            lines.append(f"专辑数量：{len(album_names)}")
    lines.extend(
        [
            f"扫描：{result.scanned}",
            f"整理成功：{result.organized}",
            f"跳过：{result.skipped}",
            f"失败：{result.failed}",
            f"结果：{result.message or state}",
        ]
    )
    return f"{marker} {title_prefix} 整理结果", "\n".join(lines)
