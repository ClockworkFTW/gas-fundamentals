"""Storage band + net-flow metrics (README §2 pre-computed series).

Python pre-computes the windowed/historical-join storage series that are awkward
in Power BI Pro DAX — the PG&E system inventory-vs-operating-band and net storage
flow (with its day-over-day delta), plus the EIA regional context. ``etl/facts.py``
flattens these into ``fact_storage`` rows; the EIA 5-year bands themselves come
from ``ebb.eia.five_year_bands`` (kept with its data source).

Pure functions over the normalized §2 record dicts (and the EIA weekly snapshot).
No I/O, no network.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

from ._common import _filter


def _net_storage_flow(pr_records: Iterable[dict[str, Any]]) -> Optional[float]:
    """Net Pipe Ranger storage flow (injection +, withdrawal −) in Dth/d."""
    storage_recs = _filter(pr_records, dataset_type="storage", source="pipe_ranger")
    if not storage_recs:
        return None
    net = 0.0
    for r in storage_recs:
        qty = r.get("scheduled_qty")
        if qty is None:
            continue
        if r.get("flow_direction") == "withdrawal":
            net -= qty
        else:
            net += qty
    return net


def _eia_region(snapshot: Optional[dict[str, Any]], region: str) -> Optional[dict[str, Any]]:
    """Working gas + 5-yr band context for one EIA region from a pull snapshot.

    Reads the bands the EIA pull pre-computed (``snapshot['bands'][region]``) and
    the latest weekly record (for value + week-over-week change). Returns ``None``
    when the region is absent.
    """
    if not snapshot:
        return None
    bands = (snapshot.get("bands") or {}).get(region)
    region_recs = [r for r in snapshot.get("records", []) if r.get("region") == region]
    latest = max(region_recs, key=lambda r: r.get("period", "")) if region_recs else None
    if bands is None and latest is None:
        return None
    out: dict[str, Any] = {
        "working_gas_bcf": (bands or {}).get("current") if bands else (latest or {}).get("value"),
        "wow_change": (latest or {}).get("wow_change"),
    }
    if bands:
        out.update(
            {
                "as_of_period": bands.get("as_of_period"),
                "five_yr_avg": bands.get("five_yr_avg"),
                "five_yr_min": bands.get("five_yr_min"),
                "five_yr_max": bands.get("five_yr_max"),
                "vs_5yr_pct": bands.get("vs_5yr_pct"),
                "band_n_years": bands.get("n_years"),
            }
        )
    return out


def storage(
    pr_records: list[dict[str, Any]],
    prior_pr_records: Optional[list[dict[str, Any]]] = None,
    eia_snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """PG&E system inventory-vs-band + net storage flow, plus EIA regional context.

    Returns ``{pge_system, eia_pacific, eia_lower48}``. ``etl/facts.py`` consumes
    ``pge_system`` directly for the PG&E ``fact_storage`` row and builds the EIA
    rows from the snapshot bands (all regions, as-of the gas day).
    """
    out: dict[str, Any] = {"pge_system": None, "eia_pacific": None, "eia_lower48": None}

    inv = _filter(pr_records, dataset_type="inventory", source="pipe_ranger")
    if inv:
        rec = inv[0]
        end = rec.get("scheduled_qty")
        max_band = rec.get("design_capacity")     # parse_inventory: max band → design_capacity
        min_band = rec.get("available_capacity")  # parse_inventory: min band → available_capacity
        pct_of_band = None
        if end is not None and min_band is not None and max_band is not None and max_band > min_band:
            pct_of_band = (end - min_band) / (max_band - min_band)
        net = _net_storage_flow(pr_records)
        net_prior = _net_storage_flow(prior_pr_records) if prior_pr_records is not None else None
        out["pge_system"] = {
            "ending_inventory": end,
            "min_band": min_band,
            "max_band": max_band,
            "pct_of_band": pct_of_band,
            "net_flow": net,
            "net_flow_prior": net_prior,
            "net_flow_dod": (net - net_prior) if (net is not None and net_prior is not None) else None,
            "units": rec.get("units"),
        }

    out["eia_pacific"] = _eia_region(eia_snapshot, "Pacific")
    out["eia_lower48"] = _eia_region(eia_snapshot, "Lower 48")
    return out
