# gas-fundamentals

Data ingestion pipeline that feeds a Microsoft Copilot Studio agent summarizing
natural gas market fundamentals for PG&E's core gas supply desk.

**This repo is the ingestion side only** (Python, run on Jenkins). It pulls pipe
and storage operational data from western EBBs plus EIA, computes analytics
deterministically, and hands two outputs to Power Automate. The agent, SharePoint
knowledge base, and Power Automate flows live outside this repo.

---

## 1. How this repo fits the larger system

```
Jenkins (Python, this repo)
   │  HTTP POST (JSON, <5 MB payload per call)
   ▼
Power Automate cloud flow  ── "When an HTTP request is received" (premium)
   │  SharePoint connector: Create file / Create-Update list item
   ▼
SharePoint  ── knowledge base (document libraries) + optional "Current Curves" list
   │  grounding
   ▼
Copilot Studio agent  ── daily report (scheduled flow) + Teams chatbot
```

### Decisions already made (do not relitigate without reason)

- **Do all quantitative work here, in Python.** The LLM agent is weak at math and
  at reasoning over spreadsheets. This repo computes every number (day-over-day
  changes, storage vs. band, basis, utilization) and emits a *pre-rendered
  narrative brief* with the numbers already in prose + small tables. The agent
  only does language synthesis, never calculation.
- **No Dataverse custom tables, no Azure.** Storage is SharePoint. (Copilot Studio
  still uses an auto-provisioned Dataverse to index knowledge — that's fine and
  not our concern here.)
- **M365 Copilot license is present in-tenant** → SharePoint knowledge supports
  files up to 200 MB with Work IQ on, and interactive chatbot use by licensed
  users is zero-rated. The scheduled daily run still consumes Copilot credits.
- **No headless browsers.** All ingestion is `requests`-based (GET, or
  `requests.Session` for ASP.NET WebForms POST). See §4.

---

## 2. Scope of this repo

One module per source. Each module: fetch the source's dataset(s) for a given
gas day + cycle → normalize to the common schema (§5) → return records. A
separate orchestration layer computes analytics, renders the brief, writes
lineage files, and POSTs to Power Automate.

**Build order:**
1. `pipe_ranger.py` — highest value, also the storage aggregator (start here)
2. EIA client (clean REST/JSON, easy win for the macro/storage layer)
3. GTN, Ruby, El Paso, Transwestern, Kern River (reuse the Pipe Ranger pattern)
4. Orchestration: analytics → brief renderer → Power Automate POST

---

## 3. Data sources

Supply reaches PG&E at three border zones — **Malin** (Canada + Rockies),
**Topock** (Southwest/San Juan/Permian), **Daggett** (Rockies) — then moves on
PG&E's backbone to **PG&E Citygate** (the pricing point).

| Source | Feeds PG&E at | Owner / platform | Public EBB | Access |
|---|---|---|---|---|
| **PG&E CGT — Pipe Ranger** | whole backbone, Citygate, inventory, storage | PG&E | `pge.com/pipeline` | public, has CSV download |
| **GTN** | Malin (Canada via Kingsgate) | TC Energy | `tcplus.com/GTN` | "Download" on postings |
| **Ruby Pipeline** | Malin (Rockies via Opal) | Tallgrass | `pipeline.tallgrassenergylp.com` | postings download |
| **El Paso Natural Gas** | Topock | Kinder Morgan | `pipeline2.kindermorgan.com` | postings download |
| **Transwestern** | Topock | Energy Transfer | iPost (`*.energytransfer.com/ipost/…`) | confirm subdomain via NAESB list |
| **Kern River** | Daggett | Berkshire/BHE | `services.kernrivergas.com/portal` | Services Portal export |
| **EIA Open Data API v2** | macro / weekly storage (Pacific) | EIA | `api.eia.gov` | real REST/JSON, free key |

- **NAESB master directory** of every pipeline's current postings URL:
  `naesb.org/members/printed_url_of_pipelines.pdf` — use this for exact, current
  links rather than hardcoding (ownership/platforms change; e.g. Ruby moved from
  Kinder Morgan to Tallgrass).
