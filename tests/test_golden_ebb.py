"""Golden-master (characterization) tests for the src/ebb/ clients.

Locks the CURRENT normalized output of every ebb client BEFORE the ebb refactor,
so any behavior change is caught byte-for-byte. For each client the full
``pull()`` envelope (records + notices) — or, for EIA, the normalized weekly
records + 5-yr bands — is snapshotted from the committed raw fixtures
(``tests/fixtures/``) into ``tests/golden/``.

Determinism: network and wall-clock are eliminated. The HTTP fetches are
monkeypatched to return the committed fixtures, and each module's
``utc_now_iso()`` is frozen, so a client's output depends only on the fixtures +
the parsing/normalization code under test. ``raw_ref`` is derived from a fixed
``data_dir``/``gas_day`` and is therefore stable too.

How it works: on first run (golden absent) or when ``UPDATE_GOLDEN`` is set, the
produced payload is written to ``tests/golden/<name>.json``; every run then
asserts the produced payload equals the stored golden. To regenerate
intentionally (only after an APPROVED output change):

    PowerShell:  $env:UPDATE_GOLDEN=1; .\\.venv\\Scripts\\python.exe -m pytest tests/test_golden_ebb.py; Remove-Item Env:UPDATE_GOLDEN
    bash:        UPDATE_GOLDEN=1 ./.venv/Scripts/python.exe -m pytest tests/test_golden_ebb.py
"""
import csv
import io
import json
import os
import pathlib

from ebb import (
    eia,
    el_paso,
    foothills,
    gtn,
    kern_river,
    nova,
    pipe_ranger,
    ruby,
    transwestern,
)
from ebb.schema import norm_date

FIX = pathlib.Path(__file__).parent / "fixtures"
GOLDEN = pathlib.Path(__file__).parent / "golden"
FROZEN = "2026-06-22T15:00:00Z"  # frozen pulled_at_utc for every client


# --------------------------------------------------------------------------- #
# snapshot helper
# --------------------------------------------------------------------------- #


