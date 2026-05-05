"""HTTP client for hud.pytorch.org with on-disk content-addressed cache.

One place for HTTP, polite rate limiting, gzip handling, retries, and caching.
Cache is shared across runs so re-running a sweep is cheap and offline-capable.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

from . import auth as auth_mod

BASE = "https://hud.pytorch.org"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = PROJECT_ROOT / "cache"

# Fallback UA when auth.json is missing (will likely 429 against Vercel).
FALLBACK_UA = "greendog/0.1 (pytorch trunk health analyzer; ezyang)"
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
        self.user_agent = (creds or {}).get("user_agent") or FALLBACK_UA
        self.cookies = (creds or {}).get("cookies") or {}

    def _cache_paths(self, url: str) -> tuple[Path, Path]:
        key = hashlib.sha256(url.encode()).hexdigest()
        d = self.cache_dir / key[:2]
        return d / f"{key}.json", d / f"{key}.meta"

    def _read_cache(self, url: str, ttl: int) -> Optional[Any]:
        body, meta = self._cache_paths(url)
        if not body.exists() or not meta.exists():
            return None
        m = json.loads(meta.read_text())
        if not self.offline and time.time() - m["fetched_at"] > ttl:
            return None
        self.stats["hits"] += 1
        return json.loads(body.read_text())

    def _write_cache(self, url: str, data: Any) -> None:
        body, meta = self._cache_paths(url)
        body.parent.mkdir(parents=True, exist_ok=True)
        body.write_text(json.dumps(data))
        meta.write_text(json.dumps({"fetched_at": time.time(), "url": url}))

    def _fetch(self, url: str) -> Any:
        elapsed = time.time() - self._last_request
        if elapsed < MIN_DELAY_S:
            time.sleep(MIN_DELAY_S - elapsed)
        last_err: Optional[Exception] = None
        for attempt in range(RETRIES):
            try:
                self._last_request = time.time()
                headers = {
                    "User-Agent": self.user_agent,
                    "Accept-Encoding": "gzip",
                    "Accept": "application/json,text/plain,*/*",
                }
                if self.cookies:
                    headers["Cookie"] = "; ".join(
                        f"{k}={v}" for k, v in self.cookies.items()
                    )
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                    raw = resp.read()
                    if resp.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                self.stats["bytes"] += len(raw)
                self.stats["requests"] += 1
                return json.loads(raw)
            except (urllib.error.URLError, TimeoutError) as e:
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
            # Sorted for stable cache keys.
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

    def clickhouse(
        self, query_name: str, parameters: dict, ttl: Optional[int] = None
    ) -> Any:
        # sort_keys for stable cache keys.
        return self.get_json(
            f"/api/clickhouse/{query_name}",
            {"parameters": json.dumps(parameters, sort_keys=True)},
            ttl=ttl,
        )
