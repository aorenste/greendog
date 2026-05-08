"""Diff two greendog runs: what broke, what healed, what's still broken."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .report import FAILURE_CONCLUSIONS, _is_failure, _job_name, _short


def _compute_persistent(data: dict) -> dict[str, dict]:
    """Return {job_name: {fail, success, other, sample, url}} for persistently red jobs."""
    grid = data["grid"]
    rows = grid["shaGrid"]
    job_names = grid["jobNames"]
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
    result = {}
    for i, st in states.items():
        if st["fail"] >= 3 and st["success"] == 0:
            name = _job_name(job_names, i)
            result[name] = dict(st)
    return result


def _compute_unclear(data: dict) -> dict[str, dict]:
    """Return {job_name: {count, commits, sample, url}} for unclear green→red transitions.

    Groups by job name so we can compare across runs where the commits differ.
    """
    grid = data["grid"]
    rows = grid["shaGrid"]
    job_names = grid["jobNames"]
    autorevert = data.get("autorevert_commits", [])
    verdicts = data.get("advisor_verdicts", [])

    autorev_shas = {ev.get("sha") or ev.get("commit_sha") for ev in autorevert}
    verdict_shas = {v.get("suspect_commit") for v in verdicts}

    by_job: dict[str, dict] = {}
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
                    name = _job_name(job_names, j)
                    if name not in by_job:
                        lines = jobs[j].get("failureLines") or []
                        by_job[name] = {
                            "count": 0,
                            "commits": [],
                            "sample": lines[0] if lines else None,
                            "url": jobs[j].get("htmlUrl"),
                        }
                    by_job[name]["count"] += 1
                    by_job[name]["commits"].append(sha[:8])
            if c in FAILURE_CONCLUSIONS or c == "success":
                prev = c
    return by_job


def _compute_sev_set(data: dict) -> dict[int, dict]:
    """Return {issue_number: sev_dict} for open SEVs."""
    return {
        s["number"]: s
        for s in data.get("sevs", [])
        if s.get("state") == "open"
    }


def render_diff(before: dict, after: dict) -> str:
    parts = [
        f"# greendog diff",
        f"_before: {before['fetched_at']} · after: {after['fetched_at']}_",
        "",
        _diff_sevs(before, after),
        _diff_persistent(before, after),
        _diff_unclear(before, after),
        _diff_autorevert(before, after),
    ]
    return "\n".join(parts)


def _diff_sevs(before: dict, after: dict) -> str:
    old = _compute_sev_set(before)
    new = _compute_sev_set(after)
    out = ["## SEVs"]
    opened = set(new) - set(old)
    closed = set(old) - set(new)
    ongoing = set(old) & set(new)
    if not opened and not closed and not ongoing:
        out += ["_none_", ""]
        return "\n".join(out)
    if opened:
        for n in sorted(opened):
            s = new[n]
            out.append(f"- **NEW** [#{n}]({s['html_url']}) {s['title']}")
    if closed:
        for n in sorted(closed):
            s = old[n]
            out.append(f"- **RESOLVED** [#{n}]({s['html_url']}) {s['title']}")
    if ongoing:
        for n in sorted(ongoing):
            s = new[n]
            out.append(f"- ongoing [#{n}]({s['html_url']}) {s['title']}")
    out.append("")
    return "\n".join(out)


def _diff_persistent(before: dict, after: dict) -> str:
    old = _compute_persistent(before)
    new = _compute_persistent(after)
    old_names = set(old)
    new_names = set(new)

    fixed = sorted(old_names - new_names)
    broken = sorted(new_names - old_names)
    still = sorted(old_names & new_names, key=lambda n: -new[n]["fail"])

    out = ["## Persistent failures"]
    out.append(f"_{len(broken)} newly broken · {len(fixed)} fixed · {len(still)} still broken_")
    out.append("")

    if fixed:
        out.append("### Fixed (were persistently red, now have greens)")
        for name in fixed:
            st = old[name]
            out.append(f"- ~~{name}~~ — was {st['fail']} fail")
        out.append("")

    if broken:
        out.append("### Newly broken (not persistently red before)")
        for name in broken:
            st = new[name]
            out.append(f"- **{name}** — {st['fail']} fail / {st['other']} other")
            if st["sample"]:
                out.append(f"  - `{_short(st['sample'])}`")
            if st["url"]:
                out.append(f"  - [logs]({st['url']})")
        out.append("")

    if still:
        out.append("### Still broken")
        for name in still[:20]:
            st = new[name]
            out.append(f"- **{name}** — {st['fail']} fail (was {old[name]['fail']})")
        if len(still) > 20:
            out.append(f"_…+{len(still)-20} more_")
        out.append("")

    return "\n".join(out)


def _diff_unclear(before: dict, after: dict) -> str:
    old = _compute_unclear(before)
    new = _compute_unclear(after)
    old_names = set(old)
    new_names = set(new)

    appeared = sorted(new_names - old_names)
    cleared = sorted(old_names - new_names)
    recurring = sorted(old_names & new_names, key=lambda n: -new[n]["count"])

    out = ["## Unclear transitions (green→red, unhandled)"]
    out.append(
        f"_{len(appeared)} new · {len(cleared)} cleared · {len(recurring)} recurring_"
    )
    out.append("")

    if cleared:
        out.append("### Cleared (had unclear transitions before, none now)")
        for name in cleared:
            st = old[name]
            out.append(f"- ~~{name}~~ — was {st['count']}x")
        out.append("")

    if appeared:
        out.append("### New unclear failures")
        for name in appeared:
            st = new[name]
            out.append(
                f"- **{name}** — {st['count']}x on [{', '.join(st['commits'][:5])}]"
            )
            if st["sample"]:
                out.append(f"  - `{_short(st['sample'])}`")
            if st["url"]:
                out.append(f"  - [logs]({st['url']})")
        out.append("")

    if recurring:
        out.append("### Recurring (unclear transitions in both runs)")
        for name in recurring[:30]:
            st_new = new[name]
            st_old = old[name]
            out.append(f"- **{name}** — {st_old['count']}x → {st_new['count']}x")
            if st_new["sample"]:
                out.append(f"  - `{_short(st_new['sample'])}`")
        if len(recurring) > 30:
            out.append(f"_…+{len(recurring)-30} more recurring_")
        out.append("")

    return "\n".join(out)


def _diff_autorevert(before: dict, after: dict) -> str:
    def _stats(data):
        ar = data.get("autorevert_commits", [])
        av = data.get("advisor_verdicts", [])
        from collections import Counter
        mix = Counter(v.get("verdict", "?") for v in av)
        return len(ar), len(av), dict(mix)

    b_ar, b_av, b_mix = _stats(before)
    a_ar, a_av, a_mix = _stats(after)
    out = ["## Autorevert system"]
    out.append(f"- autoreverts: {b_ar} → {a_ar}")
    out.append(f"- advisor verdicts: {b_av} → {a_av}")
    out.append(f"  - before: {b_mix}")
    out.append(f"  - after:  {a_mix}")
    out.append("")
    return "\n".join(out)
