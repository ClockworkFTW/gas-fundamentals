"""Capture live Pipe Ranger JSON responses into tests/fixtures/ for offline tests.

Run occasionally to refresh sample payloads:
    .\.venv\Scripts\python.exe tests\refresh_fixtures.py
The committed fixtures are what the unit tests parse — no network in tests.
"""
import json
import pathlib
import requests

FIX = pathlib.Path(__file__).parent / "fixtures"
FIX.mkdir(exist_ok=True)
BASE = "https://www.pge.com/bin/pipeline"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.pge.com/pipeline/operations/cgt_pipeline_status.page",
}

GETS = [
    "dthphysicalpipeline",
    "physicalpipelinemmcf",
    "scheduledvolumes",
    "scheduledvolumedata",
    "supplydemand",
    "storageactivity",
    "systeminventorysummary",
    "systemInventoryStatus",
    "HeatingValuesServlet",
]
POSTS = [("ofoefoarchive", {"ofotype": "ofo"}), ("ofoefoarchive_efo", {"ofotype": "efo"})]

sess = requests.Session()
sess.headers.update(HEADERS)

for name in GETS:
    r = sess.get(f"{BASE}/{name}", timeout=30)
    (FIX / f"{name}.json").write_text(r.text, encoding="utf-8")
    print(f"saved {name}.json ({len(r.text)} bytes, {r.headers.get('content-type')})")

for name, data in POSTS:
    ep = name.split("_")[0]
    r = sess.post(f"{BASE}/{ep}", data=data, timeout=30)
    (FIX / f"{name}.json").write_text(r.text, encoding="utf-8")
    print(f"saved {name}.json ({len(r.text)} bytes)")

# Print the exact PlanData keys + Date field so the parser maps them correctly.
sd = json.loads((FIX / "supplydemand.json").read_text(encoding="utf-8"))
print("\nsupplydemand: list len", len(sd))
pd0 = sd[0]["PlanData"]
print("PlanData keys:", sorted(pd0.keys()))
print("Date field sample:", pd0.get("Date"), "| Additional_Data:", pd0.get("Additional_Data"))

sv = json.loads((FIX / "scheduledvolumes.json").read_text(encoding="utf-8"))
print("\nscheduledvolumes rows:", len(sv["schd_values"]["v_schd_value"]))
print("cycles:", [r["cycle"] for r in sv["schd_values"]["v_schd_value"]])
print("row0 keys:", sorted(sv["schd_values"]["v_schd_value"][0].keys()))

# --------------------------------------------------------------------------- #
# EIA weekly storage (needs EIA_API_KEY in .env). Captures Pacific + Lower 48
# for a small recent window as a fixture for offline tests.
# --------------------------------------------------------------------------- #
import os  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()
eia_key = os.getenv("EIA_API_KEY")
if not eia_key:
    print("\n[eia] EIA_API_KEY not set — skipping EIA fixture refresh")
else:
    params = [
        ("api_key", eia_key),
        ("frequency", "weekly"),
        ("data[0]", "value"),
        ("facets[series][]", "NW2_EPG0_SWO_R35_BCF"),  # Pacific
        ("facets[series][]", "NW2_EPG0_SWO_R48_BCF"),  # Lower 48
        ("start", "2026-04-01"),
        ("sort[0][column]", "period"),
        ("sort[0][direction]", "asc"),
        ("length", 5000),
    ]
    r = requests.get(
        "https://api.eia.gov/v2/natural-gas/stor/wkly/data",
        params=params,
        headers={"User-Agent": "gas-fundamentals/0.1"},
        timeout=30,
    )
    # EIA echoes the api_key back in request.params — scrub it before committing.
    import re as _re  # noqa: E402
    _eia_text = _re.sub(r'("api_key":")[^"]*(")', r"\1REDACTED\2", r.text)
    (FIX / "eia_weekly_storage.json").write_text(_eia_text, encoding="utf-8")
    rows = r.json().get("response", {}).get("data", [])
    print(f"\nsaved eia_weekly_storage.json ({len(r.text)} bytes, {len(rows)} rows)")
    if rows:
        print("eia row sample:", json.dumps(rows[-1]))

# --------------------------------------------------------------------------- #
# GTN (TC Energy) — operationally available capacity + scheduled quantity.
# --------------------------------------------------------------------------- #
from datetime import datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

gtn = requests.Session()
gtn.headers.update({"User-Agent": HEADERS["User-Agent"], "X-Requested-With": "XMLHttpRequest",
                    "Referer": "https://www.tcplus.com/GTN/OperationalCapacity"})
