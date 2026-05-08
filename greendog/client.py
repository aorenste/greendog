"""HTTP client for hud.pytorch.org with on-disk content-addressed cache.

Uses curl_cffi for Chrome TLS fingerprint impersonation (bypasses Cloudflare).
Supports x-hud-internal-bot token for API auth (bypasses Vercel challenge).
Cache is shared across runs so re-running a sweep is cheap and offline-capable.
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Optional

from curl_cffi import requests as cffi_requests

from . import auth as auth_mod

BASE = "https://hud.pytorch.org"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"

MIN_DELAY_S = 0.4
DEFAULT_TTL_S = 300
RETRIES = 3
TIMEOUT_S = 60


class HudClient:
    def __init__(
        self,
        cache_dir: Path = CACHE_DIR,
        default_ttl: int = DEFAULT_TTL_S,
        offline: bool = False,
    ):
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.default_ttl = default_ttl
        self.offline = offline
        self._last_request = 0.0
        self.stats = {"hits": 0, "misses": 0, "bytes": 0, "requests": 0}
        creds = auth_mod.load() if not offline else None
        self.cookies = (creds or {}).get("cookies") or {}
        self.bot_token = auth_mod.get_bot_token() if not offline else None

    def _cache_paths(self, key: str) -> tuple[Path, Path]:
        h = hashlib.sha256(key.encode()).hexdigest()
        d = self.cache_dir / h[:2]
        return d / f"{h}.json", d / f"{h}.meta"

    def _read_cache(self, key: str, ttl: int) -> Optional[Any]:
        body, meta = self._cache_paths(key)
        if not body.exists() or not meta.exists():
            return None
        m = json.loads(meta.read_text())
        if not self.offline and time.time() - m["fetched_at"] > ttl:
            return None
        self.stats["hits"] += 1
        return json.loads(body.read_text())

    def _write_cache(self, key: str, data: Any) -> None:
        body, meta = self._cache_paths(key)
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(json.dumps(data))
        meta.write_text(json.dumps({"fetched_at": time.time(), "key": key}))

    def _throttle(self):
        elapsed = time.time() - self._last_request
        if elapsed < MIN_DELAY_S:
            time.sleep(MIN_DELAY_S - elapsed)

    def _headers(self) -> dict:
        headers = {"Accept": "application/json,text/plain,*/*"}
        if self.bot_token:
            headers["x-hud-internal-bot"] = self.bot_token
        return headers

    def _fetch(self, url: str) -> Any:
        self._throttle()
        last_err: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                self._last_request = time.time()
                r = cffi_requests.get(
                    url,
                    headers=self._headers(),
                    cookies=self.cookies,
                    impersonate="chrome",
                    timeout=TIMEOUT_S,
                )
                r.raise_for_status()
                self.stats["bytes"] += len(r.content)
                self.stats["requests"] += 1
                return r.json()
            except Exception as e:
                last_err = e
                backoff = 2 ** attempt
                print(f"  fetch failed ({e}); retrying in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
        raise RuntimeError(f"giving up on {url}: {last_err}")

    def _post(self, url: str, body: dict) -> Any:
        self._throttle()
        last_err: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                self._last_request = time.time()
                r = cffi_requests.post(
                    url,
                    headers=self._headers(),
                    json=body,
                    cookies=self.cookies,
                    impersonate="chrome",
                    timeout=TIMEOUT_S,
                )
                r.raise_for_status()
                self.stats["bytes"] += len(r.content)
                self.stats["requests"] += 1
                return r.json()
            except Exception as e:
                last_err = e
                backoff = 2 ** attempt
                print(f"  fetch failed ({e}); retrying in {backoff}s", file=sys.stderr)
                time.sleep(backoff)
        raise RuntimeError(f"giving up on {url}: {last_err}")

    def get_json(
        self,
        path: str,
        params: Optional[dict] = None,
        ttl: Optional[int] = None,
    ) -> Any:
        if ttl is None:
            ttl = self.default_ttl
        url = BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()))
        cached = self._read_cache(url, ttl)
        if cached is not None:
            return cached
        if self.offline:
            raise RuntimeError(f"offline mode but no cache for {url}")
        data = self._fetch(url)
        self._write_cache(url, data)
        self.stats["misses"] += 1
        return data

    def post_json(
        self,
        path: str,
        body: dict,
        ttl: Optional[int] = None,
    ) -> Any:
        if ttl is None:
            ttl = self.default_ttl
        url = BASE + path
        cache_key = url + ":" + json.dumps(body, sort_keys=True)
        cached = self._read_cache(cache_key, ttl)
        if cached is not None:
            return cached
        if self.offline:
            raise RuntimeError(f"offline mode but no cache for {url}")
        data = self._post(url, body)
        self._write_cache(cache_key, data)
        self.stats["misses"] += 1
        return data

    def clickhouse(
        self, query_name: str, parameters: dict, ttl: Optional[int] = None
    ) -> Any:
        return self.get_json(
            f"/api/clickhouse/{query_name}",
            {"parameters": json.dumps(parameters, sort_keys=True)},
            ttl=ttl,
        )
