"""Offline tests for the Kern River (BHE) client against captured HTML fixtures.

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import pathlib

import pytest

from ebb import kern_river as kr

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-23T15:00:00Z"
GAS_DAY = "2026-06-22"


def oac_html():
    return (FIX / "kern_oac.html").read_text(encoding="utf-8")


def notice_html(category):
    return (FIX / f"kern_notices_{category}.html").read_text(encoding="utf-8")


@pytest.fixture
def client():
    return kr.KernRiverClient(data_dir="data/kern_river")


# --------------------------------------------------------------------------- #
# OAC + scheduled
# --------------------------------------------------------------------------- #


def test_parse_oac_shape_and_units(client):
    recs = client.parse_operational_capacity(oac_html(), GAS_DAY, PULLED, "ref")
    assert len(recs) > 80
    assert all(r.source == "kern_river" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" and r.original_units == "Dth/d" for r in recs)
    # FlowInd R/D -> receipt/delivery; BD (bidirectional compressors) -> None
    assert {r.flow_direction for r in recs} <= {"receipt", "delivery", None}
    assert any(r.flow_direction is None for r in recs)  # bidirectional compressors present


def test_parse_oac_daggett_pge_delivery(client):
    recs = client.parse_operational_capacity(oac_html(), GAS_DAY, PULLED, "ref")
    dag = [r for r in recs if r.point_name.upper() == "DAGGETT - PG&E"]
    assert dag, "Daggett - PG&E delivery (Kern River -> PG&E) should be present"
    r = dag[0]
    assert r.flow_direction == "delivery"
    assert r.design_capacity and r.operational_capacity and r.available_capacity
    assert r.point_id  # location number
    assert r.gas_day == "2026-06-22"  # from the row's Eff Gas Day


def test_parse_oac_combines_kern_and_mojave_scheduled(client):
    recs = client.parse_operational_capacity(oac_html(), GAS_DAY, PULLED, "ref")
    # Kramer Junction has scheduled on BOTH Kern and Mojave -> summed.
    kramer = [r for r in recs if "KRAMER" in r.point_name.upper()]
    assert kramer
    k = kramer[0]
    assert k.scheduled_qty and k.scheduled_qty == k.original_qty
    # the combined value exceeds either system alone (Mojave portion is nonzero here)
    assert k.scheduled_qty > 600000


def test_fetch_oac_builds_gasday(client, monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return "<table></table>"

    monkeypatch.setattr(client, "_get", fake_get)
    client.fetch_operational_capacity("2026-06-22")
    assert captured["path"] == kr.OAC_PATH
    assert captured["params"] == {"gasDay": "06/22/2026"}  # ISO -> MM/DD/YYYY


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #


def test_parse_critical_notices(client):
    notices = client.parse_notices(notice_html("Critical"), "Critical", GAS_DAY, PULLED)
    assert notices
    assert all(n.source == "kern_river" and n.stage == "Critical" for n in notices)
    assert all(n.notice_type == "critical" for n in notices)
    n0 = notices[0]
    assert n0.headline and "Effective" in n0.body
    assert n0.url.startswith("https://services.kernrivergas.com")


def test_parse_noncritical_notices_window(client):
    notices = client.parse_notices(notice_html("Non-Critical"), "Non-Critical", GAS_DAY, PULLED)
    # the grid has many historical rows; only those still active (end >= gas_day-3) kept
    assert notices
    assert all(n.stage == "Non-Critical" for n in notices)
    # default type for non-critical is "other" (unless a type override matches)
    assert any(n.notice_type == "other" for n in notices)


def test_notice_type_mapping():
    assert kr.KernRiverClient._notice_type("Critical", "Pipe Cond") == "critical"
    assert kr.KernRiverClient._notice_type("Planned-Service-Outage", "Plan Serv Out") == "maintenance"
    assert kr.KernRiverClient._notice_type("Non-Critical", "Force Majeure") == "critical"
    assert kr.KernRiverClient._notice_type("Non-Critical", "Capacity Constraint") == "critical"
    assert kr.KernRiverClient._notice_type("Non-Critical", "Other") == "other"


def test_empty_planned_outage_grid(client):
    # Planned-Service-Outage grid is present but empty -> no notices, no crash.
    notices = client.parse_notices(notice_html("Planned-Service-Outage"), "Planned-Service-Outage", GAS_DAY, PULLED)
    assert notices == []


def test_fetch_all_notices_dedupes(client, monkeypatch):
    html = notice_html("Critical")
    monkeypatch.setattr(client, "fetch_notice_category", lambda category, **k: html)
    out = client.fetch_all_notices(GAS_DAY, PULLED)
    # same html for all 3 categories -> deduped by headline+gas_day
    single = client.parse_notices(html, "Critical", GAS_DAY, PULLED)
    assert len(out) == len(single)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def test_pull_offline(client, monkeypatch):
    monkeypatch.setattr(client, "fetch_operational_capacity", lambda gd, **k: oac_html())
    monkeypatch.setattr(client, "fetch_notice_category", lambda category, **k: notice_html(category))
    result = client.pull(GAS_DAY, write=False)
    assert result["source"] == "kern_river"
    assert len(result["records"]) > 80 and result["notices"]
    names = {r["point_name"].upper() for r in result["records"]}
    assert "DAGGETT - PG&E" in names
    assert result["cycle"]  # latest posted cycle recorded


def test_record_serializes_schema_keys(client):
    rec = client.parse_operational_capacity(oac_html(), GAS_DAY, PULLED, "ref")[0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "cycle", "point_name", "point_id",
                "flow_direction", "scheduled_qty", "design_capacity", "operational_capacity",
                "available_capacity", "units", "original_units", "original_qty",
                "pulled_at_utc", "raw_ref"):
        assert key in d