- **Storage:** independent California storage (Wild Goose, Central Valley, Lodi,
  Gill Ranch) scheduled volumes are **already aggregated into Pipe Ranger's
  injection/withdrawal totals** — so daily storage *flow* comes from Pipe Ranger
  alone. Hit individual storage FERC postings only if per-facility inventory or
  capacity is needed later.
- Optional later: **SoCalGas ENVOY** (public postings) for SoCal-border context /
  Citygate-vs-SoCal spread.

---

## 4. Access approach (the no-scraping method)

There is **no unified REST API** across pipeline EBBs. They publish FERC
Informational Postings in **NAESB-standardized** formats, almost always available
as **downloadable CSV** reachable by a plain HTTP GET or a simple form POST.

Method per EBB:
1. Open the postings page once in Edge/Chrome devtools → Network tab.
2. Click the page's Download/Export and capture the exact request (URL, query
   params, method, any form fields / cookies).
3. Replicate it with `requests` (use `requests.Session` for WebForms POST with
   viewstate). No Puppeteer.

**Three dataset types matter for fundamentals:**
- **Operationally Available Capacity** — utilization / constraints at points
- **Scheduled Quantities** — actual flows at Malin / Topock / Daggett / Citygate
- **Notices / Critical Notices** — outages, maintenance, OFO/EFO

Because the schema is NAESB-standardized, one parser per dataset type largely
transfers across pipelines with field-mapping tweaks.

> **Pipe Ranger is JSON, not CSV/WebForms (verified 2026-06-22).** PG&E's
> modern `pge.com/pipeline` site renders empty table cells that a JavaScript
> clientlib fills by GET-ing plain JSON servlets under
> `https://www.pge.com/bin/pipeline/` (e.g. `scheduledvolumes`,
> `dthphysicalpipeline`, `supplydemand`, `storageactivity`,
> `systemInventoryStatus`, `ofoefoarchive`). There is **no** CSV download or
> ASP.NET WebForms POST to replicate — the in-browser "Download to Excel" is
> built client-side from those same JSON responses. So `pipe_ranger.py` skips
> the devtools-capture step and fetches the JSON directly (no auth/cookies
> needed; just a real `User-Agent` + `X-Requested-With`).
>
> **GTN is also JSON-via-GET (verified 2026-06-22).** TC Energy's "ganesha"
> platform (`tcplus.com/GTN`) serves the Operationally Available Capacity
> posting as JSON from `/GTN/OperationalCapacity/Generate?GasDay=&CycleType=&ExportEnum=0`.
> So far both EBBs we've implemented are reachable as JSON without the CSV
> capture; the remaining ones (Ruby, El Paso, Transwestern, Kern River) may still
> need the devtools method — verify each before assuming.

**Cadence:** schedule to the gas-day cycle, not once daily. Pipe Ranger refreshes
across the day (the "Plans," roughly Plan 1 ~5:30 AM PT through evening updates),
and pipelines update operationally available capacity intraday. A single morning
pull misses the evening/intraday nomination cycles that move basis. Pipe Ranger's
prior-gas-day final capacity/scheduled-volume **download is available after
8:00 AM PT** — handle that timing.

**Etiquette / robustness:** respect each site's terms and robots, throttle,
set a real `User-Agent`, cache so unchanged postings aren't re-pulled, and use
retry/backoff (`tenacity`).

---

## 5. Common normalized schema (design once, reuse everywhere)

All flow/capacity modules emit the same record shape. Proposed fields:

```python
{
  "source":          "pipe_ranger",          # source/pipeline id
  "dataset_type":    "scheduled_quantity",   # | operationally_available | notice
  "gas_day":         "2026-06-22",           # ISO date, Pacific gas day
  "cycle":           "evening",              # timely | evening | id1 | id2 | id3 | final | planN
  "point_name":      "Malin",
  "point_id":        "...",                  # pipeline's location/meter id
  "flow_direction":  "receipt",              # receipt | delivery
  "scheduled_qty":   123456.0,
  "design_capacity": 200000.0,
  "operational_capacity": 180000.0,
  "available_capacity":   60000.0,
  "units":           "Dth/d",                # see unit gotcha below
  "pulled_at_utc":   "2026-06-22T15:03:00Z",
  "raw_ref":         "data/pipe_ranger/2026-06-22_evening.csv"  # lineage pointer
}
```

