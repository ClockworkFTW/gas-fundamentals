"""Offline tests for the Foothills client — the export-border view over the shared
TC Customer Express feeds (reuses NOVA's captured fixtures).

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import csv
import io
import pathlib

import pytest

from ebb import foothills
from ebb.schema import norm_date

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-23T15:00:00Z"


def chart_text():
    return (FIX / "nova_chart.csv").read_text(encoding="utf-8")


def csr_text():
    return (FIX / "nova_csr.csv").read_text(encoding="utf-8")


def outages_text():
    return (FIX / "nova_outages.csv").read_text(encoding="utf-8")


def latest_actual_gas_day() -> str:
    rows = list(csv.DictReader(io.StringIO(chart_text())))
    days = [
        norm_date(r.get("Gas Day"))
        for r in rows
        if any((v or "").strip() for k, v in r.items() if "Actual Flow" in k)
    ]
    return max(d for d in days if d)


@pytest.fixture
def client():
    return foothills.FoothillsClient(data_dir="data/foothills")


@pytest.fixture
def gas_day():
    return latest_actual_gas_day()


# --------------------------------------------------------------------------- #
# Foothills BC from NGTL's AB/BC chart zone
# --------------------------------------------------------------------------- #


def test_bc_record_from_abbc_zone(client, gas_day):
    recs, eff = client.parse_capability_flow(chart_text(), gas_day, PULLED, "ref")
    assert len(recs) == 1
    bc = recs[0]
    assert bc.source == "foothills"
    assert bc.point_id == "AB/BC"
    assert "Foothills BC" in bc.point_name and "Kingsgate" in bc.point_name
    assert bc.flow_direction == "delivery"
    # the PG&E-relevant firm capacity + flow, all canonical Dth/d
    assert bc.units == "Dth/d"
    assert bc.scheduled_qty and bc.design_capacity and bc.operational_capacity


# --------------------------------------------------------------------------- #
# Export borders from the Current System Report
# --------------------------------------------------------------------------- #


def test_csr_export_borders(client):
    recs = client.parse_system_report(csr_text(), "2026-06-21", PULLED, "ref")
    ids = {r.point_id for r in recs}
    assert ids == {"Alberta-BC Border Flow", "Empress Border Flow", "Mcneil Border Flow"}
    assert all(r.source == "foothills" and r.flow_direction == "delivery" for r in recs)
    bc = next(r for r in recs if r.point_id == "Alberta-BC Border Flow")
    assert "Kingsgate" in bc.point_name and "PG&E" in bc.point_name
    assert bc.units == "Dth/d" and bc.original_units == "MMcf/d"


# --------------------------------------------------------------------------- #
# Outage filtering to the export gates (WGAT/FHZ8 -> BC, EGAT -> SK)
# --------------------------------------------------------------------------- #


def test_outages_only_export_gates(client):
    notices = client.parse_outages(outages_text(), "2026-06-21", PULLED)
    assert notices, "expected export-gate outages in the window"
    assert all(n.source == "foothills" and n.notice_type == "maintenance" for n in notices)
    # every kept outage touches an export gate, and the body names the affected leg
    for n in notices:
        gates = {t.strip() for t in (n.stage or "").split(",")}
        assert gates & {"WGAT", "FHZ8", "EGAT"}
        assert "Affects Foothills" in n.body


def test_outages_exclude_internal_only(client):
    # An outage touching only internal areas (OSDA) is dropped.
    internal = (
        "Outage Id,Table,Start,End,Capability,Typical Flow,Type of Restriction,"
        "Area for Stated Capability,Other Restricted Segments,Description,UID\n"
        "9,OSDA,20-Jun-26,25-Jun-26,0,0,FT-R,Segments 10 11,,Internal only,9\n"
    )
    assert client.parse_outages(internal, "2026-06-21", PULLED) == []


def test_outages_bc_leg_tagged(client):
    # An outage on WGAT (Alberta/BC border) is tagged Foothills BC.
    wgat = (
        "Outage Id,Table,Start,End,Capability,Typical Flow,Type of Restriction,"
        "Area for Stated Capability,Other Restricted Segments,Description,UID\n"
        "5,WGAT,20-Jun-26,25-Jun-26,0,0,FT-D,Alberta/BC Border,,West gate work,5\n"
    )
    notices = client.parse_outages(wgat, "2026-06-21", PULLED)
    assert len(notices) == 1
    assert "Affects Foothills BC" in notices[0].body


# --------------------------------------------------------------------------- #
# Orchestration + delegation
# --------------------------------------------------------------------------- #


def test_pull_offline(client, monkeypatch):
    monkeypatch.setattr(client, "fetch_capability_flow", lambda **k: chart_text())
    monkeypatch.setattr(client, "fetch_system_report", lambda **k: csr_text())
    monkeypatch.setattr(client, "fetch_outages", lambda **k: outages_text())
    result = client.pull(latest_actual_gas_day(), write=False)
    assert result["source"] == "foothills"
    ids = {r["point_id"] for r in result["records"]}
    # Foothills BC (from chart) + the three CSR export borders
    assert "AB/BC" in ids
    assert "Alberta-BC Border Flow" in ids
    assert all(r["source"] == "foothills" for r in result["records"])
    assert result["notices"]


def test_fetch_delegates_to_nova(client, monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        return "Timestamp\n"

    monkeypatch.setattr(client._nova, "_get_csv", fake_get)
    client.fetch_system_report(duration=3)
    assert captured["path"] == "csr/csv/"
