"""Build the star-schema fact partitions (README §2 / §9).

Reads a gas day's normalized EBB lineage (and the prior day, for day-over-day),
runs the pre-compute metric functions, and writes two dated CSV partitions:

    data/operational/operational_<gas_day>.csv   (fact_operational)
    data/storage/storage_<gas_day>.csv           (fact_storage)

The hard windowed series Power BI Pro DAX can't do well are folded in here as
columns: ``dod_change`` on the operational facts, and the storage band /
net-flow series (``net_flow``, ``net_flow_dod``, ``pct_of_band``,
``five_yr_avg/min/max``, ``vs_5yr_pct``) on the storage facts. Power BI computes
the aggregational/ratio measures (utilization, basis) in DAX.

Idempotent: re-running a gas day overwrites its partition files. Gas day is
Pacific; ``pulled_at_utc`` is carried through in UTC. Canonical unit is Dth/d
(EIA storage stays Bcf); the original unit/value is preserved on each record.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
from typing import Any, Optional

import pandas as pd

from ebb.eia import five_year_bands
from metrics import dod_index
from metrics import storage as storage_metric

from . import load

log = logging.getLogger("etl.facts")

# Flow/capacity dataset types that belong on fact_operational. Storage and
# inventory route to fact_storage; supply_demand carries system supply/demand/core
# + mean temperature (Power BI builds CGT demand from these in DAX).
OPERATIONAL_DATASETS = frozenset({"scheduled_quantity", "operationally_available", "supply_demand"})

# Explicit column order so every partition (even an empty one) has a stable header
# for Power BI's SharePoint-folder connector to append.
FACT_OPERATIONAL_COLUMNS = [
    "pipeline",
    "point_id",
    "point_name",
    "gas_day",
    "cycle",
    "dataset_type",
    "flow_direction",
    "scheduled_qty",
    "design_capacity",
    "operational_capacity",
    "available_capacity",
    "dod_change",
    "units",
    "original_units",
    "original_qty",
    "pulled_at_utc",
]

FACT_STORAGE_COLUMNS = [
    "region",
    "source",
    "gas_day",
    "as_of_period",
    "working_gas",
    "net_flow",
    "net_flow_dod",
    "wow_change",
    "pct_of_band",
    "min_band",
    "max_band",
    "five_yr_avg",
    "five_yr_min",
    "five_yr_max",
    "vs_5yr_pct",
    "n_years",
    "units",
    "pulled_at_utc",
]


# --------------------------------------------------------------------------- #
# Lineage loading (mirrors the analytics snapshot's cycle handling)
# --------------------------------------------------------------------------- #


def _load_flows(
    gas_day: str,
    data_root: pathlib.Path | str,
    cycle: Optional[str],
) -> tuple[dict[str, load.SourcePull], list[str]]:
    """Load every flow source for a gas day; ``cycle`` pins only Pipe Ranger.

    Pipe Ranger is the primary nomination source, so a pinned cycle applies to it;
    the other EBBs post on their own cadence (and often write cycle-less lineage),
    so they resolve through the most-settled-available rank fallback.
    """
    pulls, missing = load.load_flows(gas_day, data_root=data_root)
    if cycle is not None:
        pinned = load.load_source("pipe_ranger", gas_day, data_root=data_root, cycle=cycle)
        if pinned is not None:
            pulls["pipe_ranger"] = pinned
            if "pipe_ranger" in missing:
                missing.remove("pipe_ranger")
        else:
            log.warning("pipe_ranger has no cycle=%s for %s; using best-available", cycle, gas_day)
    return pulls, missing


# --------------------------------------------------------------------------- #
# fact_operational
# --------------------------------------------------------------------------- #


def _operational_rows(
    gas_day: str,
    today_pulls: dict[str, load.SourcePull],
    prior_pulls: dict[str, load.SourcePull],
) -> list[dict[str, Any]]:
    """One fact_operational row per flow/capacity point, with pre-computed dod."""
    all_today = [r for p in today_pulls.values() for r in p.records]
    all_prior = [r for p in prior_pulls.values() for r in p.records]

    # Pre-computed day-over-day on scheduled_qty, matched on
    # (source, point_name, flow_direction, dataset_type) — folded in as dod_change.
    dod = dod_index(all_today, all_prior, field="scheduled_qty", dataset_types=None)

    rows: list[dict[str, Any]] = []
    for _source, pull in sorted(today_pulls.items()):
        for r in pull.records:
            dtype = r.get("dataset_type")
            if dtype not in OPERATIONAL_DATASETS:
                continue
            key = (r.get("source"), r.get("point_name"), r.get("flow_direction"), dtype)
            rows.append(
                {
                    "pipeline": r.get("source"),
                    "point_id": r.get("point_id"),
                    "point_name": r.get("point_name"),
                    "gas_day": gas_day,
                    "cycle": r.get("cycle"),
                    "dataset_type": dtype,
                    "flow_direction": r.get("flow_direction"),
                    "scheduled_qty": r.get("scheduled_qty"),
                    "design_capacity": r.get("design_capacity"),
                    "operational_capacity": r.get("operational_capacity"),
                    "available_capacity": r.get("available_capacity"),
                    "dod_change": dod.get(key),
                    "units": r.get("units"),
                    "original_units": r.get("original_units"),
                    "original_qty": r.get("original_qty"),
                    "pulled_at_utc": r.get("pulled_at_utc"),
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# fact_storage
# --------------------------------------------------------------------------- #


def _eia_storage_rows(
    eia_snapshot: Optional[dict[str, Any]],
    gas_day: str,
) -> list[dict[str, Any]]:
    """One fact_storage row per EIA region, vs its 5-year band.

    Uses the bands the EIA pull pre-computed (``snapshot['bands']`` — the kept
    ``ebb.eia.five_year_bands`` logic, run at pull time for the latest week). If a
    snapshot was captured without bands (``--no-bands``), recompute them as-of the
    gas day from the weekly records as a fallback.
    """
    if not eia_snapshot:
        return []
    records = eia_snapshot.get("records", [])
    bands = eia_snapshot.get("bands")
    if not bands and records:
        bands = five_year_bands(records, as_of=gas_day)
    bands = bands or {}
    if not bands:
        return []

    # Latest weekly record per region carries the working-gas value + WoW change.
    latest_by_region: dict[str, dict[str, Any]] = {}
    for r in records:
        region = r.get("region")
        if region is None:
            continue
        cur = latest_by_region.get(region)
        if cur is None or (r.get("period", "") > cur.get("period", "")):
            latest_by_region[region] = r
    pulled_at = eia_snapshot.get("pulled_at_utc")

    rows: list[dict[str, Any]] = []
    for region in bands:
        band = bands.get(region) or {}
        latest = latest_by_region.get(region, {})
        current = band.get("current", latest.get("value"))
        fmin, fmax = band.get("five_yr_min"), band.get("five_yr_max")
        pct = None
        if current is not None and fmin is not None and fmax is not None and fmax > fmin:
            pct = (current - fmin) / (fmax - fmin)
        rows.append(
            {
                "region": f"EIA {region}",
                "source": "eia",
                "gas_day": gas_day,
                "as_of_period": band.get("as_of_period") or latest.get("period"),
                "working_gas": current,
                "net_flow": None,
                "net_flow_dod": None,
                "wow_change": latest.get("wow_change"),
                "pct_of_band": pct,
                "min_band": None,
                "max_band": None,
                "five_yr_avg": band.get("five_yr_avg"),
                "five_yr_min": fmin,
                "five_yr_max": fmax,
                "vs_5yr_pct": band.get("vs_5yr_pct"),
                "n_years": band.get("n_years"),
                "units": latest.get("units") or "Bcf",
                "pulled_at_utc": pulled_at,
            }
        )
    return rows


def _storage_rows(
    gas_day: str,
    today_pulls: dict[str, load.SourcePull],
    prior_pulls: dict[str, load.SourcePull],
    eia_snapshot: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    """fact_storage rows: the PG&E system row (Pipe Ranger) + one per EIA region."""
    pr_pull = today_pulls.get("pipe_ranger")
    pr_today = pr_pull.records if pr_pull else []
    pr_prior = prior_pulls["pipe_ranger"].records if "pipe_ranger" in prior_pulls else None

    rows: list[dict[str, Any]] = []
    st = storage_metric(pr_today, pr_prior, eia_snapshot)
    pge = st.get("pge_system")
    if pge:
        rows.append(
            {
                "region": "PG&E System",
                "source": "pipe_ranger",
                "gas_day": gas_day,
                "as_of_period": None,
                "working_gas": pge.get("ending_inventory"),
                "net_flow": pge.get("net_flow"),
                "net_flow_dod": pge.get("net_flow_dod"),
                "wow_change": None,
                "pct_of_band": pge.get("pct_of_band"),
                "min_band": pge.get("min_band"),
                "max_band": pge.get("max_band"),
                "five_yr_avg": None,
                "five_yr_min": None,
                "five_yr_max": None,
                "vs_5yr_pct": None,
                "n_years": None,
                "units": pge.get("units") or "Dth",
                "pulled_at_utc": pr_pull.pulled_at_utc if pr_pull else None,
            }
        )
    rows.extend(_eia_storage_rows(eia_snapshot, gas_day))
    return rows


# --------------------------------------------------------------------------- #
# Write + orchestrate
# --------------------------------------------------------------------------- #


def _write_partition(
    rows: list[dict[str, Any]],
    path: pathlib.Path,
    columns: list[str],
) -> pathlib.Path:
    """Write rows to a CSV partition with a stable header (idempotent overwrite)."""
    df = pd.DataFrame(rows, columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def build_facts(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    cycle: Optional[str] = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build both fact partitions for ``gas_day`` from on-disk lineage.

    Returns ``{gas_day, cycle, operational, storage, sources_loaded,
    sources_missing, paths}``; ``operational`` / ``storage`` are the row lists.
    """
    prior_day = load.prior_gas_day(gas_day)
    today_pulls, missing = _load_flows(gas_day, data_root, cycle)
    prior_pulls, _ = _load_flows(prior_day, data_root, None)  # prior: best-available
    eia_snapshot = load.load_eia(gas_day, data_root=data_root)

    op_rows = _operational_rows(gas_day, today_pulls, prior_pulls)
    st_rows = _storage_rows(gas_day, today_pulls, prior_pulls, eia_snapshot)

    root = pathlib.Path(data_root)
    op_path = root / "operational" / f"operational_{gas_day}.csv"
    st_path = root / "storage" / f"storage_{gas_day}.csv"
    if write:
        _write_partition(op_rows, op_path, FACT_OPERATIONAL_COLUMNS)
        _write_partition(st_rows, st_path, FACT_STORAGE_COLUMNS)
        log.info(
            "wrote %s (%d rows) and %s (%d rows) [%d sources, %d missing, eia=%s]",
            op_path, len(op_rows), st_path, len(st_rows),
            len(today_pulls), len(missing), eia_snapshot is not None,
        )

    return {
        "gas_day": gas_day,
        "cycle": cycle,
        "operational": op_rows,
        "storage": st_rows,
        "sources_loaded": sorted(today_pulls),
        "sources_missing": sorted(missing),
        "paths": {"operational": op_path.as_posix(), "storage": st_path.as_posix()},
    }


# --------------------------------------------------------------------------- #
# CLI (ad-hoc; the primary entry point is `python -m src`)
# --------------------------------------------------------------------------- #


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build star-schema fact partitions for a gas day.")
    parser.add_argument("--gas-day", required=True, help="ISO gas day (Pacific).")
    parser.add_argument("--cycle", default=None, help="Pin a Pipe Ranger cycle (else most-settled).")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = build_facts(args.gas_day, data_root=args.data_root, cycle=args.cycle, write=not args.no_write)
    print(
        f"fact_operational: {len(result['operational'])} rows | "
        f"fact_storage: {len(result['storage'])} rows | "
        f"sources: {result['sources_loaded']} | missing: {result['sources_missing']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
