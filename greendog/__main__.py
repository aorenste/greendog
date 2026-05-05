"""greendog CLI: fetch + report on pytorch/pytorch trunk health."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import auth
from .client import HudClient, PROJECT_ROOT
from .fetch import collect
from .report import render

RUNS_DIR = PROJECT_ROOT / "runs"


def cmd_run(args):
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS_DIR / ts
    run_dir.mkdir(parents=True, exist_ok=True)
    client = HudClient(offline=args.offline)
    print(f"greendog: window={args.hours}h{' [offline]' if args.offline else ''}", file=sys.stderr)
    data = collect(client, hours=args.hours)
    raw = run_dir / "raw.json"
    raw.write_text(json.dumps(data, indent=2))
    s = client.stats
    print(
        f"  cache: {s['hits']} hit / {s['misses']} miss · "
        f"{s['requests']} request(s) · {s['bytes']:,} bytes",
        file=sys.stderr,
    )
    report = render(data)
    out = run_dir / "report.md"
    out.write_text(report)
    print(report)
    print(f"\n[wrote {out.relative_to(PROJECT_ROOT)}]", file=sys.stderr)


def cmd_report(args):
    p = Path(args.run)
    raw = p / "raw.json" if p.is_dir() else p
    data = json.loads(raw.read_text())
    report = render(data)
    out = raw.parent / "report.md"
    out.write_text(report)
    print(report)
    print(f"\n[wrote {out}]", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser(prog="greendog")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="fetch + render a fresh report")
    p_run.add_argument("--hours", type=int, default=24, help="lookback window")
    p_run.add_argument("--offline", action="store_true", help="cache only, no network")
    p_run.set_defaults(func=cmd_run)

    p_report = sub.add_parser("report", help="re-render report from raw.json")
    p_report.add_argument("run", help="path to runs/{ts} dir or raw.json file")
    p_report.set_defaults(func=cmd_report)

    p_auth = sub.add_parser(
        "auth", help="grab hud.pytorch.org cookies from Chrome (refresh when 429)"
    )
    p_auth.add_argument("--user-agent", help="override Chrome UA string")
    p_auth.set_defaults(func=auth.cmd_auth)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
