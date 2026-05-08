"""Render a 'state of master' markdown report from collected raw data."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

FAILURE_CONCLUSIONS = {"failure", "cancelled", "timed_out"}


def render(data: dict) -> str:
    parts = [
        f"# greendog: state of {data['repo']}@{data['branch']}",
        f"_window: {data['window_hours']}h · fetched: {data['fetched_at']}_",
        "",
        _section_sevs(data["sevs"]),
        _section_head(data["grid"]),
        _section_persistent(data["grid"]),
        _section_auto_handled(data["autorevert_commits"], data["advisor_verdicts"]),
        _section_unclear(
            data["grid"], data["autorevert_commits"], data["advisor_verdicts"]
        ),
    ]
    return "\n".join(parts)


def _is_failure(c):
    return c in FAILURE_CONCLUSIONS


def _section_sevs(sevs: list[dict]) -> str:
    open_sevs = [s for s in sevs if s.get("state") == "open"]
    out = ["## Active SEVs"]
    if not open_sevs:
        out += ["_none_", ""]
        return "\n".join(out)
    for s in open_sevs:
        labels = s.get("labels") or []
        mb = " **(merge-blocking)**" if "merge blocking" in labels else ""
        out.append(f"- [#{s['number']}]({s['html_url']}) {s['title']}{mb}")
    out.append("")
    return "\n".join(out)


def _pick_mature_commit(rows: list[dict]) -> tuple[int, dict]:
    """Find the most recent commit where per-commit CI has largely completed.

    The job grid includes periodic/nightly slots that don't run on every commit,
    so even a "fully done" commit only reaches ~27% concluded.  We pick the
    first commit with at least 25% concluded and at least 250 concluded jobs,
    which filters out commits whose CI just started.
    """
    for idx, row in enumerate(rows):
        jobs = row.get("jobs") or []
        total = len(jobs)
        if total == 0:
            continue
        concluded = sum(1 for j in jobs if j.get("conclusion"))
        if concluded >= 250 and concluded >= total * 0.25:
            return idx, row
    return 0, rows[0]


def _section_head(grid: dict) -> str:
    rows = grid["shaGrid"]
    job_names = grid["jobNames"]
    out = ["## Trunk HEAD (most recent commit with CI results)"]
    if not rows:
        out += ["_no commits in window_", ""]
        return "\n".join(out)
    head_idx, head = _pick_mature_commit(rows)
    if head_idx > 0:
        out.append(f"_skipped {head_idx} newer commit(s) with mostly pending jobs_")
    out += [
        f"`{head['sha'][:8]}` — {head.get('commitTitle','')}",
        f"author: {head.get('author','?')}  ·  time: {head.get('time','?')}",
    ]
    failures = []
    success = pending = 0
    for i, job in enumerate(head.get("jobs", [])):
        c = job.get("conclusion")
        if _is_failure(c):
            failures.append((_job_name(job_names, i), job))
        elif c == "success":
            success += 1
        else:
            pending += 1
    out.append(f"jobs: {success} green · {len(failures)} red · {pending} pending/missing")
    if failures:
        out += ["", "### Red jobs on HEAD"]
        for name, job in failures[:30]:
            out += _format_job_bullet(name, job)
        if len(failures) > 30:
            out.append(f"_…+{len(failures)-30} more red jobs on HEAD_")
    out.append("")
    return "\n".join(out)


def _section_persistent(grid: dict) -> str:
    """Jobs that fail across the window with no green."""
    rows = grid["shaGrid"]
    job_names = grid["jobNames"]
    out = ["## Persistently red jobs (no green in window)"]
    if not rows:
        out += ["_no data_", ""]
        return "\n".join(out)
    states: dict[int, dict] = defaultdict(
        lambda: {"fail": 0, "success": 0, "other": 0, "sample": None, "url": None}
    )
    for row in rows:
        for i, job in enumerate(row.get("jobs", [])):
            c = job.get("conclusion")
            st = states[i]
            if _is_failure(c):
                st["fail"] += 1
                if not st["sample"]:
                    lines = job.get("failureLines") or []
                    if lines:
                        st["sample"] = lines[0]
                    st["url"] = job.get("htmlUrl")
            elif c == "success":
                st["success"] += 1
            else:
                st["other"] += 1
    persistent = [
        (i, st) for i, st in states.items() if st["fail"] >= 3 and st["success"] == 0
    ]
    persistent.sort(key=lambda x: -x[1]["fail"])
    if not persistent:
        out += ["_none_", ""]
        return "\n".join(out)
    for i, st in persistent[:40]:
        name = _job_name(job_names, i)
        out.append(f"- **{name}** — {st['fail']} fail / 0 green / {st['other']} other")
        if st["sample"]:
            out.append(f"  - `{_short(st['sample'])}`")
        if st["url"]:
            out.append(f"  - [logs]({st['url']})")
    if len(persistent) > 40:
        out.append(f"_…+{len(persistent)-40} more persistently red jobs_")
    out.append("")
    return "\n".join(out)


def _section_auto_handled(autorevert: list[dict], verdicts: list[dict]) -> str:
    out = ["## Auto-handled by existing systems"]
    out.append(f"- autoreverts in window: **{len(autorevert)}**")
    out.append(f"- advisor verdicts in window: **{len(verdicts)}**")
    if verdicts:
        mix = Counter(v.get("verdict", "?") for v in verdicts)
        out.append(f"  - verdict mix: {dict(mix)}")
    if autorevert:
        out.append("")
        out.append("### Autoreverted commits")
        for ev in autorevert[:20]:
            sha = (ev.get("sha") or ev.get("commit_sha") or "")[:8]
            wfs = ev.get("all_workflows") or ev.get("workflows") or []
            sigs = ev.get("all_source_signal_keys") or ev.get("source_signal_keys") or []
            out.append(f"- `{sha}` — workflows: {wfs} · signals: {sigs}")
    out.append("")
    return "\n".join(out)


def _section_unclear(
    grid: dict, autorevert: list[dict], verdicts: list[dict]
) -> str:
    """Green→red transitions with no advisor verdict and no autorevert.

    These are the gaps where the autorevert system either didn't fire or
    didn't have an opinion — exactly where greendog can eventually add value.
    """
    rows = grid["shaGrid"]
    job_names = grid["jobNames"]
    out = ["## Unclear cases (green→red, no advisor, no autorevert)"]
    if len(rows) < 2:
        out += ["_not enough data_", ""]
        return "\n".join(out)
    autorev_shas = {
        ev.get("sha") or ev.get("commit_sha") for ev in autorevert
    }
    verdict_shas = {v.get("suspect_commit") for v in verdicts}
    transitions: list[tuple[str, int, dict]] = []
    # Iterate oldest→newest so prev_conclusion is well-defined.
    for j in range(len(job_names)):
        prev = None
        for row in reversed(rows):
            jobs = row.get("jobs") or []
            if j >= len(jobs):
                continue
            c = jobs[j].get("conclusion")
            if prev == "success" and _is_failure(c):
                sha = row["sha"]
                if sha not in autorev_shas and sha not in verdict_shas:
                    transitions.append((sha, j, jobs[j]))
            if c in FAILURE_CONCLUSIONS or c == "success":
                prev = c
    if not transitions:
        out += ["_none_", ""]
        return "\n".join(out)
    for sha, j, job in transitions[:40]:
        name = _job_name(job_names, j)
        out.append(f"- `{sha[:8]}` **{name}**")
        out += [f"  {line}" for line in _format_job_detail(job)]
    if len(transitions) > 40:
        out.append(f"_…+{len(transitions)-40} more unclear transitions_")
    out.append("")
    return "\n".join(out)


def _job_name(names: list[str], i: int) -> str:
    return names[i] if 0 <= i < len(names) else f"job#{i}"


def _short(s: str, n: int = 140) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


def _format_job_bullet(name: str, job: dict) -> list[str]:
    bullets = [f"- **{name}**"]
    bullets += [f"  {line}" for line in _format_job_detail(job)]
    return bullets


def _format_job_detail(job: dict) -> list[str]:
    out = []
    lines = job.get("failureLines") or []
    if lines:
        out.append(f"- `{_short(lines[0])}`")
    if job.get("htmlUrl"):
        out.append(f"- [logs]({job['htmlUrl']})")
    return out
