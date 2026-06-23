"""Kern River — Kern River Gas Transmission (Berkshire Hathaway Energy) ingestion.

Kern River carries Rockies supply (Opal, WY) to California and feeds PG&E at
**Daggett** and **Fremont Peak** (it also serves SoCalGas at Wheeler Ridge / Kramer
Junction and interconnects with the co-operated Mojave system). Its EBB is BHE's
**Services Portal** at ``services.kernrivergas.com/portal`` — a DotNetNuke ASP.NET
WebForms site, but (unlike El Paso) every posting **renders server-side on a plain
GET**, so no ``__VIEWSTATE`` POST / reCAPTCHA dance is needed:

  * OAC + scheduled:
    ``/Informational-Postings/Capacity/Operationally-Available?gasDay=MM/DD/YYYY``
    → one HTML grid with Design/Operating/Operationally-Available capacity and Total
    Scheduled Quantity (split Kern + Mojave) per location, for the latest posted
    cycle of that gas day. Includes **Daggett - PG&E** and **Fremont Peak - PG&E**.
  * Notices: ``/Informational-Postings/Notices/{Critical|Non-Critical|Planned-Service-Outage}``
    → an HTML notices grid each (Notice Type / Posted / Eff / End / Id / Subject).

Quantities are expressed in **Dth** (MeasBasisDesc) — already canonical Dth/d, no
conversion. The OAC posting has no cycle selector; it shows the latest posted cycle
for the requested gas day (the per-row ``Cycle`` is recorded).

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .schema import FlowRecord, Notice, norm_date, to_float, utc_now_iso

log = logging.getLogger("kern_river")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "kern_river"
BASE_URL = "https://services.kernrivergas.com/portal"
OAC_PATH = "/Informational-Postings/Capacity/Operationally-Available"
NOTICE_PATH = "/Informational-Postings/Notices/{category}"
NOTICE_CATEGORIES = ("Critical", "Non-Critical", "Planned-Service-Outage")
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"
PACIFIC = ZoneInfo("America/Los_Angeles")

FLOW_DIRECTION = {"R": "receipt", "D": "delivery"}  # BD (bidirectional compressor) -> None

# Notice category -> default normalized §5 type; overridden by a few Notice Type strings.
CATEGORY_DEFAULT_TYPE = {
    "Critical": "critical",
    "Planned-Service-Outage": "maintenance",
    "Non-Critical": "other",
}
NOTICE_TYPE_OVERRIDES = (
    ("force maj", "critical"),
    ("capacit", "critical"),
    ("mainten", "maintenance"),
    ("outage", "maintenance"),
    ("ofo", "OFO"),
)
NOTICE_LOOKBACK_DAYS = 3


def _norm(text: str) -> str:
    return " ".join((text or "").split())


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class KernRiverClient:
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/kern_river",
        session: Optional[requests.Session] = None,
        timeout: int = 60,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "text/html,*/*"})

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> str:
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _write_raw(raw_dir: Optional[pathlib.Path], name: str, text: str) -> None:
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / name).write_text(text, encoding="utf-8")

    # -- HTML grid helpers ------------------------------------------------- #

    @staticmethod
    def _find_data_table(soup: BeautifulSoup, required: Iterable[str]):
        """Return the first <table> whose header row contains all ``required``
        substrings (skips the page's TSP-header/layout tables)."""
        req = list(required)
        for tbl in soup.find_all("table"):
            first = tbl.find("tr")
            if not first:
                continue
            headers = [_norm(c.get_text()) for c in first.find_all(["th", "td"])]
            if all(any(r in h for h in headers) for r in req):
                return tbl
        return None

    @staticmethod
    def _grid_rows(tbl) -> list[dict[str, Any]]:
        trs = tbl.find_all("tr")
        if not trs:
            return []
        headers = [_norm(c.get_text()) for c in trs[0].find_all(["th", "td"])]
        rows: list[dict[str, Any]] = []
        for tr in trs[1:]:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row: dict[str, Any] = {}
            for i, c in enumerate(cells):
                if i < len(headers):
                    row[headers[i]] = _norm(c.get_text())
            link = tr.find("a", href=True)
            row["_link"] = link["href"] if link else None
            rows.append(row)
        return rows

    # -- fetch ------------------------------------------------------------- #

    def fetch_operational_capacity(
        self, gas_day: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> str:
        """OAC grid HTML for a gas day. ``gas_day`` is ISO; portal expects MM/DD/YYYY."""
        y, m, d = gas_day.split("-")
        text = self._get(OAC_PATH, {"gasDay": f"{m}/{d}/{y}"})
        self._write_raw(raw_dir, "operational_capacity.html", text)
        return text

    def fetch_notice_category(
        self, category: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> str:
        text = self._get(NOTICE_PATH.format(category=category))
        self._write_raw(raw_dir, f"notices_{category}.html", text)
        return text

    # -- parse: OAC -------------------------------------------------------- #

    def parse_operational_capacity(
        self, html: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        soup = BeautifulSoup(html, "lxml")
        tbl = self._find_data_table(soup, ("DesignCapacity", "OperationallyAvailableCapacity"))
        if tbl is None:
            return []
        records: list[FlowRecord] = []
        for row in self._grid_rows(tbl):
            name = row.get("Loc Name", "")
            loc = row.get("Loc", "")
            if not name and not loc:
                continue
            kern = to_float(row.get("Total ScheduledQuantity-Kern"))
            moj = to_float(row.get("Total ScheduledQuantity-Mojave"))
            sched = None if kern is None and moj is None else (kern or 0.0) + (moj or 0.0)
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=norm_date(row.get("Eff Gas Day")) or gas_day,
                    cycle=(row.get("Cycle") or "").lower() or None,
                    point_name=name,
                    point_id=loc or None,
                    flow_direction=FLOW_DIRECTION.get((row.get("FlowInd") or "").strip().upper()),
                    scheduled_qty=sched,                                # Kern + Mojave, Dth
                    design_capacity=to_float(row.get("DesignCapacity")),
                    operational_capacity=to_float(row.get("OperatingCapacity")),
                    available_capacity=to_float(row.get("OperationallyAvailableCapacity")),
                    units="Dth/d",
                    original_units="Dth/d",
                    original_qty=sched,
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return records

    # -- parse: notices ---------------------------------------------------- #

    @classmethod
    def _notice_type(cls, category: str, notice_type: str) -> str:
        nt = (notice_type or "").lower()
        for needle, mapped in NOTICE_TYPE_OVERRIDES:
            if needle in nt:
                return mapped
        return CATEGORY_DEFAULT_TYPE.get(category, "other")

    def parse_notices(
        self, html: str, category: str, gas_day: str, pulled_at: str
    ) -> list[Notice]:
        soup = BeautifulSoup(html, "lxml")
        tbl = self._find_data_table(soup, ("NoticeIdentifier", "Subject"))
        if tbl is None:
            return []
        cutoff = (dt.date.fromisoformat(gas_day) - dt.timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
        category_url = f"{BASE_URL}{NOTICE_PATH.format(category=category)}"
        out: list[Notice] = []
        for row in self._grid_rows(tbl):
            notice_id = row.get("NoticeIdentifier", "")
            subject = row.get("Subject", "")
            if not notice_id and not subject:
                continue
            eff = norm_date(row.get("Notice EffectiveDate/Time"))
            end = norm_date(row.get("Notice EndDate/Time"))
            if end is not None and end < cutoff:
                continue
            ntype = row.get("Notice Type", "")
            link = row.get("_link")
            url = link if (link and "Notice" in link) else category_url
            if url and url.startswith("/"):
                url = f"https://services.kernrivergas.com{url}"
            out.append(
                Notice(
                    source=SOURCE,
                    gas_day=eff or gas_day,
                    posted_at=row.get("PostedDate/Time") or None,
                    notice_type=self._notice_type(category, ntype),
                    stage=category,
                    headline=subject,
                    body=f"[{category} / {ntype}] Effective {eff} through {end}. Notice {notice_id}.".strip(),
                    url=url,
                )
            )
        return out

    def fetch_all_notices(
        self, gas_day: str, pulled_at: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> list[Notice]:
        seen: set[str] = set()
        out: list[Notice] = []
        for category in NOTICE_CATEGORIES:
            html = self.fetch_notice_category(category, raw_dir=raw_dir)
            for n in self.parse_notices(html, category, gas_day, pulled_at):
                key = f"{n.headline}|{n.gas_day}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(n)
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, *, write: bool = True) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / gas_day
        raw_ref = raw_dir.as_posix()

        oac_html = self.fetch_operational_capacity(gas_day, raw_dir=raw_dir)
        records = self.parse_operational_capacity(oac_html, gas_day, pulled_at, raw_ref)
        notices = self.fetch_all_notices(gas_day, pulled_at, raw_dir=raw_dir)

        result = {
            "source": SOURCE,
            "gas_day": gas_day,
            "cycle": records[0].cycle if records else None,
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
            "notices": [n.to_dict() for n in notices],
        }
        if write:
            out_path = self.data_dir / f"{gas_day}.normalized.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records, %d notices)", out_path, len(records), len(notices))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_gas_day() -> str:
    now = dt.datetime.now(PACIFIC)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull Kern River (BHE) operationally available capacity + scheduled quantity + notices."
    )
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-22. Default: latest available.")
    parser.add_argument("--data-dir", default="data/kern_river")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gas_day = args.gas_day or _default_gas_day()
    client = KernRiverClient(data_dir=args.data_dir)
    result = client.pull(gas_day, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
