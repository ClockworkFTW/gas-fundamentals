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


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


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
