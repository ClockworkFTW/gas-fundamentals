"""Offline tests for the Ruby (Tallgrass) client against captured fixtures.

The fixtures are the WebForms async-postback responses for the OA grid (delivery /
receipt). Refresh: .\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py
(needs a valid RUBY_COOKIE — the site is behind an Incapsula WAF).
"""
import pathlib

import pytest

from ebb import ruby

FIX = pathlib.Path(__file__).parent / "fixtures"
PULLED = "2026-06-23T15:00:00Z"
GAS_DAY = "2026-06-23"

CHALLENGE_HTML = (
    '<html><head><META NAME="robots" CONTENT="noindex,nofollow">'
    '<script src="/_Incapsula_Resource?SWJIYLWA=abc"></script></head><body></body></html>'
)


def delivery_html():
    return (FIX / "ruby_oa_delivery.html").read_text(encoding="utf-8")


def receipt_html():
    return (FIX / "ruby_oa_receipt.html").read_text(encoding="utf-8")


@pytest.fixture
def client():
    # explicit cookie so the test never reads .env / hits the network
    return ruby.RubyClient(data_dir="data/ruby", cookie="ASP.NET_SessionId=x; visid_incap_2123500=y")


# --------------------------------------------------------------------------- #
# OA grid parsing
# --------------------------------------------------------------------------- #


def test_parse_delivery_grid_shape_and_units(client):
    recs = client.parse_grid(delivery_html(), "delivery", GAS_DAY, 0, PULLED, "ref")
    assert recs, "expected delivery OA records"
    assert all(r.source == "ruby" and r.dataset_type == "operationally_available" for r in recs)
    assert all(r.units == "Dth/d" and r.original_units == "Dth/d" for r in recs)
    assert all(r.flow_direction == "delivery" for r in recs)
    assert all(r.gas_day == GAS_DAY and r.cycle == "best available" for r in recs)


def test_parse_grid_onyx_hill_pge_delivery(client):
    recs = client.parse_grid(delivery_html(), "delivery", GAS_DAY, 0, PULLED, "ref")
    onyx = [r for r in recs if "ONYX HILL" in r.point_name.upper()]
    assert onyx, "Ruby->PG&E delivery (PACGAS/RUBY ONYX HILL == onyx_ruby) should be present"
    r = onyx[0]
    assert r.design_capacity and r.operational_capacity and r.available_capacity
    assert r.scheduled_qty is not None and r.scheduled_qty == r.original_qty
    assert r.point_id  # location number


def test_parse_grid_has_malin_and_floats(client):
    recs = client.parse_grid(delivery_html(), "delivery", GAS_DAY, 0, PULLED, "ref")
    assert any("MALIN" in r.point_name.upper() for r in recs)
    for r in recs:
        for v in (r.design_capacity, r.operational_capacity, r.available_capacity, r.scheduled_qty):
            assert v is None or isinstance(v, float)  # comma-strings -> floats


def test_parse_receipt_grid_direction(client):
    recs = client.parse_grid(receipt_html(), "receipt", GAS_DAY, 0, PULLED, "ref")
    assert recs and all(r.flow_direction == "receipt" for r in recs)


# --------------------------------------------------------------------------- #
# WAF challenge handling + cookie parsing
# --------------------------------------------------------------------------- #


def test_load_form_raises_on_challenge(client, monkeypatch):
    monkeypatch.setattr(client, "_get_page", lambda: CHALLENGE_HTML)
    with pytest.raises(ruby.RubyChallengeError):
        client._load_form()


def test_load_form_harvests_when_cleared(client, monkeypatch):
    html = '<form><input name="__VIEWSTATE" value="VS"/><input name="__EVENTVALIDATION" value="EV"/></form>'
    monkeypatch.setattr(client, "_get_page", lambda: html)
    fields = client._load_form()
    assert fields["__VIEWSTATE"] == "VS" and fields["__EVENTVALIDATION"] == "EV"


def test_parse_cookie_header():
    out = ruby._parse_cookie_header("a=1; b=two=2; visid_incap_2123500=zzz ")
    assert out["a"] == "1" and out["visid_incap_2123500"] == "zzz"
    assert out["b"] == "two=2"  # only split on first '='


# --------------------------------------------------------------------------- #
# fetch params + cycles + orchestration
# --------------------------------------------------------------------------- #


def test_fetch_location_builds_postback(client, monkeypatch):
    captured = {}

    class FakeResp:
        text = "<table></table>"
        def raise_for_status(self):
            pass

    def fake_post(url, data=None, timeout=None, headers=None):
        captured["data"] = data
        captured["headers"] = headers
        return FakeResp()

    monkeypatch.setattr(client.session, "post", fake_post)
    client.fetch_location({"__VIEWSTATE": "VS"}, "rbDelivery", "2026-06-23", 1)
    d = captured["data"]
    assert d["ctl00$mainContent$location"] == "rbDelivery"
    assert d["ctl00$mainContent$ddlCycle"] == "1"
    assert d["ctl00$mainContent$tbGasFlow"] == "6/23/2026"      # ISO -> M/D/YYYY
    assert d["ctl00$mainContent$tbgasflowend"] == "6/24/2026"   # +1 day
    assert d["__ASYNCPOST"] == "true"
    assert d[ruby.RETRIEVE_TRIGGER] == "Retrieve"
    assert captured["headers"]["X-MicrosoftAjax"] == "Delta=true"


def test_pull_rejects_unknown_cycle(client):
    with pytest.raises(ValueError):
        client.pull("2026-06-23", "bogus")


def test_pull_offline_combines_receipt_and_delivery(client, monkeypatch):
    monkeypatch.setattr(client, "_load_form", lambda: {"__VIEWSTATE": "VS"})

    def fake_fetch(fields, location, gas_day, cycle_value, *, raw_dir=None):
        return delivery_html() if location == "rbDelivery" else receipt_html()

    monkeypatch.setattr(client, "fetch_location", fake_fetch)
    result = client.pull(GAS_DAY, "best", write=False)
    assert result["source"] == "ruby"
    dirs = {r["flow_direction"] for r in result["records"]}
    assert dirs == {"receipt", "delivery"}
    names = {r["point_name"].upper() for r in result["records"]}
    assert any("ONYX HILL" in n for n in names)


def test_record_serializes_schema_keys(client):
    rec = client.parse_grid(delivery_html(), "delivery", GAS_DAY, 0, PULLED, "ref")[0]
    d = rec.to_dict()
    for key in ("source", "dataset_type", "gas_day", "cycle", "point_name", "point_id",
                "flow_direction", "scheduled_qty", "design_capacity", "operational_capacity",
                "available_capacity", "units", "original_units", "original_qty",
                "pulled_at_utc", "raw_ref"):
        assert key in d
