"""Auth: scrape Chrome cookies for hud.pytorch.org so Vercel lets us through.

Vercel's Attack Challenge Mode binds the `_vercel_jwt` cookie to the User-Agent
that solved the JS challenge, so we need to send both. Stored in
~/.config/greendog/auth.json. Refresh by re-running `greendog auth` whenever
requests start 429ing again.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

DOMAIN = "hud.pytorch.org"
AUTH_PATH = Path.home() / ".config" / "greendog" / "auth.json"
CHROME_PROFILE = "/Users/ezyang/Library/Application Support/Google/Chrome/Profile 4"


def grab_from_chrome() -> dict:
    import browser_cookie3

    cj = browser_cookie3.chrome(domain_name=DOMAIN, key_file=None)
    cookies = {c.name: c.value for c in cj if DOMAIN in c.domain}
    if not cookies:
        raise RuntimeError(
            f"no cookies found for {DOMAIN} — visit https://{DOMAIN} in Chrome first"
        )
    return cookies


def save(cookies: dict, user_agent: str) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    AUTH_PATH.write_text(
        json.dumps({"cookies": cookies, "user_agent": user_agent}, indent=2)
    )
    AUTH_PATH.chmod(0o600)


def load() -> dict | None:
    if not AUTH_PATH.exists():
        return None
    return json.loads(AUTH_PATH.read_text())


def cmd_auth(args):
    print(f"reading cookies from Chrome profile…", file=sys.stderr)
    cookies = grab_from_chrome()
    # Match the UA Chrome currently sends. Hardcoded major version is fine —
    # only matters that it stays stable per JWT. Re-run `greendog auth` if
    # Chrome auto-updates and JWTs start failing.
    ua = args.user_agent or _default_chrome_ua()
    save(cookies, ua)
    print(f"saved {len(cookies)} cookie(s) to {AUTH_PATH}", file=sys.stderr)
    for k in sorted(cookies):
        print(f"  {k}", file=sys.stderr)


def _default_chrome_ua() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
