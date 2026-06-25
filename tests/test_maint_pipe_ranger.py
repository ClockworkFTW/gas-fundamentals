"""Offline tests for the PG&E / Pipe Ranger maintenance source.

Exercises the pure parsers against committed fixtures (no network):
  - ``parse_foghorn`` merges the 3 foghorn views (pipelineCap / maxCap / firmCuts)
    per (path, Dates) into MaintenanceImpact rows — Redwood one path row, Baja three
    delivery points (Kettleman / Hinkley / Topock).
  - ``parse_flow_orders`` maps OFO/EFO archive rows to NoticeEvent.

Fixtures are the raw foghorn JSON saved under
exploration/notices/pipe_ranger/maint_probe/, copied verbatim into
tests/fixtures/maint/. Values asserted here were confirmed against the raw and
against exploration/maintenance/pipe_ranger.json.
"""
import json
import pathlib

import pytest

from etl.maintenance_sources import pipe_ranger as pr

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
GAS_DAY = "2026-06-25"
PULLED = "2026-06-25T05:00:00Z"


def _load(path: str, view: str) -> list[dict]:
    return json.loads((FIX / f"pipe_ranger_foghorn_{path}_{view}.json").read_text(encoding="utf-8"))


@pytest.fixture
def views() -> dict[str, list[dict]]:
    return {f"{p}_{v}": _load(p, v) for p in pr.PATHS for v in pr.VIEWS}


@pytest.fixture
def impacts(views):
    return pr.parse_foghorn(views, GAS_DAY, PULLED)


# --------------------------------------------------------------------------- #
# row counts + shape
# --------------------------------------------------------------------------- #


def test_row_count_redwood_plus_baja(impacts):
    # 7 Redwood Dates (one path row each) + 6 Baja Dates x 3 points = 25 rows.
    assert len(impacts) == 25
    labels = [im.affected_label for im in impacts]
    assert labels.count("Redwood Path") == 7
    assert labels.count("Kettleman") == 6
    assert labels.count("Hinkley") == 6
    assert labels.count("Topock") == 6


def test_common_invariants(impacts):
    assert all(im.source == "pipe_ranger" for im in impacts)
    assert all(im.join_kind == "path" for im in impacts)
    assert all(im.capacity_basis == "remaining" for im in impacts)
    assert all(im.original_units == "MMcf/d" for im in impacts)
    assert all(im.is_unplanned is False for im in impacts)
    # v1: Topock keeps point_id null (orchestrator links it); notices unlinked here.
    assert all(im.point_id is None for im in impacts)
    assert all(im.notice_id is None for im in impacts)
    assert all(im.pulled_at_utc == PULLED for im in impacts)


# --------------------------------------------------------------------------- #
# Redwood — capacity / pct / base back-computation / dates
# --------------------------------------------------------------------------- #


def test_redwood_first_row_values(impacts):
    # raw "June 28": Capacity 1960 MMcf/d, maxCap 99.0%, firmCuts 0%.
    rw = next(im for im in impacts if im.affected_label == "Redwood Path" and im.date_start == "2026-06-28")
    assert rw.date_end == "2026-06-28"
    assert rw.original_value == 1960.0
    assert rw.capacity_remaining_dthd == 1960000.0      # 1960 MMcf/d * 1000 BTU/cf
    assert rw.pct_of_capacity == 99.0                    # the maxCap %
    assert rw.pct_firm_cut == 0.0
    # base back-computed from remaining / (pct/100): 1960000 / 0.99 -> 1979798.0
    assert rw.base_capacity_dthd == 1979798.0
    assert rw.reduction_dthd == round(1979798.0 - 1960000.0, 1)
    assert rw.work_description == "Burney Station Maintenance"


def test_redwood_firm_cut_and_multi_note(impacts):
    # raw "July 21": firmCuts 0.92%, two maintenance <p> notes joined.
    rw = next(im for im in impacts if im.affected_label == "Redwood Path" and im.date_start == "2026-07-21")
    assert rw.date_end == "2026-07-21"
    assert rw.pct_firm_cut == 0.92
    assert rw.capacity_remaining_dthd == 1840000.0
    assert rw.work_description == "Burney Station Maintenance; Gerber Station Maintenance"


