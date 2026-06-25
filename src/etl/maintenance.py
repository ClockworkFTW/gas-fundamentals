"""Build the maintenance / notices snapshot facts (exploration/FACT_NOTICES_DESIGN.md).

Two facts, current-snapshot grain (overwritten each run, not dated partitions):

    data/notices/notices_current.csv          (fact_notices  — the severity feed)
    data/maintenance/maintenance_current.csv   (fact_maintenance — the timeline)

``fact_notices`` is one row per notice (event grain); ``fact_maintenance`` is one
row per (maintenance item x affected location x date-span) and carries the
structured capacity impact. They link by ``notice_id`` but neither requires the
other (OFO/EFO + Kern advisories have no capacity line; PG&E foghorn + NGTL outages
have no NAESB notice).

Per-source parsing lives in ``etl.maintenance_sources.<pipe>`` (the ebb clients'
request logic stays frozen; those modules reuse the clients' existing fetch methods
and add only the small additive fetches the maintenance feeds need — the PG&E
``foghorn`` POST, the EPNG/TW detail GETs). This module owns the shapes' shared
pre-compute: notice classification/severity, unit -> Dth/d conversion,
``pct_of_capacity``, supersession -> ``is_current``, the foghorn year-less date
parse, and the snapshot writers.

Canonical unit is Dth/d (original preserved); ``pulled_at`` is UTC; writes are
idempotent. Gas day is Pacific.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import logging
import pathlib
import re
from typing import Any, Optional

import pandas as pd

from ebb.schema import MaintenanceImpact, NoticeEvent

log = logging.getLogger("etl.maintenance")

# --------------------------------------------------------------------------- #
# Source registry — one module per pipe under etl.maintenance_sources.
# Each exposes ``build(gas_day, *, session=None, raw_dir=None)
#   -> tuple[list[NoticeEvent], list[MaintenanceImpact]]`` (fetch + parse) and
# pure ``parse_*`` functions the offline tests exercise against fixtures.
# --------------------------------------------------------------------------- #
SOURCES: tuple[str, ...] = (
    "pipe_ranger", "gtn", "el_paso", "transwestern", "kern_river", "nova", "foothills",
)

# Stable CSV column order (matches the dataclass field order; one header even when empty).
FACT_NOTICES_COLUMNS = [f.name for f in NoticeEvent.__dataclass_fields__.values()]  # type: ignore[attr-defined]
FACT_MAINTENANCE_COLUMNS = [f.name for f in MaintenanceImpact.__dataclass_fields__.values()]  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- #
# Classification — normalized notice_type -> severity / category (for the feed).
# Source modules set notice_type from their own raw vocab; these map it uniformly.
# --------------------------------------------------------------------------- #
NOTICE_TYPES = frozenset({
    "maintenance", "planned_outage", "critical", "capacity_constraint",
    "ofo", "efo", "advisory", "other",
})
_SEVERITY_BY_TYPE = {
    "efo": "critical", "ofo": "high", "critical": "high",
    "capacity_constraint": "medium", "maintenance": "medium", "planned_outage": "medium",
    "advisory": "low", "other": "info",
}
_CATEGORY_BY_TYPE = {
    "maintenance": "maintenance", "planned_outage": "maintenance",
    "capacity_constraint": "capacity",
    "ofo": "operational", "efo": "operational", "critical": "operational", "advisory": "operational",
    "other": "administrative",
}


def severity_for(notice_type: str) -> str:
    return _SEVERITY_BY_TYPE.get(notice_type, "info")


def category_for(notice_type: str) -> str:
    return _CATEGORY_BY_TYPE.get(notice_type, "administrative")


# --------------------------------------------------------------------------- #
# Unit conversion to canonical Dth/d (original always preserved on the record).
#   MMcf/d  : 1 MMcf at h BTU/cf == h Dth        (Pipe Ranger / GTN)
#   MMBtu/d : 1 MMBtu == 1 Dth                   (Transwestern)
#   10^3m^3/d: 1e3 m^3 == 35,314.7 cf -> Dth at h BTU/cf  (NGTL / Foothills)
# The 10^3m^3 factor uses a default heat content; NGTL outage rows carry no per-row
# heat value, so prefer pct_of_capacity (unit-free) for cross-pipe comparison.
# --------------------------------------------------------------------------- #
DEFAULT_HEAT_BTU_PER_CF = 1000.0
_CF_PER_1000M3 = 35_314.666721


def mmcf_to_dth(v: Optional[float], heat: float = DEFAULT_HEAT_BTU_PER_CF) -> Optional[float]:
    return None if v is None else round(v * heat, 1)


def mmbtu_to_dth(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v, 1)


def m3e3_to_dth(v: Optional[float], heat: float = DEFAULT_HEAT_BTU_PER_CF) -> Optional[float]:
    return None if v is None else round(v * _CF_PER_1000M3 * heat / 1e6, 1)


def to_dth(value: Optional[float], units: str, heat: float = DEFAULT_HEAT_BTU_PER_CF) -> Optional[float]:
    """Convert ``value`` in ``units`` to canonical Dth/d (None-safe)."""
    if value is None:
        return None
    u = (units or "").lower()
    if u in ("dth/d", "dth", "mmbtu/d", "mmbtu"):
        return mmbtu_to_dth(value)
    if u in ("mmcf/d", "mmcf"):
        return mmcf_to_dth(value, heat)
    if u in ("10^3m^3/d", "10^3m3/d", "e3m3/d", "10e3m3/d"):
        return m3e3_to_dth(value, heat)
    return value  # unknown unit: pass through (original_units records the truth)


def pct_of_capacity(remaining: Optional[float], base: Optional[float]) -> Optional[float]:
    """remaining / base * 100 — the unit-free, cross-pipe-comparable measure."""
    if remaining is None or not base:
        return None
    return round(remaining / base * 100.0, 1)


# --------------------------------------------------------------------------- #
# Date helpers
# --------------------------------------------------------------------------- #
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july",
     "august", "september", "october", "november", "december"], start=1)}


def parse_monthday_range(text: str, anchor: dt.date) -> tuple[Optional[str], Optional[str]]:
    """Parse a year-less month/day range (PG&E foghorn ``Dates``) to ISO start/end.

    Handles "June 28", "June 29 - 30", "July 13 - 31", "June 27 - July 02".
    Year is inferred from ``anchor`` (today): a month >= anchor's month is this
    year; a month that has wrapped past December rolls to next year.
    """
    s = " ".join((text or "").split())
    if not s:
        return None, None

    def infer_year(month: int) -> int:
        # forward-looking feed: a month earlier than the anchor's has wrapped into next year
        return anchor.year + 1 if month < anchor.month else anchor.year

    def iso(month: int, day: int) -> Optional[str]:
        try:
            return dt.date(infer_year(month), month, day).isoformat()
        except ValueError:
            return None

    # "Month D" optionally "- D" or "- Month D"
    m = re.match(r"^([A-Za-z]+)\s+(\d{1,2})(?:\s*[-–]\s*(?:([A-Za-z]+)\s+)?(\d{1,2}))?$", s)
    if not m:
        return None, None
    mon1 = _MONTHS.get(m.group(1).lower())
    if mon1 is None:
        return None, None
    d1 = int(m.group(2))
    start = iso(mon1, d1)
    if m.group(4) is None:
        return start, start
    mon2 = _MONTHS.get((m.group(3) or m.group(1)).lower()) or mon1
    d2 = int(m.group(4))
    return start, iso(mon2, d2)


# --------------------------------------------------------------------------- #
# Cross-source post-processing
# --------------------------------------------------------------------------- #


def resolve_is_current(notices: list[NoticeEvent]) -> None:
    """Set ``is_current`` in place: a notice is stale if another notice in the same
    source supersedes it (its id appears as a ``prior_notice_id``) or it terminated."""
    superseded: dict[str, set[str]] = {}
    for n in notices:
        if n.prior_notice_id:
            superseded.setdefault(n.source, set()).add(str(n.prior_notice_id))
    for n in notices:
        stale = str(n.notice_id) in superseded.get(n.source, set())
        n.is_current = not stale and (n.status or "").lower() != "terminate"


def link_capacity_impact(notices: list[NoticeEvent], impacts: list[MaintenanceImpact]) -> None:
    """Set ``has_capacity_impact`` / ``primary_point_id`` from the impact rows."""
    by_notice: dict[tuple[str, str], list[MaintenanceImpact]] = {}
    for im in impacts:
        if im.notice_id is not None:
            by_notice.setdefault((im.source, str(im.notice_id)), []).append(im)
    for n in notices:
        linked = by_notice.get((n.source, str(n.notice_id)), [])
        n.has_capacity_impact = bool(linked)
        if n.primary_point_id is None:
            n.primary_point_id = next((im.point_id for im in linked if im.point_id), None)


def backfill_base_capacity(impacts: list[MaintenanceImpact], design_by_point: dict[tuple[str, str], float]) -> None:
    """Where an impact has remaining capacity but no base, fill base from a
    ``(source, point_id) -> design_capacity`` lookup (e.g. from fact_operational),
    then (re)compute ``pct_of_capacity``. Used for GTN (remaining-only in feed)."""
    for im in impacts:
        if im.base_capacity_dthd is None and im.point_id is not None:
            base = design_by_point.get((im.source, str(im.point_id)))
            if base:
                im.base_capacity_dthd = base
        if im.pct_of_capacity is None:
            im.pct_of_capacity = pct_of_capacity(im.capacity_remaining_dthd, im.base_capacity_dthd)
        # derive the missing one of {remaining, reduction} when base is known
        if im.base_capacity_dthd is not None:
            if im.reduction_dthd is None and im.capacity_remaining_dthd is not None:
                im.reduction_dthd = round(im.base_capacity_dthd - im.capacity_remaining_dthd, 1)
            elif im.capacity_remaining_dthd is None and im.reduction_dthd is not None:
                im.capacity_remaining_dthd = round(im.base_capacity_dthd - im.reduction_dthd, 1)


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #


def _write_csv(rows: list[dict[str, Any]], path: pathlib.Path, columns: list[str]) -> pathlib.Path:
    df = pd.DataFrame(rows, columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def write_current(
    notices: list[NoticeEvent],
    impacts: list[MaintenanceImpact],
    data_root: pathlib.Path | str = "data",
) -> dict[str, pathlib.Path]:
    """Overwrite the two current-snapshot CSVs (idempotent)."""
    root = pathlib.Path(data_root)
    n_path = root / "notices" / "notices_current.csv"
    m_path = root / "maintenance" / "maintenance_current.csv"
    _write_csv([n.to_dict() for n in notices], n_path, FACT_NOTICES_COLUMNS)
    _write_csv([im.to_dict() for im in impacts], m_path, FACT_MAINTENANCE_COLUMNS)
    return {"notices": n_path, "maintenance": m_path}


# --------------------------------------------------------------------------- #
# Orchestrate
# --------------------------------------------------------------------------- #


def build_maintenance(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    sources: Optional[list[str]] = None,
    session: Any = None,
    design_by_point: Optional[dict[tuple[str, str], float]] = None,
    write: bool = True,
) -> dict[str, Any]:
    """Fetch + parse every source's maintenance feed, post-process, write snapshots.

    Network-bound (the source modules fetch); each source is wrapped so one failure
    doesn't sink the rest. Returns ``{gas_day, notices, impacts, sources_ok,
    sources_failed, paths}``.
    """
    targets = sources or list(SOURCES)
    root = pathlib.Path(data_root)
    notices: list[NoticeEvent] = []
    impacts: list[MaintenanceImpact] = []
    ok: list[str] = []
    failed: dict[str, str] = {}

    for source in targets:
        try:
            mod = importlib.import_module(f"etl.maintenance_sources.{source}")
            ev, im = mod.build(gas_day, session=session, raw_dir=root / source / "_maint_raw")
            notices.extend(ev)
            impacts.extend(im)
            ok.append(source)
        except Exception as exc:  # noqa: BLE001 - convenience path; log and continue
            log.warning("maintenance build failed for %s: %s", source, exc)
            failed[source] = f"{type(exc).__name__}: {exc}"

    resolve_is_current(notices)
    if design_by_point:
        backfill_base_capacity(impacts, design_by_point)
    link_capacity_impact(notices, impacts)

    paths: dict[str, str] = {}
    if write:
        written = write_current(notices, impacts, data_root)
        paths = {k: v.as_posix() for k, v in written.items()}
        log.info("wrote notices=%d, maintenance=%d [%d sources ok, %d failed]",
                 len(notices), len(impacts), len(ok), len(failed))

    return {
        "gas_day": gas_day,
        "notices": notices,
        "impacts": impacts,
        "sources_ok": ok,
        "sources_failed": failed,
        "paths": paths,
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Build the maintenance + notices snapshot facts.")
    parser.add_argument("--gas-day", required=True, help="ISO gas day (Pacific).")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--sources", nargs="*", default=None, help="Limit to these sources.")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    result = build_maintenance(args.gas_day, data_root=args.data_root, sources=args.sources, write=not args.no_write)
    print(
        f"fact_notices: {len(result['notices'])} rows | fact_maintenance: {len(result['impacts'])} rows | "
        f"ok: {result['sources_ok']} | failed: {list(result['sources_failed'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
