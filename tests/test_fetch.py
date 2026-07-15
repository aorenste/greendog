"""Tests for greendog.fetch.

Focus: fetch_hud_grid must keep each commit's per-job cells aligned to the
right job *name* when walking multiple HUD pages. The HUD API returns a
`jobNames` list per page and each commit's `jobs` array is positionally
aligned to *that page's* names — but different pages (covering different
commit ranges) return different name sets/orders. Concatenating rows while
keeping only page 0's names silently misattributes every later page's cells.
"""
from __future__ import annotations

import datetime as _dt

from greendog import fetch


class FakeClient:
    """Serves canned HUD page payloads by page index; empty beyond the list.

    Records the page indices requested so tests can assert we don't over-fetch.
    """

    def __init__(self, pages: list[dict]):
        self.pages = pages
        self.requested_pages: list[int] = []

    def get_json(self, path: str, ttl=None):
        page = int(path.rstrip("/").rsplit("/", 1)[-1])
        self.requested_pages.append(page)
        if 0 <= page < len(self.pages):
            return self.pages[page]
        return {"shaGrid": [], "jobNames": []}


class _FrozenNow(_dt.datetime):
    """datetime subclass with a fixed now(); other methods (fromisoformat) are
    inherited unchanged so _parse_time keeps working after monkeypatching."""

    @classmethod
    def now(cls, tz=None):
        return _dt.datetime(2026, 7, 15, 12, 0, 0, tzinfo=tz)


def _job(name: str, conclusion: str = "success") -> dict:
    # `tag` records the column this cell truly belongs to, so tests can assert
    # alignment independent of position.
    return {"tag": name, "conclusion": conclusion}


def _pages_with_divergent_names() -> list[dict]:
    return [
        {
            "jobNames": ["A", "B"],
            "shaGrid": [
                {
                    "sha": "c0",
                    "time": "2026-07-15T12:00:00Z",
                    "jobs": [_job("A"), _job("B")],
                }
            ],
        },
        {
            # Different order AND a new column ("C") absent from page 0.
            "jobNames": ["B", "C", "A"],
            "shaGrid": [
                {
                    "sha": "c1",
                    "time": "2026-07-15T11:00:00Z",
                    "jobs": [_job("B"), _job("C"), _job("A")],
                }
            ],
        },
    ]


# Large window (~50y) so time-based pagination never excludes our synthetic
# commits, while staying inside datetime's representable range.
BIG_HOURS = 24 * 365 * 50


def test_cells_stay_aligned_to_job_names_across_pages():
    grid = fetch.fetch_hud_grid(FakeClient(_pages_with_divergent_names()), hours=BIG_HOURS)
    names = grid["jobNames"]
    for row in grid["shaGrid"]:
        for j, cell in enumerate(row["jobs"]):
            if isinstance(cell, dict) and cell.get("tag"):
                assert cell["tag"] == names[j], (
                    f"commit {row['sha']} cell at index {j} is job {cell['tag']!r} "
                    f"but jobNames[{j}]={names[j]!r}"
                )


def test_later_page_row_maps_to_correct_named_column():
    grid = fetch.fetch_hud_grid(FakeClient(_pages_with_divergent_names()), hours=BIG_HOURS)
    names = grid["jobNames"]
    by_sha = {r["sha"]: r for r in grid["shaGrid"]}
    # Every declared column must exist in canonical names.
    for n in ("A", "B", "C"):
        assert n in names
    # c1 came from page 1 (order B,C,A); its "A" cell must land under column A.
    c1 = by_sha["c1"]
    assert c1["jobs"][names.index("A")].get("tag") == "A"
    assert c1["jobs"][names.index("B")].get("tag") == "B"
    assert c1["jobs"][names.index("C")].get("tag") == "C"


def test_missing_columns_filled_with_empty_dict():
    grid = fetch.fetch_hud_grid(FakeClient(_pages_with_divergent_names()), hours=BIG_HOURS)
    names = grid["jobNames"]
    by_sha = {r["sha"]: r for r in grid["shaGrid"]}
    # c0 (page 0) never ran column "C"; it must be a {} sentinel, not misaligned.
    c0 = by_sha["c0"]
    assert c0["jobs"][names.index("C")] == {}
    # Every row's jobs list is padded to the full canonical width.
    for row in grid["shaGrid"]:
        assert len(row["jobs"]) == len(names)


def test_identical_pages_preserve_order_and_content():
    # When pages share the same names (the common adjacent-page case), output
    # order must be unchanged — the fix must not perturb the aligned path.
    page = {
        "jobNames": ["A", "B"],
        "shaGrid": [
            {"sha": "c0", "time": "2026-07-15T12:00:00Z", "jobs": [_job("A"), _job("B")]},
        ],
    }
    page2 = {
        "jobNames": ["A", "B"],
        "shaGrid": [
            {"sha": "c1", "time": "2026-07-15T11:00:00Z", "jobs": [_job("A"), _job("B")]},
        ],
    }
    grid = fetch.fetch_hud_grid(FakeClient([page, page2]), hours=BIG_HOURS)
    assert grid["jobNames"] == ["A", "B"]
    assert [r["sha"] for r in grid["shaGrid"]] == ["c0", "c1"]
    for row in grid["shaGrid"]:
        assert [c["tag"] for c in row["jobs"]] == ["A", "B"]