gtn_day = (datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1))
r = gtn.get(
    "https://www.tcplus.com/GTN/OperationalCapacity/Generate",
    params={"GasDay": gtn_day.strftime("%m/%d/%Y"), "CycleType": 1, "ExportEnum": 0},
    timeout=40,
)
(FIX / "gtn_operational_capacity.json").write_text(r.text, encoding="utf-8")
content = r.json().get("data", {}).get("Content", [])
print(f"\nsaved gtn_operational_capacity.json ({len(r.text)} bytes, {len(content)} locations for {gtn_day})")

# GTN notices grid: indicator="" = all categories; verbose sort_direction; eff-date window.
nr = gtn.get(
    "https://www.tcplus.com/GTN/Notice/Retrieve",
    params={"filter.SelectedIndicator": "", "filter.SelectedStatus": "", "filter.SelectedTypeIds": "",
            "filter.EffDate": (gtn_day - timedelta(days=30)).strftime("%m/%d/%Y"),
            "filter.EndDate": (gtn_day + timedelta(days=45)).strftime("%m/%d/%Y"),
            "page": 1, "sort": "PostingDate", "sort_direction": "Descending"},
    timeout=40,
)
(FIX / "gtn_notices.json").write_text(nr.text, encoding="utf-8")
ndata = nr.json().get("data", [])
print(f"saved gtn_notices.json ({len(nr.text)} bytes, {len(ndata)} notices)")

# --------------------------------------------------------------------------- #
# El Paso (EPNG, Kinder Morgan) — WebForms OAC: GET page, harvest viewstate,
# POST the Retrieve button, save the populated grid HTML.
# --------------------------------------------------------------------------- #
from bs4 import BeautifulSoup  # noqa: E402

epng = requests.Session()
epng.headers.update({"User-Agent": HEADERS["User-Agent"]})
EPNG_PAGE = "https://pipeline2.kindermorgan.com/Capacity/OpAvailPoint.aspx?code=EPNG"
_p = BeautifulSoup(epng.get(EPNG_PAGE, timeout=60).text, "lxml")
_f = {}
for _i in _p.find_all("input"):
    if _i.get("name") and (_i.get("type") or "").lower() not in ("submit", "button", "image"):
        _f[_i["name"]] = _i.get("value", "")
for _s in _p.find_all("select"):
    if _s.get("name"):
        _o = _s.find("option", selected=True) or _s.find("option")
        _f[_s["name"]] = _o.get("value", "") if _o else ""
_f["__EVENTTARGET"] = "ctl00$WebSplitter1$tmpl1$ContentPlaceHolder1$HeaderBTN1$btnRetrieve"
_f["__EVENTARGUMENT"] = ""
_resp = epng.post(EPNG_PAGE, data=_f, timeout=60).text
(FIX / "epng_operational_capacity.html").write_text(_resp, encoding="utf-8")
_n = sum(1 for _tr in BeautifulSoup(_resp, "lxml").find_all("tr")
         if len(_tr.find_all("td")) >= 12 and (_tr.find_all("td")[1].get_text(strip=True) or "").isdigit())
print(f"saved epng_operational_capacity.html ({len(_resp)} bytes, ~{_n} data rows)")

# EPNG notices render on a plain GET (no postback).
_nhtml = epng.get("https://pipeline2.kindermorgan.com/Notices/Notices.aspx?code=EPNG", timeout=60).text
(FIX / "epng_notices.html").write_text(_nhtml, encoding="utf-8")
print(f"saved epng_notices.html ({len(_nhtml)} bytes)")

# --------------------------------------------------------------------------- #
# NOVA / NGTL (TC Energy) — TC Customer Express public AWS-gateway CSV feeds.
# chart/csv (capability+flow), csr/csv (system balance), outages, plant turnarounds.
# Same public endpoints serve Foothills' export-border view (see foothills.py).
# --------------------------------------------------------------------------- #
NOVA_API = "https://f51561ras5.execute-api.us-west-2.amazonaws.com/production"
nova = requests.Session()
nova.headers.update({"User-Agent": HEADERS["User-Agent"], "Accept": "text/csv, */*",
                     "Origin": "https://my.tccustomerexpress.com",
                     "Referer": "https://my.tccustomerexpress.com/"})