Notices get their own shape: `source, gas_day, posted_at, notice_type
(OFO|EFO|maintenance|critical|other), stage, headline, body, url`.

**Unit gotcha:** Pipe Ranger uses **MMcf/d**; interstate postings usually use
**Dth/d** (≈ MMBtu/d). Normalize to one internal unit and record the original.
Rough conversion ≈ 1 MMcf ≈ 1,000 Dth at ~1.0 MMBtu/cf heat content — make the
heat-content assumption explicit and configurable.

---

## 6. Outputs

Two streams, both handed to Power Automate (or written for it to pick up):

1. **Machine-readable lineage** — dated JSON/Parquet of the normalized records in
   `data/<source>/<gas_day>_<cycle>.*` (gitignored). For audit/reproducibility.
2. **Pre-rendered daily brief** — Markdown/HTML with the numbers already computed
   and written into prose + small tables. This is what the agent reads/summarizes.

POST to the Power Automate "When an HTTP request is received" flow as JSON, one
file per call (mind the ~5 MB connector payload limit; base64 binaries).

Optional (only if live curve lookups are wanted): publish current/recent curve
values as **SharePoint list items** via the same POST path, for the agent to query
live. Decision still open.

---

## 7. Environment & conventions

- **Python**: **3.11** — Jenkins agent runs 3.11.9 (confirmed). Pin to the 3.11
  series and develop locally on 3.11 so you don't accidentally use 3.12+ syntax.
  Exact patch within 3.11.x doesn't matter (bugfix/security only). Local dev in a
  `.venv` created with `py -3.11 -m venv .venv`.
- **Packages** (`requirements.txt`): `requests, pandas, beautifulsoup4, lxml,
  python-dateutil, openpyxl, python-dotenv, tenacity` (+ `pyarrow` if writing
  Parquet). Commit this file; Jenkins installs from it.
- **Secrets**: the Power Automate trigger URL and EIA API key go in a local
  `.env` (loaded via `python-dotenv`, gitignored) and in **Jenkins Credentials**
  on the server. Never hardcode or commit them. Add a shared-secret header that
  the PA flow verifies, since the trigger URL is the only thing guarding the flow.
- **Time zones**: gas day is Pacific. Be explicit everywhere; never rely on local
  machine TZ. Store `pulled_at` in UTC.
- **Idempotency**: re-running a pull for the same gas_day/cycle should overwrite
  cleanly, not duplicate.
- **Layout**:

```
gas-fundamentals/
  .venv/            # gitignored
  .gitignore        # .venv, .env, data/, __pycache__
  .env              # gitignored
  requirements.txt
  README.md         # this file
  src/ebb/
    pipe_ranger.py  # first module
  data/             # gitignored lineage
  tests/
```

## 8. First task for Claude Code — `pipe_ranger.py` (DONE)

`src/ebb/pipe_ranger.py` is implemented. It fetches the Pipe Ranger JSON
servlets (see the §4 note), normalizes them to the §5 schema, and returns
`records` + `notices`.

**Endpoint → dataset map** (all GET unless noted):

| Dataset | Servlet | Native unit |
|---|---|---|
| Scheduled flows by path/cycle (Malin/Topock/Daggett) | `scheduledvolumes` | Dth |
| Physical capacity | `dthphysicalpipeline` | Dth |
| Supply/demand "Plans" + system metrics | `supplydemand` | MMcf |
| Storage inj/withdrawal (Wild Goose, Lodi, Central Valley, Gill Ranch) | `storageactivity` | MMcf |
| System ending inventory (+ min/max band) | `systemInventoryStatus` / `systeminventorysummary` | MMcf |
| Per-point heat content | `scheduledvolumedata` | BTU/cf |
| OFO/EFO notices | `ofoefoarchive` (POST `ofotype=ofo\|efo`) | — |

