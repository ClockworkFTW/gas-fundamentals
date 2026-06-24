# gas-fundamentals

A **Power BI dashboard** of western natural gas market fundamentals for PG&E's
core gas supply desk, fed by a Python ETL on Jenkins, a daily ICE price export, a
Copilot-summarized fundamentals feed, and (roadmap) weather. SharePoint is the
single store; Power BI is the single front end.

> **This README describes the target architecture after the 2026 Power BI pivot.**
> The repo is being refactored to match it (see §9). The detailed build log from
> the prior agent/Python-dashboard design lives in git history; the load-bearing
> source-client specifics are preserved in §4.

---

## 1. Architecture

```
Jenkins (Python, this repo)             Local (you, each AM)
  scrape pipe + storage EBBs              ICE price master (Excel)
  -> ETL to star-schema fact + dim CSVs   -> values-only CSV export
  -> pre-compute hard series                     |
     (5-yr storage bands, day-over-day)          |
        |  HTTP POST (JSON, <5 MB/call)           | upload
        v                                         v
  Power Automate  ── receives POSTs, writes files to SharePoint
        |           ── news: public RSS/HTTP (+ Outlook folder fallback)
        |                 -> Copilot agent (summarize) -> news list
        v
  SharePoint  ── document library: dated fact partitions + dim files + pricing CSV
              ── list: fundamentals_summary (news)
        |
        v
  Power BI (Pro)  ── SharePoint folder + list connectors -> star schema
                  ── DAX measures (utilization %, basis, ratios) + date dim
                  ── dashboard (Deneb hero schematic, per-pipeline drill, KPIs,
                     storage band, forward curve, fundamentals panel, notices)
                  ── 8x/day refresh + email subscription (replaces the digest)
```

Each component does one job: **Python = operational data + ETL + hard math**,
**Power Automate = file delivery + news/agent**, **Power BI = model + presentation
math + visuals + distribution**.

---

## 2. Data model & SharePoint storage (the core)

**Storage method:** a **SharePoint document library of dated partition files**,
read by Power BI's **"SharePoint folder" connector** (it appends all files in a
folder into one table on refresh). Append-only, scales for years, no Dataverse /
Azure, no data gateway (SharePoint Online is a cloud source). Model everything as
a **star schema** — narrow fact tables + small dimension tables.

### Fact tables (dated partition CSVs, one file per gas day per fact)
| Fact | Grain (one row per…) | Key measures | Source |
|---|---|---|---|
| `fact_operational` | pipeline · point_id · gas_day · cycle · dataset_type · flow_direction | scheduled_qty, design_capacity, operational_capacity, available_capacity, **dod_change** (pre-computed) | Python (EBBs) |
| `fact_storage` | region/facility · gas_day | working_gas / ending_inventory, net_flow, **net_flow_dod**, **five_yr_avg/min/max**, **pct_of_band**, **vs_5yr_pct** (all pre-computed) | Python (Pipe Ranger + EIA) |
| `fact_pricing` | point · price_type(cash\|forward) · contract · gas_day | value, as_of | ICE export, **reshaped in Power Query** |
| `fact_weather` *(roadmap)* | demand_zone · gas_day | hdd, cdd, vs_normal | §10 |

Folder layout in the library: `operational/operational_<gas_day>.csv`,
`storage/storage_<gas_day>.csv`, `pricing/ice_<gas_day>.csv`. One folder per fact,
folder connector points at each.

### Dimension tables (static reference; maintained by hand, loaded once)
| Dim | Purpose | Notes |
|---|---|---|
| `dim_pipeline` | pipeline name, owner, role, zones | small CSV |
| `dim_cycle` | cycle ordering (timely→evening→id1→id2→id3→final) | for sort order |
| `dim_location` | nodes for the schematic: point_id → (x,y), type, label, zone | join to `fact_operational.point_id`; per-pipeline rows |
| `dim_segment` | segments: from_node/to_node, segment join key, path_kind | drives the schematic edges |
| `dim_date` | date table | **built in Power BI** (Power Query / `CALENDAR`), not Python |

`dim_location` / `dim_segment` are the layout/topology tables (seeded for
Transwestern in `transwestern_nodes.csv` / `transwestern_segments.csv`; fill in
`point_id` from real EBB Loc IDs). They're the only thing standing between the
operational facts and the per-pipeline schematic.

### News (not a file fact)
`fundamentals_summary` is a **SharePoint list** (low volume; Power Automate writes
items): `date, category(market|regulatory), headline, summary, source_url,
ingested_at`. Power BI reads the list directly.

**Pro caveat:** Power BI Pro has **no incremental refresh**, so the combined folder
re-imports fully each refresh. Keep partitions modest and archive partitions older
than ~2 years into a subfolder the connector doesn't read.

### What "compute where" means (the hybrid decision)
- **Python pre-computes** the windowed/historical-join series that are awkward in
  Pro DAX: the EIA **5-year storage bands** (5 yr of weekly history, ISO-week
  convention) and **day-over-day** deltas (operational, storage net flow). These
  ship as columns in the fact tables.
