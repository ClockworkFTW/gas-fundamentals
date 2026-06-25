# gas-fundamentals

A **Power BI dashboard** of western natural-gas market fundamentals for PG&E's core
gas-supply desk — fed by a Python ETL on Jenkins, a daily ICE price export, a
Copilot-summarized fundamentals feed, and (roadmap) weather. SharePoint is the single
store; Power BI is the single front end.

**This repository is the operational-data engine** — the Python half of that system. It
ingests the western pipelines' Electronic Bulletin Boards (EBBs) plus EIA storage, runs
the windowed math that Power BI Pro DAX can't, shapes everything into a **star schema of
CSV partitions**, and POSTs them to Power Automate (→ SharePoint → Power BI). It does
**not** handle pricing (Power Query owns that), news (Power Automate + a Copilot agent),
or any dashboard rendering (Power BI).

Status: the engine is **built and tested** — offline `pytest`, all green. Jump to
[Quickstart](#2-quickstart) to install and run it.

---

## 1. Architecture

```
Jenkins (Python, THIS repo)                  Local (you, each AM)
  pull pipe + storage EBBs                     ICE price master (Excel, RTD)
  -> ETL to star-schema fact + dim CSVs        -> values-only CSV export
  -> pre-compute hard series                          |
     (EIA 5-yr storage bands, day-over-day)           |
  -> maintenance/notices snapshot                      |
        |  HTTP POST (JSON, <5 MB/call)                | upload
        v                                              v
  Power Automate  ── receives POSTs, writes files to SharePoint
        |           ── news: public RSS/HTTP (+ Outlook folder fallback)
        |                 -> Copilot agent (summarize) -> news list
        v
  SharePoint  ── document library: dated fact partitions + snapshot facts
              ──   + dim files + pricing CSV
              ── list: fundamentals_summary (news)
        |
        v
  Power BI (Pro)  ── SharePoint folder + list connectors -> star schema
                  ── DAX measures (utilization %, basis, ratios) + date dim
                  ── dashboard (Deneb hero schematic, per-pipeline drill, KPIs,
                     storage band, forward curve, fundamentals panel,
                     notices feed + maintenance timeline)
                  ── 8x/day refresh + email subscription (replaces the digest)
```

Each component does one job: **Python = operational data + ETL + hard math**,
**Power Automate = file delivery + news/agent**, **Power BI = model + presentation
math + visuals + distribution**.

---

## 2. Quickstart

### Requirements

- **Python 3.11** (the Jenkins agent is 3.11.9). No 3.12+ syntax.
- Network access to the pipeline EBBs + `api.eia.gov` (and, to publish, the Power
  Automate trigger URL).
- Windows is the primary target (commands below use the Windows venv layout,
  `.venv\Scripts\`). On macOS/Linux use `.venv/bin/` instead.

### Install

From the repo root:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# secrets — copy the template and fill it in (.env is gitignored)
copy .env.example .env
```

Then edit `.env` (see [Secrets](#secrets-env) under §9). `truststore` is in the
requirements so `requests` trusts the **OS certificate store** — this is what lets
ingestion work behind a corporate TLS-inspection proxy (the company root CA is in the
Windows store via group policy, not in certifi). No extra configuration needed; it is a
no-op where not required.

### Run

Everything runs through one CLI, `python -m src`, for a single gas day:

```powershell
# Full daily job: pull EBBs -> build facts/dims -> maintenance snapshot -> publish
.\.venv\Scripts\python.exe -m src --gas-day 2026-06-24 --pull --maintenance --publish

# Latest available gas day, ETL only (re-shape existing on-disk lineage; offline)
.\.venv\Scripts\python.exe -m src

# Pull + ETL + maintenance, but do NOT publish
.\.venv\Scripts\python.exe -m src --gas-day 2026-06-24 --pull --maintenance

# Assemble the publish payloads without sending them
.\.venv\Scripts\python.exe -m src --gas-day 2026-06-24 --publish --dry-run

# Limit the pull to specific sources
.\.venv\Scripts\python.exe -m src --gas-day 2026-06-24 --pull --sources pipe_ranger eia
```

| Flag | Effect |
|---|---|
| `--gas-day <ISO>` | Pacific gas day (e.g. `2026-06-24`). Default: latest available (prior day before 08:00 PT, else today). |
| `--cycle <c>` | Pin a Pipe Ranger cycle (`timely`/`evening`/`id1`/`id2`/`id3`/`final`); other EBBs use their most-settled posting. |
| `--pull` | Refresh EBB + EIA lineage first (network). Each source is wrapped so one failure doesn't sink the rest. |
| `--maintenance` | Also build the maintenance + notices snapshot facts (network; forward-looking). |
| `--publish` | POST the fact partitions + dim files to Power Automate (needs `.env`). |
| `--dry-run` | With `--publish`, build/validate payloads but do not POST. |
| `--sources ...` | Limit the pull to a subset (e.g. `pipe_ranger eia`). |
| `--data-root` / `--dim-dir` | Override the partition/dim directories (default `data/` and `dim/`). |
| `--no-write` | Run the pipeline without writing partition/dim files. |
| `-v` / `--verbose` | Debug logging. |

> **Gas-day timing.** Pipe Ranger's `scheduledvolumes` servlet serves only a rolling
> ~2-day window, so the operational partition for a gas day must be built **on that gas
> day** (a backfill shows zero scheduled supply). Prior-day final is available after
> 08:00 PT. The maintenance/notices feeds are forward-looking snapshots, refreshed each
> run.

### Test

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

All tests are **offline** — they parse captured fixtures under `tests/fixtures/` and
golden outputs under `tests/golden/`; no network. See [§10 Testing](#10-testing).

### Individual stages (ad-hoc)

Each ETL stage and each source client is independently runnable, but unlike
`python -m src` (which bootstraps the path) they need **`src` on `PYTHONPATH`**:

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m etl.facts --gas-day 2026-06-24       # facts only
.\.venv\Scripts\python.exe -m etl.dims                             # rebuild dims from dim/seeds
.\.venv\Scripts\python.exe -m etl.maintenance --gas-day 2026-06-24
.\.venv\Scripts\python.exe -m etl.publish --gas-day 2026-06-24 --dry-run
.\.venv\Scripts\python.exe -m ebb.pipe_ranger --gas-day 2026-06-24 --cycle id2
```

(`python -m pytest` and `python -m src` do not need this — `conftest.py` and
`src/__main__.py` add `src` to the path themselves.)

---

## 3. Data model — the star schema (the core)

**Storage method:** a **SharePoint document library of partition files**, read by Power
BI's "SharePoint folder" connector (it appends all files in a folder into one table on
refresh). Append-only, scales for years, no Dataverse / Azure / data gateway. Model
everything as a **star schema** — narrow fact tables + small dimension tables.

### Fact tables

| Fact | Grain (one row per…) | Key measures | Source | Written as |
|---|---|---|---|---|
| `fact_operational` | pipeline · point_id · gas_day · cycle · dataset_type · flow_direction | scheduled_qty, design/operational/available capacity, **dod_change** (pre-computed) | Python (EBBs) | `operational/operational_<gas_day>.csv` (dated) |
| `fact_storage` | region/facility · gas_day | working_gas / ending_inventory, net_flow, **net_flow_dod**, **five_yr_avg/min/max**, **pct_of_band**, **vs_5yr_pct** | Python (Pipe Ranger + EIA) | `storage/storage_<gas_day>.csv` (dated) |
| `fact_maintenance` | source · maintenance item · affected location · date-span | capacity_remaining_dthd, reduction_dthd, base_capacity_dthd, **pct_of_capacity**, capacity_basis, join_kind | Python (EBB notices + foghorn + NGTL outages) | `maintenance/maintenance_current.csv` (**snapshot**) |
| `fact_notices` | source · notice | notice_type, severity, status, **is_current**, has_capacity_impact | Python (EBB notices + OFO/EFO) | `notices/notices_current.csv` (**snapshot**) |
| `fact_pricing` | point · price_type(cash\|forward) · contract · gas_day | value, as_of | ICE export, **reshaped in Power Query** | `pricing/ice_<gas_day>.csv` |
| `fact_weather` *(roadmap)* | demand_zone · gas_day | hdd, cdd, vs_normal | NOAA/NWS (§11) | — |

Folder layout in the library: one folder per fact, the folder connector points at each.
`fact_operational` / `fact_storage` are **dated partitions** (one file per gas day,
append-only). `fact_maintenance` / `fact_notices` are **current snapshots** — overwritten
each run (idempotent), since the maintenance and notice feeds are an as-of view, not a
per-gas-day event.

### Dimension tables (committed under `dim/`, loaded once)

| Dim | Purpose | Status |
|---|---|---|
| `dim_pipeline` | pipeline name, owner, role, zones, active flag | authored in `etl/dims.py` |
| `dim_cycle` | nomination-cycle ordering (timely→…→final) for sort | authored in `etl/dims.py` |
| `dim_location` | schematic nodes: `point_id → (x,y), type, label, zone` | **seeded** for all 7 pipelines (`dim/seeds/*_nodes.csv`) |
| `dim_segment` | schematic edges: `from_node/to_node, path_kind` | **seeded** for all 7 pipelines (`dim/seeds/*_segments.csv`) |
| `dim_date` | date table | **built in Power BI** (Power Query / `CALENDAR`), not here |

`dim_location.point_id` joins to `fact_operational.point_id` **verbatim** — the seed
ids are copied byte-for-byte from real EBB output (do not normalize/pad/re-case). See
[`dim/seeds/README.md`](dim/seeds/README.md) for the node/edge convention, the `type`
vocabulary, and the join rules a guard test enforces.

### News (not a file fact)

`fundamentals_summary` is a **SharePoint list** (low volume; Power Automate writes
items): `date, category(market|regulatory), headline, summary, source_url, ingested_at`.
Power BI reads the list directly.

> **Pro caveat:** Power BI Pro has **no incremental refresh**, so a folder re-imports
> fully each refresh. Keep partitions modest and archive partitions older than ~2 years
> into a subfolder the connector doesn't read. (The snapshot facts are single small files,
> so they sidestep this.)

### What "compute where" means (the hybrid split)

- **Python pre-computes** the windowed/historical-join series that are awkward in Pro
  DAX: the EIA **5-year storage bands** and **day-over-day** deltas (shipped as fact
  columns), and the maintenance **unit normalization + `pct_of_capacity`**.
- **Power BI computes** everything aggregational/ratio in **DAX**: utilization
  (`scheduled / operational_capacity`), basis (Citygate − HH), totals, % of capacity,
  cross-filtered rollups.

---

## 4. The maintenance + notices facts

Each western pipeline structures its planned-maintenance / critical-notice postings
differently; `fact_maintenance` and `fact_notices` normalize them into the two shapes
above so Power BI can render a **notices feed** (severity-colored) and a
**maintenance-constraint timeline** (colored by remaining capacity).

- **Two facts, current-snapshot grain.** `fact_notices` is one row per notice (OFO/EFO +
  maintenance + critical + advisory); `fact_maintenance` is one row per (maintenance item
  × affected location × date-span) and carries the structured capacity impact. They link
  by `notice_id` but neither requires the other (OFO/EFO + Kern advisories have no
  capacity line; PG&E foghorn + NGTL outages have no NAESB notice).
- **Capacity normalized to Dth/d** (original value + units always preserved), with
  `pct_of_capacity` as the **unit-free, cross-pipe-comparable** measure and
  `capacity_basis` flagging remaining-vs-reduction (sources disagree on which they post).
- **`join_kind`** records how — if at all — a row reaches `fact_operational.point_id`:
  GTN's in-prose `LOC #` and El Paso's numeric scheduling-location ids join directly;
  NGTL/Foothills join by gate code; the rest are text labels.
- **Per-pipe parsing** lives in `src/etl/maintenance_sources/<pipe>.py` (the frozen `ebb`
  clients are reused only as fetchers; the maintenance modules add just the small extra
  fetches a maintenance feed needs).

Where each pipe's maintenance detail comes from is mapped in
[`exploration/maintenance/INVENTORY.md`](exploration/maintenance/INVENTORY.md); the
schema rationale is in [`exploration/FACT_NOTICES_DESIGN.md`](exploration/FACT_NOTICES_DESIGN.md).

---

## 5. Source clients (kept) — preserved specifics

Ingestion is `requests`-based (GET, or `requests.Session` for ASP.NET WebForms) — **no
headless browser**. Units are normalized to **Dth/d** on ingest; the original unit/value
is always preserved.

| Source | Feeds | Platform / access | Status |
|---|---|---|---|
| **Pipe Ranger** (PG&E) | backbone, Citygate, inventory, storage, **maintenance** | JSON-via-GET servlets (+ `foghorn` POST) | active, **centerpiece** |
| **EIA** | Pacific/Lower-48 storage + 5-yr bands | Open Data API v2 (key) | active |
| **GTN** | Malin (Canada) | JSON-via-GET (ganesha) | active |
| **El Paso** | Topock (SW) | ASP.NET WebForms POST | active |
| **Transwestern** | Topock (SW) | Energy Transfer iPost CSV | active |
| **Kern River** | Daggett (Rockies) | BHE Services Portal (GET) | active |
| **NOVA / Foothills** | **AECO upstream → Kingsgate** | TC Customer Express CSV | active |
| **Ruby** | Malin (Rockies) | Tallgrass / Incapsula WAF | **inactive** — see below |

Critical, hard-won gotchas:

- **Pipe Ranger** = plain JSON under `https://www.pge.com/bin/pipeline/`
  (`scheduledvolumes`, `dthphysicalpipeline`, `supplydemand`, `storageactivity`,
  `systemInventoryStatus`, `ofoefoarchive`, …); no auth, just a real `User-Agent` +
  `X-Requested-With`. It aggregates independent CA storage (Wild Goose, Lodi, Central
  Valley, Gill Ranch) into inj/withdrawal totals, and `onyx_ruby` carries the Ruby→PG&E
  flow. **Planned maintenance** for the Redwood + Baja paths comes from a separate
  **`POST /bin/pipeline/foghorn`** (`dropDownVal` = `pipelineCap`/`maxCap`/`firmCuts`,
  `maintananceVal` = `redwood`/`baja`, `pagePath` = the maintenance page) — discovered in
  the page's clientlib JS; GET returns 405. `scheduledvolumes` is a rolling ~2-day window
  (build the partition on the gas day); PlanData (storage/inventory/supply-demand) stays
  queryable by date.
- **GTN** = JSON from `/GTN/OperationalCapacity/Generate`; the notices grid
  `/GTN/Notice/Retrieve` needs `filter.EffDate`/`filter.EndDate` (MM/DD/YYYY) and verbose
  `sort_direction=Descending` ("desc" → HTTP 500). Maintenance capacity + the affected
  `LOC #` (which equals the OAC LocationID = `point_id`) are in the notice prose; the
  attached schedule PDF is the richest forward view.
- **El Paso / Transwestern** = ASP.NET WebForms (Infragistics `clientState`,
  `__VIEWSTATE` async postback) for the OAC grid — replicate with `requests.Session`, no
  browser. Notice **detail pages** render on a plain GET; El Paso's monthly maintenance
  notice embeds a structured per-location/per-day reduction calendar in Dth/d.
- **NOVA / Foothills** = public CSV-via-GET on the TC Customer Express AWS gateway
  (`chart`, `csr`, `outages`, `plantturnaroundactivity`); the outages CSV is the
  best-structured maintenance feed (Capability + local base/outage). Foothills has no own
  feed — it is the export-gate subset of NGTL's outages.
- **Ruby (inactive):** Incapsula WAF clearance rides on short-lived cookies that can't be
  refreshed unattended, so Ruby is out of the active source list and `onyx_ruby` covers
  the Ruby→PG&E flow. The client (`src/ebb/ruby.py`) is retained for manual use; supply a
  `RUBY_COOKIE` to run it. **Goal: re-include via a durable feed** (authorized NAESB
  FF/EDM batch or IP allowlist from Tallgrass; PG&E-internal pipeline data; a one-off
  Playwright cookie-mint only as a last resort).

---

## 6. The pipeline in detail — `pull → etl → publish`

`python -m src` runs one gas day through up to four stages:

1. **pull** *(opt-in, `--pull`, network)* — each EBB client and the EIA client refresh
   their normalized lineage to `data/<source>/<gas_day>[_<cycle>].normalized.json`. Best
   effort: a failing source is logged and skipped.
2. **etl** *(always)* — `etl/load` reads the on-disk lineage; `etl/facts` builds
   `fact_operational` + `fact_storage` (folding in the pre-computed `metrics/` series);
   `etl/dims` rebuilds the four dimension CSVs (re-reading `dim/seeds/`).
3. **maintenance** *(opt-in, `--maintenance`, network)* — `etl/maintenance` fetches +
   parses every pipe's maintenance/notice feed and writes the two snapshot facts. It
   reuses the just-built `fact_operational` design capacities to backfill `base_capacity`
   / `pct_of_capacity` on point-joined impacts (e.g. GTN).
4. **publish** *(opt-in, `--publish`)* — `etl/publish` POSTs each fact partition + dim
   file to the Power Automate trigger as JSON, with a shared-secret header, under the
   ~5 MB/call cap. Missing files are skipped (non-fatal). `--dry-run` validates payloads
   without sending.

---

## 7. Pricing, news, and the dashboard (outside Python)

- **Pricing (§ Power Query).** `fact_pricing` originates in your **ICE price master
  Excel** (RTD). RTD only updates in an open, entitled Excel session, so it can't be
  driven server-side — after your AM refresh, export a **values-only CSV** of the price
  block (Citygate, Malin, SoCal, Henry Hub — cash + forward strip) and upload it to
  `pricing/`. **Power Query unpivots** it into the long `fact_pricing` shape on import.
  Basis and price day-over-day are DAX. Confirm ICE display/redistribution terms before
  publishing raw prices.
- **News (§ Power Automate + Copilot).** A flow collects market + regulatory items from
  public RSS/HTTP (with a dedicated Outlook folder as fallback), summarizes each via a
  narrow custom Copilot agent (text only — it never sees the numbers), and writes the
  result to the `fundamentals_summary` SharePoint list.
- **Dashboard (§ Power BI Pro).** Single report, refreshed 8×/day, with an email
  subscription (replaces the old text digest). Hero **Deneb / Vega-Lite** schematic
  (supply → Citygate → demand) driven by `dim_location` / `dim_segment`; per-pipeline
  drill; KPI tiles (Citygate cash + Δ, total receipts + Δ, Pacific storage % vs 5-yr
  band, key basis + Δ); storage band chart; forward curve (Citygate vs HH); fundamentals
  panel; and a **notices feed + maintenance timeline** off `fact_notices` /
  `fact_maintenance`. Deneb is Microsoft-certified (no external calls) — confirm your
  tenant allows custom visuals.

---

## 8. Repository layout (as built)

```
src/
  ebb/                  # source clients — pipe_ranger, eia, gtn, el_paso, transwestern,
                        #   kern_river, nova, foothills (+ ruby, inactive). schema.py holds
                        #   the record shapes (FlowRecord, Notice, NoticeEvent,
                        #   MaintenanceImpact); base.py the shared session/retry/raw-write.
  metrics/              # pre-computed series Python owns: operational.day_over_day
                        #   (-> dod_change) and storage.storage (PG&E band + net-flow).
                        #   EIA 5-yr bands live in ebb/eia.py (five_year_bands).
  etl/
    load.py             # read normalized EBB lineage off disk (the only fs input)
    facts.py            # build fact_operational / fact_storage (+ pre-computed cols)
    maintenance.py      # build fact_maintenance / fact_notices snapshots (+ helpers)
    maintenance_sources/ #   one parser module per pipe (clients used as fetchers only)
    dims.py             # build dim_pipeline / dim_cycle / dim_location / dim_segment
    publish.py          # POST partitions + dims to Power Automate (shared-secret)
  __main__.py           # CLI: pull -> etl (+maintenance) -> publish for a gas day
data/                   # gitignored local lineage + partitions (regenerated each run)
dim/                    # committed dim CSVs + dim/seeds/<pipeline>_nodes|segments.csv
tests/                  # offline tests (fixtures/ captured responses, golden/ outputs)
exploration/            # design + recon record (FACT_NOTICES_DESIGN.md, maintenance/INVENTORY.md)
requirements.txt        # runtime + dev (pytest) deps
.env.example            # secrets template (copy to .env)
```

---

## 9. Conventions

- **Python 3.11** (Jenkins agent 3.11.9). Venv at `.venv`; run via
  `.\.venv\Scripts\python.exe`. No 3.12+ syntax.
- **Packages:** `requests, pandas, beautifulsoup4, lxml, python-dateutil, python-dotenv,
  tenacity, truststore` (+ `pytest` for tests). No `pyarrow`/Parquet — CSV partitions only.
- **Canonical unit is Dth/d** (EIA storage stays Bcf); convert on ingest, always preserve
  the original unit/value.
- **Gas day is Pacific**; `pulled_at` is stored in **UTC**; be explicit about TZ.
- **Idempotent** writes — re-running a gas day overwrites its partition; the snapshot
  facts overwrite their single current file.
- **Inheritable**: no hardcoded local-only paths; keep it in Git so the desk isn't
  bus-factor-1. ICE export runs local; ingestion/ETL runs on Jenkins.

### Secrets (`.env`)

Copied from `.env.example`, gitignored, also provided via Jenkins Credentials — never
committed:

| Variable | Purpose |
|---|---|
| `POWER_AUTOMATE_URL` | the "When an HTTP request is received" trigger URL (publish target) |
| `POWER_AUTOMATE_SHARED_SECRET` | shared-secret header value the flow verifies |
| `POWER_AUTOMATE_SECRET_HEADER` | *(optional)* header name carrying the secret (default `X-Shared-Secret`) |
| `EIA_API_KEY` | EIA Open Data API v2 key (storage) |
| `RUBY_COOKIE` | *(optional)* Incapsula clearance cookie for the inactive Ruby client |

---

## 10. Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Tests are **fully offline**. Each source client is tested against a captured fixture in
`tests/fixtures/` (refresh with `tests/refresh_fixtures.py` when a feed changes), the
golden normalized outputs live in `tests/golden/` (`test_golden_ebb.py`), and the ETL +
metrics + maintenance layers are tested on the record dicts the loaders return
(`test_etl_*`, `test_metrics`, `test_maint_*`). No network, no secrets required.

---

## 11. Roadmap

- **Weather ingestion + display** — NOAA/NWS API (`api.weather.gov`, free) →
  population-weighted **HDD/CDD** for PG&E demand zones → `fact_weather` partitions →
  banner + a demand-vs-weather view (a Python job on Jenkins, same publish path).
- **Maintenance enrichments** — parse GTN's attached Planned Maintenance Schedule PDF;
  link foghorn's Topock rows to `dim_location`; optionally surface NGTL/foghorn maintenance
  in the notices feed (today they are timeline-only). See
  [`exploration/FACT_NOTICES_DESIGN.md`](exploration/FACT_NOTICES_DESIGN.md).
- **Per-facility storage** — `fact_storage` currently emits one aggregated PG&E System row;
  per-facility partitions would light up the storage nodes in the schematic.
- **Ruby authorized feed** — re-activate Ruby via a durable, unattended feed (§5).