**Behavior:** MMcf datasets are converted to **Dth/d** on ingest (canonical
unit; original unit + value preserved); a configurable heat content
(`--heat-content`, default 1000 BTU/cf) drives the conversion. Retry/backoff
via `tenacity`; every raw response is written under
`data/pipe_ranger/<gas_day>_<cycle>/` as the `raw_ref` lineage pointer, plus a
`*.normalized.json`. Gas-day handling is Pacific-aware (timely noms are
day-ahead, so a gas day may only carry the intraday/final cycle). OFO/EFO
notices are filtered to the requested gas day and forward.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.pipe_ranger --gas-day 2026-06-22 --cycle id2
.\.venv\Scripts\python.exe -m src.ebb.pipe_ranger            # latest available, all cycles
```

`--cycle` accepts `timely | evening | id1 | id2 | id3 | final`; omit `--gas-day`
to default to the latest available Pacific gas day (prior day before 08:00 PT).

**Tests** (offline, against committed fixtures in `tests/fixtures/`):

```
.\.venv\Scripts\python.exe -m pytest tests -q
```

Refresh the fixtures from the live site with
`.\.venv\Scripts\python.exe tests\refresh_fixtures.py`.

### Open follow-ups (not blocking)
- **Daily Citygate price:** Pipe Ranger publishes per-point *heat content*
  (`scheduledvolumedata`), not a Citygate *price*. The Citygate price is a market
  quote (ICE / Gas Daily) and belongs to a future market-data source, not this
  module — Citygate *flow/inventory* is covered here.
- **Per-point heat content in conversion:** `scheduledvolumedata` BTU values are
  fetched and saved, but conversion currently uses the single configurable
  default. Wiring per-point BTU into `mmcf_to_dth` is a small enhancement.

## 9. EIA client — `eia.py` (DONE)

`src/ebb/eia.py` reads the **EIA Open Data API v2** Weekly Natural Gas Storage
Report (working gas in underground storage, Bcf). Clean REST/JSON at
`https://api.eia.gov/v2/natural-gas/stor/wkly/data` with `?api_key=`.

- **Series resolution is discovered, not hardcoded.** Region codes are
  unintuitive (R31=East, R34=Mountain, **R35=Pacific**, R48=Lower 48), so the
  client queries the dataset's `facet/series` metadata and matches by region
  name; `DEFAULT_STORAGE_SERIES` is only a documented fallback.
- **Default regions:** Pacific (PG&E-relevant) + Lower 48 + the others for macro
  context. Emits a small EIA time-series record shape (not the §5 flow/capacity
  schema) with per-series week-over-week change. Raw + normalized lineage under
  `data/eia/`.
- **Secrets:** `EIA_API_KEY` loads from `.env` via `python-dotenv`
  (gitignored). Free key: https://www.eia.gov/opendata/register.php — note the
  40-char key has easily-confused `l`/`I` characters.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.eia --dataset storage --regions Pacific "Lower 48" --start 2026-01-01
.\.venv\Scripts\python.exe -m src.ebb.eia            # default regions, full history
```

Tests are offline against `tests/fixtures/eia_weekly_storage.json` (real
captured response); refresh with `tests\refresh_fixtures.py` (needs the key).

## 10. GTN client — `gtn.py` (DONE)

`src/ebb/gtn.py` reads **Gas Transmission Northwest** (TC Energy), which feeds
PG&E at **Malin** with Canadian gas via **Kingsgate**. Source: the "ganesha"
EBB at `tcplus.com/GTN` (see the §4 note — JSON via GET).

- **One endpoint covers two dataset types.** `/GTN/OperationalCapacity/Generate`
  returns Operationally Available Capacity *and* `TotalScheduledQuantity` for all
  ~61 locations in one call, so each `FlowRecord` carries design/operating/
  available capacity **and** scheduled quantity.
- **Notices** come from the `/GTN/Notice/Retrieve` grid (Critical, Non-Critical,
  Planned Service Outage) and map to the §5 `Notice` shape — outage/maintenance
  notices that move basis. See the request gotchas below.
- **Units:** the posting's `MeasurementBasis` is "Million BTU's" (MMBtu/d ==
  **Dth/d**), already canonical — no conversion, original unit recorded.
- **Cycle** maps to GTN's `CycleType` (timely=1, id1=2, id2=3, evening=4, id3=5);
  ISO gas day is reformatted to MM/DD/YYYY for the request. Raw + normalized
  lineage under `data/gtn/`.
- Emits the shared §5 `FlowRecord` from `src/ebb/schema.py` (factored out here so
  every pipeline reuses one record shape).

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.gtn --gas-day 2026-06-21 --cycle timely
```

Tests are offline against `tests/fixtures/gtn_operational_capacity.json` and
`tests/fixtures/gtn_notices.json`.

