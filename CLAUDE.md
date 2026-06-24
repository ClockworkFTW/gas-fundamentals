# gas-fundamentals — Claude Code instructions

Full context — architecture, the star-schema data model, source-client specifics,
the dashboard spec, and the refactor plan — is in **README.md**. Read it first.

**What this project is now (2026 Power BI pivot):** a Power BI dashboard of western
gas fundamentals. This Python repo is **only** the operational-data engine:
ingestion → ETL into star-schema fact/dimension CSVs → POST to Power Automate →
SharePoint → Power BI. No pricing (Power Query), no news (Power Automate + a Copilot
agent), no dashboard rendering, no agent in Python.

## Python environment

- **Python 3.11** (Jenkins agent 3.11.9). Venv at `.venv`; run everything via
  `.\.venv\Scripts\python.exe ...`. No 3.12+ syntax. (Windows venv uses `Scripts\`.)
- Packages: `requests, pandas, beautifulsoup4, lxml, python-dateutil,
  python-dotenv, tenacity`. No `pyarrow`/Parquet; CSV partitions only.

## Conventions

- **Compute split (hybrid):** Python pre-computes the hard windowed series — EIA
  **5-year storage bands** and **day-over-day** deltas — and folds them into the
  fact tables. Power BI does aggregations/ratios (utilization, basis) in DAX.
- **Ingestion is `requests`-based** (GET, or `requests.Session` for ASP.NET
  WebForms). No headless browser. WAF'd Ruby is **inactive**; `onyx_ruby` (Pipe
  Ranger) covers the Ruby→PG&E flow — don't delete the Ruby client (README §4).
- **Output is a star schema:** narrow fact partitions (`fact_operational`,
  `fact_storage`) at the README §2 grain + dimension CSVs (`dim_location`,
  `dim_segment`, `dim_pipeline`, `dim_cycle`). `dim_date` is built in Power BI, not
  here. Normalize to these shapes; units to **Dth/d** (preserve original).
- **Gas day is Pacific**; store `pulled_at` in UTC. Pipe Ranger `scheduledvolumes`
  is a rolling ~2-day window — build a gas day's operational partition **on that
  gas day** (README §4).
- **Secrets** (PA trigger URL, EIA key) from `.env` + Jenkins Credentials; never
  commit. Shared-secret header on the PA flow.
- **Idempotent** partition writes; keep code **inheritable** (no hardcoded
  local-only paths). ICE export runs local; ingestion/ETL runs on Jenkins.

## Layout (post-refactor, as built)

The README §9 refactor is **done**. The repo is now the operational-data engine:

- `src/ebb/` — source clients (unchanged request logic; ruby kept but inactive).
- `src/metrics/` — pre-computed series Python owns: `operational.day_over_day`
  (→ `dod_change`) and `storage.storage` (PG&E band + net-flow). EIA 5-yr bands
  stay in `ebb/eia.py` (`five_year_bands`); the DAX-superseded ratios
  (utilization, border supply, path util) were dropped.
- `src/etl/` — `load` (lineage reader), `facts` (writes `fact_operational` +
  `fact_storage` partitions with pre-computed cols), `dims` (writes
  `dim_pipeline`/`dim_cycle` + stub `dim_location`/`dim_segment`), `publish`
  (POSTs partitions + dims to Power Automate, shared-secret header).
- `src/__main__.py` — `python -m src --gas-day <ISO> [--cycle] [--pull] [--publish]`
  runs `pull → etl → publish`.
- `dim/` — committed dimension CSVs (not gitignored). `dim/seeds/<pipeline>_nodes.csv`
  / `_segments.csv` are folded into `dim_location`/`dim_segment` when authored;
  none exist yet, so those two dims are header-only stubs.
- Deleted: the old `analytics/` (brief + snapshot-as-deliverable) and `dashboard/`
  packages, their tests + sample fixtures, and the `pyarrow/plotly/jinja2/openpyxl`
  deps. Power BI owns rendering; Power Query owns pricing; Power Automate owns news.

Offline tests live in `tests/` (`test_metrics`, `test_etl_*`, + the kept ebb
client tests). Run: `.\.venv\Scripts\python.exe -m pytest -q`.