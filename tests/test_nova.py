"""Offline tests for the NOVA / NGTL (TC Energy) client against captured CSV
fixtures (TC Customer Express public AWS-gateway feeds).

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import csv
import io
import pathlib

import pytest

from ebb import nova
from ebb.schema import norm_date

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-23T15:00:00Z"


def chart_text():
    return (FIX / "nova_chart.csv").read_text(encoding="utf-8")


def csr_text():
    return (FIX / "nova_csr.csv").read_text(encoding="utf-8")


def outages_text():
    return (FIX / "nova_outages.csv").read_text(encoding="utf-8")


def plant_text():
    return (FIX / "nova_plant_turnarounds.csv").read_text(encoding="utf-8")


def latest_actual_gas_day() -> str:
    """The newest chart row that actually carries flow (forward rows are blank)."""
    rows = list(csv.DictReader(io.StringIO(chart_text())))
    days = [
        norm_date(r.get("Gas Day"))
        for r in rows
        if any((v or "").strip() for k, v in r.items() if "Actual Flow" in k)
    ]
    days = [d for d in days if d]
    assert days, "fixture has no rows with actual flow"
    return max(days)


@pytest.fixture
def client():
    return nova.NovaClient(data_dir="data/nova")


@pytest.fixture
def gas_day():
    return latest_actual_gas_day()


# --------------------------------------------------------------------------- #
# chart/csv — capability & historical flow
# --------------------------------------------------------------------------- #


def test_chart_zones_and_units(client, gas_day):
    recs, eff = client.parse_capability_flow(chart_text(), gas_day, PULLED, "ref")
    assert eff == gas_day
    assert {r.point_id for r in recs} == {"USJR", "AB/BC", "EGAT", "OSDA"}
    assert all(r.source == "nova" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" and r.original_units == "10^3m^3/d" for r in recs)
    assert all(r.cycle == "daily" for r in recs)


def test_chart_abbc_is_export_delivery(client, gas_day):
    recs, _ = client.parse_capability_flow(chart_text(), gas_day, PULLED, "ref")
    abbc = next(r for r in recs if r.point_id == "AB/BC")
    # Alberta/BC border is the AECO export toward Foothills BC / Kingsgate / PG&E.
    assert abbc.flow_direction == "delivery"
    assert "Foothills" in abbc.point_name
    # Carries scheduled flow + firm design (FT-D) + operational capability, all in Dth/d.
    assert abbc.scheduled_qty and abbc.scheduled_qty > 0
    assert abbc.design_capacity and abbc.design_capacity > 0
    assert abbc.operational_capacity and abbc.operational_capacity > 0
    # available = operating - scheduled
    assert abbc.available_capacity == round(abbc.operational_capacity - abbc.scheduled_qty, 1)
    # original volume preserved
    assert abbc.original_qty is not None


def test_usjr_is_receipt(client, gas_day):
    recs, _ = client.parse_capability_flow(chart_text(), gas_day, PULLED, "ref")
    usjr = next(r for r in recs if r.point_id == "USJR")
    assert usjr.flow_direction == "receipt"


# --------------------------------------------------------------------------- #
# unit conversions
# --------------------------------------------------------------------------- #


def test_vol_to_dth_uses_heat_value():
    # 90000 (10^3 m^3/d) * 38.5 (GJ/10^3m^3) * 0.94781712 (Dth/GJ)
    out = nova.NovaClient._vol_to_dth(90000.0, 38.5)
    assert out == round(90000.0 * 38.5 * nova.GJ_TO_DTH, 1)
    assert nova.NovaClient._vol_to_dth(None, 38.5) is None
    assert nova.NovaClient._vol_to_dth(90000.0, None) is None


def test_mmcf_to_dth_default_heat(client):
    # 1 MMcf @ 1000 BTU/cf == 1000 Dth
    assert client._mmcf_to_dth(2478.0) == 2478000.0
    assert client._mmcf_to_dth(None) is None


def test_mmcf_to_dth_configurable_heat():
    c = nova.NovaClient(heat_btu_per_cf=1050.0)
    assert c._mmcf_to_dth(100.0) == 105000.0


# --------------------------------------------------------------------------- #
# csr/csv — current system report (system balance)
# --------------------------------------------------------------------------- #


def test_csr_balance_points_and_directions(client):
    recs = client.parse_system_report(csr_text(), "2026-06-21", PULLED, "ref")
    by_id = {r.point_id: r for r in recs}
    # the key AECO export border is present and is a delivery
    assert "Alberta-BC Border Flow" in by_id
    abbc = by_id["Alberta-BC Border Flow"]
    assert abbc.flow_direction == "delivery"
    assert abbc.dataset_type == "scheduled_quantity"
    assert abbc.units == "Dth/d" and abbc.original_units == "MMcf/d"
    assert abbc.scheduled_qty == client._mmcf_to_dth(abbc.original_qty)
    # receipts are receipts
    assert by_id["Total Receipts"].flow_direction == "receipt"
    # linepack/storage carry no flow direction and are supply_demand context
    lp = by_id["Current Linepack"]
    assert lp.flow_direction is None and lp.dataset_type == "supply_demand"


def test_csr_uses_latest_snapshot(client):
    recs = client.parse_system_report(csr_text(), "1999-01-01", PULLED, "ref")
    # no matching day -> falls back to the latest snapshot, still returns balance
    assert recs and any(r.point_id == "Total Deliveries" for r in recs)


# --------------------------------------------------------------------------- #
# row selection
# --------------------------------------------------------------------------- #


def test_select_chart_row_fallback_to_prior_actual(client):
    rows = [
        {"Gas Day": "20-Jun-26", "USJR Actual Flow (10^3m^3/d)": "100"},
        {"Gas Day": "21-Jun-26", "USJR Actual Flow (10^3m^3/d)": "110"},
        {"Gas Day": "22-Jun-26", "USJR Actual Flow (10^3m^3/d)": ""},  # forward: no actual
    ]
    row, day = client._select_chart_row(rows, "2026-06-22")
    assert day == "2026-06-21" and row["USJR Actual Flow (10^3m^3/d)"] == "110"


def test_select_chart_row_exact_with_actual(client):
    rows = [
        {"Gas Day": "21-Jun-26", "USJR Actual Flow (10^3m^3/d)": "110"},
        {"Gas Day": "22-Jun-26", "USJR Actual Flow (10^3m^3/d)": "120"},
    ]
    row, day = client._select_chart_row(rows, "2026-06-22")
    assert day == "2026-06-22" and row["USJR Actual Flow (10^3m^3/d)"] == "120"


# --------------------------------------------------------------------------- #
# outages + plant turnarounds -> notices
# --------------------------------------------------------------------------- #


def test_parse_outages_notices(client):
    gd = latest_actual_gas_day()
    notices = client.parse_outages(outages_text(), gd, PULLED)
    assert notices, "expected outage notices in the window"
    assert all(n.source == "nova" and n.notice_type == "maintenance" for n in notices)
    n0 = notices[0]
    assert n0.headline
    assert "Effective" in n0.body
    assert n0.url.endswith("#Outages")


def test_outages_window_excludes_far_future(client):
    # An outage starting well beyond the lookahead horizon is dropped.
    far = (
        "Outage Id,Table,Start,End,Capability,Typical Flow,Type of Restriction,"
        "Area for Stated Capability,Other Restricted Segments,Description,UID\n"
        "1,USJR,01-Jan-30,02-Jan-30,0,0,FT-R,X,,Far future,1\n"
    )
    assert client.parse_outages(far, "2026-06-21", PULLED) == []


def test_parse_plant_turnarounds(client):
    gd = latest_actual_gas_day()
    notices = client.parse_plant_turnarounds(plant_text(), gd, PULLED)
    assert all(n.notice_type == "maintenance" and n.stage == "plant turnaround" for n in notices)
    if notices:
        assert "impact" in notices[0].headline.lower()


# --------------------------------------------------------------------------- #
# date helper + fetch params + serialization + offline orchestration
# --------------------------------------------------------------------------- #


def test_norm_date_tc_formats():
    assert norm_date("23-Dec-24") == "2024-12-23"
    assert norm_date("22-Jun-2026") == "2026-06-22"
    assert norm_date("2026-06-16 09:30:00") == "2026-06-16"
    assert norm_date("2026-6-1") == "2026-06-01"


def test_fetch_system_report_builds_params(client, monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return "Timestamp\n"

    monkeypatch.setattr(client, "_get_csv", fake_get)
    client.fetch_system_report(duration=5)
    assert captured["path"] == "csr/csv/"
    assert captured["params"] == {"unit": "MMcf", "duration": 5}


def test_record_serializes_schema_keys(client, gas_day):
    rec = client.parse_capability_flow(chart_text(), gas_day, PULLED, "ref")[0][0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "point_name", "point_id",
                "flow_direction", "scheduled_qty", "design_capacity",
                "operational_capacity", "available_capacity", "units",
                "original_units", "original_qty", "pulled_at_utc", "raw_ref"):
        assert key in d


def test_pull_offline(client, monkeypatch):
    monkeypatch.setattr(client, "fetch_capability_flow", lambda **k: chart_text())
    monkeypatch.setattr(client, "fetch_system_report", lambda **k: csr_text())
    monkeypatch.setattr(client, "fetch_outages", lambda **k: outages_text())
    monkeypatch.setattr(client, "fetch_plant_turnarounds", lambda **k: plant_text())
    result = client.pull(latest_actual_gas_day(), write=False)
    assert result["source"] == "nova"
    assert result["records"] and result["notices"]
    # 4 chart zones + CSR balance columns
    ids = {r["point_id"] for r in result["records"]}
    assert {"USJR", "AB/BC", "EGAT", "OSDA"} <= ids
    assert "Alberta-BC Border Flow" in ids