### Notices request gotchas (reverse-engineered from `resourcetable.js`)
The notices grid `/GTN/Notice/Retrieve` is a stateful resourcetable. The working
GET params:
- `filter.SelectedIndicator=""` → all categories (or 1=Critical, 2=Non-Critical,
  3=Planned Service Outage); `filter.SelectedStatus`, `filter.SelectedTypeIds`
  may be blank.
- `filter.EffDate` / `filter.EndDate` are **required** (MM/DD/YYYY) — the
  effective-date window. The module brackets the gas day by
  −14/+45 days by default.
- `sort=PostingDate`, `sort_direction=Descending` (**verbose** form — "desc"
  returns HTTP 500), `page=1`.

## 11. Ruby Pipeline — `ruby.py` (DONE, with a WAF cookie caveat)

`src/ebb/ruby.py` reads **Ruby Pipeline** (Tallgrass, pipeline id 325), which carries
Rockies supply (Opal, WY) west and **delivers into Malin**, reaching PG&E at the
`PACGAS/RUBY (OXH) ONYX HILL` delivery — the Ruby→PG&E flow (the same one Pipe
Ranger calls `onyx_ruby`). Source: Tallgrass's EBB at `pipeline.tallgrassenergylp.com`
— an **ASP.NET WebForms** OA grid (`__VIEWSTATE` async postback, like El Paso).

**The Incapsula WAF caveat (corrected 2026-06-23).** An earlier pass marked Ruby
*blocked* because a plain `requests` GET gets the Incapsula JS-challenge page (212
bytes), not data. That much is true — but once a **real browser has solved the
challenge**, the resulting Incapsula clearance cookies make the WebForms POST work
**fully from `requests`** (verified: real page + grid, 25 points). So Ruby *is*
reachable with the README §4 method, with one manual dependency:

- Set **`RUBY_COOKIE`** in `.env` to the full `Cookie:` header copied from a browser
  session that has loaded the Ruby OA page (devtools → Network → Cookie). The data
  pull stays 100% `requests`-based — no headless browser for ingestion; the cookie
  just carries WAF clearance.
- Cookies are **session-scoped and expire.** When challenged, the client raises
  `RubyChallengeError` telling you to refresh `RUBY_COOKIE`. (A future option, if the
  manual refresh is too frequent: a tiny one-off Playwright step *solely* to mint the
  clearance cookie — still no browser in the data path.)
- **One posting, both directions:** the client POSTs the Retrieve grid for receipt
  **and** delivery and combines them → DesignCapacity / OperatingCapacity /
  TotalScheduledQuantity / OperationallyAvailableCapacity per point, including
  `ONYX HILL` (PG&E), `MALIN HUB POOL`, Turquoise/Tuscarora/Opal Valley interconnects.
- **Cycle:** `ddlCycle` — best(0) | timely(1) | evening(2) | id1(3) | id2(4) | id3(6).
- **Units:** **Dth** (interstate) — already canonical Dth/d, no conversion. Emits the
  shared §5 `FlowRecord`. Lineage under `data/ruby/`.
- **Cross-check:** Pipe Ranger's `onyx_ruby` already carries the Ruby→PG&E *scheduled*
  flow; this module adds Ruby *system* OA capacity/utilization + all its interconnects.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.ruby --gas-day 2026-06-23 --cycle best
