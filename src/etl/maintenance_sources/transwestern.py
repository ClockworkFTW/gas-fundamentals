"""Transwestern (Energy Transfer iPost) maintenance -> NoticeEvent + MaintenanceImpact.

Transwestern feeds PG&E at **Topock** (loc 56698). Maintenance/capacity-impact
detail lives on the per-notice DETAIL page (plain GET, no auth):

    https://twtransfer.energytransfer.com/ipost/notice/show/<id>?asset=TW

The 3 category CSVs (critical / non-critical / planned-service-outage) carry only
metadata + subject. We select the maintenance-relevant notices from them — every
**Planned Service Outage** row, plus any **Capacity Constraint** / **Operational
Alert** whose subject *names* a maintenance — then GET each detail page and parse the
labelled ``<table>`` (Notice Type Description, Notice Identifier, Notice Status
Description, Posting/Effective/End Date-Time, Subject, Notice Text, Reason, Location).

The capacity transition is in PROSE, e.g. "reduced from 750,000 MMBtu/d to 650,000
MMBtu/d"; we regex from/to (MMBtu/d == Dth/d). ``from`` is the true unconstrained
capacity, so ``base_capacity_dthd`` + ``pct_of_capacity`` are consistent. The affected
Location is an upstream segment (Station 9 / East Mainline), NOT the Topock delivery —
no numeric loc id is exposed, so ``join_kind="text_label"`` and ``point_id=None``.

Fetch reuses the frozen ``TranswesternClient`` (category CSVs) and adds only the small
additive detail-page GET; parsing is here. Reference:
``exploration/extract/transwestern_maint.py``.
"""
from __future__ import annotations

import csv
import io
import pathlib
import re
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

from ebb.schema import (
    MaintenanceImpact,
    NoticeEvent,
    collapse_ws,
    to_float,
    utc_now_iso,
)
from ebb.transwestern import (
    NOTICE_CATEGORIES,
    NOTICE_DETAIL_URL,
    USER_AGENT,
    TranswesternClient,
)

from etl.maintenance import category_for, pct_of_capacity, severity_for, to_dth

SOURCE = "transwestern"
UNITS = "MMBtu/d"
PGE_TOPOCK_LOC = "56698"

# Notice Type values that warrant a detail fetch *only when the subject names a
# maintenance event*. (Planned Service Outage is always included regardless.)
CONDITIONAL_TYPES = {"capacity constraint", "operational alert"}
# Words in the subject that signal an actual maintenance (vs a routine daily cut).
MAINT_SUBJECT_RE = re.compile(
    r"maintenance|outage|pigging|\bpig\b|repair|inspection|compressor|"
    r"hydrotest|hydro\s*test|station\s*work|planned\s*(?:service|work)",
    re.IGNORECASE,
)

_NUM = r"[\d,]+(?:\.\d+)?"
_UNIT = r"MMBtu/d|MMBtu/D|Dth/d|Dth/D|MMcf/d|MMcf/D|MMBtu|Dth"
# "reduced from 750,000 MMBtu/d to 650,000 MMBtu/d"
# "increase from 650,000 MMBtu/d to the original capacity of 750,000 MMBtu/d"
FROM_TO_RE = re.compile(
    rf"from\s+(?P<from>{_NUM})\s*(?P<unit_from>{_UNIT})?\s+to\s+"
    rf"(?:the\s+original\s+capacity\s+of\s+)?(?P<to>{_NUM})\s*(?P<unit_to>{_UNIT})?",
    re.IGNORECASE,
)

# Notice Status Description -> normalized status vocab (initiate|supersede|terminate|active).
_STATUS_MAP = {
    "initiate": "initiate",
    "supersede": "supersede",
    "terminate": "terminate",
    "complete": "terminate",
    "cancel": "terminate",
}


# --------------------------------------------------------------------------- #
# Pure parse helpers
# --------------------------------------------------------------------------- #


