"""NOVA / NGTL maintenance -> MaintenanceImpact rows (reference source module).

NGTL's Daily Operating Plan outages (``csv/outages/``) are the best-structured
maintenance feed of the set: one row per (outage x operational-area gate), with a
remaining ``Capability`` and, on compressor/USJR rows, ``Local Base`` vs ``Local
Outage Capability`` that give the local reduction. Plus upstream
``plantturnaroundactivity`` receipt/delivery impacts. Units are 10^3 m^3/d.

NGTL outages are not NAESB notices, so this source emits ``fact_maintenance`` rows
only (no ``NoticeEvent``). Fetch reuses the frozen ``NovaClient``; parsing is here.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import pathlib
from typing import Any, Optional

from ebb.nova import NovaClient
from ebb.schema import MaintenanceImpact, NoticeEvent, norm_date, to_float, utc_now_iso

from etl.maintenance import to_dth

SOURCE = "nova"
UNITS = "10^3m^3/d"
OUTAGES_URL = "https://my.tccustomerexpress.com/#Outages"
LOOKBACK_DAYS = 7      # keep outages still active on/after gas_day - LOOKBACK
LOOKAHEAD_DAYS = 45    # ...and starting within gas_day + LOOKAHEAD


def _clean(v: Optional[str]) -> str:
    return (v or "").strip()


def _in_window(start: Optional[str], end: Optional[str], gas_day: str) -> bool:
    day = dt.date.fromisoformat(gas_day)
    cutoff = (day - dt.timedelta(days=LOOKBACK_DAYS)).isoformat()
    horizon = (day + dt.timedelta(days=LOOKAHEAD_DAYS)).isoformat()
    if end is not None and end < cutoff:
        return False
    if start is not None and start > horizon:
        return False
    return True


def parse_outages(text: str, gas_day: str, pulled_at: str, *, source: str = SOURCE) -> list[MaintenanceImpact]:
    """One MaintenanceImpact per (outage x gate) within the gas-day window."""
    out: list[MaintenanceImpact] = []
    for r in csv.DictReader(io.StringIO(text)):
        gate = _clean(r.get("Table"))
        start = norm_date(r.get("Start"))
        end = norm_date(r.get("End"))
        if not gate or not _in_window(start, end, gas_day):
            continue
        capability = to_float(r.get("Capability"))
        local_base = to_float(r.get("Local Base Capability"))
        local_outage = to_float(r.get("Local Outage Capability"))
        # local_base/local_outage are a LOCAL (constrained-area) pair; their
        # difference is the only quantified cut NGTL gives. The gate ``Capability``
        # is a different reference frame (gate FT-R), so we do NOT treat local_base
        # as the gate's base — leaving base_capacity_dthd (and hence pct_of_capacity,
        # which is strictly remaining/base) null keeps pct's meaning uniform across
        # pipes. The local cut is still surfaced as reduction_dthd.
        reduction = (
            round(local_base - local_outage, 1)
            if local_base is not None and local_outage is not None else None
        )
        area = _clean(r.get("Area for Stated Capability"))
        desc = _clean(r.get("Description"))
        uid = _clean(r.get("UID")) or _clean(r.get("Outage Id"))
        out.append(
            MaintenanceImpact(
                source=source,
                maintenance_id=f"{source}:{uid}",
                notice_id=None,
                point_id=None,
                segment_or_gate=gate,
                affected_label=area or desc or gate,
                join_kind="gate_code",
                date_start=start or gas_day,
                date_end=end,
                capacity_basis="remaining",
                capacity_remaining_dthd=to_dth(capability, UNITS),
                reduction_dthd=to_dth(reduction, UNITS),
                base_capacity_dthd=None,
                pct_of_capacity=None,
                pct_firm_cut=None,
                reduction_planned_dthd=None,
                reduction_fm_dthd=None,
                original_value=capability,
                original_units=UNITS,
                restriction_type=_clean(r.get("Type of Restriction")) or None,
                work_description=desc,
                is_unplanned="unplanned" in desc.lower(),
                pulled_at_utc=pulled_at,
            )
        )
    return out


def parse_plant_turnarounds(text: str, gas_day: str, pulled_at: str, *, source: str = SOURCE) -> list[MaintenanceImpact]:
    """One MaintenanceImpact per upstream plant turnaround (Receipt|Delivery impact)."""
    out: list[MaintenanceImpact] = []
    for r in csv.DictReader(io.StringIO(text)):
        start = norm_date(r.get("Start"))
        end = norm_date(r.get("End"))
        if not _in_window(start, end, gas_day):
            continue
        kind = _clean(r.get("Type"))      # Receipt | Delivery
        impact = to_float(r.get("Impact"))
        out.append(
            MaintenanceImpact(
                source=source,
                maintenance_id=f"{source}:plant:{kind}:{start}:{end}",
                notice_id=None,
                point_id=None,
                segment_or_gate=None,
                affected_label=f"Upstream plant turnaround ({kind})",
                join_kind="text_label",
                date_start=start or gas_day,
                date_end=end,
                capacity_basis="reduction",
                capacity_remaining_dthd=None,
                reduction_dthd=to_dth(impact, UNITS),
                base_capacity_dthd=None,
                pct_of_capacity=None,
                pct_firm_cut=None,
                reduction_planned_dthd=None,
                reduction_fm_dthd=None,
                original_value=impact,
                original_units=UNITS,
                restriction_type=kind or None,
                work_description="NGTL upstream plant turnaround",
                is_unplanned=False,
                pulled_at_utc=pulled_at,
            )
        )
    return out


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    pulled_at = utc_now_iso()
    client = NovaClient(data_dir=(raw_dir or pathlib.Path("data/nova")), session=session)
    outages_text = client.fetch_outages(raw_dir=raw_dir)
    plant_text = client.fetch_plant_turnarounds(raw_dir=raw_dir)
    impacts = parse_outages(outages_text, gas_day, pulled_at)
    impacts += parse_plant_turnarounds(plant_text, gas_day, pulled_at)
    return [], impacts