```

Tests are offline against `tests/fixtures/ruby_oa_delivery.html` / `ruby_oa_receipt.html`
(refreshing them needs a valid `RUBY_COOKIE`).

## 12. El Paso Natural Gas client — `el_paso.py` (DONE)

`src/ebb/el_paso.py` reads **El Paso Natural Gas** (EPNG, Kinder Morgan), which
feeds PG&E at Topock. Source: KM's `pipeline2.kindermorgan.com` portal (TSP code
`EPNG`) — a classic **ASP.NET WebForms** site (the `requests.Session` +
`__VIEWSTATE` POST case README §4 anticipated; no WAF, unlike Ruby).

- **OAC mechanism:** the OAC point page renders an empty grid on GET; data appears
  only after a "Retrieve" postback. The client GETs the page, harvests the form
  state (`__VIEWSTATE` / `__EVENTVALIDATION` / control values), POSTs with
  `__EVENTTARGET` = the Retrieve button, then parses the grid HTML.
- **One posting, two dataset types:** Operationally Available Capacity **and**
  Total Scheduled Quantity per location → each `FlowRecord` carries design/
  operating/available capacity **and** scheduled quantity.
- **Gas day + cycle selection:** the date picker and cycle combo are Infragistics
  editors driven by opaque pipe-format `_clientState` hidden fields (not JSON —
  reverse-engineered from real browser requests, see below). `--gas-day` /
  `--cycle` set them; omit for the default (current gas day / BEST AVAILABLE).
- **Notices:** `/Notices/Notices.aspx?code=EPNG` renders the full grid on a plain
  GET (no postback). Parsed (leaf rows, dedup by Notice ID) → §5 `Notice` shape;
  types map MAINTENANCE→maintenance, FORCE MAJEURE/CAPACITY CONSTRAINT→critical.
- **Units:** Meas Basis "Million BTU's (displayed as Dth)" → **Dth/d**, canonical.
- Emits the shared §5 `FlowRecord`/`Notice`. Lineage under `data/el_paso/`.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.el_paso                                  # current / best available
.\.venv\Scripts\python.exe -m src.ebb.el_paso --gas-day 2026-06-20 --cycle id2
```

`--cycle` accepts `timely | evening | id1 | id2 | id3`. Tests are offline against
`tests/fixtures/epng_operational_capacity.html` and `epng_notices.html`.

### Infragistics clientState formats (reverse-engineered from real POSTs)
Both editors post an opaque pipe-delimited `_clientState`; an empty value = default.
- **Date** (`dtePickerBegin_clientState`): `|0|01<Y>-<M>-<D>-0-0-0-0||[...,"01<Y>-<M>-<D>-0-0-0-0"]`
  (month/day unpadded).
- **Cycle** (`ddlCycleDD_clientState`): `|0|<NAME>&tilda;<INDEX>||<stateJSON>` where the
  selection delta `obj[1][0]` is `{"0":[41,INDEX],"1":[7,INDEX],"2":[23,"NAME"]}`.
  Indexes: TIMELY=1, EVENING=2, INTRADAY 1/2/3 = 3/4/5. The delta (not the prefix or
  first-array) is what actually drives the server selection.

### Scope note
- The OAC **Points** posting is EPNG's receipt/interconnect points (~75) — it does
  **not** include the **Topock** delivery to PG&E. That's expected for this posting;
  **Topock→PG&E is already covered by Pipe Ranger** (`baja_elpaso` scheduled +
  `El_Paso_Phys` capacity). This module adds El Paso *system* capacity/utilization.

## 13. NOVA / NGTL — `nova.py` (DONE)

`src/ebb/nova.py` reads **NOVA Gas Transmission Ltd. (NGTL)** — TC Energy's Alberta
intra-basin system, the upstream visibility into **AECO/NIT-basin supply**. NGTL's
**Alberta/BC border** hands gas to **Foothills BC → Kingsgate → GTN → Malin → PG&E**,
so this is where the AECO gas that reaches northern California originates.

- **Different TC Energy platform than GTN.** The US systems use the `tcplus.com`
  "ganesha" platform (§10); the Canadian systems publish through **TC Customer
  Express** (`tccustomerexpress.com`), whose `my.` SPA is backed by a **public AWS
  API Gateway** serving plain **CSV via GET** — no auth, no WebForms (the §4 ideal):
  | Dataset | Endpoint | Native units |
  |---|---|---|
  | Capability & Historical Flow at zones USJR / **AB/BC** / EGAT / OSDA (actual flow + base/outage capability + firm FT-D + heat value) | `chart/csv` | 10³m³/d, TJ/d, GJ/10³m³ |
  | Current System Report — system balance: receipts, intraprovincial demand, **every export border (incl. Alberta-BC)**, linepack, net storage flow | `csr/csv/?unit=&duration=N` | 10³m³/d or MMcf |
  | Daily Operating Plan outages (maintenance / capability restrictions) → notices | `csv/outages/` | — |
  | Upstream plant turnaround receipt/delivery impacts (supply outages) → notices | `plantturnaroundactivity/csv/` | 10³m³/d |
