"""El Paso Natural Gas (EPNG / Kinder Morgan, Topock) maintenance.

EPNG posts planned maintenance as monthly **MAINTENANCE** notices on the KM
``pipeline2.kindermorgan.com`` portal. Each notice's DETAIL page (plain GET) embeds
a Word-exported HTML **"Maintenance List"** summary table — the structured,
per-location, per-date-span capacity-impact rows, already in **Dth/d** (canonical).
That table is the richest maintenance feed of the set: each row carries a true
unconstrained Base Capacity, the Total Reduction (split into PLM = planned and
FMJ = force-majeure), and the resulting Net (remaining) capacity.

This source emits one ``NoticeEvent`` per MAINTENANCE notice (header grain) and one
``MaintenanceImpact`` per Maintenance List row. Fetch reuses the frozen
``ElPasoClient`` for ``USER_AGENT`` / ``NOTICE_DETAIL_URL`` and adds only the small
additive detail-page GET a maintenance feed needs. Reference recon:
``exploration/extract/el_paso_maint.py``.
"""
from __future__ import annotations

import pathlib
import re
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup, Tag

from ebb.el_paso import NOTICE_DETAIL_URL, USER_AGENT, ElPasoClient
from ebb.schema import (
    MaintenanceImpact,
    NoticeEvent,
    collapse_ws,
    norm_date,
    to_float,
    utc_now_iso,
)

from etl.maintenance import category_for, pct_of_capacity, severity_for, to_dth

SOURCE = "el_paso"
TSP = "EPNG"
UNITS = "Dth/d"          # the Maintenance List table is native Dth/d (canonical)
NOTICE_TYPE = "maintenance"

# Maintenance List table column order (per the detail HTML header row).
LIST_HEADERS = [
    "Start Date", "End Date", "Region", "Scheduling Location", "Maintenance List",
    "Base Capacity Dthd", "Total Reduction Dthd", "PLM Reduction Dthd",
    "FMJ Reduction Dthd", "Net Dthd", "Update",
]
_N_COLS = len(LIST_HEADERS)

# Header label -> the <span id$=> suffix that holds its value, in the notice header.
HEADER_FIELDS = {
    "tsp": "_lblTSP",
    "critical": "_lblCritical",
    "notice_type_desc_1": "_lblType",
    "notice_type_desc_2": "_lblSubType",
    "eff": "_lblEffDate",
    "end": "_lblEndDate",
    "posted": "_lblPostDate",
    "notice_id": "_lblID",
    "notice_stat": "_lblStatus",
    "prior_notice": "_lblPriorNotice",
    "subject": "_lblSubject",
}


# --------------------------------------------------------------------------- #
# small cell helpers (date/number parse mirrors the recon extractor)
# --------------------------------------------------------------------------- #
def _clean(text: Any) -> str:
    """Collapse whitespace/NBSP runs to single spaces; strip."""
    return collapse_ws((text or "").replace("\xa0", " ") if isinstance(text, str) else text)


def _cell_multiline(td: Tag) -> str:
    """Cell text preserving <br>/<p> line breaks (the multi-line description column)."""
    for br in td.find_all("br"):
        br.replace_with("\n")
    lines = [_clean(ln) for ln in td.get_text("\n").split("\n")]
    return "\n".join(ln for ln in lines if ln)


def _parse_number(text: str) -> Optional[float]:
    """Parse a Dthd cell: '2,223,400', '50,000', '-' (zero), '' -> float|None."""
    t = _clean(text)
    if t in ("", "-", "–", "—"):
        return 0.0 if t in ("-", "–", "—") else None
    m = re.search(r"-?\d+(?:\.\d+)?", t.replace(",", ""))
    return float(m.group(0)) if m else None


def _split_scheduling_location(raw: str) -> tuple[str, Optional[str]]:
    """Return (label, loc_id). Handles '57419', '57419 (IKINGLAN)', 'NORTH ML'.

    loc_id is the leading numeric loc id, else a standalone 4+ digit number anywhere
    (the join key to ``fact_operational.point_id``); None for a segment name.
    """
    label = _clean(raw)
    m = re.match(r"^(\d{3,})\b", label)
    if m:
        return label, m.group(1)
    m2 = re.search(r"\b(\d{4,})\b", label)
    return label, (m2.group(1) if m2 else None)


# --------------------------------------------------------------------------- #
# header
# --------------------------------------------------------------------------- #
def parse_header(soup: BeautifulSoup) -> dict[str, Optional[str]]:
    """Pull the notice header fields (Notice Type, Critical, Eff/End, Post,
    Notice Stat, Prior Notice, Subject) from the detail's <span id$=...> labels."""
    out: dict[str, Optional[str]] = {}
    for key, suffix in HEADER_FIELDS.items():
        span = soup.find("span", id=lambda v, s=suffix: bool(v) and v.endswith(s))
        out[key] = _clean(span.get_text()) if span else None
    return out


