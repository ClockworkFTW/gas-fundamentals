"""PG&E / Pipe Ranger maintenance -> MaintenanceImpact + NoticeEvent rows.

PG&E's FORWARD planned maintenance for its OWN CGT backbone comes from the foghorn
feed (``POST /bin/pipeline/foghorn``), not a per-customer NAESB capacity-release
notice. The endpoint returns three "views" per (path, ``Dates``) row keyed by
``dropDownVal``:

  - ``pipelineCap`` : REMAINING capacity in MMcf/d (absolute), e.g. ``1960.0``
  - ``maxCap``      : remaining as a % of the path's unconstrained max, e.g. ``"99.0%"``
  - ``firmCuts``    : % of firm rights being cut, e.g. ``"5.22%"``

``parse_foghorn`` merges the three views per (path, ``Dates``) row. Redwood is one
path-level row; Baja explodes into its three delivery points (Kettleman, Hinkley,
Topock). The remaining capacity is normalized to Dth/d (original MMcf/d preserved);
``maxCap`` is a TRUE remaining/max %, so the unconstrained base is back-computed from
``remaining / (pct/100)`` — base + pct stay internally consistent (README §5).

OFO/EFO Operational/Emergency Flow Orders come from ``PipeRangerClient.fetch('ofo'
/'efo')`` (the ``ofoefoarchive`` servlet) and emit NoticeEvent rows. They are often
empty live / an old archive, so the fetch is handled gracefully.
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from typing import Any, Optional

from ebb.base import write_raw
from ebb.pipe_ranger import PipeRangerClient
from ebb.schema import NoticeEvent, MaintenanceImpact, norm_date, to_float, utc_now_iso

from etl.maintenance import (
    category_for,
    parse_monthday_range,
    severity_for,
    to_dth,
)

SOURCE = "pipe_ranger"
UNITS = "MMcf/d"

PAGE_PATH = "/pipeline/en/operating-data/current-pipeline-status/pipeline-maintenance/foghorn.html"
PAGE_URL = "https://www.pge.com" + PAGE_PATH
FOGHORN_URL = "https://www.pge.com/bin/pipeline/foghorn"
NOTICE_URL = "https://www.pge.com/pipeline/operations/cgt_pipeline_status.page"

VIEWS = ("pipelineCap", "maxCap", "firmCuts")
PATHS = ("redwood", "baja")

# Baja delivery points -> (affected_label, raw capacity field on the pipelineCap/
# maxCap/firmCuts rows). Only Topock is a metered border point that joins
# dim_location (baja_elpaso / baja_transw, segments pr_elpaso_cgt / pr_transw_cgt);
# point_id is kept null in v1 (the orchestrator links primary_point_id).
BAJA_POINTS: tuple[tuple[str, str], ...] = (
    ("Kettleman", "bajaKettlemanCapacity"),
    ("Hinkley", "bajaHinkleyCapacity"),
    ("Topock", "bajaTopockCapacity"),
)


def _num(value: Any) -> Optional[float]:
    """Parse a foghorn numeric / '99.0%' / '0%' cell to float (None-safe)."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    m = re.search(r"-?\d+(?:\.\d+)?", str(value))
    return float(m.group(0)) if m else None


def _strip_notes(html: Optional[str]) -> list[str]:
    """Strip the MaintenanceNotes HTML into a list of <p> text items."""
    if not html:
        return []
    items = re.findall(r"<p[^>]*>(.*?)</p>", html, flags=re.IGNORECASE | re.DOTALL)
    if not items:  # no <p> wrappers: split on <br>/newlines
        items = re.split(r"<br\s*/?>|\r?\n", html)
    out: list[str] = []
    for it in items:
        txt = re.sub(r"<[^>]+>", "", it)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            out.append(txt)
    return out