- **Units → Dth/d (canonical).** Chart flows/capabilities convert with the **per-zone
  assumed heat value** (`10³m³/d × GJ/10³m³ × 0.94781712`); firm design (FT-D) is
  `TJ/d × 1000 × 0.94781712`; CSR is fetched in **MMcf** and converted with the
  configurable `--heat-content` BTU/cf (consistent with `pipe_ranger`). Originals
  (value + unit) are always preserved.
- **Notices** come from the public outages + plant-turnaround CSVs (maintenance),
  bracketed to a −7/+45-day window around the gas day. The general **`bulletin`**
  notices and the **Gas Day Summary / `chart/summary`** JSON are **Cognito
  login-gated and skipped** — the operationally-relevant maintenance is already in
  the public outages feed.
- Gas day is the **Alberta (Mountain) gas day**; emits the shared §5
  `FlowRecord`/`Notice`. Lineage (4 raw CSVs + normalized JSON) under `data/nova/`.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.nova                       # latest available (MT)
.\.venv\Scripts\python.exe -m src.ebb.nova --gas-day 2026-06-21 --duration 2
```

Tests are offline against `tests/fixtures/nova_chart.csv`, `nova_csr.csv`,
`nova_outages.csv`, `nova_plant_turnarounds.csv`.

## 14. Foothills — `foothills.py` (DONE)

`src/ebb/foothills.py` reads **Foothills Pipe Lines** (TC Energy) — the export leg
downstream of NGTL that carries AECO gas to the international boundary:
**Foothills BC** (NGTL Alberta/BC border → **Kingsgate** → GTN → Malin → **PG&E**,
the leg that delivers AECO supply to northern California) and **Foothills SK**
(NGTL Empress/McNeill borders → Canadian Mainline).

- **No separate public EBB.** TC Energy reports Foothills throughput *as NGTL's
  border flows*, so this module **reuses `NovaClient`** (same TC Customer Express
  feeds, §13) and presents the **export-border subset** as the `foothills` source —
  a single source of truth for fetching + Dth/d conversion, no duplication.
- **Records:** Foothills BC firm (FT-D) + flow + capability from NGTL's **AB/BC**
  chart zone, plus the **Alberta-BC / Empress / McNeill** border flows from the
  Current System Report. All re-tagged `source="foothills"` with leg-specific names.
- **Notices:** NGTL outages are grouped per outage (the feed has one row per
  affected operational-area gate) and filtered to the **export gates** — confirmed
  from the outages' *Area for Stated Capability*: `WGAT` ("Alberta/BC and
  Alberta/Montana Borders") and `FHZ8` ("Alberta/BC Border") → **Foothills BC**;
  `EGAT` ("Empress/McNeill Borders") → **Foothills SK**. Each notice is tagged with
  the affected leg.
- Gas day = Alberta (Mountain); shared §5 `FlowRecord`/`Notice`. Lineage (raw CSVs +
  normalized JSON) under `data/foothills/`.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.foothills                  # latest available (MT)
.\.venv\Scripts\python.exe -m src.ebb.foothills --gas-day 2026-06-21
```

Tests are offline and reuse NOVA's fixtures (`tests/fixtures/nova_*.csv`).

## 15. Transwestern — `transwestern.py` (DONE)

`src/ebb/transwestern.py` reads **Transwestern Pipeline** (Energy Transfer), which
carries San Juan / Permian / West Texas supply west and feeds **PG&E at Topock**
(the AZ/CA border). Source: Energy Transfer's **iPost** platform at
`twtransfer.energytransfer.com` (asset code `TW`).