for _fname, _path, _params in [
    ("nova_chart.csv", "chart/csv", None),
    ("nova_csr.csv", "csr/csv/", {"unit": "MMcf", "duration": 2}),
    ("nova_outages.csv", "csv/outages/", None),
    ("nova_plant_turnarounds.csv", "plantturnaroundactivity/csv/", None),
]:
    _r = nova.get(f"{NOVA_API}/{_path}", params=_params, timeout=60)
    (FIX / _fname).write_text(_r.text, encoding="utf-8")
    print(f"saved {_fname} ({len(_r.text)} bytes, {_r.headers.get('content-type')})")

# --------------------------------------------------------------------------- #
# Transwestern (Energy Transfer / iPost) — OAC+scheduled CSV + 3 notice CSVs.
# Plain CSV via GET (no viewstate/auth). asset=TW, cycle 0=Timely/1=Evening.
# --------------------------------------------------------------------------- #
TW_BASE = "https://twtransfer.energytransfer.com"
tw = requests.Session()
tw.headers.update({"User-Agent": HEADERS["User-Agent"], "Accept": "text/csv, */*"})
tw_day = (datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)).strftime("%m/%d/%Y")
_oac = tw.get(f"{TW_BASE}/ipost/capacity/operationally-available",
              params={"asset": "TW", "gasDay": tw_day, "cycle": 0, "searchType": "ALL",
                      "locType": "ALL", "locZone": "ALL", "max": "ALL", "f": "csv", "extension": "csv"},
              timeout=60)
(FIX / "tw_operational_capacity.csv").write_text(_oac.text, encoding="utf-8")
print(f"\nsaved tw_operational_capacity.csv ({len(_oac.text)} bytes, {len(_oac.text.splitlines())} lines) for {tw_day}")
for _cat in ("critical", "non-critical", "planned-service-outage"):
    _r = tw.get(f"{TW_BASE}/ipost/notice/{_cat}", params={"asset": "TW", "f": "csv", "extension": "csv"}, timeout=40)
    (FIX / f"tw_notices_{_cat}.csv").write_text(_r.text, encoding="utf-8")
    print(f"saved tw_notices_{_cat}.csv ({len(_r.text)} bytes)")

# --------------------------------------------------------------------------- #
# Kern River (BHE) — Services Portal renders OAC + notices grids on plain GET
# (?gasDay sidesteps the WebForms/reCAPTCHA path).
# --------------------------------------------------------------------------- #
KR_BASE = "https://services.kernrivergas.com/portal"
kr = requests.Session()
kr.headers.update({"User-Agent": HEADERS["User-Agent"], "Accept": "text/html,*/*"})
kr_day = (datetime.now(ZoneInfo("America/Los_Angeles")).date() - timedelta(days=1)).strftime("%m/%d/%Y")
_oac = kr.get(f"{KR_BASE}/Informational-Postings/Capacity/Operationally-Available",
              params={"gasDay": kr_day}, timeout=60)
(FIX / "kern_oac.html").write_text(_oac.text, encoding="utf-8")
print(f"\nsaved kern_oac.html ({len(_oac.text)} bytes) for {kr_day}")
for _cat in ("Critical", "Non-Critical", "Planned-Service-Outage"):
    _r = kr.get(f"{KR_BASE}/Informational-Postings/Notices/{_cat}", timeout=40)
    (FIX / f"kern_notices_{_cat}.html").write_text(_r.text, encoding="utf-8")
    print(f"saved kern_notices_{_cat}.html ({len(_r.text)} bytes)")

# --------------------------------------------------------------------------- #
# Ruby (Tallgrass, pipeline 325) — Incapsula WAF; needs RUBY_COOKIE (browser
# clearance cookie) in .env. WebForms async-postback OA grid (delivery+receipt).
# --------------------------------------------------------------------------- #
ruby_cookie = os.getenv("RUBY_COOKIE")
if not ruby_cookie:
    print("\n[ruby] RUBY_COOKIE not set — skipping Ruby fixture refresh")
else:
    from ebb.ruby import RubyClient  # noqa: E402

    _rc = RubyClient(cookie=ruby_cookie)
    _rday = (datetime.now(ZoneInfo("America/Los_Angeles")).date()).isoformat()
    try:
        _fields = _rc._load_form()
        for _loc in ("rbDelivery", "rbReceipt"):
            _txt = _rc.fetch_location(_fields, _loc, _rday, 0)
            _name = "delivery" if _loc == "rbDelivery" else "receipt"
            (FIX / f"ruby_oa_{_name}.html").write_text(_txt, encoding="utf-8")
            print(f"saved ruby_oa_{_name}.html ({len(_txt)} bytes)")
    except Exception as _e:  # noqa: BLE001
        print(f"[ruby] refresh failed ({type(_e).__name__}): {_e} — refresh RUBY_COOKIE")
