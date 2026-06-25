"""GTN (TC Energy GTN -> Malin) maintenance -> NoticeEvent + MaintenanceImpact.

GTN's EBB (``tcplus.com/GTN``) posts maintenance two ways:

  1. Critical ``Maint`` notices and the monthly ``Plnd Outage`` schedule notice
     (the NAESB notice grain) -> one ``NoticeEvent`` each.
  2. Capacity-reduction prose inside the ``Maint`` notices -> one
     ``MaintenanceImpact`` per (notice x affected LOC# x stated Available Capacity).

GTN states *Available Capacity* — the remaining capacity on the affected segment
during the outage — in MMcf/d (``capacity_basis="remaining"``). It is NOT a
reduction amount and NOT a % cut; there is no unconstrained base in the prose, so
``base_capacity_dthd`` / ``pct_of_capacity`` are left null for the orchestrator to
backfill from ``fact_operational`` design via ``point_id``.

Join key: a GTN OAC ``LocationID`` is the join handle to
``fact_operational.point_id`` (GTN LOC# == OAC LocationID == point_id — confirmed
in recon), so ``join_kind="point_id"``.

The attached "GTN Planned Maintenance Schedule" PDF is the richest forward-looking
source but needs ``pdftotext``; PDF parsing is OPTIONAL and skipped when the tool
is unavailable. Fetch reuses the frozen ``GTNClient.fetch_notices``; parsing is
here. Reference: exploration/extract/gtn_maint.py.
"""
from __future__ import annotations

import pathlib
import re
from typing import Any, Optional

from ebb.gtn import NOTICE_DETAIL_URL, GTNClient
from ebb.schema import (
    MaintenanceImpact,
    NoticeEvent,
    collapse_ws,
    norm_date,
    utc_now_iso,
)

from etl.maintenance import category_for, severity_for, to_dth

SOURCE = "gtn"
UNITS = "MMcf/d"
BODY_MAX = 2000

# Notice window (wide, so the monthly MAINTENANCE SCHEDULE notice is caught).
EFF_START = "04/15/2026"
EFF_END = "08/15/2026"

# A GTN notice is maintenance iff NoticeType is one of these; map to normalized.
MAINT_NOTICE_TYPES: dict[str, str] = {
    "Maint": "maintenance",
    "Plnd Outage": "planned_outage",
}

# GTN feeds PG&E at Malin — every GTN maintenance notice is PG&E-relevant.
AFFECTS_PGE = True

# "Available Capacity 1,650 MMcf/d" / "2,500 MMcf/d" -> value + units.
RE_CAPACITY = re.compile(
    r"(?:Available\s+Capacity\s+)?"
    r"(?P<value>\d{1,3}(?:,\d{3})*|\d+)\s*[-–]?\s*"
    r"(?P<units>MMcf/d|MMcfd|MMcf|Dth/d|Dth|MMBtu/d)",
    re.IGNORECASE,
)
# Affected LOC numbers, e.g. "LOC #18480" / "#954690".
RE_LOC = re.compile(r"(?:LOC\s*)?#\s*(?P<loc>\d{3,})", re.IGNORECASE)
# The segment name immediately preceding "(LOC #...)", e.g. "Station 9 CFTP".
# Each token must start uppercase/digit so we grab only the proper-noun segment
# name (e.g. "Station 9 CFTP"), not the sentence prose leading up to it.
RE_SEGMENT_BEFORE_LOC = re.compile(
    r"(?P<name>(?:[A-Z0-9][\w/&.\-]*\s+){0,4}[A-Z0-9][\w/&.\-]*)\s*\(LOC\s*#\s*\d{3,}\)"
)


def _normalize_dashes(text: str) -> str:
    return (text or "").replace("–", "-").replace("—", "-").replace("\xa0", " ")


def _posted_at(row: dict[str, Any]) -> Optional[str]:
    """``PostingDate`` + ``PostingTime`` -> 'YYYY-MM-DD HH:MM' (or just the date)."""
    date = norm_date(row.get("PostingDate"))
    if not date:
        return None
    time = (row.get("PostingTime") or "").strip()
    return f"{date} {time}".strip()


def _clean_body(text: str) -> str:
    """Strip HTML, collapse whitespace, truncate to ~BODY_MAX chars."""
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    cleaned = collapse_ws(_normalize_dashes(no_tags))
    return cleaned[:BODY_MAX]


def _segment_label(text: str, subject: str) -> Optional[str]:
    """Affected segment name near the LOC# (e.g. 'Station 9 CFTP'), else Subject."""
    m = RE_SEGMENT_BEFORE_LOC.search(text)
    if m:
        return collapse_ws(m.group("name")) or None
    return collapse_ws(subject) or None


