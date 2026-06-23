"""Offline parser tests for pipe_ranger against saved fixtures (tests/fixtures/).

Refresh fixtures with: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import json
import pathlib

import pytest

from ebb import pipe_ranger as pr

FIX = pathlib.Path(__file__).parent / "fixtures"
GAS_DAY = "2026-06-22"  # the gas day captured in the committed fixtures
PULLED = "2026-06-22T15:00:00Z"


def load(name: str):
    text = (FIX / f"{name}.json").read_text(encoding="utf-8")
    return pr.PipeRangerClient._decode(text)


@pytest.fixture
def client():
    return pr.PipeRangerClient(data_dir="data/pipe_ranger")


# --- pure helpers ---------------------------------------------------------- #


def test_to_float_handles_commas_and_na():
    assert pr.to_float("1,434,727") == 1434727.0
    assert pr.to_float(999.375) == 999.375
    assert pr.to_float("n/a") is None
    assert pr.to_float("") is None
    assert pr.to_float(None) is None


def test_norm_date_variants():
    assert pr.norm_date("06/22/26") == "2026-06-22"
    assert pr.norm_date("6/19/2026") == "2026-06-19"
    assert pr.norm_date("22-06-2026") == "2026-06-22"
    assert pr.norm_date("garbage") is None


def test_mmcf_to_dth():
    assert pr.mmcf_to_dth(1.0, 1000.0) == 1000.0
    assert pr.mmcf_to_dth(2.5, 1020.0) == 2550.0
    assert pr.mmcf_to_dth(None) is None


# --- parsers --------------------------------------------------------------- #


def test_scheduled_volumes_dth_and_points(client):
    recs = client.parse_scheduled_volumes(load("scheduledvolumes"), GAS_DAY, None, PULLED, "ref")
    assert recs, "expected scheduled-volume records for the fixture gas day"
    malin = [r for r in recs if r.point_name == "Malin (GTN)"]
    assert malin and malin[0].units == "Dth/d"
    assert malin[0].scheduled_qty == malin[0].original_qty  # no conversion (already Dth)
    assert all(r.dataset_type == "scheduled_quantity" for r in recs)
    # Daggett receipt point is present in the Baja path mapping
    assert any(r.point_name == "Daggett (KRGT)" for r in recs)


def test_scheduled_volumes_cycle_filter(client):
    # In the fixture, gas day 2026-06-22 carries the ID2 cycle; the Timely
    # schedule is day-ahead (gas day 2026-06-23). Filtering must respect both.
    all_recs = client.parse_scheduled_volumes(load("scheduledvolumes"), GAS_DAY, None, PULLED, "ref")
    id2 = client.parse_scheduled_volumes(load("scheduledvolumes"), GAS_DAY, "id2", PULLED, "ref")
    assert id2, "expected ID2-cycle records for 2026-06-22"
    assert all("id2" in r.cycle.lower() for r in id2)
    assert len(id2) == len(all_recs)  # ID2 is the only cycle on this gas day

    # Timely is for the next gas day, so it must not appear under 2026-06-22...
    assert client.parse_scheduled_volumes(load("scheduledvolumes"), GAS_DAY, "timely", PULLED, "ref") == []
    # ...but it does under 2026-06-23.
    next_day = client.parse_scheduled_volumes(load("scheduledvolumes"), "2026-06-23", "timely", PULLED, "ref")
    assert next_day and all("timely" in r.cycle.lower() for r in next_day)


def test_physical_capacity_is_dth(client):
    recs = client.parse_physical_capacity(load("dthphysicalpipeline"), GAS_DAY, PULLED, "ref")
    assert recs
    r = recs[0]
    assert r.dataset_type == "operationally_available"
    assert r.design_capacity is not None and r.units == "Dth/d"


def test_storage_converts_mmcf_to_dth(client):
    recs = client.parse_storage(load("storageactivity"), GAS_DAY, PULLED, "ref")
    assert recs
    facilities = {r.point_name for r in recs}
    assert "Wild Goose" in facilities
    for r in recs:
        assert r.original_units == "MMcf/d" and r.units == "Dth/d"
        # canonical = original * default heat content (1000)
        assert r.scheduled_qty == pytest.approx(r.original_qty * 1000.0)
        assert r.flow_direction in ("injection", "withdrawal")


def test_inventory_records(client):
    recs = client.parse_inventory(load("systemInventoryStatus"), GAS_DAY, PULLED, "ref")
    assert len(recs) == 1
    r = recs[0]
    assert r.dataset_type == "inventory"
    assert r.scheduled_qty is not None and r.original_units == "MMcf"


def test_supply_demand_metrics(client):
    recs = client.parse_supply_demand(load("supplydemand"), GAS_DAY, PULLED, "ref")
    labels = {r.point_name for r in recs}
    assert "System Supply" in labels
    temp = [r for r in recs if r.point_name == "Mean Temperature"]
    assert temp and temp[0].units == "degF"  # temperature is not converted


def test_notices_efo_shape(client):
    # ofo fixture is empty today; efo fixture carries a historical EFO row.
    notices = client.parse_notices(load("ofoefoarchive"), load("ofoefoarchive_efo"), PULLED, "url")
    efo = [n for n in notices if n.notice_type == "EFO"]
    assert efo, "expected an EFO notice from the efo fixture"
    assert efo[0].source == "pipe_ranger" and efo[0].gas_day
    assert "Emergency Flow Order" in efo[0].headline or "EFO" in efo[0].headline


def test_records_serialize_to_schema_dict(client):
    recs = client.parse_scheduled_volumes(load("scheduledvolumes"), GAS_DAY, None, PULLED, "ref")
    d = recs[0].to_dict()
    for key in ("source", "dataset_type", "gas_day", "point_name", "units", "pulled_at_utc", "raw_ref"):
        assert key in d
