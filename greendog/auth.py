"""Auth: manage credentials for hud.pytorch.org.

Two auth methods:
1. Bot token (preferred for API access): uses x-hud-internal-bot header,
   bypasses Vercel/Cloudflare entirely. Token from Keeper password manager
   shared folder "circleci-aws-keys".
2. Chrome cookies (fallback for browser-only endpoints): scraped via
   browser_cookie3, paired with curl_cffi for TLS fingerprint impersonation.

Stored in ~/.config/greendog/auth.json. Refresh with `greendog auth`.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

DOMAIN = "hud.pytorch.org"
AUTH_PATH = Path.home() / ".config" / "greendog" / "auth.json"


def grab_from_chrome() -> dict:
    import browser_cookie3

    cj = browser_cookie3.chrome(domain_name=DOMAIN, key_file=None)
    cookies = {c.name: c.value for c in cj if DOMAIN in c.domain}
    if not cookies:
        raise RuntimeError(
            f"no cookies found for {DOMAIN} — visit https://{DOMAIN} in Chrome first"
        )
    return cookies


def save(data: dict) -> None:
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = load() or {}
    existing.update(data)
    AUTH_PATH.write_text(json.dumps(existing, indent=2))
    AUTH_PATH.chmod(0o600)


def load() -> dict | None:
    if not AUTH_PATH.exists():
        return None
    return json.loads(AUTH_PATH.read_text())


def get_bot_token() -> str | None:
    tok = os.environ.get("HUD_INTERNAL_BOT_TOKEN")
    if tok:
        return tok
    creds = load()
    return (creds or {}).get("bot_token")


def cmd_auth(args):
    if args.bot_token:
        save({"bot_token": args.bot_token})
        print(f"saved bot token to {AUTH_PATH}", file=sys.stderr)
        return

    print(f"reading cookies from Chrome profile…", file=sys.stderr)
    cookies = grab_from_chrome()
    ua = args.user_agent or _default_chrome_ua()
    save({"cookies": cookies, "user_agent": ua})
    print(f"saved {len(cookies)} cookie(s) to {AUTH_PATH}", file=sys.stderr)
    for k in sorted(cookies):
        print(f"  {k}", file=sys.stderr)


def _default_chrome_ua() -> str:
    return (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
