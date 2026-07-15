"""Bulk fetchers — one HUD endpoint each. Output is raw JSON, no analysis."""
from __future__ import annotations

import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any

from .client import HudClient

REPO_OWNER = "pytorch"
REPO_NAME = "pytorch"
REPO = f"{REPO_OWNER}/{REPO_NAME}"
BRANCH = "main"

# Effectively-immutable data → long TTL so re-runs are cheap.
IMMUTABLE_TTL_S = 7 * 24 * 3600
# Things that change as commits land → short TTL.
LIVE_TTL_S = 300


def fetch_sevs(client: HudClient) -> list[dict]:
    label = urllib.parse.quote("ci: sev", safe="")
    return client.get_json(f"/api/issue/{label}", ttl=LIVE_TTL_S)


def fetch_hud_grid(client: HudClient, hours: int) -> dict:
    """Walk pages until the oldest commit on a page is past the cutoff.

    Each HUD page returns its own ``jobNames`` list, and every commit's
    ``jobs`` array is positionally aligned to *that page's* names. Different
    pages cover different commit ranges and therefore expose different job
    sets/orders, so we cannot reuse page 0's names for later pages — doing so
    silently misattributes every later page's cells to the wrong job. Instead
    we build a canonical union of names (first-seen order) and realign every
    commit's cells to it by name, padding absent columns with ``{}``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    canonical_names: list[str] = []
    seen_names: set[str] = set()
    # (row, {job_name: cell}) — cells keyed by name so we can realign at the end
    # once the full canonical name set is known.
    collected: list[tuple[dict, dict]] = []
    page = 0
    while page < 20:  # safety cap; ~50/page * 20 = 1000 commits
        data = client.get_json(
            f"/api/hud/{REPO_OWNER}/{REPO_NAME}/{BRANCH}/{page}",
            ttl=LIVE_TTL_S,
        )
        rows = data.get("shaGrid", []) or []
        if not rows:
            break
        page_names = data.get("jobNames", []) or []
        # jobNames are this page's column headers and the source of truth for
        # names. We assume they're unique within a page (HUD headers are); if a
        # name ever repeats, the canonical column is added once and the later
        # cell wins below — the duplicate column collapses. Acceptable since it
        # shouldn't happen in practice.
        for name in page_names:
            if name not in seen_names:
                seen_names.add(name)
                canonical_names.append(name)
        for row in rows:
            cells = row.get("jobs") or []
            # Key each cell by its page-local column name. zip is intentionally
            # lenient (not strict=True): a row shorter than page_names leaves
            # the missing trailing columns to be filled with {} at realign
            # time, and a row longer than page_names drops the unnamed extra
            # cells (no header ⇒ unusable). We prefer graceful degradation to
            # raising — this tool runs autonomously and one malformed row
            # shouldn't sink the whole report.
            collected.append((row, dict(zip(page_names, cells))))
        oldest_t = rows[-1].get("time")
        if not oldest_t:
            break
        oldest = _parse_time(oldest_t)
        if oldest < cutoff:
            break
        page += 1
    in_window = []
    for row, cells_by_name in collected:
        if _parse_time(row["time"]) < cutoff:
            continue
        realigned = [cells_by_name.get(name, {}) for name in canonical_names]
        in_window.append({**row, "jobs": realigned})
    return {
        "shaGrid": in_window,
        "jobNames": canonical_names,
        "pages_walked": page + 1,
    }


def fetch_advisor_verdicts(client: HudClient, shas: list[str]) -> list[dict]:
    if not shas:
        return []
    return client.clickhouse(
        "advisor_verdicts_for_hud",
        {"repo": REPO, "shas": shas},
        ttl=IMMUTABLE_TTL_S,
    )


def fetch_autorevert_commits(client: HudClient, shas: list[str]) -> list[dict]:
    if not shas:
        return []
    return client.clickhouse(
        "autorevert_commits",
        {"repo": REPO, "shas": shas},
        ttl=IMMUTABLE_TTL_S,
    )


def collect(client: HudClient, hours: int) -> dict:
    print(f"  sevs…", file=sys.stderr)
    sevs = fetch_sevs(client)
    print(f"  hud grid (window={hours}h)…", file=sys.stderr)
    grid = fetch_hud_grid(client, hours=hours)
    shas = [r["sha"] for r in grid["shaGrid"]]
    print(f"  found {len(shas)} commits across {grid['pages_walked']} page(s)", file=sys.stderr)
    print(f"  advisor verdicts…", file=sys.stderr)
    verdicts = fetch_advisor_verdicts(client, shas)
    print(f"  autorevert commits…", file=sys.stderr)
    autorevert = fetch_autorevert_commits(client, shas)
    return {
        "repo": REPO,
        "branch": BRANCH,
        "window_hours": hours,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "sevs": sevs,
        "grid": grid,
        "advisor_verdicts": verdicts,
        "autorevert_commits": autorevert,
    }


def _parse_time(s: str) -> datetime:
    # HUD returns ISO with "Z" or with offset.
    return datetime.fromisoformat(s.replace("Z", "+00:00"))
