"""Offline tests for the El Paso (EPNG / Kinder Morgan) maintenance source.

Exercises the pure ``parse_*`` functions against a SMALL trimmed fixture extracted
from a real ~4MB notice-detail page: ``tests/fixtures/maint/el_paso_maint_list.html``
holds the notice header <span> labels + the "Maintenance List" table from EPNG
notice 628770 ("Updated June 2026 Maintenance", a SUPERSEDE of 628754). Values were
confirmed against the raw detail under exploration/notices/el_paso/raw/.
"""
import pathlib

import pytest
from bs4 import BeautifulSoup

from etl.maintenance_sources import el_paso

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint" / "el_paso_maint_list.html"
PULLED = "2026-06-25T05:00:00Z"


def fixture_html() -> str:
    return FIX.read_text(encoding="utf-8")


@pytest.fixture
def soup():
    return BeautifulSoup(fixture_html(), "lxml")


@pytest.fixture
def header(soup):
    return el_paso.parse_header(soup)


@pytest.fixture
def impacts(header):
    return el_paso.parse_maint_impacts(fixture_html(), header, PULLED)


# --------------------------------------------------------------------------- #
# header / NoticeEvent
# --------------------------------------------------------------------------- #
def test_parse_header_fields(header):
    assert header["notice_id"] == "628770"
    assert header["notice_type_desc_1"] == "MAINTENANCE"
    assert header["critical"] == "Y"
    assert header["notice_stat"] == "SUPERSEDE"
    assert header["prior_notice"] == "628754"
    assert header["subject"] == "Updated June 2026 Maintenance"
    assert header["tsp"] == "8001703-EL PASO NATURAL GAS CO. L.L.C."


def test_notice_event(header):
    ev = el_paso.parse_notice_event(header, PULLED)
    assert ev.source == "el_paso"
    assert ev.notice_id == "628770"
    assert ev.notice_type == "maintenance"
    assert ev.notice_type_raw == "MAINTENANCE"
    assert ev.category == "maintenance"
    assert ev.severity == "high"          # Critical == "Y"
    assert ev.status == "supersede"
    assert ev.prior_notice_id == "628754"
    assert ev.effective_start == "2026-06-24"
    assert ev.effective_end == "2026-06-25"
    assert ev.posted_at_utc == "2026-06-24"
    assert ev.headline == "Updated June 2026 Maintenance"
    assert ev.affects_pge is True
    assert ev.url.endswith("notc_nbr=628770")
    # orchestrator-owned fields left at their defaults
    assert ev.is_current is True
    assert ev.has_capacity_impact is False
    assert ev.primary_point_id is None


# --------------------------------------------------------------------------- #
# Maintenance List -> MaintenanceImpact rows
# --------------------------------------------------------------------------- #
def test_row_count(impacts):
    assert len(impacts) == 81


def test_first_row_values(impacts):
    """First data row: NORTH ML, 06/01/26, Line 1201 pipeline remediation."""
    r0 = impacts[0]
    assert r0.source == "el_paso"
    assert r0.notice_id == "628770"
    assert r0.maintenance_id == "el_paso:628770:0"
    assert r0.affected_label == "NORTH ML"
    assert r0.segment_or_gate == "NORTH ML"
    assert r0.point_id is None
    assert r0.join_kind == "segment_name"
    assert r0.date_start == "2026-06-01"
    assert r0.date_end == "2026-06-01"
    assert r0.restriction_type == "NM"     # Region column
    assert r0.capacity_basis == "reduction"
    assert r0.base_capacity_dthd == 2223400.0
    assert r0.reduction_dthd == 196319.0
    assert r0.reduction_planned_dthd == 196319.0
    assert r0.reduction_fm_dthd == 0.0
    assert r0.capacity_remaining_dthd == 2027081.0
    assert r0.pct_of_capacity == 91.2       # 2027081 / 2223400 * 100
    assert r0.original_value == 196319.0     # Total Reduction
    assert r0.original_units == "Dth/d"
    assert r0.is_unplanned is False          # FMJ == 0
    assert "Line 1201 pipeline remediation" in r0.work_description
    # orchestrator-owned fields left at their defaults
    assert r0.pulled_at_utc == PULLED


def test_numeric_scheduling_location_is_point_id(impacts):
    """A bare numeric Scheduling Location (e.g. 57419) becomes the point_id join."""
    by_point = {r.point_id: r for r in impacts if r.point_id}
    assert "57419" in by_point
    r = by_point["57419"]
    assert r.join_kind == "point_id"
    assert r.segment_or_gate is None
    assert r.affected_label == "57419"
    assert r.base_capacity_dthd == 150000.0
    assert r.capacity_remaining_dthd == 100000.0
    assert r.pct_of_capacity == 66.7        # 100000 / 150000 * 100


def test_join_kind_split(impacts):
    point_rows = [r for r in impacts if r.join_kind == "point_id"]
    segment_rows = [r for r in impacts if r.join_kind == "segment_name"]
    assert len(point_rows) == 7
    assert len(segment_rows) == 74
    # numeric-loc rows carry a point_id and no segment label; segment rows the reverse
    assert all(r.point_id and r.segment_or_gate is None for r in point_rows)
    assert all(r.point_id is None and r.segment_or_gate for r in segment_rows)


def test_fmj_marks_unplanned(impacts):
    """Rows with a force-majeure (FMJ) reduction component are flagged unplanned."""
    unplanned = [r for r in impacts if r.is_unplanned]
    assert len(unplanned) == 26
    assert all((r.reduction_fm_dthd or 0) > 0 for r in unplanned)
    # ...and rows with no FMJ are not unplanned
    assert all(not r.is_unplanned for r in impacts if (r.reduction_fm_dthd or 0) == 0)


def test_net_equals_base_minus_total_reduction(impacts):
    """The source invariant: Net == Base - Total Reduction, for every row."""
    checked = 0
    for r in impacts:
        if r.base_capacity_dthd is None or r.reduction_dthd is None:
            continue
        assert r.capacity_remaining_dthd == round(r.base_capacity_dthd - r.reduction_dthd, 1)
        checked += 1
    assert checked == 81   # every row carries the full Base / Reduction / Net triple


def test_pct_is_remaining_over_base(impacts):
    """pct_of_capacity is strictly capacity_remaining / base * 100."""
    for r in impacts:
        if r.base_capacity_dthd and r.capacity_remaining_dthd is not None:
            assert r.pct_of_capacity == round(
                r.capacity_remaining_dthd / r.base_capacity_dthd * 100.0, 1
            )


def test_all_rows_native_dthd(impacts):
    assert all(r.original_units == "Dth/d" for r in impacts)
    assert all(r.capacity_basis == "reduction" for r in impacts)
