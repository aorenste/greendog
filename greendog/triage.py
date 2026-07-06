"""OSS PR triage: is a maintainer already engaged?

The pytorch/pytorch "needs triage" queue is open, non-draft PRs labeled
`open source` but not `triaged` and not yet approved.  This module answers
step 1 of triage deterministically: if someone with merge/review rights is
already engaged, we can bulk-mark the PR `triaged` (a human is on the hook).

No LLM is needed for this step — the signal is GitHub's authorAssociation.
See CLAUDE.md "OSS PR triage modality" for the rubric this encodes.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any

REPO = "pytorch/pytorch"

# The canonical "needs triage" search (the is:pr/repo/state parts are supplied
# by `gh pr list` flags, so they're omitted here).
SEARCH = (
    'base:main -label:triaged draft:false label:"open source" '
    "NOT WIP NOT TESTING in:title -review:approved sort:updated-desc"
)

# authorAssociation values that indicate merge/review rights.
MAINTAINER_ASSOC = {"MEMBER", "COLLABORATOR"}

# A PR carrying this label is already claimed for landing (Edward's mergedog
# automation, or jansel's).  Whoever claimed it should already be a reviewer,
# so it will normally be caught by engagement anyway — but skip it explicitly
# so we never fight over an owned PR.
CLAIMED_LABELS = {"mergedog"}

# Comment bodies that are mechanical bot triggers, NOT real engagement.  A
# maintainer leaving only one of these has not actually reviewed the PR.
#   - jansel's automation posts "@claude review these changes" — per direct
#     agreement this is NOT him signing up to review/land.
#   - "@pytorchbot fix-lint" / other bot commands are drive-bys.
BOT_COMMAND_PREFIXES = ("@claude", "@pytorchbot", "@pytorch-bot", "@pytorchmergebot")


def _is_bot(login: str) -> bool:
    login = login.lower()
    return login.endswith("bot") or login in {
        "claude",
        "facebook-github-bot",
        "codecov",
    }


def _is_bot_command(body: str) -> bool:
    b = (body or "").strip().lower()
    return b.startswith(BOT_COMMAND_PREFIXES)


def fetch_prs() -> list[dict]:
    """Fetch the needs-triage queue with reviews + comments in one call."""
    cmd = [
        "gh", "pr", "list",
        "--repo", REPO,
        "--search", SEARCH,
        "--state", "open",
        "--limit", "200",
        "--json",
        "number,title,author,labels,reviewRequests,reviews,comments",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def _engaged_maintainers(pr: dict) -> list[str]:
    """Non-author, non-bot maintainers who substantively engaged.

    Returns the sorted usernames who should be on the hook (and thus
    reviewers).  Empty means nobody with merge rights has engaged.
    """
    author = (pr.get("author") or {}).get("login")
    engaged: set[str] = set()

    for rv in pr.get("reviews") or []:
        u = (rv.get("author") or {}).get("login")
        if not u or u == author or _is_bot(u):
            continue
        if rv.get("authorAssociation") in MAINTAINER_ASSOC:
            # Any real review state (COMMENTED/CHANGES_REQUESTED/APPROVED) counts.
            engaged.add(u)

    for c in pr.get("comments") or []:
        u = (c.get("author") or {}).get("login")
        if not u or u == author or _is_bot(u):
            continue
        if c.get("authorAssociation") not in MAINTAINER_ASSOC:
            continue
        if _is_bot_command(c.get("body", "")):
            continue  # mechanical drive-by, not engagement
        engaged.add(u)

    return sorted(engaged)


def adjudicate(prs: list[dict]) -> list[dict]:
    """Classify each PR.  Pure function over fetched data."""
    results = []
    for pr in prs:
        labels = {l["name"] for l in (pr.get("labels") or [])}
        # reviewRequests entries are Users (login) or Teams (slug); keep both.
        req = {
            r.get("login") or r.get("slug")
            for r in (pr.get("reviewRequests") or [])
        }
        req.discard(None)
        claimed = labels & CLAIMED_LABELS
        engaged = _engaged_maintainers(pr)

        if claimed:
            verdict = "claimed"  # already owned for landing; leave alone
        elif engaged:
            verdict = "mark_triaged"
        else:
            verdict = "needs_triage"

        results.append({
            "number": pr["number"],
            "title": pr["title"],
            "author": (pr.get("author") or {}).get("login"),
            "verdict": verdict,
            "on_the_hook": engaged,
            "add_reviewers": [u for u in engaged if u not in req],
            "claimed_by": sorted(claimed),
        })
    return results


def _apply(r: dict) -> None:
    """Add the triaged label and any missing reviewers for one PR."""
    n = str(r["number"])
    subprocess.run(
        ["gh", "pr", "edit", n, "--repo", REPO, "--add-label", "triaged"],
        check=True, capture_output=True, text=True,
    )
    # --add-reviewer takes one reviewer per flag; add them one at a time.
    for u in r["add_reviewers"]:
        subprocess.run(
            ["gh", "pr", "edit", n, "--repo", REPO, "--add-reviewer", u],
            check=True, capture_output=True, text=True,
        )


def cmd_triage(args) -> None:
    print(f"fetching needs-triage queue from {REPO}…", file=sys.stderr)
    prs = fetch_prs()
    print(f"  {len(prs)} PR(s) in queue", file=sys.stderr)
    results = adjudicate(prs)

    if args.raw:
        print(json.dumps(results, indent=2))
        return

    mt = [r for r in results if r["verdict"] == "mark_triaged"]
    nt = [r for r in results if r["verdict"] == "needs_triage"]
    cl = [r for r in results if r["verdict"] == "claimed"]

    print(f"\n## mark_triaged ({len(mt)}) — maintainer already engaged")
    for r in sorted(mt, key=lambda x: x["number"]):
        add = f"  +reviewer {','.join(r['add_reviewers'])}" if r["add_reviewers"] else ""
        print(f"  #{r['number']}  hook={','.join(r['on_the_hook'])}{add}")
        print(f"      {r['title'][:80]}")

    if cl:
        print(f"\n## claimed ({len(cl)}) — labeled for landing, left alone")
        for r in sorted(cl, key=lambda x: x["number"]):
            print(f"  #{r['number']}  claimed_by={','.join(r['claimed_by'])}  {r['title'][:60]}")

    print(f"\n## needs_triage ({len(nt)}) — no maintainer engaged")
    for r in sorted(nt, key=lambda x: x["number"]):
        print(f"  #{r['number']}  by {r['author']}  {r['title'][:60]}")

    if not args.apply:
        print(
            f"\n[dry-run] {len(mt)} PR(s) would be labeled triaged. "
            "Re-run with --apply to act.",
            file=sys.stderr,
        )
        return

    print(f"\napplying to {len(mt)} PR(s)…", file=sys.stderr)
    for r in mt:
        try:
            _apply(r)
            add = f" +{','.join(r['add_reviewers'])}" if r["add_reviewers"] else ""
            print(f"  #{r['number']}: triaged{add}", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  #{r['number']}: FAILED — {e.stderr.strip()}", file=sys.stderr)