- **Power BI computes** everything aggregational/ratio in **DAX**: utilization
  (`scheduled / operational_capacity`), basis (Citygate − HH), totals, % of
  capacity, and any cross-filtered rollups.

---

## 3. Python scope after the refactor

Python is now **operational-data ingestion + ETL-to-star-schema + the hard
pre-computed series.** Nothing else. No pricing (Power Query handles it), no news
(Power Automate handles it), no dashboard rendering (Power BI), no agent.

- **Keep:** all source clients (§4); the EIA 5-year-band and day-over-day metric
  logic (repurposed into the fact writers); the normalized-record core.
- **Add / change:** a **publish/ETL layer** that turns normalized records into the
  star-schema **fact partition CSVs** (with the pre-computed columns folded in) and
  maintains the **dimension CSVs**, then POSTs the files to Power Automate.
- **Remove:** the brief renderer (`brief.py` + tests); the agent-oriented
  fundamentals-snapshot *as a deliverable* (its metric computation is repurposed,
  the snapshot-JSON output is dropped); SharePoint hot-list references; `pyarrow` /
  Parquet (CSV partitions only).

---

## 4. Source clients (kept) — preserved specifics

Ingestion is unchanged; only the **output** moves to the fact-table shape. The
hard-won, non-obvious bits, so they survive the refactor:

| Source | Feeds | Platform / access | Status |
|---|---|---|---|
| **Pipe Ranger** (PG&E) | backbone, Citygate, inventory, storage | JSON-via-GET servlets | active, **centerpiece** |
| **EIA** | Pacific/Lower-48 storage + 5-yr bands | Open Data API v2 (key) | active |
| **GTN** | Malin (Canada) | JSON-via-GET (ganesha) | active |
| **El Paso** | Topock (SW) | ASP.NET WebForms POST | active |
| **Transwestern** | Topock (SW) | Energy Transfer iPost / WebForms | active |
| **Kern River** | Daggett (Rockies) | Services Portal | active |
| **NOVA / Foothills** | **AECO upstream → Kingsgate** | Canadian postings | **active — AECO is a critical CA basin** |
| **Ruby** | Malin (Rockies) | Tallgrass / **Incapsula WAF** | **inactive** — see below |

Critical gotchas:
- **Pipe Ranger** = plain JSON under `https://www.pge.com/bin/pipeline/`
  (`scheduledvolumes`, `dthphysicalpipeline`, `supplydemand`, `storageactivity`,
  `systemInventoryStatus`, `ofoefoarchive`); no auth, just a real `User-Agent` +
  `X-Requested-With`. It **aggregates independent CA storage** (Wild Goose, Lodi,
  Central Valley, Gill Ranch) into inj/withdrawal totals, and its `onyx_ruby`
  carries the **Ruby→PG&E** flow. The `scheduledvolumes` servlet only serves a
  **rolling ~2-day window**, so the operational partition for a gas day must be
  **built on that gas day** (a backfill shows zero scheduled supply); PlanData
  (storage/inventory/supply-demand) stays queryable by date. Prior-day final is
  available after **08:00 PT**.
- **GTN** = JSON from `/GTN/OperationalCapacity/Generate?GasDay=&CycleType=&ExportEnum=0`;
  notices grid `/GTN/Notice/Retrieve` needs `filter.EffDate`/`filter.EndDate`
  (MM/DD/YYYY) and verbose `sort_direction=Descending` ("desc" → HTTP 500).
- **El Paso / Transwestern** = ASP.NET WebForms (Infragistics `clientState`,
  `__VIEWSTATE` async postback) — replicate with `requests.Session`, no browser.
- **Ruby (inactive):** Incapsula WAF clearance rides on short-lived cookies that
  can't be refreshed unattended, so Ruby is **out of the active source list** and
  `onyx_ruby` covers the Ruby→PG&E flow. **Goal: re-include via a durable feed** —
  preferred order: an authorized NAESB **FF/EDM batch feed or IP allowlist** from
  Tallgrass via PG&E's gas-scheduling/EDI team; **PG&E internal pipeline data** if
  Ruby ops already land in a desk system; a one-off Playwright cookie-mint only as
  last resort. The client + write-up are retained for when one lands.

All ingestion is **`requests`-based** (GET or `requests.Session` for WebForms). No
headless browser. Units normalized to **Dth/d** on ingest (original preserved).

---

## 5. Pricing — ICE export → SharePoint → Power Query

`fact_pricing` originates in your **ICE price master Excel** (ICE XL, RTD). RTD only
updates in an open, entitled Excel session, so it can't be driven server-side. You
already refresh it each AM for the operating review, so: after refresh, export a
**values-only CSV** of the price block (Citygate, Malin, SoCal, Henry Hub — cash +
forward strip) and upload it to `pricing/` in the library. **Power BI's Power Query
unpivots** the wide export into the long `fact_pricing` shape on import — no Python.
Basis and price day-over-day are **DAX measures** (simple, no window needed).
Confirm ICE display/redistribution terms before publishing raw prices to the desk.

---

## 6. Fundamentals news + summary (Power Automate + Copilot agent)

