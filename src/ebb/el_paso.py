"""El Paso Natural Gas (EPNG, Kinder Morgan) ingestion.

EPNG feeds PG&E at Topock. Its informational postings live on Kinder Morgan's
``pipeline2.kindermorgan.com`` portal (TSP code ``EPNG``) — a classic ASP.NET
**WebForms** site (this is the ``requests.Session`` + ``__VIEWSTATE`` POST case
README §4 anticipated, unlike the JSON EBBs).

The Operationally Available - Point Capacity page renders an empty grid on a
plain GET; the data appears only after a "Retrieve" postback. So we: GET the
page, harvest the form fields (``__VIEWSTATE`` / ``__EVENTVALIDATION`` / control
state), POST with ``__EVENTTARGET`` = the Retrieve button, then parse the grid
HTML. The posting carries Operationally Available Capacity **and** Total
Scheduled Quantity per location — capacity + scheduled in one call (like GTN).

Values are in **Dth/d** (the page's measurement basis), already canonical.

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import re
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

log = logging.getLogger("el_paso")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "el_paso"
TSP = "EPNG"
BASE_URL = "https://pipeline2.kindermorgan.com"
OAC_PATH = "/Capacity/OpAvailPoint.aspx?code=EPNG"
NOTICES_PATH = "/Notices/Notices.aspx?code=EPNG"
NOTICE_DETAIL_URL = BASE_URL + "/Notices/NoticeDetail.aspx?code=EPNG&notc_nbr={notice_id}"

# EPNG notice "Notice Type Desc (1)" -> normalized §5 notice_type.
NOTICE_TYPE_MAP = {
    "MAINTENANCE": "maintenance",
    "FORCE MAJEURE": "critical",
    "CAPACITY CONSTRAINT": "critical",
    "OVER-UNDER PERFORMANCE": "other",
}
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PACIFIC = ZoneInfo("America/Los_Angeles")

# The Retrieve button that triggers the data postback (WebForms control path).
RETRIEVE_TARGET = "ctl00$WebSplitter1$tmpl1$ContentPlaceHolder1$HeaderBTN1$btnRetrieve"

# Gas day + cycle are Infragistics editor "_clientState" hidden fields (pipe-format,
# reverse-engineered from real browser requests). Leaving them empty = current gas
# day + BEST AVAILABLE cycle. The field names end with these suffixes.
DATE_FIELD_SUFFIX = "dtePickerBegin_clientState"
CYCLE_FIELD_SUFFIX = "ddlCycleDD_clientState"

# Cycle alias -> (display name, index) used by the cycle clientState.
CYCLES = {
    "timely": ("TIMELY", 1),
    "evening": ("EVENING", 2),
    "id1": ("INTRADAY 1", 3),
    "intraday1": ("INTRADAY 1", 3),
    "id2": ("INTRADAY 2", 4),
    "intraday2": ("INTRADAY 2", 4),
    "id3": ("INTRADAY 3", 5),
    "intraday3": ("INTRADAY 3", 5),
}

# Cycle clientState template (captured from a real Retrieve POST). The selection is
# the delta object at obj[1][0]; we swap its index ([41]/[7]) and name ([23]).
_CYCLE_CS_TEMPLATE = (
    '[[[[null,null,null,null,null,null,null,-1,null,null,null,null,null,null,null,null,null,null,'
    'null,null,null,null,null,"EVENING",null,null,null,null,null,null,null,null,null,null,null,null,'
    'null,null,0,0,null,null,2,null,null,null,null,null,null,null,null]],[],null],'
    '[{"0":[41,1],"1":[7,1],"2":[23,"TIMELY"]},[{"0":[2,0,17],"1":["1",0,81],"2":[2,9,0],'
    '"3":["1",9,1],"5":["1",7,1],"6":[2,7,0]}]],null]'
)

# OAC grid column order (stable NAESB layout). Index into a data row's <td> cells.
COL = {
    "loc": 1,
    "loc_name": 2,
    "loc_zone": 3,
    "loc_segment": 4,
    "design": 5,
    "operating": 6,
    "scheduled": 7,
    "available": 8,
    "it": 9,
    "flow_ind": 10,
}
MIN_CELLS = 12  # a real data row has the full column set

FLOW_DIRECTION = {"R": "receipt", "D": "delivery"}


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class ElPasoClient:
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/el_paso",
        session: Optional[requests.Session] = None,
        timeout: int = 60,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )

    # -- fetch ------------------------------------------------------------- #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str) -> str:
        r = self.session.get(f"{BASE_URL}{path}", timeout=self.timeout)
        r.raise_for_status()
        return r.text

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _post(self, path: str, data: dict[str, str]) -> str:
        r = self.session.post(f"{BASE_URL}{path}", data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    @staticmethod
    def _form_fields(html: str) -> dict[str, str]:
        """Harvest current form state: hidden inputs (__VIEWSTATE etc.), inputs, selects."""
        soup = BeautifulSoup(html, "lxml")
        fields: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if not name:
                continue
            itype = (inp.get("type") or "").lower()
            if itype in ("submit", "button", "image"):
                continue
            if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
                continue
            fields[name] = inp.get("value", "")
        for sel in soup.find_all("select"):
            name = sel.get("name")
            if not name:
                continue
            chosen = sel.find("option", selected=True) or sel.find("option")
            fields[name] = chosen.get("value", "") if chosen else ""
        for ta in soup.find_all("textarea"):
            if ta.get("name"):
                fields[ta["name"]] = ta.get_text()
        return fields

    @staticmethod
    def _field_key(fields: dict[str, str], suffix: str) -> Optional[str]:
        return next((k for k in fields if k.endswith(suffix)), None)

    @staticmethod
    def _date_clientstate(gas_day: str) -> str:
        """Infragistics WebDatePicker clientState for an ISO gas day (YYYY-MM-DD).

        Value format is ``01<year>-<month>-<day>-0-0-0-0`` (month/day not padded).
        """
        y, m, d = (int(x) for x in gas_day.split("-"))
        v = f"01{y}-{m}-{d}-0-0-0-0"
        return f'|0|{v}||[[[[]],[],[]],[{{}},[]],"{v}"]'

    @staticmethod
    def _cycle_clientstate(name: str, index: int) -> str:
        """Infragistics WebDropDown clientState selecting (name, index)."""
        obj = json.loads(_CYCLE_CS_TEMPLATE)
        delta = obj[1][0]            # {"0":[41,1],"1":[7,1],"2":[23,"TIMELY"]}
        delta["0"][1] = index
        delta["1"][1] = index
        delta["2"][1] = name
        return f"|0|{name}&tilda;{index}||" + json.dumps(obj, separators=(",", ":"))

    def fetch_operational_capacity(
        self,
        gas_day: Optional[str] = None,
        cycle: Optional[str] = None,
        *,
        raw_dir: Optional[pathlib.Path] = None,
    ) -> str:
        """GET the OAC page, then POST the Retrieve to populate the grid; return that HTML.

        ``gas_day`` (ISO) and ``cycle`` set the Infragistics date/cycle editors via
        their clientState fields. Omit either to use the portal default (current gas
        day / BEST AVAILABLE cycle).
        """
        page = self._get(OAC_PATH)
        fields = self._form_fields(page)
        if gas_day:
            k = self._field_key(fields, DATE_FIELD_SUFFIX)
            if k:
                fields[k] = self._date_clientstate(gas_day)
        if cycle:
            if cycle.lower() not in CYCLES:
                raise ValueError(f"unknown cycle {cycle!r}; expected one of {sorted(CYCLES)}")
            name, index = CYCLES[cycle.lower()]
            k = self._field_key(fields, CYCLE_FIELD_SUFFIX)
            if k:
                fields[k] = self._cycle_clientstate(name, index)
        fields["__EVENTTARGET"] = RETRIEVE_TARGET
        fields["__EVENTARGUMENT"] = ""
        html = self._post(OAC_PATH, fields)
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "operational_capacity.html").write_text(html, encoding="utf-8")
        return html

    # -- parse ------------------------------------------------------------- #

    @staticmethod
    def _posting_context(html: str) -> dict[str, Optional[str]]:
        """Extract the gas day / cycle the grid reflects.

        Gas day comes from the begin date-picker (``dtePickerBegin``); cycle from
        the Infragistics cycle combo (e.g. "BEST AVAILABLE TIMELY").
        """
        gas_day = None
        m = re.search(r'dtePickerBegin.*?value="(\d{1,2}/\d{1,2}/\d{4})"', html, re.S)
        if m:
            gas_day = norm_date(m.group(1))
        # Scope the cycle search to the window just after the cycle combo, so a
        # stray "ID2"/"TIMELY" elsewhere (viewstate, scripts) can't be picked up.
        cycle = None
        anchor = re.search(r"ddlCycleDD", html)
        if anchor:
            window = html[anchor.start(): anchor.start() + 4000]
            cm = re.search(
                r"(BEST AVAILABLE TIMELY|TIMELY|EVENING|INTRADAY ?[123]|ID ?[123]|FINAL)",
                window,
            )
            if cm:
                cycle = cm.group(1).strip()
        return {"gas_day": gas_day, "cycle": cycle}

    def parse_operational_capacity(
        self, html: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        ctx = self._posting_context(html)
        eff_gas_day = ctx["gas_day"] or gas_day
        cycle = ctx["cycle"]

        soup = BeautifulSoup(html, "lxml")
        records: list[FlowRecord] = []
        seen: set[str] = set()
        for tr in soup.find_all("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
            if len(cells) < MIN_CELLS:
                continue
            loc = cells[COL["loc"]]
            if not re.fullmatch(r"\d{3,}", loc or ""):  # data rows have a numeric Loc id
                continue
            if loc in seen:  # the page can repeat a row in header/scroll panels
                continue
            seen.add(loc)
            flow = cells[COL["flow_ind"]].strip().upper() if len(cells) > COL["flow_ind"] else ""
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=eff_gas_day,
                    cycle=cycle,
                    point_name=cells[COL["loc_name"]],
                    point_id=loc,
                    flow_direction=FLOW_DIRECTION.get(flow),
                    scheduled_qty=to_float(cells[COL["scheduled"]]),
                    design_capacity=to_float(cells[COL["design"]]),
                    operational_capacity=to_float(cells[COL["operating"]]),
                    available_capacity=to_float(cells[COL["available"]]),
                    units="Dth/d",
                    original_units="Dth/d",
                    original_qty=to_float(cells[COL["scheduled"]]),
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return records

    # -- notices ----------------------------------------------------------- #

    def fetch_notices(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        """The notices page renders the full grid on a plain GET (no postback)."""
        html = self._get(NOTICES_PATH)
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "notices.html").write_text(html, encoding="utf-8")
        return html

    def parse_notices(self, html: str, pulled_at: str) -> list[Notice]:
        """Parse the notices grid. Rows are: Type1, Type2, PostDT, EffDT, EndDT,
        NoticeID (6-digit), Subject. The grid nests tables, so take leaf rows
        only and dedupe by Notice ID."""
        soup = BeautifulSoup(html, "lxml")
        out: list[Notice] = []
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
            type1, type2 = cells[k - 5], cells[k - 4]
            post, eff, end = cells[k - 3], cells[k - 2], cells[k - 1]
            subject = cells[k + 1]
            out.append(
                Notice(
                    source=SOURCE,
                    gas_day=norm_date(eff) or "",
                    posted_at=post,
                    notice_type=NOTICE_TYPE_MAP.get(type1.upper(), "other"),
                    stage=type2 or None,
                    headline=subject,
                    body=f"[{type1} / {type2}] Effective {eff} through {end}. {subject}".strip(),
                    url=NOTICE_DETAIL_URL.format(notice_id=nid),
                )
            )
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(
        self,
        gas_day: Optional[str] = None,
        cycle: Optional[str] = None,
        *,
        write: bool = True,
    ) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        gas_day = gas_day or dt.datetime.now(PACIFIC).date().isoformat()
        tag = f"{gas_day}_{cycle}" if cycle else gas_day
        raw_dir = self.data_dir / tag
        raw_ref = raw_dir.as_posix()

        html = self.fetch_operational_capacity(gas_day, cycle, raw_dir=raw_dir)
        records = self.parse_operational_capacity(html, gas_day, pulled_at, raw_ref)
        notices = self.parse_notices(self.fetch_notices(raw_dir=raw_dir), pulled_at)

        result = {
            "source": SOURCE,
            "gas_day": records[0].gas_day if records else gas_day,
            "requested_gas_day": gas_day,
            "cycle": cycle,
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
            "notices": [n.to_dict() for n in notices],
        }
        if write:
            out_path = self.data_dir / f"{tag}.normalized.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records, %d notices)", out_path, len(records), len(notices))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pull El Paso Natural Gas operationally available capacity.")
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-21. Default: current posting.")
    parser.add_argument("--cycle", default=None, help="timely | evening | id1 | id2 | id3 (default: best available)")
    parser.add_argument("--data-dir", default="data/el_paso")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    client = ElPasoClient(data_dir=args.data_dir)
    result = client.pull(args.gas_day, args.cycle, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
