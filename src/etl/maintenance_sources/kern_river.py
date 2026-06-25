"""Kern River maintenance -> NoticeEvent rows (no MaintenanceImpact).

Kern River (BHE) posts its operational-constraint signal as **notices**, not a
structured capacity feed. The relevant category is **Critical / "Pipe Cond"**
(Pipeline Conditions — line-pack high/low/returned-to-normal advisories); the
``Planned-Service-Outage`` grid is the only true scheduled-outage category but is
almost always empty (header + one blank row). These notices carry **no numeric
capacity** anywhere — the grid has no capacity column and the bodies are
qualitative line-pack prose ("may be forced to curtail previously scheduled
quantities for Gas Day 8 ... ID1/ID2 nomination cycles") — so this source emits
``NoticeEvent`` rows **only** and ZERO ``MaintenanceImpact`` (returns an empty
impacts list).

Kern River's *quantified* capacity lives in the separate OAC posting handled by the
frozen ``src/ebb/kern_river.py`` client (Design/Operating/Operationally-Available +
scheduled qty per location), not in notices.

Fetch reuses the frozen ``KernRiverClient`` (``fetch_notice_category`` + the grid
helpers ``_find_data_table`` / ``_grid_rows``); parsing is here. Reference recon:
``exploration/extract/kern_river_maint.py``.
"""
from __future__ import annotations

import pathlib
from typing import Any, Optional

from bs4 import BeautifulSoup

from ebb.kern_river import BASE_URL, NOTICE_PATH, KernRiverClient
from ebb.schema import MaintenanceImpact, NoticeEvent, norm_date, utc_now_iso

from etl.maintenance import category_for, severity_for

SOURCE = "kern_river"
NOTICE_TYPE = "advisory"          # Pipe Cond / line-pack -> §5 advisory
# Categories that carry the operational-constraint signal. Planned-Service-Outage
# is the only true scheduled-outage grid (usually empty); Critical holds the
# line-pack "Pipe Cond" advisories.
CATEGORIES: tuple[str, ...] = ("Critical", "Planned-Service-Outage")

# Grid NoticeStatus code -> NAESB status word (1=initiate, 2=supersede, 3=terminate).
STATUS_BY_CODE = {"1": "initiate", "2": "supersede", "3": "terminate"}


def _clean(v: Any) -> str:
    return str(v).strip() if v is not None else ""


def _none_if_blank(v: Any) -> Optional[str]:
    return _clean(v) or None


def category_url(category: str) -> str:
    return f"{BASE_URL}{NOTICE_PATH.format(category=category)}"


def parse_notice_grid(
    html: str,
    category: str,
    pulled_at: str,
    *,
    source: str = SOURCE,
    client: Optional[KernRiverClient] = None,
) -> list[NoticeEvent]:
    """One ``NoticeEvent`` per real grid row in a Kern notices category page.

    Reuses the frozen client's grid helpers. Emits no capacity (these advisories
    carry none); ``has_capacity_impact`` stays False and ``is_current=True`` (the
    orchestrator resolves supersession + capacity linkage downstream).
    """
    cli = client or KernRiverClient()
    soup = BeautifulSoup(html, "lxml")
    tbl = cli._find_data_table(soup, ("NoticeIdentifier", "Subject"))
    if tbl is None:
        return []
    url = category_url(category)
    severity = severity_for(NOTICE_TYPE)
    category_norm = category_for(NOTICE_TYPE)
    out: list[NoticeEvent] = []
    for row in cli._grid_rows(tbl):
        notice_id = _clean(row.get("NoticeIdentifier"))
        subject = _clean(row.get("Subject"))
        if not notice_id and not subject:      # skip the empty placeholder row
            continue
        eff_start = norm_date(row.get("Notice EffectiveDate/Time"))
        eff_end = norm_date(row.get("Notice EndDate/Time"))
        status = STATUS_BY_CODE.get(_clean(row.get("NoticeStatus")))
        out.append(
            NoticeEvent(
                source=source,
                notice_id=notice_id,
                notice_type=NOTICE_TYPE,
                notice_type_raw=_clean(row.get("Notice Type")),
                severity=severity,
                category=category_norm,
                # PostedDate/Time is the portal's (Pacific) posting stamp; we keep the
                # ISO date. pulled_at carries the UTC truth for this run.
                posted_at_utc=norm_date(row.get("PostedDate/Time")),
                effective_start=eff_start,
                effective_end=eff_end,
                status=status,
                prior_notice_id=_none_if_blank(row.get("PriorNoticeIdentifier")),
                is_current=True,                 # orchestrator overwrites via supersession
                has_capacity_impact=False,       # no numeric capacity in Kern notices
                primary_point_id=None,           # orchestrator links
                affects_pge=True,                # system-wide line pack feeds PG&E (Daggett/Fremont Peak)
                headline=subject,
                body=subject,                    # grid carries no body; subject is the line
                url=url,
                gas_day=eff_start or pulled_at[:10],
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
    """Fetch the Kern notices categories, parse to NoticeEvents. No impacts."""
    pulled_at = utc_now_iso()
    client = KernRiverClient(
        data_dir=(raw_dir or pathlib.Path("data/kern_river")), session=session
    )
    notices: list[NoticeEvent] = []
    seen: set[str] = set()
    for category in CATEGORIES:
        html = client.fetch_notice_category(category, raw_dir=raw_dir)
        for ev in parse_notice_grid(html, category, pulled_at, client=client):
            if ev.notice_id and ev.notice_id in seen:
                continue
            if ev.notice_id:
                seen.add(ev.notice_id)
            notices.append(ev)
    return notices, []
