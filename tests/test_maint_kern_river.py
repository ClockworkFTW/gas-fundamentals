"""Offline tests for the Kern River maintenance source (NoticeEvent-only).

Exercises the pure ``parse_notice_grid`` over a committed Critical-notices fixture
(copied from exploration/notices/kern_river/raw/). Kern notices carry NO numeric
capacity, so the source emits NoticeEvents and ZERO MaintenanceImpacts.
"""
import pathlib

import pytest

from etl.maintenance_sources import kern_river as kr

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
PULLED = "2026-06-25T05:00:00Z"


def critical_html() -> str:
    return (FIX / "kern_river_notices_Critical.html").read_text(encoding="utf-8")


def parse():
    return kr.parse_notice_grid(critical_html(), "Critical", PULLED)


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_row_count_and_constant_fields():
    notices = parse()
    # 17 "Pipe Cond" rows in the captured Critical grid (the empty placeholder skipped).
    assert len(notices) == 17
    assert all(n.source == "kern_river" for n in notices)
    assert all(n.notice_type == "advisory" for n in notices)
    assert all(n.notice_type_raw == "Pipe Cond" for n in notices)
    # advisory -> severity low / category operational (from the shared helpers)
    assert all(n.severity == "low" for n in notices)
    assert all(n.category == "operational" for n in notices)
    # contract invariants: orchestrator owns these
    assert all(n.is_current is True for n in notices)
    assert all(n.has_capacity_impact is False for n in notices)
    assert all(n.primary_point_id is None for n in notices)
    assert all(n.affects_pge is True for n in notices)
    assert all(
        n.url == "https://services.kernrivergas.com/portal/Informational-Postings/Notices/Critical"
        for n in notices
    )


def test_no_capacity_impacts_emitted():
    # The whole point: NoticeEvent ONLY, never a MaintenanceImpact.
    notices = parse()
    by_id = {n.notice_id: n for n in notices}
    # Spot-check the dataclass has the notice grain (no numeric capacity attrs).
    assert not hasattr(by_id["20260313"], "capacity_remaining_dthd")


# --------------------------------------------------------------------------- #
# Field mapping — values confirmed against exploration/maintenance/kern_river.json
# --------------------------------------------------------------------------- #


def test_first_row_full_mapping():
    by_id = {n.notice_id: n for n in parse()}
    n = by_id["20260313"]
    assert n.headline == "Kern River - Line Pack Has Returned to Normal"
    assert n.body == n.headline           # grid has no body; subject is the line
    assert n.status == "supersede"        # NoticeStatus 2 -> supersede
    assert n.prior_notice_id == "20260308"
    assert n.effective_start == "2026-06-24"
    assert n.effective_end == "2026-06-25"
    assert n.posted_at_utc == "2026-06-24"
    assert n.gas_day == "2026-06-24"      # gas_day = effective_start


def test_status_code_word_mapping():
    by_id = {n.notice_id: n for n in parse()}
    assert by_id["20260313"].status == "supersede"   # 2
    assert by_id["20260308"].status == "terminate"   # 3
    assert by_id["20260303"].status == "supersede"   # 2
    # only codes 2 and 3 appear in this grid; 1 -> initiate is mapped too
    assert kr.STATUS_BY_CODE["1"] == "initiate"


def test_prior_notice_blank_is_none():
    by_id = {n.notice_id: n for n in parse()}
    # 20260308 has an empty PriorNoticeIdentifier in the grid -> None (not "")
    assert by_id["20260308"].prior_notice_id is None
    # 20260303 chains back to 20260302
    assert by_id["20260303"].prior_notice_id == "20260302"


def test_effective_dates_long_range_row():
    by_id = {n.notice_id: n for n in parse()}
    # the curtailment-prose notice spans 2026-06-08 -> 2026-06-21
    n = by_id["20260279"]
    assert n.effective_start == "2026-06-08"
    assert n.effective_end == "2026-06-21"
    assert n.status == "terminate"        # NoticeStatus 3
    assert n.prior_notice_id == "20260277"


# --------------------------------------------------------------------------- #
# build() returns (notices, []) with empty impacts (no network)
# --------------------------------------------------------------------------- #


def test_build_emits_no_impacts(monkeypatch):
    def fake_fetch(self, category, *, raw_dir=None):
        # Critical -> the real grid; Planned-Service-Outage -> empty table.
        if category == "Critical":
            return critical_html()
        return "<table><tr><th>NoticeIdentifier</th><th>Subject</th></tr></table>"

    monkeypatch.setattr(kr.KernRiverClient, "fetch_notice_category", fake_fetch)
    notices, impacts = kr.build("2026-06-24")
    assert impacts == []
    assert len(notices) == 17
    assert all(n.notice_type == "advisory" for n in notices)


def test_empty_grid_returns_nothing():
    html = "<table><tr><th>NoticeIdentifier</th><th>Subject</th></tr><tr><td></td><td></td></tr></table>"
    assert kr.parse_notice_grid(html, "Planned-Service-Outage", PULLED) == []
