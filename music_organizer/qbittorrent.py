"""Minimal qBittorrent Web API integration."""

import json
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any


class QBittorrentClient:
    """Poll completed torrents through the qBittorrent Web API."""

    def __init__(self, base_url: str, username: str, password: str, api_key: str = "", timeout: int = 10):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def login(self) -> None:
        if self.api_key or (not self.username and not self.password):
            return
        data = urllib.parse.urlencode({"username": self.username, "password": self.password}).encode("utf-8")
        request = urllib.request.Request(
            self.url("/api/v2/auth/login"),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": self.base_url, "User-Agent": "music-organizer"},
            method="POST",
        )
        with self.opener.open(request, timeout=self.timeout) as response:
            status = getattr(response, "status", 200)
            body = response.read().decode("utf-8", errors="replace").strip()
        if status != 204 and body != "Ok.":
            raise RuntimeError("qBittorrent login failed")

    def headers(self) -> dict[str, str]:
        headers = {"Referer": self.base_url, "User-Agent": "music-organizer"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def read_api_text(self, path: str) -> str:
        self.login()
        request = urllib.request.Request(self.url(path), headers=self.headers())
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and self.api_key and (self.username or self.password):
                self.api_key = ""
                self.login()
                request = urllib.request.Request(self.url(path), headers=self.headers())
                with self.opener.open(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            raise

    def torrents_info(self, category: str = "", tag: str = "", hashes: list[str] | None = None) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if hashes:
            params["hashes"] = "|".join(hashes)
        else:
            params["filter"] = "completed"
        if category:
            params["category"] = category
        if tag:
            params["tag"] = tag
        query = urllib.parse.urlencode(params)
        data = json.loads(self.read_api_text(f"/api/v2/torrents/info?{query}"))
        if not isinstance(data, list):
            raise RuntimeError("qBittorrent returned an unexpected response")
        return data

    def sync_maindata(self, rid: int) -> dict[str, Any]:
        query = urllib.parse.urlencode({"rid": str(max(rid, 0))})
        data = json.loads(self.read_api_text(f"/api/v2/sync/maindata?{query}"))
        if not isinstance(data, dict):
            raise RuntimeError("qBittorrent sync returned an unexpected response")
        return data
