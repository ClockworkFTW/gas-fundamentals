"""Common normalized record shapes (README §5) + shared helpers.

Every flow/capacity EBB module emits ``FlowRecord``; notices use ``Notice``.
Designed once here and reused across pipe_ranger, gtn, and the other pipelines.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import re
from typing import Any, Optional

UTC = dt.timezone.utc

# NAESB flow indicator -> normalized direction. Shared by the OAC pipeline
# clients (gtn, el_paso, transwestern, kern_river); "BD" (bidirectional
# compressor) and anything else fall through to None via .get().
FLOW_DIRECTION = {"R": "receipt", "D": "delivery"}


@dataclasses.dataclass
class FlowRecord:
    source: str
    dataset_type: str        # scheduled_quantity | operationally_available | storage | inventory | supply_demand | notice
    gas_day: str             # ISO date, Pacific gas day
    cycle: Optional[str]
    point_name: str
    point_id: Optional[str]
    flow_direction: Optional[str]   # receipt | delivery
    scheduled_qty: Optional[float]
    design_capacity: Optional[float]
    operational_capacity: Optional[float]
    available_capacity: Optional[float]
    units: str               # canonical (Dth/d)
    original_units: Optional[str]
    original_qty: Optional[float]
    pulled_at_utc: str
    raw_ref: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Notice:
    source: str
    gas_day: str
    posted_at: Optional[str]
    notice_type: str         # OFO | EFO | maintenance | critical | other
    stage: Optional[Any]
    headline: str
    body: str
    url: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class NoticeEvent:
    """One notice (event/header grain) -> ``fact_notices`` (the severity feed).

    Covers every notice category across the pipes (OFO/EFO, critical, maintenance,
    planned outage, capacity constraint, advisory). Supersession is resolved to
    ``is_current`` in the ETL; ``has_capacity_impact`` links to ``MaintenanceImpact``
    rows. See exploration/FACT_NOTICES_DESIGN.md.
    """
    source: str
    notice_id: str
    notice_type: str            # normalized: maintenance|planned_outage|critical|capacity_constraint|ofo|efo|advisory|other
    notice_type_raw: str
    severity: str               # info|low|medium|high|critical (pre-computed)
    category: str               # maintenance|capacity|operational|administrative
    posted_at_utc: Optional[str]
    effective_start: Optional[str]
    effective_end: Optional[str]
    status: Optional[str]       # initiate|supersede|terminate|active
    prior_notice_id: Optional[str]
    is_current: bool            # pre-computed: latest non-superseded in its chain
    has_capacity_impact: bool
    primary_point_id: Optional[str]
    affects_pge: bool
    headline: str
    body: str
    url: str
    gas_day: str                # OFO/EFO = order gas day; ranged notices = effective_start
    pulled_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class MaintenanceImpact:
    """One (maintenance item x affected location x date-span) -> ``fact_maintenance``.

    The maintenance-constraint timeline. Capacity is normalized to Dth/d with the
    original preserved; ``pct_of_capacity`` is the unit-free cross-pipe comparable.
    ``capacity_basis`` flags whether the source natively reports remaining capacity,
    the reduction amount, or a % cut. ``join_kind`` is honest about how (if at all)
    the row reaches ``fact_operational.point_id`` / the schematic.
    """
    source: str
    maintenance_id: str
    notice_id: Optional[str]            # FK -> NoticeEvent; None for foghorn / NGTL outages
    point_id: Optional[str]            # join -> dim_location / fact_operational.point_id
    segment_or_gate: Optional[str]
    affected_label: str
    join_kind: str                     # point_id|gate_code|segment_name|path|text_label
    date_start: str
    date_end: Optional[str]
    capacity_basis: str                # remaining|reduction|pct_cut
    capacity_remaining_dthd: Optional[float]
    reduction_dthd: Optional[float]
    base_capacity_dthd: Optional[float]
    pct_of_capacity: Optional[float]
    pct_firm_cut: Optional[float]
    reduction_planned_dthd: Optional[float]   # EPNG PLM split
    reduction_fm_dthd: Optional[float]        # EPNG FMJ split
    original_value: Optional[float]
    original_units: str                # MMcf/d | Dth/d | 10^3m^3/d | MMBtu/d
    restriction_type: Optional[str]
    work_description: str
    is_unplanned: bool
    pulled_at_utc: str

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def collapse_ws(text: Any) -> str:
    """Collapse all runs of whitespace to single spaces and strip (HTML cell text)."""
    return " ".join((text or "").split())


def default_gas_day(tz: dt.tzinfo) -> str:
    """Latest available gas day in ``tz``: prior day before 08:00 local, else today.

    The shared CLI default for the pipeline clients (Pacific) and the Canadian
    systems (Mountain) — prior-day final isn't posted until ~08:00 local.
    """
    now = dt.datetime.now(tz)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def to_float(value: Any) -> Optional[float]:
    """Parse EBB numbers: comma-strings ("1,434,727"), numbers, "n/a"/blank."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if s == "" or s.lower() in {"n/a", "na", "--", "-"}:
        return None
    s = s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return None


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def norm_date(value: Any) -> Optional[str]:
    """Normalize EBB date strings to ISO 'YYYY-MM-DD'.

    Handles 'MM/DD/YYYY', 'M/D/YY', 'D-M-YYYY', ISO 'YYYY-MM-DD' (with optional
    trailing time), and 'DD-Mon-YY[YY]' (e.g. '23-Dec-24', TC Energy CSVs).
    Ignores any trailing time.
    """
    if not value:
        return None
    s = str(value).strip().split()[0]  # drop any trailing " 9:00 AM CCT" / time
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)  # ISO Y-M-D
    if m:
        yr, mo, da = (int(x) for x in m.groups())
        return f"{yr:04d}-{mo:02d}-{da:02d}"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)  # M/D/Y
    if m:
        mo, da, yr = (int(x) for x in m.groups())
        yr += 2000 if yr < 100 else 0
        return f"{yr:04d}-{mo:02d}-{da:02d}"
    m = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})$", s)  # D-Mon-Y (TC Energy)
    if m:
        da = int(m.group(1))
        mo = _MONTHS.get(m.group(2).lower())
        yr = int(m.group(3))
        if mo:
            yr += 2000 if yr < 100 else 0
            return f"{yr:04d}-{mo:02d}-{da:02d}"
    m = re.match(r"^(\d{1,2})-(\d{1,2})-(\d{4})$", s)  # D-M-Y
    if m:
        da, mo, yr = (int(x) for x in m.groups())
        return f"{yr:04d}-{mo:02d}-{da:02d}"
    return None
