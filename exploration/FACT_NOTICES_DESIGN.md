# fact_notices + fact_maintenance — schema draft

Draft (not built). Designed from the 7-pipe maintenance reconnaissance
(`exploration/maintenance/INVENTORY.md`). Fits the README §2 star schema: narrow
fact partitions + small dims, canonical **Dth/d** with originals preserved,
`pulled_at` in UTC, idempotent writes, Python pre-computes the hard bits.

## TL;DR — two facts, not one

The data has **two natural grains**, matching the two dashboard visuals:

1. **`fact_notices`** — one row per **notice** (event/header grain). Powers the
   README §7 **"Notices feed"** (OFO/EFO + maintenance + critical, severity-colored).
2. **`fact_maintenance`** — one row per **(maintenance item × affected location ×
   date-span)** (impact-line grain). Powers the **maintenance-constraint timeline**
   and colors the schematic by remaining capacity.

They link by `notice_id` but **neither requires the other**:
- OFO/EFO and Kern line-pack advisories → `fact_notices` only (no capacity line).
- PG&E `foghorn` rows and NGTL outage rows have **no NAESB notice** → `fact_maintenance`
  only (`notice_id` null).
- An EPNG monthly maintenance notice → 1 `fact_notices` header + N `fact_maintenance` lines.

(Alternative considered: one wide `fact_notices` with optional capacity columns.
Rejected — it denormalizes 293 EPNG impact lines onto notice headers, and forces
synthetic notice rows for foghorn/NGTL which have no notice. Two facts is the
cleaner star.)

---

## fact_maintenance (the timeline — the important one)

**Grain:** one row per `(source, maintenance_id, affected location, date-span)`.
Forward-looking; store the **date range**, do not explode per gas day (DAX filters
active-on-day). Units normalized to Dth/d; original always kept; **`pct_of_capacity`
is the cross-pipe-comparable measure** (unit-free).

