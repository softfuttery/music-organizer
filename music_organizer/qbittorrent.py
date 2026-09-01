"""Minimal qBittorrent Web API integration."""

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any


class QBittorrentClient:
    """Poll completed torrents through the qBittorrent Web API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        api_key: str = "",
        timeout: int = 10,
        *,
        max_attempts: int = 3,
        retry_base_seconds: float = 1.0,
        retry_max_seconds: float = 5.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max(1, min(int(max_attempts), 5))
        self.retry_base_seconds = max(0.0, min(float(retry_base_seconds), 30.0))
        self.retry_max_seconds = max(
            self.retry_base_seconds,
            min(float(retry_max_seconds), 60.0),
        )
        self.cookie_jar = CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookie_jar))
        self._authenticated = False

    def url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _is_transient_error(exc: BaseException) -> bool:
        if isinstance(exc, urllib.error.HTTPError):
            return exc.code in {408, 425, 429} or exc.code >= 500
        return isinstance(exc, (urllib.error.URLError, TimeoutError))

    def _request_text(self, request: urllib.request.Request) -> tuple[int, str]:
        for attempt in range(self.max_attempts):
            try:
                with self.opener.open(request, timeout=self.timeout) as response:
                    status = int(getattr(response, "status", 200))
                    body = response.read().decode("utf-8", errors="replace")
                return status, body
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 >= self.max_attempts or not self._is_transient_error(exc):
                    raise
                delay = min(
                    self.retry_base_seconds * (2**attempt),
                    self.retry_max_seconds,
                )
                if delay:
                    time.sleep(delay)
        raise RuntimeError("qBittorrent request retry loop ended unexpectedly")

    def login(self, *, force: bool = False) -> None:
        if self.api_key or (not self.username and not self.password):
            return
        if self._authenticated and not force:
            return
        data = urllib.parse.urlencode({"username": self.username, "password": self.password}).encode("utf-8")
        request = urllib.request.Request(
            self.url("/api/v2/auth/login"),
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Referer": self.base_url, "User-Agent": "music-organizer"},
            method="POST",
        )
        status, body = self._request_text(request)
        body = body.strip()
        if status != 204 and body != "Ok.":
            raise RuntimeError("qBittorrent login failed")
        self._authenticated = True

    def headers(self) -> dict[str, str]:
        headers = {"Referer": self.base_url, "User-Agent": "music-organizer"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def read_api_text(self, path: str) -> str:
        self.login()
        request = urllib.request.Request(self.url(path), headers=self.headers())
        try:
            return self._request_text(request)[1]
        except urllib.error.HTTPError as exc:
            if exc.code == 403 and (self.username or self.password):
                if self.api_key:
                    self.api_key = ""
                self._authenticated = False
                self.login(force=True)
                request = urllib.request.Request(self.url(path), headers=self.headers())
                return self._request_text(request)[1]
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
