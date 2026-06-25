"""Offline tests for the GTN maintenance source against a trimmed notices fixture.

Fixture ``fixtures/maint/gtn_notices.json`` is a 4-notice slice of a wide GTN
``Notice/Retrieve`` pull: the 2 critical ``Maint`` capacity-reduction notices
(1585 Station 9 CFTP, 1583 Rosalia/Station 6 CFTP), the monthly ``Plnd Outage``
MAINTENANCE SCHEDULE notice (1577), and one non-maintenance ``Rates-Chrgs`` notice
(1582) to exercise filtering. Asserted values are confirmed against the raw notice
``Text`` and exploration/maintenance/gtn.json.
"""
import json
import pathlib

import pytest

from etl.maintenance import to_dth
from etl.maintenance_sources import gtn

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
PULLED = "2026-06-25T05:00:00Z"


def load_rows():
    return json.loads((FIX / "gtn_notices.json").read_text(encoding="utf-8"))["data"]


def test_filter_keeps_only_maint_and_plnd_outage():
    rows = load_rows()
    kept = [r for r in rows if gtn.is_maintenance(r)]
    # 1585 + 1583 (Maint) + 1577 (Plnd Outage); the Rates-Chrgs notice 1582 dropped.
    assert {r["NoticeId"] for r in kept} == {1585, 1583, 1577}


def test_parse_event_count_and_classification():
    events, _ = gtn.parse(load_rows(), PULLED)
    assert len(events) == 3
    by_id = {e.notice_id: e for e in events}
    # Maint -> maintenance; Plnd Outage -> planned_outage.
    assert by_id["1585"].notice_type == "maintenance"
    assert by_id["1583"].notice_type == "maintenance"
    assert by_id["1577"].notice_type == "planned_outage"
    # severity/category come from the shared classifier (maintenance -> medium).
    assert by_id["1585"].severity == "medium"
    assert by_id["1585"].category == "maintenance"
    assert all(e.source == "gtn" for e in events)


def test_event_fields_for_station9():
    events, _ = gtn.parse(load_rows(), PULLED)
    ev = next(e for e in events if e.notice_id == "1585")
    assert ev.notice_type_raw == "Maint"
    assert ev.status == "supersede"
    assert ev.prior_notice_id == "1584"
    assert ev.posted_at_utc == "2026-06-21 18:40"
    assert ev.effective_start == "2026-06-21"
    assert ev.effective_end == "2026-06-27"
    assert ev.gas_day == "2026-06-21"  # ranged notice -> effective_start
    assert ev.url == "https://www.tcplus.com/GTN/Notice/ShowDetails/1585"
    assert ev.affects_pge is True
    assert "Station 9 CFTP" in ev.headline
    assert "<" not in ev.body and "&" not in ev.body  # HTML stripped/unescaped-free
    assert len(ev.body) <= 2000
    # orchestrator-owned fields untouched here
    assert ev.is_current is True
    assert ev.has_capacity_impact is False
    assert ev.primary_point_id is None


def test_impact_count_and_join():
    _, impacts = gtn.parse(load_rows(), PULLED)
    # One impact per Maint notice (one LOC# x one capacity value); the schedule
    # notice 1577 names no LOC# in prose, so it yields no impact row.
    assert len(impacts) == 2
    assert all(im.join_kind == "point_id" for im in impacts)
    assert all(im.capacity_basis == "remaining" for im in impacts)
    assert all(im.original_units == "MMcf/d" for im in impacts)
    assert {im.notice_id for im in impacts} == {"1585", "1583"}


def test_impact_station9_capacity_value():
    _, impacts = gtn.parse(load_rows(), PULLED)
    im = next(i for i in impacts if i.notice_id == "1585")
    assert im.point_id == "18480"              # GTN LOC# == OAC LocationID == point_id
    assert im.original_value == 1650.0
    assert im.capacity_remaining_dthd == to_dth(1650.0, "MMcf/d")  # 1,650,000 Dth/d
    assert im.capacity_remaining_dthd == 1_650_000.0
    assert im.affected_label == "Station 9 CFTP"
    assert im.date_start == "2026-06-21"
    assert im.date_end == "2026-06-27"
    # no unconstrained base in the prose: base + pct stay null for backfill
    assert im.base_capacity_dthd is None
    assert im.pct_of_capacity is None
    assert im.reduction_dthd is None


def test_impact_rosalia_capacity_and_unplanned_flag():
    _, impacts = gtn.parse(load_rows(), PULLED)
    im = next(i for i in impacts if i.notice_id == "1583")
    assert im.point_id == "954690"
    assert im.original_value == 2500.0
    assert im.capacity_remaining_dthd == 2_500_000.0
    assert im.affected_label == "Station 6 CFTP"
    # "extended the unplanned maintenance" -> is_unplanned True
    assert im.is_unplanned is True


def test_impacts_serialize_schema_keys():
    _, impacts = gtn.parse(load_rows(), PULLED)
    d = impacts[0].to_dict()
    for key in (
        "source", "maintenance_id", "notice_id", "point_id", "join_kind",
        "capacity_basis", "capacity_remaining_dthd", "original_value",
        "original_units", "date_start", "pulled_at_utc",
    ):
        assert key in d


def test_parse_is_pure_no_network(monkeypatch):
    # parse() takes raw rows directly; it must never touch the network.
    def boom(*a, **k):  # pragma: no cover - guard
        raise AssertionError("parse() must not fetch")

    monkeypatch.setattr(gtn.GTNClient, "fetch_notices", boom)
    events, impacts = gtn.parse(load_rows(), PULLED)
    assert events and impacts