- **iPost = clean CSV-via-GET** (unlike El Paso's WebForms — no viewstate, no auth):
  | Dataset | Endpoint (all `&f=csv&extension=csv`) |
  |---|---|
  | OAC **and** scheduled (DC/OPC/TSQ/OAC per location) | `/ipost/capacity/operationally-available?asset=TW&gasDay=MM/DD/YYYY&cycle=N` |
  | Notices (3 categories) | `/ipost/notice/{critical\|non-critical\|planned-service-outage}?asset=TW` |
- **One posting, two dataset types:** the OAC CSV carries Design Capacity (DC),
  Operating Capacity (OPC), Total Scheduled Quantity (**TSQ**) and Operationally
  Available Capacity (OAC) for ~299 locations — including **`PG&E TOPOCK`** (loc
  56698), the Transwestern→PG&E delivery (plus SOCAL/MOJAVE Topock & Needles).
- **Cycle:** iPost posts two nomination cycles — `timely` (0) and `evening` (1).
  `--gas-day` is reformatted ISO → MM/DD/YYYY.
- **Notices:** fetched from all three category CSVs, deduped by Notice ID; type maps
  by the Notice Type string (Force Majeure / Capacity Constraint / Operational Alert
  → critical, Planned Service Outage → maintenance), else the category default.
  iPost's `Mon DD YYYY h:mmAM` timestamps are parsed via `python-dateutil`. Detail
  URL: `/ipost/notice/show/{id}?asset=TW`.
- **Units:** quantities are expressed in **DTH** (Measurement Basis MMBtu) — already
  canonical Dth/d, no conversion. Emits the shared §5 `FlowRecord`/`Notice`; lineage
  (OAC CSV + 3 notice CSVs + normalized JSON) under `data/transwestern/`.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.transwestern --gas-day 2026-06-22 --cycle timely
.\.venv\Scripts\python.exe -m src.ebb.transwestern                          # latest available
```

Tests are offline against `tests/fixtures/tw_operational_capacity.csv` and
`tw_notices_*.csv`.

## 16. Kern River — `kern_river.py` (DONE)

`src/ebb/kern_river.py` reads **Kern River Gas Transmission** (Berkshire Hathaway
Energy), which carries Rockies supply (Opal, WY) to California and feeds **PG&E at
Daggett and Fremont Peak** (and SoCalGas at Wheeler Ridge / Kramer Junction). Source:
BHE's **Services Portal** at `services.kernrivergas.com/portal`.

- **DNN WebForms shell, but GET-only data.** The OAC page has a `__VIEWSTATE` +
  `btnRetrieve`/`btnDownload` form gated by reCAPTCHA — but appending **`?gasDay=MM/DD/YYYY`**
  renders the full grid server-side, so no POST / viewstate / reCAPTCHA is needed:
  | Dataset | Endpoint (plain GET) |
  |---|---|
  | OAC **and** scheduled (Design/Operating/Operationally-Available + Total Scheduled Quantity, split Kern + Mojave) | `/Informational-Postings/Capacity/Operationally-Available?gasDay=MM/DD/YYYY` |
  | Notices (3 grids) | `/Informational-Postings/Notices/{Critical\|Non-Critical\|Planned-Service-Outage}` |
- **One grid, two dataset types:** ~120 locations with DC / OPC / OAC **and** scheduled
  quantity — including **`Daggett - PG&E`** (DC ~385,793 / OPC ~393,000) and
  **`Fremont Peak - PG&E`**. Scheduled quantity is reported split across the Kern and
  Mojave (co-operated) systems; the module **sums them** (preserving the total).
  `FlowInd` R/D → receipt/delivery; `BD` (bidirectional compressors) → no direction.
- **Cycle:** the OAC posting has no cycle selector — it shows the **latest posted
  cycle** for the gas day; the per-row `Cycle` (e.g. `id3`) is recorded on each record.
- **Notices:** each category grid is parsed (Notice Type / Posted / Eff / End / Id /
  Subject), deduped, windowed to currently-active (end ≥ gas_day − 3); type maps by
  category (Critical → critical, Planned-Service-Outage → maintenance) with Notice
  Type overrides (Force Majeure / Capacity → critical).
- **Units:** quantities are in **Dth** (MeasBasisDesc) — already canonical Dth/d, no
  conversion. Emits the shared §5 `FlowRecord`/`Notice`; lineage (OAC HTML + 3 notice
  HTMLs + normalized JSON) under `data/kern_river/`.

**Run it:**

```
.\.venv\Scripts\python.exe -m src.ebb.kern_river --gas-day 2026-06-22
.\.venv\Scripts\python.exe -m src.ebb.kern_river                            # latest available
```

Tests are offline against `tests/fixtures/kern_oac.html` and `kern_notices_*.html`.

---

**All EBBs are now implemented** (Pipe Ranger, EIA, GTN, El Paso, NOVA, Foothills,
Transwestern, Kern River; Ruby skipped — §11). Next: the orchestration layer
(analytics → brief renderer → Power Automate POST, §2 build order step 4).