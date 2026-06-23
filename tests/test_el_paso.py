"""Offline tests for the El Paso (EPNG) client against a captured grid fixture.

Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
"""
import json
import pathlib
import re

import pytest

from ebb import el_paso

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-22T15:00:00Z"


def load_html():
    return (FIX / "epng_operational_capacity.html").read_text(encoding="utf-8")


def load_notices_html():
    return (FIX / "epng_notices.html").read_text(encoding="utf-8")


@pytest.fixture
def client():
    return el_paso.ElPasoClient(data_dir="data/el_paso")


def test_parse_oac_rows(client):
    recs = client.parse_operational_capacity(load_html(), "2026-06-22", PULLED, "ref")
    assert len(recs) >= 50, "expected the full OAC point posting"
    assert all(r.source == "el_paso" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" for r in recs)
    # point ids are the numeric Loc ids and must be unique (no duplicate scroll rows)
    ids = [r.point_id for r in recs]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"\d{3,}", i) for i in ids)


def test_parse_oac_capacity_and_scheduled(client):
    recs = client.parse_operational_capacity(load_html(), "2026-06-22", PULLED, "ref")
    # OAC posting carries capacity AND scheduled quantity together
    withcap = [r for r in recs if r.design_capacity is not None]
    assert withcap
    r = withcap[0]
    assert r.operational_capacity is not None
    assert r.available_capacity is not None
    assert r.scheduled_qty is not None and r.scheduled_qty == r.original_qty
    # comma-strings parsed to floats
    assert all(isinstance(r.design_capacity, float) for r in withcap)


def test_parse_oac_gas_day_and_flow(client):
    recs = client.parse_operational_capacity(load_html(), "2026-01-01", PULLED, "ref")
    # gas day comes from the page's date picker, not the passed fallback
    assert recs[0].gas_day and re.fullmatch(r"\d{4}-\d{2}-\d{2}", recs[0].gas_day)
    assert recs[0].gas_day != "2026-01-01"
    # flow indicator maps R->receipt, D->delivery (BD/other -> None)
    dirs = {r.flow_direction for r in recs}
    assert "receipt" in dirs


def test_posting_context_extracts_dth_cycle(client):
    ctx = client._posting_context(load_html())
    assert ctx["gas_day"] and re.fullmatch(r"\d{4}-\d{2}-\d{2}", ctx["gas_day"])
    assert ctx["cycle"] and "TIMELY" in ctx["cycle"].upper()


def test_form_fields_harvests_viewstate(client):
    fields = client._form_fields(load_html())
    assert "__VIEWSTATE" in fields and fields["__VIEWSTATE"]
    assert "__EVENTVALIDATION" in fields


def test_parse_notices_shape_and_dedupe(client):
    notices = client.parse_notices(load_notices_html(), PULLED)
    assert len(notices) >= 40, "expected the recent notices window"
    assert all(n.source == "el_paso" for n in notices)
    # deduped by Notice ID (url carries the id)
    urls = [n.url for n in notices]
    assert len(urls) == len(set(urls))
    assert all(n.url.startswith("https://pipeline2.kindermorgan.com/Notices/NoticeDetail.aspx") for n in notices)


def test_parse_notices_types_and_fields(client):
    notices = client.parse_notices(load_notices_html(), PULLED)
    types = {n.notice_type for n in notices}
    assert types <= {"critical", "maintenance", "other"}
    assert "maintenance" in types  # fixture has MAINTENANCE notices
    n0 = notices[0]
    assert n0.headline and n0.posted_at
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", n0.gas_day)  # effective date normalized
    assert "Effective" in n0.body


def test_date_clientstate_format(client):
    # ISO -> Infragistics pipe format: 01<year>-<month>-<day>-0-0-0-0 (unpadded)
    cs = client._date_clientstate("2026-06-20")
    assert cs == '|0|012026-6-20-0-0-0-0||[[[[]],[],[]],[{},[]],"012026-6-20-0-0-0-0"]'


def test_cycle_clientstate_encodes_index_and_name(client):
    cs = client._cycle_clientstate("EVENING", 2)
    assert cs.startswith("|0|EVENING&tilda;2||")
    body = cs.split("||", 1)[1]
    obj = json.loads(body)
    delta = obj[1][0]
    assert delta["0"][1] == 2 and delta["1"][1] == 2   # index at [41]/[7]
    assert delta["2"][1] == "EVENING"                  # name at [23]


def test_fetch_sets_date_and_cycle_clientstate(client, monkeypatch):
    # The GET returns a minimal page with the two clientState hidden fields.
    page = (
        '<input type="hidden" name="__VIEWSTATE" value="vs"/>'
        '<input type="hidden" name="__EVENTVALIDATION" value="ev"/>'
        '<input type="hidden" name="ctl00$x$dtePickerBegin_clientState" value=""/>'
        '<input type="hidden" name="ctl00$x$ddlCycleDD_clientState" value=""/>'
    )
    captured = {}
    monkeypatch.setattr(client, "_get", lambda path: page)
    monkeypatch.setattr(client, "_post", lambda path, data: captured.update(data) or "<html></html>")

    client.fetch_operational_capacity("2026-06-19", "id2")
    assert captured["ctl00$x$dtePickerBegin_clientState"].startswith("|0|012026-6-19-0-0-0-0||")
    cyc = captured["ctl00$x$ddlCycleDD_clientState"]
    assert cyc.startswith("|0|INTRADAY 2&tilda;4||")
    assert captured["__EVENTTARGET"] == el_paso.RETRIEVE_TARGET


def test_fetch_rejects_unknown_cycle(client, monkeypatch):
    monkeypatch.setattr(client, "_get", lambda path: '<input name="x" value=""/>')
    monkeypatch.setattr(client, "_post", lambda path, data: "<html></html>")
    with pytest.raises(ValueError):
        client.fetch_operational_capacity(cycle="bogus")


def test_record_serializes_schema_keys(client):
    rec = client.parse_operational_capacity(load_html(), "2026-06-22", PULLED, "ref")[0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "point_name", "point_id",
                "design_capacity", "available_capacity", "units", "pulled_at_utc"):
        assert key in d