def assert_golden(name, payload):
    """Compare ``payload`` to ``tests/golden/<name>.json``.

    Writes the golden on first run (file absent) or when ``UPDATE_GOLDEN`` is
    set, then always asserts the produced payload equals the stored golden.
    Comparison is on the loaded JSON structure: dict-key order is irrelevant,
    values must match exactly. Re-serializing identical data is byte-stable.
    """
    GOLDEN.mkdir(parents=True, exist_ok=True)
    path = GOLDEN / f"{name}.json"
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    produced = json.loads(serialized)
    if os.getenv("UPDATE_GOLDEN") or not path.exists():
        path.write_text(serialized + "\n", encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert produced == expected, (
        f"normalized output drifted from tests/golden/{name}.json — if this change "
        f"is intended and approved, regenerate with UPDATE_GOLDEN=1"
    )


def read_fix(name):
    return (FIX / name).read_text(encoding="utf-8")


def freeze_time(monkeypatch, module):
    """Freeze a client module's pulled_at clock so snapshots are reproducible."""
    monkeypatch.setattr(module, "utc_now_iso", lambda: FROZEN)


def _latest_actual_gas_day(chart_csv):
    """Newest NGTL chart row that actually carries flow (matches the live default)."""
    rows = list(csv.DictReader(io.StringIO(chart_csv)))
    days = [
        norm_date(r.get("Gas Day"))
        for r in rows
        if any((v or "").strip() for k, v in r.items() if "Actual Flow" in k)
    ]
    return max(d for d in days if d)


# --------------------------------------------------------------------------- #
# Pipe Ranger (PG&E) — 9 JSON servlets -> records + notices
# --------------------------------------------------------------------------- #

PR_FIX = {
    "scheduled_volumes": "scheduledvolumes.json",
    "physical_capacity": "dthphysicalpipeline.json",
    "supply_demand": "supplydemand.json",
    "storage": "storageactivity.json",
    "inventory_status": "systemInventoryStatus.json",
    "inventory_summary": "systeminventorysummary.json",
    "daily_btu": "scheduledvolumedata.json",
    "ofo": "ofoefoarchive.json",
    "efo": "ofoefoarchive_efo.json",
}


def test_golden_pipe_ranger(monkeypatch):
    freeze_time(monkeypatch, pipe_ranger)
    client = pipe_ranger.PipeRangerClient(data_dir="data/pipe_ranger")

    def fake_fetch(key, *, raw_dir=None):
        return pipe_ranger.PipeRangerClient._decode(read_fix(PR_FIX[key]))

    monkeypatch.setattr(client, "fetch", fake_fetch)
    result = client.pull("2026-06-22", cycle=None, write=False)
    assert_golden("pipe_ranger", result)


# --------------------------------------------------------------------------- #
# GTN (TC Energy) — JSON OAC + notices grid
# --------------------------------------------------------------------------- #


def test_golden_gtn(monkeypatch):
    freeze_time(monkeypatch, gtn)
    client = gtn.GTNClient(data_dir="data/gtn")
    oac = json.loads(read_fix("gtn_operational_capacity.json"))
    notices = json.loads(read_fix("gtn_notices.json"))["data"]
    monkeypatch.setattr(client, "fetch_operational_capacity", lambda gd, cy, **k: oac)
    monkeypatch.setattr(client, "fetch_notices", lambda gd, **k: notices)
    result = client.pull("2026-06-21", "timely", write=False)
    assert_golden("gtn", result)


# --------------------------------------------------------------------------- #
# El Paso (EPNG, Kinder Morgan) — WebForms OAC grid + notices grid
# --------------------------------------------------------------------------- #


def test_golden_el_paso(monkeypatch):
    freeze_time(monkeypatch, el_paso)
    client = el_paso.ElPasoClient(data_dir="data/el_paso")
    oac = read_fix("epng_operational_capacity.html")
    notices = read_fix("epng_notices.html")
    monkeypatch.setattr(client, "fetch_operational_capacity", lambda gd=None, cy=None, **k: oac)
    monkeypatch.setattr(client, "fetch_notices", lambda **k: notices)
    result = client.pull("2026-06-22", None, write=False)
    assert_golden("el_paso", result)


# --------------------------------------------------------------------------- #
# Transwestern (Energy Transfer / iPost) — CSV OAC + 3 notice CSVs
# --------------------------------------------------------------------------- #


def test_golden_transwestern(monkeypatch):
    freeze_time(monkeypatch, transwestern)
    client = transwestern.TranswesternClient(data_dir="data/transwestern")
    monkeypatch.setattr(
        client, "fetch_operational_capacity", lambda gd, cy, **k: read_fix("tw_operational_capacity.csv")
    )
    monkeypatch.setattr(
        client, "fetch_notice_category", lambda category, **k: read_fix(f"tw_notices_{category}.csv")
    )
    result = client.pull("2026-06-22", "timely", write=False)
    assert_golden("transwestern", result)


# --------------------------------------------------------------------------- #
# Kern River (BHE) — HTML OAC grid + 3 notice HTML grids
# --------------------------------------------------------------------------- #


def test_golden_kern_river(monkeypatch):
    freeze_time(monkeypatch, kern_river)
    client = kern_river.KernRiverClient(data_dir="data/kern_river")
    monkeypatch.setattr(
        client, "fetch_operational_capacity", lambda gd, **k: read_fix("kern_oac.html")
    )
    monkeypatch.setattr(
        client, "fetch_notice_category", lambda category, **k: read_fix(f"kern_notices_{category}.html")
    )
    result = client.pull("2026-06-22", write=False)
    assert_golden("kern_river", result)


# --------------------------------------------------------------------------- #
# NOVA / NGTL (TC Energy) — chart + CSR + outages + plant turnarounds
# --------------------------------------------------------------------------- #


def test_golden_nova(monkeypatch):
    freeze_time(monkeypatch, nova)
    client = nova.NovaClient(data_dir="data/nova")
    chart = read_fix("nova_chart.csv")
    monkeypatch.setattr(client, "fetch_capability_flow", lambda **k: chart)
    monkeypatch.setattr(client, "fetch_system_report", lambda **k: read_fix("nova_csr.csv"))
    monkeypatch.setattr(client, "fetch_outages", lambda **k: read_fix("nova_outages.csv"))
    monkeypatch.setattr(client, "fetch_plant_turnarounds", lambda **k: read_fix("nova_plant_turnarounds.csv"))
    result = client.pull(_latest_actual_gas_day(chart), write=False)
    assert_golden("nova", result)


# --------------------------------------------------------------------------- #
# Foothills — export-border view over the shared NGTL feeds
# --------------------------------------------------------------------------- #


def test_golden_foothills(monkeypatch):
    freeze_time(monkeypatch, foothills)
    client = foothills.FoothillsClient(data_dir="data/foothills")
    chart = read_fix("nova_chart.csv")
    monkeypatch.setattr(client, "fetch_capability_flow", lambda **k: chart)
    monkeypatch.setattr(client, "fetch_system_report", lambda **k: read_fix("nova_csr.csv"))
    monkeypatch.setattr(client, "fetch_outages", lambda **k: read_fix("nova_outages.csv"))
    result = client.pull(_latest_actual_gas_day(chart), write=False)
    assert_golden("foothills", result)


# --------------------------------------------------------------------------- #
# Ruby (Tallgrass) — WebForms async-postback OA grid (receipt + delivery)
# --------------------------------------------------------------------------- #


def test_golden_ruby(monkeypatch):
    freeze_time(monkeypatch, ruby)
    client = ruby.RubyClient(data_dir="data/ruby", cookie="ASP.NET_SessionId=x; visid_incap_2123500=y")
    monkeypatch.setattr(client, "_load_form", lambda: {"__VIEWSTATE": "VS"})

    def fake_fetch(fields, location, gas_day, cycle_value, *, raw_dir=None):
        return read_fix("ruby_oa_delivery.html") if location == "rbDelivery" else read_fix("ruby_oa_receipt.html")

    monkeypatch.setattr(client, "fetch_location", fake_fetch)
    result = client.pull("2026-06-23", "best", write=False)
    assert_golden("ruby", result)


# --------------------------------------------------------------------------- #
# EIA (Open Data API v2) — weekly storage normalize + 5-yr bands
# --------------------------------------------------------------------------- #

PAC = "NW2_EPG0_SWO_R35_BCF"
L48 = "NW2_EPG0_SWO_R48_BCF"


def test_golden_eia(monkeypatch):
    monkeypatch.setattr(eia, "utc_now_iso", lambda: FROZEN)
    client = eia.EIAClient(api_key="test-key", data_dir="data/eia")
    payload = json.loads(read_fix("eia_weekly_storage.json"))
    rows = payload["response"]["data"]
    region_by_series = {PAC: "Pacific", L48: "Lower 48"}
    recs = client.normalize_storage(rows, region_by_series, FROZEN, "ref")
    record_dicts = [r.to_dict() for r in recs]
    bands = eia.five_year_bands(record_dicts, years=5)
    assert_golden("eia", {"records": record_dicts, "bands": bands})