# --------------------------------------------------------------------------- #
# Baja — explode into 3 points, ranged dates, per-point capacity
# --------------------------------------------------------------------------- #


def test_baja_topock_first_window(impacts):
    # raw "June 17 - 25": bajaTopockCapacity 640, maxCap 65.6%.
    top = next(im for im in impacts if im.affected_label == "Topock" and im.date_start == "2026-06-17")
    assert top.date_end == "2026-06-25"
    assert top.original_value == 640.0
    assert top.capacity_remaining_dthd == 640000.0
    assert top.pct_of_capacity == 65.6
    assert top.base_capacity_dthd == round(640000.0 / (65.6 / 100.0), 1)
    assert top.reduction_dthd == round(top.base_capacity_dthd - 640000.0, 1)
    assert "L300A Pipeline Maintenance" in top.work_description


def test_baja_three_points_share_a_window(impacts):
    window = [im for im in impacts if im.date_start == "2026-06-17" and im.date_end == "2026-06-25"]
    by_label = {im.affected_label: im for im in window}
    assert set(by_label) == {"Kettleman", "Hinkley", "Topock"}
    # per-point remaining differs (Kettleman 500, Hinkley 400, Topock 640 MMcf/d)
    assert by_label["Kettleman"].capacity_remaining_dthd == 500000.0
    assert by_label["Hinkley"].capacity_remaining_dthd == 400000.0
    assert by_label["Topock"].capacity_remaining_dthd == 640000.0


def test_baja_cross_month_range_parsed(impacts):
    # "July 13 - 31" is the last Baja window; confirm end date parses.
    kett = next(im for im in impacts if im.affected_label == "Kettleman" and im.date_start == "2026-07-13")
    assert kett.date_end == "2026-07-31"


def test_baja_full_capacity_zero_reduction(impacts):
    # Topock "June 27 - 30": 975 MMcf/d at 100% -> base == remaining, reduction 0.
    top = next(im for im in impacts if im.affected_label == "Topock" and im.date_start == "2026-06-27")
    assert top.pct_of_capacity == 100.0
    assert top.base_capacity_dthd == 975000.0
    assert top.reduction_dthd == 0.0


# --------------------------------------------------------------------------- #
# OFO / EFO -> NoticeEvent
# --------------------------------------------------------------------------- #


def test_parse_flow_orders_efo():
    efo = [{
        "numCustomer": 0, "reason": "Low Inventory", "typeDesc": "Emergency Flow Order",
        "stage": 0, "typeShortName": "EFO", "nonComplianceCharge": 50,
        "gasDay": "12/21/1998", "tolerance": 0,
    }]
    notices = pr.parse_flow_orders(efo, "efo", GAS_DAY, PULLED)
    assert len(notices) == 1
    n = notices[0]
    assert n.source == "pipe_ranger"
    assert n.notice_type == "efo"
    assert n.notice_type_raw == "EFO"
    assert n.severity == "critical"        # severity_for("efo")
    assert n.category == "operational"
    assert n.gas_day == "1998-12-21"
    assert n.effective_start == "1998-12-21" and n.effective_end == "1998-12-21"
    assert "Low Inventory" in n.headline
    assert n.affects_pge is True
    assert n.is_current is True and n.has_capacity_impact is False and n.primary_point_id is None


def test_parse_flow_orders_empty_is_graceful():
    assert pr.parse_flow_orders([], "ofo", GAS_DAY, PULLED) == []
    assert pr.parse_flow_orders(None, "ofo", GAS_DAY, PULLED) == []


# --------------------------------------------------------------------------- #
# build() offline — foghorn POST + ofo/efo fetch monkeypatched out
# --------------------------------------------------------------------------- #


def test_build_offline(monkeypatch, views):
    import requests

    monkeypatch.setattr(pr, "_fetch_foghorn", lambda session, raw_dir: views)
    monkeypatch.setattr(pr.PipeRangerClient, "fetch", lambda self, key, **kw: [])
    notices, impacts = pr.build(GAS_DAY, session=requests.Session())
    assert notices == []
    assert len(impacts) == 25