| column | type | notes |
|---|---|---|
| `source` | str | pipeline key → `dim_pipeline` |
| `maintenance_id` | str | stable key, e.g. `{source}:{notice_or_outage_id}:{loc}:{date_start}` |
| `notice_id` | str? | FK → `fact_notices`; **null** for foghorn / NGTL outages |
| `point_id` | str? | join → `dim_location` / `fact_operational.point_id` (GTN LOC#, EPNG loc id, PR Topock) |
| `segment_or_gate` | str? | NGTL gate (USJR/WGAT/FHZ8/EGAT), EPNG segment (NORTH ML), GTN segment |
| `affected_label` | str | human label: "Station 9 CFTP", "Alberta/BC Border", "Topock", "Burney Station" |
| `join_kind` | str | **honesty column**: `point_id` \| `gate_code` \| `segment_name` \| `path` \| `text_label` |
| `date_start` | str | ISO (Pacific gas day) |
| `date_end` | str? | ISO; null = open / "until further notice" |
| `capacity_basis` | str | what the source natively reports: `remaining` \| `reduction` \| `pct_cut` |
| `capacity_remaining_dthd` | float? | canonical; remaining capability under the outage |
| `reduction_dthd` | float? | canonical; pre-computed where derivable (base − remaining, or native) |
| `base_capacity_dthd` | float? | unconstrained/design capacity (native, or back-filled from `fact_operational.design_capacity` via point_id) |
| `pct_of_capacity` | float? | `remaining / base * 100` — **the cross-pipe comparable**; DAX colors green/amber/red off this |
| `pct_firm_cut` | float? | PR foghorn "% of firm rights cut" (distinct from physical %) |
| `reduction_planned_dthd` | float? | EPNG **PLM** (planned) split |
| `reduction_fm_dthd` | float? | EPNG **FMJ** (force-majeure) split |
| `original_value` | float? | pre-conversion value |
| `original_units` | str | `MMcf/d` \| `Dth/d` \| `10^3m^3/d` \| `MMBtu/d` |
| `restriction_type` | str? | "Potential impact to FT-R/FT-D", "force majeure", "planned" |
| `work_description` | str | "Rosalia Unit Outage", "Line 1201 pipeline remediation Navajo to Dilkon" |
| `is_unplanned` | bool | GTN PDF `*`, EPNG `FMJ>0` |
| `pulled_at_utc` | str | lineage |

**Pre-computed by Python:** unit→Dth/d conversion; fill the missing one of
{remaining, reduction, base} when two are known; `pct_of_capacity`; `is_unplanned`;
date-range parse (incl. foghorn's year-less dates).
**Left to DAX:** active-on-gas-day filter, green/amber/red thresholds, rollups.

---

## fact_notices (the feed)

**Grain:** one row per notice, as-of the pull. Supersession resolved to `is_current`.

| column | type | notes |
|---|---|---|
| `source` | str | → `dim_pipeline` |
| `notice_id` | str | source id |
| `notice_type` | str | normalized: `maintenance` \| `planned_outage` \| `critical` \| `capacity_constraint` \| `ofo` \| `efo` \| `advisory` \| `other` |
| `notice_type_raw` | str | "Maint", "Plnd Outage", "Pipe Cond", "Capacity Constraint", "EFO" |
| `severity` | str | pre-computed: `info` \| `low` \| `medium` \| `high` \| `critical` (drives feed color) → optional `dim_notice_type` |
| `category` | str | `maintenance` \| `capacity` \| `operational` \| `administrative` |
| `posted_at_utc` | str? | ISO; note source tz |
| `effective_start` | str? | ISO |
| `effective_end` | str? | ISO; null = open |
| `status` | str? | `initiate` \| `supersede` \| `terminate` \| `active` |
| `prior_notice_id` | str? | supersession chain |
| `is_current` | bool | **pre-computed**: latest non-superseded in its chain |
| `has_capacity_impact` | bool | true → joinable rows exist in `fact_maintenance` |
| `primary_point_id` | str? | affected point if joinable |
| `affects_pge` | bool | touches a PG&E-relevant point/path |
| `headline` | str | subject |
| `body` | str | cleaned text (truncate ~2k; full text stays in raw lineage) |
| `url` | str | detail link |
| `gas_day` | str | OFO/EFO = order gas day; ranged notices = `effective_start` |
| `pulled_at_utc` | str | lineage |

Optional **`dim_notice_type`** (small managed map): `(source, raw_type) →
normalized_type, severity, category`. Lets the desk recolor without reprocessing.

---

## How each source maps

| source | → fact_notices | → fact_maintenance | join_kind | capacity_basis | original_units |
|---|---|---|---|---|---|
| pipe_ranger **OFO/EFO** | ✅ events | — | — | none | — |
| pipe_ranger **foghorn** | — (no notice) | ✅ 25, per path/point/date | path / point_id (Topock) | remaining + pct_of_max + pct_firm_cut | MMcf/d |
| **el_paso** | ✅ 5 monthly | ✅ **293** lines | point_id (31) / segment_name | **reduction** (+base+net ⇒ all three) | Dth/d |
| **gtn** | ✅ maint notices | ✅ prose + **16 PDF** | **point_id (LOC# = OAC id)** | remaining | MMcf/d |
| **transwestern** | ✅ PSO | ✅ 1 (prose from→to) | text_label (upstream Stn 9) | remaining | MMBtu/d |
| **kern_river** | ✅ advisories | — (no numeric) | text_label | none | — |
| **nova** | ◐ optional synthetic | ✅ 124 + 25 plant | gate_code + area | remaining (+local base/outage ⇒ reduction) | 10^3m^3/d |
| **foothills** | — | ✅ 55 (27 → PG&E) | gate_code | remaining | 10^3m^3/d |

---

## Cross-cutting decisions

1. **Units.** Canonical **Dth/d** + `original_value`/`original_units` (project rule).
   MMcf/d (PR, GTN) and 10³m³/d (NGTL) convert via the existing heat-content helpers;
   NGTL outage rows lack a per-row heat value, so either use the default assumption
   **or** rely on `pct_of_capacity` (unit-free) for cross-pipe comparison. **% is the
   common denominator** — recommend the schematic colors on `pct_of_capacity`, not Dth.
2. **Remaining vs reduction.** PR/NGTL/GTN report *remaining*; EPNG reports the
   *reduction* (+ base + net). Carry **both** `capacity_remaining_dthd` and
   `reduction_dthd`; Python fills the missing one when base is known. For GTN (remaining
   only, no base in feed) the base back-fills from `fact_operational.design_capacity`
   via the confirmed `point_id` → `pct_of_capacity` computable downstream.
3. **Write strategy.** These are **as-of snapshots**, not per-gas-day facts. Overwrite
   a single current file each run — `notices/notices_current.csv`,
   `maintenance/maintenance_current.csv` (idempotent; sidesteps Pro's full-reimport
   caveat). Optional `…/archive/<pull_date>.csv` if point-in-time history is wanted.
4. **Supersession.** Resolve chains in Python → `is_current`; the feed/timeline show
   current-only by default, history available via `status`/`prior_notice_id`.
5. **Seeds dim_location.** The recon yields real `point_id`s (GTN 18480/954690/3500/18446;
   EPNG 9 loc ids; PR Topock → baja_elpaso/baja_transw) — these directly seed the
   `dim_location`/`dim_segment` stubs the schematic needs (README §2).

---

## Dataclass sketch (mirrors src/ebb/schema.py)

```python
@dataclass
class MaintenanceImpact:
    source: str
    maintenance_id: str
    notice_id: Optional[str]
    point_id: Optional[str]
    segment_or_gate: Optional[str]
    affected_label: str
    join_kind: str                       # point_id|gate_code|segment_name|path|text_label
    date_start: str
    date_end: Optional[str]
    capacity_basis: str                  # remaining|reduction|pct_cut
    capacity_remaining_dthd: Optional[float]
    reduction_dthd: Optional[float]
    base_capacity_dthd: Optional[float]
    pct_of_capacity: Optional[float]
    pct_firm_cut: Optional[float]
    reduction_planned_dthd: Optional[float]
    reduction_fm_dthd: Optional[float]
    original_value: Optional[float]
    original_units: str
    restriction_type: Optional[str]
    work_description: str
    is_unplanned: bool
    pulled_at_utc: str

@dataclass
class NoticeEvent:
    source: str
    notice_id: str
    notice_type: str
    notice_type_raw: str
    severity: str
    category: str
    posted_at_utc: Optional[str]
    effective_start: Optional[str]
    effective_end: Optional[str]
    status: Optional[str]
    prior_notice_id: Optional[str]
    is_current: bool
    has_capacity_impact: bool
    primary_point_id: Optional[str]
    affects_pge: bool
    headline: str
    body: str
    url: str
    gas_day: str
    pulled_at_utc: str
```

## Decisions (locked 2026-06-24)
- ✅ **Two facts** — `fact_notices` (event) + `fact_maintenance` (impact line).
- ✅ **Current-snapshot write** — overwrite `notices/notices_current.csv` and
  `maintenance/maintenance_current.csv` each run; optional `…/archive/<pull_date>.csv`
  only if point-in-time history is later wanted.
- ✅ **Truncated `body`** (~2k) in `fact_notices`; full text stays in raw lineage.
- ✅ Keep both names; the timeline is `fact_maintenance`.

## Status: BUILT (2026-06-24)
Implemented and verified end-to-end (full suite green; live run = 7/7 sources,
~419 maintenance + ~27 notice rows):
- Shapes `MaintenanceImpact` / `NoticeEvent` in `src/ebb/schema.py`.
- `src/etl/maintenance.py` — shared pre-compute (unit→Dth/d, `pct_of_capacity`,
  severity/category, supersession→`is_current`, base-capacity backfill from
  `fact_operational`, year-less date parse) + orchestrator + snapshot writers.
- `src/etl/maintenance_sources/<pipe>.py` — one module per pipe (the recon
  extractors promoted to the contract; the frozen `ebb` clients are fetchers only,
  request logic unchanged — additive fetches: foghorn POST, EPNG/TW detail GETs).
- `publish.py` POSTs the two current files; `python -m src --maintenance` wired.
- Offline tests: `tests/test_etl_maintenance.py` + `tests/test_maint_<pipe>.py`.
Deferred: GTN PDF schedule parse (needs `pdftotext`); Topock `point_id` linkage in
foghorn rows; synthesizing NoticeEvents for NGTL/foghorn if the feed should list them.
