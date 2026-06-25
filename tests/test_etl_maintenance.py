"""Offline tests for the maintenance/notices facts core (etl.maintenance) and the
nova reference source module, against committed fixtures (no network)."""
import datetime as dt
import pathlib

import pytest

from ebb.schema import MaintenanceImpact, NoticeEvent
from etl import maintenance as M
from etl.maintenance_sources import nova

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
GAS_DAY = "2026-06-23"
PULLED = "2026-06-25T00:00:00Z"


def _notice(source, nid, ntype, prior=None, status="initiate"):
    return NoticeEvent(
        source=source, notice_id=nid, notice_type=ntype, notice_type_raw=ntype,
        severity=M.severity_for(ntype), category=M.category_for(ntype),
        posted_at_utc=None, effective_start="2026-06-20", effective_end="2026-06-27",
        status=status, prior_notice_id=prior, is_current=True, has_capacity_impact=False,
        primary_point_id=None, affects_pge=True, headline="h", body="b", url="u",
        gas_day="2026-06-20", pulled_at_utc=PULLED,
    )


# --------------------------------------------------------------------------- #
# Unit conversion + pct
# --------------------------------------------------------------------------- #


def test_to_dth_by_unit():
    assert M.to_dth(1960, "MMcf/d") == 1960000.0           # MMcf/d * 1000 BTU/cf
    assert M.to_dth(650000, "MMBtu/d") == 650000.0         # MMBtu == Dth
    assert M.to_dth(500, "Dth/d") == 500.0                 # already canonical
    assert M.to_dth(362000, "10^3m^3/d") == pytest.approx(12783909.4, abs=0.5)
    assert M.to_dth(None, "MMcf/d") is None


def test_pct_of_capacity_definition():
    assert M.pct_of_capacity(226000, 243000) == 93.0
    assert M.pct_of_capacity(100, None) is None            # no base -> null (strict remaining/base)
    assert M.pct_of_capacity(None, 100) is None


# --------------------------------------------------------------------------- #
# Year-less month/day range parsing (PG&E foghorn)
# --------------------------------------------------------------------------- #


def test_parse_monthday_range():
    anchor = dt.date(2026, 6, 24)
    assert M.parse_monthday_range("June 28", anchor) == ("2026-06-28", "2026-06-28")
    assert M.parse_monthday_range("June 29 - 30", anchor) == ("2026-06-29", "2026-06-30")
    assert M.parse_monthday_range("July 13 - 31", anchor) == ("2026-07-13", "2026-07-31")
    assert M.parse_monthday_range("June 27 - July 02", anchor) == ("2026-06-27", "2026-07-02")
    # a month well behind the anchor wrapped past December -> next year
    assert M.parse_monthday_range("January 05", anchor) == ("2027-01-05", "2027-01-05")
    assert M.parse_monthday_range("", anchor) == (None, None)


# --------------------------------------------------------------------------- #
# Supersession -> is_current, capacity linkage, base backfill
# --------------------------------------------------------------------------- #


def test_resolve_is_current_marks_superseded_stale():
    notices = [_notice("gtn", "1585", "maintenance", prior="1584", status="supersede"),
               _notice("gtn", "1584", "maintenance")]
    M.resolve_is_current(notices)
    by_id = {n.notice_id: n.is_current for n in notices}
    assert by_id == {"1585": True, "1584": False}


def test_resolve_is_current_terminate_is_not_current():
    notices = [_notice("kern_river", "x", "advisory", status="terminate")]
    M.resolve_is_current(notices)
    assert notices[0].is_current is False


def test_link_capacity_impact_sets_flag_and_primary_point():
    notices = [_notice("gtn", "1585", "maintenance"), _notice("kern_river", "z", "advisory")]
    impacts = [MaintenanceImpact(
        source="gtn", maintenance_id="gtn:1585:18480", notice_id="1585", point_id="18480",
        segment_or_gate=None, affected_label="Station 9 CFTP", join_kind="point_id",
        date_start="2026-06-21", date_end="2026-06-27", capacity_basis="remaining",
        capacity_remaining_dthd=1650000.0, reduction_dthd=None, base_capacity_dthd=None,
        pct_of_capacity=None, pct_firm_cut=None, reduction_planned_dthd=None,
        reduction_fm_dthd=None, original_value=1650.0, original_units="MMcf/d",
        restriction_type=None, work_description="x", is_unplanned=False, pulled_at_utc=PULLED,
    )]
    M.link_capacity_impact(notices, impacts)
    g = next(n for n in notices if n.notice_id == "1585")
    k = next(n for n in notices if n.notice_id == "z")
    assert g.has_capacity_impact and g.primary_point_id == "18480"
    assert not k.has_capacity_impact and k.primary_point_id is None