def is_maintenance(row: dict[str, Any]) -> bool:
    """A notice is maintenance iff its NoticeType is Maint or Plnd Outage."""
    return (row.get("NoticeType") or "").strip() in MAINT_NOTICE_TYPES


def parse_notice_event(row: dict[str, Any], pulled_at: str, *, source: str = SOURCE) -> NoticeEvent:
    """One GTN maintenance/planned-outage notice -> a NoticeEvent (header grain)."""
    notice_type = MAINT_NOTICE_TYPES[(row.get("NoticeType") or "").strip()]
    notice_id = str(row.get("NoticeId"))
    eff_start = norm_date(row.get("EffDate"))
    prior = row.get("PriorNoticeID")
    return NoticeEvent(
        source=source,
        notice_id=notice_id,
        notice_type=notice_type,
        notice_type_raw=(row.get("NoticeType") or "").strip(),
        severity=severity_for(notice_type),
        category=category_for(notice_type),
        posted_at_utc=_posted_at(row),
        effective_start=eff_start,
        effective_end=norm_date(row.get("EndDate")),
        status=(row.get("NoticeStatus") or "").strip().lower() or None,
        prior_notice_id=str(prior) if prior else None,
        is_current=True,                     # orchestrator overwrites via supersession
        has_capacity_impact=False,           # orchestrator links from impacts
        primary_point_id=None,               # orchestrator links from impacts
        affects_pge=AFFECTS_PGE,
        headline=collapse_ws(row.get("Subject")),
        body=_clean_body(row.get("Text") or ""),
        url=NOTICE_DETAIL_URL.format(notice_id=notice_id),
        gas_day=eff_start or "",             # ranged notice -> effective_start
        pulled_at_utc=pulled_at,
    )


def parse_capacity_impacts(
    row: dict[str, Any], pulled_at: str, *, source: str = SOURCE
) -> list[MaintenanceImpact]:
    """One MaintenanceImpact per (notice x affected LOC# x stated capacity value).

    GTN's reduction prose names a single segment + LOC# and a single remaining
    "Available Capacity X MMcf/d". We pair each LOC# with each capacity value found
    in the text (in practice one of each per Maint notice).
    """
    text = _normalize_dashes(row.get("Text") or "")
    subject = _normalize_dashes(row.get("Subject") or "")
    notice_id = str(row.get("NoticeId"))

    locs: list[str] = []
    for m in RE_LOC.finditer(text):
        loc = m.group("loc")
        if loc not in locs:
            locs.append(loc)

    values: list[float] = []
    seen: set[str] = set()
    for m in RE_CAPACITY.finditer(text):
        raw = m.group("value")
        if raw in seen:
            continue
        seen.add(raw)
        values.append(float(raw.replace(",", "")))

    eff_start = norm_date(row.get("EffDate"))
    eff_end = norm_date(row.get("EndDate"))
    label = _segment_label(text, subject)
    is_unplanned = "unplanned" in text.lower()

    out: list[MaintenanceImpact] = []
    for loc in locs:
        for value in values:
            out.append(
                MaintenanceImpact(
                    source=source,
                    maintenance_id=f"{source}:{notice_id}:{loc}:{value:g}",
                    notice_id=notice_id,
                    point_id=loc,
                    segment_or_gate=None,
                    affected_label=label or loc,
                    join_kind="point_id",
                    date_start=eff_start or "",
                    date_end=eff_end,
                    capacity_basis="remaining",
                    capacity_remaining_dthd=to_dth(value, UNITS),
                    reduction_dthd=None,
                    base_capacity_dthd=None,     # no unconstrained base in prose
                    pct_of_capacity=None,        # orchestrator backfills via point_id
                    pct_firm_cut=None,
                    reduction_planned_dthd=None,
                    reduction_fm_dthd=None,
                    original_value=value,
                    original_units=UNITS,
                    restriction_type=None,
                    work_description=collapse_ws(subject),
                    is_unplanned=is_unplanned,
                    pulled_at_utc=pulled_at,
                )
            )
    return out


def parse(
    rows: list[dict[str, Any]], pulled_at: str, *, source: str = SOURCE
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    """Filter to maintenance notices, then emit NoticeEvents + MaintenanceImpacts."""
    events: list[NoticeEvent] = []
    impacts: list[MaintenanceImpact] = []
    for row in rows:
        if not is_maintenance(row):
            continue
        events.append(parse_notice_event(row, pulled_at, source=source))
        impacts.extend(parse_capacity_impacts(row, pulled_at, source=source))
    return events, impacts


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    pulled_at = utc_now_iso()
    client = GTNClient(data_dir=(raw_dir or pathlib.Path("data/gtn")), session=session)
    rows = client.fetch_notices(
        gas_day, eff_start=EFF_START, eff_end=EFF_END, raw_dir=raw_dir
    )
    return parse(rows, pulled_at)
