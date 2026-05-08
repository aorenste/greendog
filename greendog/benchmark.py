"""Fetch benchmark time series from HUD's get_time_series API.

Uses the x-hud-internal-bot token for auth. See the benchmark regression
report runbook for API details.
"""
from __future__ import annotations

import json
import sys
from typing import Any

from .client import HudClient

API_PATH = "/api/benchmark/get_time_series"
IMMUTABLE_TTL_S = 7 * 24 * 3600


def fetch_time_series(
    client: HudClient,
    *,
    start: str,
    stop: str,
    arches: list[str],
    devices: list[str],
    dtypes: list[str],
    modes: list[str],
    suites: list[str] | None = None,
    branches: list[str] | None = None,
    commits: list[str] | None = None,
    granularity: str = "hour",
    name: str = "compiler_precompute",
) -> dict:
    body = {
        "name": name,
        "response_formats": ["time_series"],
        "query_params": {
            "commits": commits or [],
            "arches": arches,
            "devices": devices,
            "dtypes": dtypes,
            "granularity": granularity,
            "modes": modes,
            "startTime": start,
            "stopTime": stop,
            "suites": suites or ["torchbench", "huggingface", "timm_models"],
            "branches": branches or ["main"],
        },
    }
    return client.post_json(API_PATH, body, ttl=IMMUTABLE_TTL_S)


def summarize_regressions(
    data: dict,
    left_commit: str | None = None,
    right_commit: str | None = None,
) -> list[dict]:
    """Compare left vs right commit across all time series groups.

    Returns list of dicts with group_info, left/right values, and delta.
    If left/right commits not specified, uses first/last data points.
    """
    time_series = data.get("data", {}).get("data", {}).get("time_series", [])
    results = []
    for group in time_series:
        info = group["group_info"]
        points = group.get("data", [])
        if len(points) < 2:
            continue

        if left_commit:
            left_pts = [p for p in points if p.get("commit", "").startswith(left_commit[:12])]
        else:
            left_pts = [points[0]]

        if right_commit:
            right_pts = [p for p in points if p.get("commit", "").startswith(right_commit[:12])]
        else:
            right_pts = [points[-1]]

        if not left_pts or not right_pts:
            continue

        left_val = left_pts[0].get("value")
        right_val = right_pts[0].get("value")
        if left_val is None or right_val is None:
            continue

        delta = right_val - left_val
        results.append({
            **info,
            "left_value": left_val,
            "right_value": right_val,
            "delta": delta,
            "left_display": left_pts[0].get("displayName") or f"{left_val:.4f}",
            "right_display": right_pts[0].get("displayName") or f"{right_val:.4f}",
            "left_commit": left_pts[0].get("commit", "")[:12],
            "right_commit": right_pts[0].get("commit", "")[:12],
        })
    return results


def cmd_benchmark(args):
    if not args.start or not args.stop:
        print("error: --start and --stop are required", file=sys.stderr)
        sys.exit(1)

    client = HudClient(offline=args.offline)

    if not client.bot_token:
        print(
            "warning: no bot token configured. Run:\n"
            "  greendog auth --bot-token <TOKEN>\n"
            "or set HUD_INTERNAL_BOT_TOKEN env var.",
            file=sys.stderr,
        )

    print(f"fetching {args.mode} {args.dtype} on {args.arch}…", file=sys.stderr)
    data = fetch_time_series(
        client,
        start=args.start,
        stop=args.stop,
        arches=[args.arch],
        devices=[args.device],
        dtypes=[args.dtype],
        modes=[args.mode],
        suites=args.suites.split(",") if args.suites else None,
        granularity=args.granularity,
    )

    s = client.stats
    print(
        f"  cache: {s['hits']} hit / {s['misses']} miss · "
        f"{s['requests']} request(s) · {s['bytes']:,} bytes",
        file=sys.stderr,
    )

    if args.raw:
        print(json.dumps(data, indent=2))
        return

    if args.left_commit or args.right_commit:
        results = summarize_regressions(data, args.left_commit, args.right_commit)
        if not results:
            print("no matching data points for the specified commits", file=sys.stderr)
            return
        regressions = [r for r in results if r["delta"] < -0.001]
        improvements = [r for r in results if r["delta"] > 0.001]
        stable = [r for r in results if abs(r["delta"]) <= 0.001]

        if regressions:
            print(f"\n## Regressions ({len(regressions)})")
            for r in sorted(regressions, key=lambda x: x["delta"]):
                print(f"  {r['suite']:20s} {r['compiler']:20s} {r['metric']:12s} "
                      f"{r['left_display']:>16s} → {r['right_display']:>16s}  "
                      f"(Δ {r['delta']:+.4f})")
        if improvements:
            print(f"\n## Improvements ({len(improvements)})")
            for r in sorted(improvements, key=lambda x: -x["delta"]):
                print(f"  {r['suite']:20s} {r['compiler']:20s} {r['metric']:12s} "
                      f"{r['left_display']:>16s} → {r['right_display']:>16s}  "
                      f"(Δ {r['delta']:+.4f})")
        if stable:
            print(f"\n## Stable ({len(stable)})")
            for r in stable:
                print(f"  {r['suite']:20s} {r['compiler']:20s} {r['metric']:12s} "
                      f"{r['left_display']:>16s} → {r['right_display']:>16s}")
    else:
        ts = data.get("data", {}).get("data", {}).get("time_series", [])
        print(f"\n{len(ts)} time series groups returned")
        for g in ts:
            info = g["group_info"]
            n = len(g.get("data", []))
            print(f"  {info.get('suite','?'):20s} {info.get('compiler','?'):20s} "
                  f"{info.get('metric','?'):12s} ({n} points)")