def test_backfill_base_capacity_derives_pct_and_reduction():
    im = MaintenanceImpact(
        source="gtn", maintenance_id="m", notice_id="1585", point_id="18480",
        segment_or_gate=None, affected_label="x", join_kind="point_id",
        date_start="2026-06-21", date_end="2026-06-27", capacity_basis="remaining",
        capacity_remaining_dthd=900000.0, reduction_dthd=None, base_capacity_dthd=None,
        pct_of_capacity=None, pct_firm_cut=None, reduction_planned_dthd=None,
        reduction_fm_dthd=None, original_value=900.0, original_units="MMcf/d",
        restriction_type=None, work_description="x", is_unplanned=False, pulled_at_utc=PULLED,
    )
    M.backfill_base_capacity([im], {("gtn", "18480"): 1650000.0})
    assert im.base_capacity_dthd == 1650000.0
    assert im.pct_of_capacity == pytest.approx(54.5, abs=0.1)
    assert im.reduction_dthd == 750000.0


# --------------------------------------------------------------------------- #
# nova reference source
# --------------------------------------------------------------------------- #


def test_nova_parse_outages_known_row_and_units():
    text = (FIX / "nova_outages.csv").read_text(encoding="utf-8")
    impacts = nova.parse_outages(text, GAS_DAY, PULLED)
    assert impacts and all(i.source == "nova" and i.original_units == "10^3m^3/d" for i in impacts)
    # Meikle River D5 @ USJR (UID 202168343): capability 362000, local 243000->226000.
    row = next(i for i in impacts if i.maintenance_id == "nova:202168343")
    assert row.segment_or_gate == "USJR" and row.join_kind == "gate_code"
    assert row.date_start == "2026-06-22" and row.date_end == "2026-06-26"
    assert row.capacity_remaining_dthd == pytest.approx(12783909.4, abs=0.5)
    assert row.reduction_dthd == pytest.approx(600349.3, abs=0.5)   # (243000-226000) in Dth/d
    assert row.base_capacity_dthd is None and row.pct_of_capacity is None  # local pair != gate base
    assert row.original_value == 362000.0


def test_nova_window_filters_far_future():
    text = (FIX / "nova_outages.csv").read_text(encoding="utf-8")
    impacts = nova.parse_outages(text, GAS_DAY, PULLED)
    # horizon is gas_day + 45d = 2026-08-07; an October/November outage is excluded.
    assert all((i.date_start or "") <= "2026-08-07" for i in impacts)
    assert not any((i.date_start or "") >= "2026-10-01" for i in impacts)


def test_nova_plant_turnarounds():
    text = (FIX / "nova_plant_turnarounds.csv").read_text(encoding="utf-8")
    impacts = nova.parse_plant_turnarounds(text, GAS_DAY, PULLED)
    assert impacts and all(i.capacity_basis == "reduction" and i.join_kind == "text_label" for i in impacts)
    first = impacts[0]
    assert first.affected_label.startswith("Upstream plant turnaround")
    assert first.reduction_dthd is not None and first.capacity_remaining_dthd is None


# --------------------------------------------------------------------------- #
# Writers — stable header even when empty
# --------------------------------------------------------------------------- #


def test_write_current_stable_headers(tmp_path):
    text = (FIX / "nova_outages.csv").read_text(encoding="utf-8")
    impacts = nova.parse_outages(text, GAS_DAY, PULLED)
    paths = M.write_current([], impacts, tmp_path)          # empty notices, real impacts
    n_lines = paths["notices"].read_text(encoding="utf-8").splitlines()
    m_lines = paths["maintenance"].read_text(encoding="utf-8").splitlines()
    assert n_lines[0].split(",") == M.FACT_NOTICES_COLUMNS    # header present even with 0 rows
    assert m_lines[0].split(",") == M.FACT_MAINTENANCE_COLUMNS
    assert len(m_lines) == len(impacts) + 1
    assert len(M.FACT_MAINTENANCE_COLUMNS) == 23 and len(M.FACT_NOTICES_COLUMNS) == 20