Entirely outside Python. A Power Automate flow:
1. **Collect** market + regulatory items from **public RSS/HTTP** sources where
   available (e.g. regulatory feeds), with a **dedicated Outlook folder as
   fallback** for newsletters you route there.
2. **Summarize** by sending the item text to a **custom Copilot agent** (narrow,
   bounded — summarize text only; it never sees the numbers).
3. **Store** the result as an item in the `fundamentals_summary` SharePoint list
   (date, category, headline, summary, source_url).

Power BI reads the list into the dashboard's Fundamentals panel.

---

## 7. Power BI dashboard spec

Single report, refreshed 8×/day, with an **email subscription** delivering a daily
snapshot (this replaces the old text digest). Built on the §2 star schema.

- **Hero schematic (Deneb / Vega-Lite):** supply → Citygate → demand flow. Location
  nodes from `dim_location` (price, d/d, sparkline, flow vs norm); pipeline edges
  from `dim_segment` labeled **% of capacity**, colored green <75 / amber 75–90 /
  red >90. Top banner: net balance (long/short) + weather (HDD vs norm). Pre-join
  segment endpoint x/y in Power Query so each segment row carries from/to coords.
- **CGT demand = Core (residential + small commercial) + Industrial + Electric
  generation.** Storage is **not** demand — it's the balancing inj/withdrawal flow.
- **Per-pipeline drill** (Deneb, same `dim_location`/`dim_segment`): slicers
  (pipeline · gas day · cycle), schematic colored by utilization, cross-filtered
  **Operational Capacity table** (design / scheduled / available / util).
- **KPI tiles** — Citygate cash + Δ, total receipts + Δ, Pacific storage % + vs
  5-yr band, key basis + Δ.
- **Storage band chart** — current vs 5-yr min/max band + average (pre-computed).
- **Forward curve** — Citygate vs Henry Hub (basis = the gap, DAX).
- **Fundamentals panel** — `fundamentals_summary` list, source-linked.
- **Notices feed** — OFO/EFO + maintenance, severity-colored.

Deneb is Microsoft-certified (no external calls, exports to PDF), which helps it
clear a corporate custom-visual review; confirm your tenant allows custom visuals.

---

## 8. Conventions

- **Python 3.11** (Jenkins agent 3.11.9). Venv at `.venv`; run via
  `.\.venv\Scripts\python.exe`. No 3.12+ syntax.
- **Packages:** `requests, pandas, beautifulsoup4, lxml, python-dateutil,
  python-dotenv, tenacity` (drop `openpyxl` unless an EBB needs it; no `pyarrow`).
- **Secrets** (PA trigger URL, EIA key) in `.env` (gitignored) + Jenkins
  Credentials; never commit. Shared-secret header on the PA flow.
- **Gas day is Pacific**; store `pulled_at` in UTC; be explicit about TZ.
- **Idempotent** partition writes (re-running a gas day overwrites its file).
- **Inheritable:** no hardcoded local-only paths; keep it in Git so the desk isn't
  bus-factor-1 on you.
- **Run split:** ICE export = local; all ingestion/ETL = Jenkins.

---

## 9. Refactor plan (for Claude Code)

> **Status: complete (as built).** The layout below is in place; `metrics/` is a
> package, `dim_location`/`dim_segment` ship as header-only stubs (no Transwestern
> seed authored yet — drop seeds in `dim/seeds/` to fold them in), and the CLI is
> `python -m src`. See CLAUDE.md "Layout (post-refactor, as built)".

Target `src/` after the refactor:
```
src/
  ebb/            # source clients — KEPT (pipe_ranger, eia, gtn, el_paso,
                  #   transwestern, kern_river, nova, foothills; ruby inactive)
  etl/            # NEW: normalized records -> star-schema fact partitions
    facts.py      #   build fact_operational / fact_storage (+ pre-computed cols)
    dims.py       #   maintain dim_pipeline / dim_cycle / dim_location / dim_segment
    publish.py    #   POST partition + dim files to Power Automate
  metrics/        # KEPT/repurposed: 5-yr bands, day-over-day (now feed facts.py)
  __main__.py     # CLI: pull -> etl -> publish for a gas day
data/             # gitignored local partitions
dim/              # dim CSVs (incl. transwestern_nodes/segments seeds)
tests/            # offline tests for facts/dims (brief tests removed)
```
Delete: `analytics/brief.py` (+ its tests), the snapshot-as-deliverable output,
hot-list and Parquet/pyarrow references. Repurpose the analytics metric functions
into `etl/facts.py` and `metrics/`.

---

## 10. Roadmap

- **Weather ingestion + display:** NOAA/NWS API (`api.weather.gov`, free) →
  population-weighted **HDD/CDD** for PG&E demand zones → `fact_weather` partitions
  → banner + a demand-vs-weather view. (Python job on Jenkins, same publish path.)
- **Per-pipeline topology dims** for GTN, El Paso, Kern River, NOVA/Foothills
  (Transwestern seeded) to extend the schematic drill to all pipelines.
- **Ruby authorized feed** to re-activate Ruby (§4).