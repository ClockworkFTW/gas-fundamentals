"""Offline tests for the GTN client against a captured fixture.

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import json
import pathlib

import pytest

from ebb import gtn

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-22T15:00:00Z"


def load():
    return json.loads((FIX / "gtn_operational_capacity.json").read_text(encoding="utf-8"))


def load_notices():
    return json.loads((FIX / "gtn_notices.json").read_text(encoding="utf-8"))["data"]


@pytest.fixture
def client():
    return gtn.GTNClient(data_dir="data/gtn")


def test_parse_oac_locations_and_units(client):
    recs = client.parse_operational_capacity(load(), "2026-06-21", PULLED, "ref")
    assert recs, "expected location records"
    assert all(r.source == "gtn" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" and r.original_units == "MMBtu/d" for r in recs)
    # Gas day normalized from the posting's EffectiveGasDay.
    assert all(r.gas_day == "2026-06-21" for r in recs)


def test_parse_oac_kingsgate_receipt(client):
    recs = client.parse_operational_capacity(load(), "2026-06-21", PULLED, "ref")
    kings = [r for r in recs if r.point_name.upper() == "KINGSGATE"]
    assert kings, "Kingsgate (Canada border receipt point) should be present"
    k = kings[0]
    assert k.flow_direction == "receipt"
    # OAC posting carries capacity AND scheduled quantity together.
    assert k.design_capacity is not None
    assert k.operational_capacity is not None
    assert k.available_capacity is not None
    assert k.scheduled_qty is not None and k.scheduled_qty == k.original_qty


def test_parse_oac_numbers_are_floats(client):
    recs = client.parse_operational_capacity(load(), "2026-06-21", PULLED, "ref")
    # comma-strings like "3,043,102" must parse to floats
    for r in recs:
        for v in (r.design_capacity, r.operational_capacity, r.available_capacity):
            assert v is None or isinstance(v, float)


def test_fetch_builds_gtn_date_and_cycle(client, monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"data": {"Cycle": "Timely", "MeasurementBasis": "Million BTU's", "Content": []}}

    monkeypatch.setattr(client, "_get", fake_get)
    client.fetch_operational_capacity("2026-06-21", "id2")
    assert captured["path"] == "GTN/OperationalCapacity/Generate"
    assert captured["params"]["GasDay"] == "06/21/2026"   # ISO -> MM/DD/YYYY
    assert captured["params"]["CycleType"] == 3           # id2 -> 3


def test_fetch_rejects_unknown_cycle(client):
    with pytest.raises(ValueError):
        client.fetch_operational_capacity("2026-06-21", "bogus")


def test_parse_notices_shape_and_types(client):
    notices = client.parse_notices(load_notices(), PULLED)
    assert notices, "expected notices in the fixture window"
    assert all(n.source == "gtn" for n in notices)
    # categories map to normalized notice types
    types = {n.notice_type for n in notices}
    assert types <= {"critical", "maintenance", "other"}
    assert "critical" in types  # fixture has a Critical/Maint notice
    n0 = notices[0]
    assert n0.headline and n0.url.startswith("https://www.tcplus.com/GTN/Notice/ShowDetails/")
    assert "Effective" in n0.body
    assert n0.posted_at  # PostingDate + time


def test_parse_notices_strips_html_and_entities(client):
    notices = client.parse_notices(load_notices(), PULLED)
    for n in notices:
        assert "<" not in n.headline and "&amp;" not in n.headline
        assert "<" not in n.body


def test_fetch_notices_builds_window_and_params(client, monkeypatch):
    captured = {}

    def fake_get(path, params=None):
        captured["path"] = path
        captured["params"] = params
        return {"data": []}

    monkeypatch.setattr(client, "_get", fake_get)
    client.fetch_notices("2026-06-21")
    assert captured["path"] == "GTN/Notice/Retrieve"
    p = captured["params"]
    assert p["sort_direction"] == "Descending"   # verbose form, not "desc"
    assert p["filter.SelectedIndicator"] == ""   # all categories
    # window brackets the gas day (MM/DD/YYYY)
    assert p["filter.EffDate"] == "06/07/2026"    # 2026-06-21 minus 14 days
    assert p["filter.EndDate"] == "08/05/2026"    # 2026-06-21 plus 45 days


def test_record_serializes_schema_keys(client):
    rec = client.parse_operational_capacity(load(), "2026-06-21", PULLED, "ref")[0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "point_name", "design_capacity",
                "available_capacity", "units", "pulled_at_utc", "raw_ref"):
        assert key in d