def _index(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index a view's rows by their literal ``Dates`` string (the cross-view key)."""
    return {r["Dates"]: r for r in rows if r.get("Dates") is not None}


def _impact(
    *,
    affected_label: str,
    remaining_mmcf: Optional[float],
    pct_of_max: Optional[float],
    pct_firm_cut: Optional[float],
    work_description: str,
    date_start: Optional[str],
    date_end: Optional[str],
    pulled_at: str,
    source: str,
) -> MaintenanceImpact:
    """Build one MaintenanceImpact from a merged (path/point, Dates) foghorn row.

    capacity is REMAINING (capacity_basis='remaining'); ``maxCap`` is a TRUE
    remaining/max %, so the unconstrained base is back-computed from it and the
    reduction follows as base - remaining.
    """
    remaining_dthd = to_dth(remaining_mmcf, UNITS)
    base_dthd: Optional[float] = None
    if remaining_dthd is not None and pct_of_max:
        base_dthd = round(remaining_dthd / (pct_of_max / 100.0), 1)
    reduction_dthd = (
        round(base_dthd - remaining_dthd, 1)
        if base_dthd is not None and remaining_dthd is not None
        else None
    )
    return MaintenanceImpact(
        source=source,
        maintenance_id=f"{source}:foghorn:{affected_label}:{date_start}:{date_end}",
        notice_id=None,
        point_id=None,
        segment_or_gate=None,
        affected_label=affected_label,
        join_kind="path",
        date_start=date_start or "",
        date_end=date_end,
        capacity_basis="remaining",
        capacity_remaining_dthd=remaining_dthd,
        reduction_dthd=reduction_dthd,
        base_capacity_dthd=base_dthd,
        pct_of_capacity=pct_of_max,
        pct_firm_cut=pct_firm_cut,
        reduction_planned_dthd=None,
        reduction_fm_dthd=None,
        original_value=remaining_mmcf,
        original_units=UNITS,
        restriction_type=None,
        work_description=work_description,
        is_unplanned=False,
        pulled_at_utc=pulled_at,
    )


def parse_foghorn(
    views_by_key: dict[str, list[dict[str, Any]]],
    gas_day: str,
    pulled_at: str,
    *,
    source: str = SOURCE,
) -> list[MaintenanceImpact]:
    """Merge the 3 foghorn views per (path, ``Dates``) row into MaintenanceImpact rows.

    ``views_by_key`` maps ``"<path>_<view>"`` (e.g. ``"redwood_pipelineCap"``) to the
    raw JSON array for that (path, view). Redwood yields one path row per Dates;
    Baja yields three (Kettleman, Hinkley, Topock). Year-less ``Dates`` are anchored
    on ``gas_day``.
    """
    anchor = dt.date.fromisoformat(gas_day)
    out: list[MaintenanceImpact] = []

    # -- Redwood: one row per Dates ---------------------------------------- #
    rw_cap = _index(views_by_key.get("redwood_pipelineCap", []))
    rw_max = _index(views_by_key.get("redwood_maxCap", []))
    rw_cut = _index(views_by_key.get("redwood_firmCuts", []))
    for dates, caprow in rw_cap.items():
        start, end = parse_monthday_range(dates, anchor)
        items = _strip_notes(caprow.get("MaintenanceNotes"))
        out.append(
            _impact(
                affected_label="Redwood Path",
                remaining_mmcf=_num(caprow.get("Capacity")),
                pct_of_max=_num(rw_max.get(dates, {}).get("Capacity")),
                pct_firm_cut=_num(rw_cut.get(dates, {}).get("Capacity")),
                work_description="; ".join(items),
                date_start=start,
                date_end=end,
                pulled_at=pulled_at,
                source=source,
            )
        )

    # -- Baja: three points per Dates -------------------------------------- #
    bj_cap = _index(views_by_key.get("baja_pipelineCap", []))
    bj_max = _index(views_by_key.get("baja_maxCap", []))
    bj_cut = _index(views_by_key.get("baja_firmCuts", []))
    for dates, caprow in bj_cap.items():
        start, end = parse_monthday_range(dates, anchor)
        items = _strip_notes(caprow.get("MaintenanceNotes"))
        work = "; ".join(items)
        maxrow = bj_max.get(dates, {})
        cutrow = bj_cut.get(dates, {})
        for label, key in BAJA_POINTS:
            out.append(
                _impact(
                    affected_label=label,
                    remaining_mmcf=_num(caprow.get(key)),
                    pct_of_max=_num(maxrow.get(key)),
                    pct_firm_cut=_num(cutrow.get(key)),
                    work_description=work,
                    date_start=start,
                    date_end=end,
                    pulled_at=pulled_at,
                    source=source,
                )
            )
    return out


def parse_flow_orders(
    payload: Any,
    notice_type: str,
    gas_day: str,
    pulled_at: str,
    *,
    source: str = SOURCE,
) -> list[NoticeEvent]:
    """OFO/EFO ``ofoefoarchive`` rows -> NoticeEvent (one per order).

    ``notice_type`` is ``"ofo"`` or ``"efo"``; severity/category come from the shared
    classifier. The order's ``gasDay`` is both the gas day and effective span.
    """
    out: list[NoticeEvent] = []
    for item in payload or []:
        order_day = norm_date(item.get("gasDay")) or str(item.get("gasDay", "")) or None
        desc = str(item.get("typeDesc", notice_type.upper())).strip()
        reason = str(item.get("reason", "")).strip()
        headline = f"{desc} — {reason}".strip(" —")
        out.append(
            NoticeEvent(
                source=source,
                notice_id=f"{source}:{notice_type}:{order_day}",
                notice_type=notice_type,
                notice_type_raw=str(item.get("typeShortName") or notice_type.upper()),
                severity=severity_for(notice_type),
                category=category_for(notice_type),
                posted_at_utc=None,
                effective_start=order_day,
                effective_end=order_day,
                status=None,
                prior_notice_id=None,
                is_current=True,
                has_capacity_impact=False,
                primary_point_id=None,
                affects_pge=True,
                headline=headline,
                body=headline,
                url=NOTICE_URL,
                gas_day=order_day or gas_day,
                pulled_at_utc=pulled_at,
            )
        )
    return out


def _fetch_foghorn(
    session: Any,
    raw_dir: Optional[pathlib.Path],
) -> dict[str, list[dict[str, Any]]]:
    """POST the foghorn feed for both paths x three views (the only additive fetch).

    Reuses the frozen ``PipeRangerClient`` session/headers; the foghorn POST is a
    maintenance-only endpoint the ebb client doesn't carry, so it lives here.
    """
    import requests  # local import: offline tests monkeypatch this fetch out

    sess = session or requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Referer": PAGE_URL,
        "Origin": "https://www.pge.com",
    }
    views_by_key: dict[str, list[dict[str, Any]]] = {}
    for path in PATHS:
        for view in VIEWS:
            resp = sess.post(
                FOGHORN_URL,
                data={"dropDownVal": view, "maintananceVal": path, "pagePath": PAGE_PATH},
                headers=headers,
                timeout=40,
            )
            resp.raise_for_status()
            write_raw(raw_dir, f"foghorn_{path}_{view}.json", resp.text)
            views_by_key[f"{path}_{view}"] = json.loads(resp.text)
    return views_by_key


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    pulled_at = utc_now_iso()

    views_by_key = _fetch_foghorn(session, raw_dir)
    impacts = parse_foghorn(views_by_key, gas_day, pulled_at)

    notices: list[NoticeEvent] = []
    client = PipeRangerClient(data_dir=(raw_dir or pathlib.Path("data/pipe_ranger")), session=session)
    for notice_type in ("ofo", "efo"):
        try:
            payload = client.fetch(notice_type, raw_dir=raw_dir)
        except Exception:  # noqa: BLE001 - archive often empty/unavailable; degrade gracefully
            payload = []
        notices += parse_flow_orders(payload, notice_type, gas_day, pulled_at)

    return notices, impacts