# --------------------------------------------------------------------------- #
# maintenance list table
# --------------------------------------------------------------------------- #
def find_maint_list_table(soup: BeautifulSoup) -> Optional[Tag]:
    """Return the <table> whose first row is the Maintenance List header.

    Identified by a row carrying both 'Start Date' and 'Net Dthd'/'Scheduling
    Location' header cells (the detail body holds several tables — calendar grid,
    utilization, then this list).
    """
    for table in soup.find_all("table"):
        first_row = table.find("tr")
        if not first_row:
            continue
        joined = " | ".join(_clean(td.get_text(" ")) for td in first_row.find_all("td"))
        if "Start Date" in joined and ("Net Dthd" in joined or "Scheduling Location" in joined):
            return table
    return None


def _data_rows(table: Tag) -> list[list[Tag]]:
    """Yield the table's data rows (those with the full 11-column layout, header skipped)."""
    out: list[list[Tag]] = []
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) != _N_COLS:
            tds = tr.find_all("td")
        if len(tds) != _N_COLS:
            continue
        first = _clean(tds[0].get_text(" "))
        if first == "Start Date" or ("Start" in first and "Date" in first):
            continue  # header row
        if norm_date(first) is None and norm_date(_clean(tds[1].get_text(" "))) is None:
            continue  # not a dated data row
        out.append(tds)
    return out


def parse_maint_impacts(
    html: str,
    header: dict[str, Optional[str]],
    pulled_at: str,
    *,
    source: str = SOURCE,
) -> list[MaintenanceImpact]:
    """One ``MaintenanceImpact`` per Maintenance List row in a notice detail.

    Capacity is native Dth/d: Base = the location's true unconstrained base,
    Total Reduction = capacity removed (PLM planned + FMJ force-majeure), Net =
    remaining (base - total reduction). ``capacity_basis='reduction'``;
    ``pct_of_capacity`` is strictly Net/Base*100. ``join_kind`` is ``point_id`` when
    the Scheduling Location is/contains a bare loc number, else ``segment_name``.
    """
    soup = BeautifulSoup(html, "lxml")
    table = find_maint_list_table(soup)
    if table is None:
        return []
    notice_id = header.get("notice_id")
    out: list[MaintenanceImpact] = []
    for i, tds in enumerate(_data_rows(table)):
        cells = [_clean(td.get_text(" ")) for td in tds]
        date_start = norm_date(cells[0])
        date_end = norm_date(cells[1])
        label, loc_id = _split_scheduling_location(cells[3])
        desc = _cell_multiline(tds[4])
        base = _parse_number(cells[5])
        total_reduction = _parse_number(cells[6])
        plm = _parse_number(cells[7])
        fmj = _parse_number(cells[8])
        net = _parse_number(cells[9])
        is_numeric = loc_id is not None
        out.append(
            MaintenanceImpact(
                source=source,
                maintenance_id=f"{source}:{notice_id}:{i}",
                notice_id=notice_id,
                point_id=loc_id,
                segment_or_gate=None if is_numeric else (label or None),
                affected_label=label,
                join_kind="point_id" if is_numeric else "segment_name",
                date_start=date_start or (pulled_at[:10]),
                date_end=date_end,
                capacity_basis="reduction",
                capacity_remaining_dthd=to_dth(net, UNITS),
                reduction_dthd=to_dth(total_reduction, UNITS),
                base_capacity_dthd=to_dth(base, UNITS),
                pct_of_capacity=pct_of_capacity(to_dth(net, UNITS), to_dth(base, UNITS)),
                pct_firm_cut=None,
                reduction_planned_dthd=to_dth(plm, UNITS),
                reduction_fm_dthd=to_dth(fmj, UNITS),
                original_value=total_reduction,
                original_units=UNITS,
                restriction_type=_clean(cells[2]) or None,   # Region
                work_description=desc,
                is_unplanned=bool(fmj and fmj > 0),
                pulled_at_utc=pulled_at,
            )
        )
    return out


