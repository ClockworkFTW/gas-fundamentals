"""Offline tests for the Transwestern (Energy Transfer / iPost) client against
captured CSV fixtures.

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import pathlib

import pytest

from ebb import transwestern as tw

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-23T15:00:00Z"
GAS_DAY = "2026-06-22"


def oac_text():
    return (FIX / "tw_operational_capacity.csv").read_text(encoding="utf-8")


def notice_text(category):
    return (FIX / f"tw_notices_{category}.csv").read_text(encoding="utf-8")


@pytest.fixture
def client():
    return tw.TranswesternClient(data_dir="data/transwestern")


# --------------------------------------------------------------------------- #
# OAC + scheduled
# --------------------------------------------------------------------------- #


def test_parse_oac_shape_and_units(client):
    recs = client.parse_operational_capacity(oac_text(), GAS_DAY, "timely", PULLED, "ref")
    assert len(recs) > 200
    assert all(r.source == "transwestern" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" and r.original_units == "Dth/d" for r in recs)
    assert all(r.gas_day == GAS_DAY and r.cycle == "timely" for r in recs)
    assert {r.flow_direction for r in recs} <= {"receipt", "delivery", None}


def test_parse_oac_pge_topock_delivery(client):
    recs = client.parse_operational_capacity(oac_text(), GAS_DAY, "timely", PULLED, "ref")
    pge = [r for r in recs if r.point_name.upper() == "PG&E TOPOCK" and r.flow_direction == "delivery"]
    assert pge, "PG&E Topock delivery (Transwestern -> PG&E) should be present"
    r = pge[0]
    # OAC posting carries capacity AND scheduled quantity together.
    assert r.design_capacity and r.operational_capacity
    assert r.available_capacity is not None
    assert r.scheduled_qty is not None and r.scheduled_qty == r.original_qty
    assert r.point_id  # iPost location number


def test_parse_oac_numbers_and_blanks(client):
    recs = client.parse_operational_capacity(oac_text(), GAS_DAY, "timely", PULLED, "ref")
    for r in recs:
        for v in (r.design_capacity, r.operational_capacity, r.available_capacity, r.scheduled_qty):
            assert v is None or isinstance(v, float)
    # bidirectional points have rows with TSQ/OPC/OAC blank -> None (not a crash)
    blanks = [r for r in recs if r.scheduled_qty is None]
    assert blanks


def test_fetch_oac_builds_date_and_cycle(client, monkeypatch):
    captured = {}

    def fake_get(path, params):
        captured["path"] = path
        captured["params"] = params
        return '"Loc","Loc Name"\n'

    monkeypatch.setattr(client, "_get_csv", fake_get)
    client.fetch_operational_capacity("2026-06-22", "evening")
    assert captured["path"] == tw.OAC_PATH
    assert captured["params"]["gasDay"] == "06/22/2026"   # ISO -> MM/DD/YYYY
    assert captured["params"]["cycle"] == 1               # evening -> 1
    assert captured["params"]["asset"] == "TW"
    assert captured["params"]["f"] == "csv"


def test_fetch_oac_rejects_unknown_cycle(client):
    with pytest.raises(ValueError):
        client.fetch_operational_capacity("2026-06-22", "id2")


# --------------------------------------------------------------------------- #
# Notices
# --------------------------------------------------------------------------- #


def test_parse_notices_types_and_urls(client):
    notices = client.parse_notices(notice_text("non-critical"), "non-critical", GAS_DAY, PULLED)
    assert notices
    assert all(n.source == "transwestern" and n.stage == "non-critical" for n in notices)
    # Capacity Constraint maps to critical (fundamentals-relevant), not "other"
    assert any(n.notice_type == "critical" for n in notices)
    n0 = notices[0]
    assert n0.headline
    assert n0.url.startswith("https://twtransfer.energytransfer.com/ipost/notice/show/")
    assert "Effective" in n0.body
    assert n0.posted_at


def test_planned_outage_maps_to_maintenance(client):
    notices = client.parse_notices(notice_text("planned-service-outage"), "planned-service-outage", GAS_DAY, PULLED)
    assert notices and all(n.notice_type == "maintenance" for n in notices)


def test_notice_type_mapping():
    assert tw.TranswesternClient._notice_type("non-critical", "Force Majeure") == "critical"
    assert tw.TranswesternClient._notice_type("non-critical", "Capacity Constraint") == "critical"
    assert tw.TranswesternClient._notice_type("critical", "Operational Alert") == "critical"
    assert tw.TranswesternClient._notice_type("planned-service-outage", "Planned Service Outage") == "maintenance"
    assert tw.TranswesternClient._notice_type("non-critical", "Gas Quality") == "other"


def test_parse_dt_ipost_format():
    d, ts = tw.TranswesternClient._parse_dt("Jun 23 2026  7:34AM")
    assert d == "2026-06-23" and ts == "2026-06-23 07:34"
    assert tw.TranswesternClient._parse_dt("") == (None, None)


def test_fetch_all_notices_dedupes(client, monkeypatch):
    # Same notice id returned in two categories -> deduped by url.
    csv_one = (
        '"Notice Type","Posted Date/Time","Notice Eff Date/Time","Notice End Date/Time","Notice ID","Subject","Response Date/Time"\n'
        '"Capacity Constraint","Jun 23 2026 10:38AM","Jun 23 2026  9:00AM","Jun 30 2026  8:59AM","999","DUP NOTICE",\n'
    )
    monkeypatch.setattr(client, "fetch_notice_category", lambda category, **k: csv_one)
    out = client.fetch_all_notices(GAS_DAY, PULLED)
    assert len(out) == 1  # one unique notice across the 3 categories


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #


def test_pull_offline(client, monkeypatch):
    monkeypatch.setattr(client, "fetch_operational_capacity", lambda gd, cy, **k: oac_text())
    monkeypatch.setattr(client, "fetch_notice_category", lambda category, **k: notice_text(category))
    result = client.pull(GAS_DAY, "timely", write=False)
    assert result["source"] == "transwestern"
    assert len(result["records"]) > 200 and result["notices"]
    names = {r["point_name"].upper() for r in result["records"]}
    assert "PG&E TOPOCK" in names


def test_record_serializes_schema_keys(client):
    rec = client.parse_operational_capacity(oac_text(), GAS_DAY, "timely", PULLED, "ref")[0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "cycle", "point_name", "point_id",
                "flow_direction", "scheduled_qty", "design_capacity", "operational_capacity",
                "available_capacity", "units", "original_units", "original_qty",
                "pulled_at_utc", "raw_ref"):
        assert key in d