def _parse_dt(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """iPost 'Jun 16 2026  9:00AM' -> (ISO date, 'YYYY-MM-DD HH:MM').

    iPost stamps are 'Mon DD YYYY  H:MMAM' — a format ``norm_date`` doesn't cover,
    so parse via dateutil (as the frozen ebb client does).
    """
    s = (value or "").strip()
    if not s:
        return None, None
    try:
        from dateutil import parser as dtparser

        parsed = dtparser.parse(s)
    except (ValueError, OverflowError):
        return None, None
    return parsed.date().isoformat(), parsed.strftime("%Y-%m-%d %H:%M")


def _norm_status(value: Optional[str]) -> Optional[str]:
    s = collapse_ws(value).lower()
    if not s:
        return None
    for key, mapped in _STATUS_MAP.items():
        if key in s:
            return mapped
    return "active"


def parse_detail(html: str) -> dict[str, str]:
    """Map each labelled top-level ``<td>`` in the notice table to its value text."""
    soup = BeautifulSoup(html, "lxml")
    fields: dict[str, str] = {}
    for row in soup.select("table tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        raw_label = collapse_ws(cells[0].get_text(separator=" "))
        key = raw_label.rstrip(":").strip()
        if not key:
            continue
        # The Notice Text label cell stacks two labels ("Notice Text:" +
        # "Nominated Volume Affected"); key it on the first line.
        if raw_label.startswith("Notice Text"):
            key = "Notice Text"
        fields[key] = collapse_ws(cells[1].get_text(separator=" "))
    return fields


def extract_capacity(notice_text: str) -> dict[str, Any]:
    """Regex the capacity transition out of the prose; return the chosen cut.

    Picks the transition with the largest drop (to < from) — the headline reduction
    — over recovery transitions; falls back to the first match.
    """
    transitions: list[dict[str, Any]] = []
    for m in FROM_TO_RE.finditer(notice_text or ""):
        unit = m.group("unit_from") or m.group("unit_to")
        transitions.append(
            {
                "from_capacity": to_float(m.group("from")),
                "to_capacity": to_float(m.group("to")),
                "units": unit,
                "literal": collapse_ws(m.group(0)),
            }
        )
    out: dict[str, Any] = {
        "from_capacity": None,
        "to_capacity": None,
        "units": None,
        "literal": None,
    }
    if not transitions:
        return out
    cuts = [
        t for t in transitions
        if t["from_capacity"] is not None and t["to_capacity"] is not None
        and t["to_capacity"] < t["from_capacity"]
    ]
    chosen = (
        max(cuts, key=lambda t: t["from_capacity"] - t["to_capacity"])
        if cuts else transitions[0]
    )
    out.update(chosen)
    return out


def _notice_type_for(detail_type: str, select_reason: Optional[str]) -> str:
    """Normalized §5 notice_type. Planned Service Outage -> planned_outage; else maintenance."""
    t = (detail_type or "").lower()
    if "planned service outage" in t or (select_reason or "").startswith("planned"):
        return "planned_outage"
    return "maintenance"


def parse_notice(
    html: str,
    *,
    notice_id: str,
    select_reason: Optional[str] = "planned-service-outage",
    pulled_at: str,
    units: str = UNITS,
    source: str = SOURCE,
) -> tuple[NoticeEvent, list[MaintenanceImpact]]:
    """Parse one detail page into a NoticeEvent header + its MaintenanceImpact line(s)."""
    fields = parse_detail(html)
    cap = extract_capacity(fields.get("Notice Text", ""))

    detail_type = fields.get("Notice Type Description") or ""
    identifier = fields.get("Notice Identifier") or notice_id
    subject = fields.get("Subject") or ""
    reason = fields.get("Reason") or None
    location = fields.get("Location") or None
    notice_type = _notice_type_for(detail_type, select_reason)
    raw_type = detail_type or notice_type
    status = _norm_status(fields.get("Notice Status Description"))
    _, posted_at = _parse_dt(fields.get("Posting Date/Time"))
    eff_start, _ = _parse_dt(fields.get("Notice Effective Date/Time"))
    eff_end, _ = _parse_dt(fields.get("Notice End Date/Time"))
    url = NOTICE_DETAIL_URL.format(notice_id=notice_id)

    affects_pge = bool(location and "topock" in location.lower())

    event = NoticeEvent(
        source=source,
        notice_id=identifier,
        notice_type=notice_type,
        notice_type_raw=raw_type,
        severity=severity_for(notice_type),
        category=category_for(notice_type),
        posted_at_utc=posted_at,
        effective_start=eff_start,
        effective_end=eff_end,
        status=status,
        prior_notice_id=None,
        is_current=True,           # orchestrator overwrites via supersession
        has_capacity_impact=False,  # orchestrator links from impacts
        primary_point_id=None,     # orchestrator links
        affects_pge=affects_pge,
        headline=subject,
        body=fields.get("Notice Text", ""),
        url=url,
        gas_day=eff_start or "",
        pulled_at_utc=pulled_at,
    )

    impacts: list[MaintenanceImpact] = []
    from_cap = cap["from_capacity"]
    to_cap = cap["to_capacity"]
    if from_cap is not None and to_cap is not None:
        orig_units = cap["units"] or units
        # ``from`` is the true original/unconstrained capacity -> base + pct consistent.
        base_dth = to_dth(from_cap, orig_units)
        remaining_dth = to_dth(to_cap, orig_units)
        reduction_dth = (
            round(base_dth - remaining_dth, 1)
            if base_dth is not None and remaining_dth is not None else None
        )
        impacts.append(
            MaintenanceImpact(
                source=source,
                maintenance_id=f"{source}:{identifier}",
                notice_id=identifier,
                point_id=None,                  # no numeric loc id on the detail page
                segment_or_gate=None,
                affected_label=location or subject or identifier,
                join_kind="text_label",         # upstream Station 9 / East Mainline label
                date_start=eff_start or "",
                date_end=eff_end,
                capacity_basis="remaining",
                capacity_remaining_dthd=remaining_dth,
                reduction_dthd=reduction_dth,
                base_capacity_dthd=base_dth,
                pct_of_capacity=pct_of_capacity(remaining_dth, base_dth),
                pct_firm_cut=None,
                reduction_planned_dthd=None,
                reduction_fm_dthd=None,
                original_value=to_cap,
                original_units=orig_units,
                restriction_type=reason,
                work_description=subject,
                is_unplanned=False,             # Planned Service Outage
                pulled_at_utc=pulled_at,
            )
        )
    return event, impacts


# --------------------------------------------------------------------------- #
# Selection from the category CSVs
# --------------------------------------------------------------------------- #


def select_maintenance_notices(category_texts: dict[str, str]) -> list[dict[str, str]]:
    """Scan the category CSVs; return the maintenance-relevant rows (deduped by id)."""
    selected: dict[str, dict[str, str]] = {}
    for category, text in category_texts.items():
        for row in csv.DictReader(io.StringIO(text or "")):
            ntype = (row.get("Notice Type") or "").strip()
            subject = (row.get("Subject") or "").strip()
            notice_id = (row.get("Notice ID") or "").strip()
            if not notice_id:
                continue
            ntype_l = ntype.lower()
            reason: Optional[str] = None
            if "planned service outage" in ntype_l or category == "planned-service-outage":
                reason = "planned-service-outage"
            elif ntype_l in CONDITIONAL_TYPES and MAINT_SUBJECT_RE.search(subject):
                reason = f"{ntype_l}+maintenance-subject"
            if reason and notice_id not in selected:
                selected[notice_id] = {
                    "notice_id": notice_id,
                    "category": category,
                    "csv_notice_type": ntype,
                    "csv_subject": subject,
                    "select_reason": reason,
                }
    return list(selected.values())


# --------------------------------------------------------------------------- #
# Fetch (additive detail GET) + build
# --------------------------------------------------------------------------- #


def fetch_detail_html(
    notice_id: str, session: requests.Session, *, raw_dir: Optional[pathlib.Path] = None
) -> str:
    """GET the per-notice detail page (the small additive fetch a maintenance feed needs)."""
    url = NOTICE_DETAIL_URL.format(notice_id=notice_id)
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
    resp.raise_for_status()
    if raw_dir is not None:
        raw_dir.mkdir(parents=True, exist_ok=True)
        (raw_dir / f"detail_{notice_id}.html").write_text(resp.text, encoding="utf-8")
    return resp.text


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    pulled_at = utc_now_iso()
    client = TranswesternClient(
        data_dir=(raw_dir or pathlib.Path("data/transwestern")), session=session
    )
    category_texts = {
        category: client.fetch_notice_category(category, raw_dir=raw_dir)
        for category in NOTICE_CATEGORIES
    }
    selected = select_maintenance_notices(category_texts)

    detail_session = session or requests.Session()
    notices: list[NoticeEvent] = []
    impacts: list[MaintenanceImpact] = []
    for sel in sorted(selected, key=lambda s: s["notice_id"]):
        html = fetch_detail_html(sel["notice_id"], detail_session, raw_dir=raw_dir)
        event, line_impacts = parse_notice(
            html,
            notice_id=sel["notice_id"],
            select_reason=sel.get("select_reason"),
            pulled_at=pulled_at,
        )
        notices.append(event)
        impacts.extend(line_impacts)
    return notices, impacts
