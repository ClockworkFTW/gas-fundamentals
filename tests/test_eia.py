"""Offline tests for the EIA client (no network).

Parsing/resolution logic is tested with inline rows plus a real captured
fixture (tests/fixtures/eia_weekly_storage.json). Refresh the fixture with
.\\.venv\\Scripts\\python.exe tests\\refresh_fixtures.py (needs EIA_API_KEY).
"""
import json
import pathlib

import pytest

from ebb import eia

FIX = pathlib.Path(__file__).parent / "fixtures"


def _row(series, desc, period, value, units="BCF"):
    return {
        "period": period,
        "series": series,
        "series-description": desc,
        "value": value,
        "units": units,
        "process": "SWO",
        "product": "EPG0",
    }


PAC = "NW2_EPG0_SWO_R35_BCF"
L48 = "NW2_EPG0_SWO_R48_BCF"
PAC_DESC = "Pacific Region Natural Gas Working Underground Storage (Billion Cubic Feet)"
L48_DESC = "Lower 48 States Natural Gas Working Underground Storage (Billion Cubic Feet)"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("EIA_API_KEY", "test-key")
    return eia.EIAClient(api_key="test-key", data_dir="data/eia")


def test_to_float():
    assert eia.to_float("2,345") == 2345.0
    assert eia.to_float("271") == 271.0
    assert eia.to_float(None) is None
    assert eia.to_float("") is None
    assert eia.to_float("n/a") is None


def test_load_api_key_explicit_and_env(monkeypatch):
    assert eia.load_api_key("abc") == "abc"
    monkeypatch.setenv("EIA_API_KEY", "from-env")
    assert eia.load_api_key() == "from-env"


def test_load_api_key_missing(monkeypatch):
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    # Avoid picking up a real .env on the dev box.
    monkeypatch.setattr(eia, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        eia.load_api_key()


def test_resolve_region_series_uses_discovery(client):
    # Seed the discovery cache so no network is hit.
    client._series_cache = {PAC: PAC_DESC, L48: L48_DESC, "NW2_EPG0_SWO_R34_BCF": "Mountain Region ..."}
    resolved = client.resolve_region_series(["Pacific", "Lower 48"])
    assert resolved == {"Pacific": PAC, "Lower 48": L48}


def test_resolve_region_series_fallback_map(client):
    # Empty discovery -> documented fallback by region name.
    client._series_cache = {}
    resolved = client.resolve_region_series(["Pacific", "Mountain"])
    assert resolved["Pacific"] == eia.DEFAULT_STORAGE_SERIES["pacific"]
    assert resolved["Mountain"] == eia.DEFAULT_STORAGE_SERIES["mountain"]


def test_normalize_storage_wow_and_region(client):
    rows = [
        _row(PAC, PAC_DESC, "2026-06-12", "271"),
        _row(PAC, PAC_DESC, "2026-06-05", "260"),
        _row(L48, L48_DESC, "2026-06-12", "2800"),
    ]
    region_by_series = {PAC: "Pacific", L48: "Lower 48"}
    recs = client.normalize_storage(rows, region_by_series, "2026-06-22T15:00:00Z", "ref")

    pac = sorted([r for r in recs if r.series_id == PAC], key=lambda r: r.period)
    assert [r.period for r in pac] == ["2026-06-05", "2026-06-12"]
    assert pac[0].wow_change is None              # first period has no prior
    assert pac[1].wow_change == pytest.approx(11.0)  # 271 - 260
    assert pac[1].region == "Pacific" and pac[1].units == "BCF"
    assert pac[1].dataset == "weekly_storage" and pac[1].value == 271.0

    l48 = [r for r in recs if r.series_id == L48][0]
    assert l48.region == "Lower 48" and l48.wow_change is None


def test_normalize_storage_region_falls_back_to_description(client):
    rows = [_row(PAC, PAC_DESC, "2026-06-12", "271")]
    recs = client.normalize_storage(rows, {}, "t", None)  # no region map
    assert recs[0].region == PAC_DESC  # uses series-description


def test_normalize_real_fixture(client):
    """Parse the captured EIA v2 response end-to-end."""
    payload = json.loads((FIX / "eia_weekly_storage.json").read_text(encoding="utf-8"))
    rows = payload["response"]["data"]
    region_by_series = {PAC: "Pacific", L48: "Lower 48"}
    recs = client.normalize_storage(rows, region_by_series, "2026-06-22T15:00:00Z", "ref")

    regions = {r.region for r in recs}
    assert {"Pacific", "Lower 48"} <= regions
    assert all(r.units == "BCF" and r.dataset == "weekly_storage" for r in recs)

    pac = sorted([r for r in recs if r.region == "Pacific"], key=lambda r: r.period)
    assert len(pac) >= 2
    assert pac[0].wow_change is None
    # WoW change equals the difference of consecutive weekly values.
    assert pac[1].wow_change == pytest.approx(pac[1].value - pac[0].value)


def test_record_serializes_expected_keys(client):
    rows = [_row(PAC, PAC_DESC, "2026-06-12", "271")]
    d = client.normalize_storage(rows, {PAC: "Pacific"}, "t", "ref")[0].to_dict()
    for key in ("source", "dataset", "series_id", "region", "period", "value", "units", "pulled_at_utc"):
        assert key in d
    assert d["source"] == "eia"
