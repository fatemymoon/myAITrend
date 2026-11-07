"""공용 HTTP 클라이언트: User-Agent, 타임아웃, 호스트별 요청 간격, robots.txt 준수."""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from backend.config import USER_AGENT

log = logging.getLogger("collector.net")

TIMEOUT_SEC = 15.0
MIN_INTERVAL_SEC = 2.0  # 같은 호스트에 연속 요청할 때 최소 간격
MAX_BYTES = 10 * 1024 * 1024


class RobotsDisallowed(Exception):
    """robots.txt가 해당 경로 접근을 막을 때."""


@dataclass
class Response:
    url: str
    status: int
    content_type: str
    body: bytes

    def json(self):
        import json

        return json.loads(self.body.decode("utf-8"))


class PoliteClient:
    def __init__(self, min_interval: float = MIN_INTERVAL_SEC) -> None:
        self._min_interval = min_interval
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=TIMEOUT_SEC,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_hit.get(host, 0.0)
            wait = self._min_interval - (time.monotonic() - last)
            self._last_hit[host] = time.monotonic() + max(wait, 0)
        if wait > 0:
            time.sleep(wait)

    def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        rp = urllib.robotparser.RobotFileParser()
        try:
            self._throttle(parts.netloc)
            r = self._client.get(origin + "/robots.txt")
            if r.status_code in (401, 403):
                # 표준 동작: 인증 필요 응답이면 전면 금지로 간주
                rp.disallow_all = True
            elif r.status_code >= 400:
                rp = None  # robots 없음 → 허용
            else:
                rp.parse(r.text.splitlines())
        except httpx.HTTPError as exc:
            # 확인 불가 시 보수적으로 이번 실행에서 금지
            log.warning("robots.txt 확인 실패 %s: %s", origin, exc)
            rp = urllib.robotparser.RobotFileParser()
            rp.disallow_all = True
        self._robots[origin] = rp
        return rp

    def get(self, url: str, params: dict | None = None, accept: str | None = None) -> Response:
        rp = self._robots_for(url)
        if rp is not None and not rp.can_fetch(USER_AGENT, url):
            raise RobotsDisallowed(f"robots.txt 금지: {url}")
        self._throttle(urlsplit(url).netloc)
        headers = {"Accept": accept} if accept else {}
        with self._client.stream("GET", url, params=params, headers=headers) as r:
            r.raise_for_status()
            chunks, total = [], 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError(f"응답이 너무 큽니다: {url}")
                chunks.append(chunk)
            return Response(
                url=str(r.url),
                status=r.status_code,
                content_type=r.headers.get("content-type", ""),
                body=b"".join(chunks),
            )