def parse_notice_event(
    header: dict[str, Optional[str]],
    pulled_at: str,
    *,
    source: str = SOURCE,
) -> NoticeEvent:
    """Build the per-notice ``NoticeEvent`` (header grain) from the detail header."""
    notice_id = header.get("notice_id") or ""
    eff_start = norm_date(header.get("eff"))
    eff_end = norm_date(header.get("end"))
    notice_type_raw = header.get("notice_type_desc_1") or ""
    status = (header.get("notice_stat") or "").strip().lower() or None
    subject = header.get("subject") or ""
    critical = (header.get("critical") or "").strip().upper() == "Y"
    body = (
        f"[{notice_type_raw} / {header.get('notice_type_desc_2') or ''}] "
        f"Effective {header.get('eff')} through {header.get('end')}. {subject}"
    ).strip()
    return NoticeEvent(
        source=source,
        notice_id=notice_id,
        notice_type=NOTICE_TYPE,
        notice_type_raw=notice_type_raw,
        severity="high" if critical else severity_for(NOTICE_TYPE),
        category=category_for(NOTICE_TYPE),
        posted_at_utc=norm_date(header.get("posted")),
        effective_start=eff_start,
        effective_end=eff_end,
        status=status,
        prior_notice_id=(header.get("prior_notice") or "").strip() or None,
        is_current=True,                 # orchestrator overwrites via supersession
        has_capacity_impact=False,       # orchestrator links from the impact rows
        primary_point_id=None,           # orchestrator links
        affects_pge=True,                # EPNG feeds PG&E at Topock
        headline=subject,
        body=body,
        url=NOTICE_DETAIL_URL.format(notice_id=notice_id),
        gas_day=eff_start or (pulled_at[:10]),
        pulled_at_utc=pulled_at,
    )


# --------------------------------------------------------------------------- #
# fetch (additive detail-page GET — request logic mirrors the frozen client)
# --------------------------------------------------------------------------- #
def parse_grid(html: str) -> list[dict[str, Any]]:
    """Parse the EPNG notices grid (plain GET) into leaf rows.

    Mirrors the frozen client's notices-grid parse: leaf <tr> rows whose cells carry
    a 6-digit Notice ID preceded by Type1/Type2/PostDT/EffDT/EndDT and followed by
    Subject. Deduped by Notice ID. Used to find the MAINTENANCE notices to detail.
    """
    soup = BeautifulSoup(html, "lxml")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for tr in soup.find_all("tr"):
        if tr.find("tr"):  # skip non-leaf (nested) rows
            continue
        cells = [c for c in (td.get_text(" ", strip=True) for td in tr.find_all("td")) if c]
        ids = [i for i, c in enumerate(cells) if re.fullmatch(r"\d{6}", c)]
        if not ids:
            continue
        k = ids[0]
        if k < 5 or len(cells) < k + 2:
            continue
        if not re.search(r"\d{1,2}/\d{1,2}/\d{4}", cells[k - 3]):  # PostDT sanity
            continue
        nid = cells[k]
        if nid in seen:
            continue
        seen.add(nid)
        rows.append({
            "notice_type_1": cells[k - 5], "notice_type_2": cells[k - 4],
            "posted": cells[k - 3], "effective": cells[k - 2], "end": cells[k - 1],
            "notice_id": nid, "subject": cells[k + 1],
        })
    return rows


def _maintenance_notice_ids(grid_rows: list[dict[str, Any]]) -> list[str]:
    """Notice ids of the grid rows whose first notice type is MAINTENANCE."""
    return [
        str(r["notice_id"])
        for r in grid_rows
        if (r.get("notice_type_1") or "").strip().upper() == "MAINTENANCE"
        and r.get("notice_id")
    ]


def fetch_detail(notice_id: str, *, session: Any = None, timeout: int = 60) -> str:
    """GET a notice's full DETAIL HTML (the additive fetch a maintenance feed needs).

    Reuses the frozen client's ``USER_AGENT`` and ``NOTICE_DETAIL_URL`` template; no
    change to the client's request logic. A full detail is large (~4MB) — only the
    Maintenance List table is parsed out of it.
    """
    url = NOTICE_DETAIL_URL.format(notice_id=notice_id)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    getter = session.get if session is not None else requests.get
    r = getter(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    """Fetch every MAINTENANCE notice's detail, parse header + Maintenance List.

    The notices grid is fetched live via the frozen ``ElPasoClient.fetch_notices``
    (plain GET) and parsed by ``parse_grid``; each MAINTENANCE notice's detail is
    then GET-ed and parsed. Returns (NoticeEvents, MaintenanceImpacts).
    """
    pulled_at = utc_now_iso()
    client = ElPasoClient(data_dir=(raw_dir or pathlib.Path("data/el_paso")), session=session)
    grid_html = client.fetch_notices(raw_dir=raw_dir)
    grid_rows = parse_grid(grid_html)

    notices: list[NoticeEvent] = []
    impacts: list[MaintenanceImpact] = []
    for notice_id in _maintenance_notice_ids(grid_rows):
        html = fetch_detail(notice_id, session=session)
        header = parse_header(BeautifulSoup(html, "lxml"))
        notices.append(parse_notice_event(header, pulled_at))
        impacts.extend(parse_maint_impacts(html, header, pulled_at))
    return notices, impacts