def test_window_excludes_old_commits_and_walks_pages_by_time(monkeypatch):
    # Frozen now = 2026-07-15T12:00Z, hours=24 -> cutoff = 2026-07-14T12:00Z.
    monkeypatch.setattr(fetch, "datetime", _FrozenNow)
    pages = [
        {
            "jobNames": ["A"],
            "shaGrid": [
                {"sha": "new", "time": "2026-07-15T10:00:00Z", "jobs": [_job("A")]}
            ],
        },
        {
            # page 0's oldest is still in-window, so we page into page 1, whose
            # commit is below the cutoff and must be dropped.
            "jobNames": ["A"],
            "shaGrid": [
                {"sha": "old", "time": "2026-07-14T06:00:00Z", "jobs": [_job("A")]}
            ],
        },
    ]
    client = FakeClient(pages)
    grid = fetch.fetch_hud_grid(client, hours=24)
    assert [r["sha"] for r in grid["shaGrid"]] == ["new"]
    assert grid["pages_walked"] == 2
    assert client.requested_pages == [0, 1]


def test_paging_stops_without_over_fetching(monkeypatch):
    # When a page already contains a commit past the cutoff, we must stop and
    # not request the next page.
    monkeypatch.setattr(fetch, "datetime", _FrozenNow)
    pages = [
        {
            "jobNames": ["A"],
            "shaGrid": [
                {"sha": "new", "time": "2026-07-15T10:00:00Z", "jobs": [_job("A")]},
                {"sha": "old", "time": "2026-07-14T06:00:00Z", "jobs": [_job("A")]},
            ],
        },
        {
            "jobNames": ["A"],
            "shaGrid": [
                {"sha": "nope", "time": "2026-07-13T00:00:00Z", "jobs": [_job("A")]}
            ],
        },
    ]
    client = FakeClient(pages)
    grid = fetch.fetch_hud_grid(client, hours=24)
    assert [r["sha"] for r in grid["shaGrid"]] == ["new"]
    assert grid["pages_walked"] == 1
    assert client.requested_pages == [0]  # page 1 never fetched


def test_ragged_rows_align_by_name():
    # A row shorter than jobNames -> missing columns become {}; a row longer
    # than jobNames -> the unnamed extra cell is dropped.
    pages = [
        {
            "jobNames": ["A", "B", "C"],
            "shaGrid": [
                {
                    "sha": "short",
                    "time": "2026-07-15T12:00:00Z",
                    "jobs": [_job("A"), _job("B")],
                },
                {
                    "sha": "long",
                    "time": "2026-07-15T11:00:00Z",
                    "jobs": [_job("A"), _job("B"), _job("C"), _job("EXTRA")],
                },
            ],
        }
    ]
    grid = fetch.fetch_hud_grid(FakeClient(pages), hours=BIG_HOURS)
    names = grid["jobNames"]
    assert names == ["A", "B", "C"]
    by_sha = {r["sha"]: r for r in grid["shaGrid"]}
    # short row: column C wasn't run -> {} sentinel, others aligned
    assert by_sha["short"]["jobs"][names.index("C")] == {}
    assert by_sha["short"]["jobs"][names.index("A")]["tag"] == "A"
    # long row: A/B/C aligned, the unnamed 4th cell dropped everywhere
    assert [c.get("tag") for c in by_sha["long"]["jobs"]] == ["A", "B", "C"]
    assert all(
        c.get("tag") != "EXTRA" for r in grid["shaGrid"] for c in r["jobs"]
    )
    # every row padded to full canonical width
    for row in grid["shaGrid"]:
        assert len(row["jobs"]) == len(names)


def test_duplicate_column_name_collapses_last_wins():
    # Documents the accepted degenerate behavior: a repeated name within a page
    # collapses to one canonical column and the later cell wins.
    pages = [
        {
            "jobNames": ["A", "A", "B"],
            "shaGrid": [
                {
                    "sha": "c0",
                    "time": "2026-07-15T12:00:00Z",
                    "jobs": [_job("A", "first"), _job("A", "second"), _job("B")],
                }
            ],
        }
    ]
    grid = fetch.fetch_hud_grid(FakeClient(pages), hours=BIG_HOURS)
    names = grid["jobNames"]
    assert names == ["A", "B"]  # duplicate column deduped
    c0 = grid["shaGrid"][0]
    assert c0["jobs"][names.index("A")]["conclusion"] == "second"  # last wins
    assert c0["jobs"][names.index("B")]["tag"] == "B"
