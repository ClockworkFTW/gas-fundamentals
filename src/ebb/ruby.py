"""Ruby Pipeline — Ruby (Tallgrass, pipeline id 325) ingestion.

Ruby carries Rockies supply (Opal, WY) west and **delivers into Malin**, where it
reaches PG&E — the ``PACGAS/RUBY (OXH) ONYX HILL`` delivery is the Ruby→PG&E flow
(the same one Pipe Ranger calls ``onyx_ruby``). Source: Tallgrass's EBB at
``pipeline.tallgrassenergylp.com`` (Quorum/Latitude), an **ASP.NET WebForms** site
(``__VIEWSTATE`` async postback) for Operationally Available Capacity.

**WAF caveat (important).** The site sits behind an **Incapsula/Imperva WAF**. A
plain ``requests`` GET gets the JS-challenge page (212 bytes, ``_Incapsula_Resource``),
NOT the data — so README §4's "no headless browser" cannot, by itself, reach Ruby.
*However*, once a real browser has solved the challenge, the resulting Incapsula
clearance cookies make the WebForms POST work fully from ``requests``. So this module
is **requests-based for the data pull** but needs a browser-obtained clearance cookie
supplied via ``RUBY_COOKIE`` in ``.env`` (the full ``Cookie:`` header copied from
devtools). Cookies are session-scoped and expire — when challenged, the client raises
``RubyChallengeError`` telling you to refresh ``RUBY_COOKIE``. No headless browser is
used for ingestion; the only manual step is periodically refreshing the cookie.

Quantities are in **Dth** (interstate) — already canonical Dth/d, no conversion.

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from tenacity import retry

from .base import RETRY, BaseEBBClient, EbbChallengeError, write_raw
from .schema import FlowRecord, collapse_ws, default_gas_day, norm_date, to_float, utc_now_iso

log = logging.getLogger("ruby")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "ruby"
PIPELINE_ID = 325
BASE_URL = "https://pipeline.tallgrassenergylp.com"
OA_PATH = "/Pages/Point.aspx?pipeline=325&type=OA"
OA_URL = f"{BASE_URL}{OA_PATH}"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")
PACIFIC = ZoneInfo("America/Los_Angeles")

# ddlCycle select values (note the gap: Intra-Day 3 == 6, not 5).
CYCLES = {
    "best": 0, "bestavailable": 0,
    "timely": 1,
    "evening": 2,
    "id1": 3, "intraday1": 3,
    "id2": 4, "intraday2": 4,
    "id3": 6, "intraday3": 6,
}
CYCLE_LABEL = {0: "best available", 1: "timely", 2: "evening", 3: "id1", 4: "id2", 6: "id3"}

# location radio -> normalized flow direction.
LOCATIONS = {"rbReceipt": "receipt", "rbDelivery": "delivery"}

RETRIEVE_TRIGGER = "ctl00$mainContent$btnRetrieve"


class RubyChallengeError(EbbChallengeError, RuntimeError):
    """Raised when the Incapsula WAF returns its JS challenge instead of the page
    (i.e. RUBY_COOKIE is missing/expired and must be refreshed from a browser).

    Subclasses the shared ``EbbChallengeError`` and keeps ``RuntimeError`` for
    backward compatibility with existing callers (CLI exit-2 handler, tests)."""


def _parse_cookie_header(cookie: str) -> dict[str, str]:
    """Parse a raw 'Cookie:' header string ('a=1; b=2') into a dict."""
    out: dict[str, str] = {}
    for part in (cookie or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class RubyClient(BaseEBBClient):
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/ruby",
        cookie: Optional[str] = None,
        session: Optional[requests.Session] = None,
        timeout: int = 60,
    ) -> None:
        super().__init__(
            data_dir,
            session,
            timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        if cookie is None:
            load_dotenv()
            cookie = os.getenv("RUBY_COOKIE")
        self.cookie = cookie
        for k, v in _parse_cookie_header(cookie or "").items():
            self.session.cookies.set(k, v, domain="pipeline.tallgrassenergylp.com")

    @retry(**RETRY)
    def _get_page(self) -> str:
        resp = self.session.get(OA_URL, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _harvest_fields(html: str) -> dict[str, str]:
        soup = BeautifulSoup(html, "lxml")
        fields: dict[str, str] = {}
        for inp in soup.find_all("input"):
            name = inp.get("name")
            if name and (inp.get("type") or "").lower() not in ("submit", "button", "image", "radio", "checkbox"):
                fields[name] = inp.get("value", "")
        for sel in soup.find_all("select"):
            if sel.get("name"):
                opt = sel.find("option", selected=True) or sel.find("option")
                fields[sel["name"]] = opt.get("value", "") if opt else ""
        return fields

    def _load_form(self) -> dict[str, str]:
        """GET the OA page and harvest the WebForms state (raises if challenged)."""
        html = self._get_page()
        if "__VIEWSTATE" not in html:
            raise RubyChallengeError(
                "Incapsula challenge returned instead of the Ruby OA page. "
                "Refresh RUBY_COOKIE in .env with a fresh 'Cookie:' header from your "
                "browser devtools (the ASP.NET_SessionId + incap_ses_*/visid_incap_* cookies)."
            )
        return self._harvest_fields(html)

    # -- fetch ------------------------------------------------------------- #

    @retry(**RETRY)
    def fetch_location(
        self, fields: dict[str, str], location: str, gas_day: str, cycle_value: int,
        *, raw_dir: Optional[pathlib.Path] = None,
    ) -> str:
        """POST the Retrieve async-postback for one location type (receipt/delivery)."""
        y, m, d = gas_day.split("-")
        begin = f"{int(m)}/{int(d)}/{y}"
        end = (dt.date.fromisoformat(gas_day) + dt.timedelta(days=1))
        form = dict(fields)
        form.update({
            "ctl00$scp_default": f"ctl00$updContent|{RETRIEVE_TRIGGER}",
            "ctl00$mainContent$tbGasFlow": begin,
            "ctl00$mainContent$tbgasflowend": f"{end.month}/{end.day}/{end.year}",
            "ctl00$mainContent$ddlCycle": str(cycle_value),
            "ctl00$mainContent$location": location,
            "ctl00$mainContent$tbsegment": "",
            "ctl00$mainContent$tbpoint": "",
            "ctl00$mainContent$tbDRN": "",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__ASYNCPOST": "true",
            RETRIEVE_TRIGGER: "Retrieve",
        })
        resp = self.session.post(OA_URL, data=form, timeout=self.timeout, headers={
            "X-MicrosoftAjax": "Delta=true",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": BASE_URL,
            "Referer": OA_URL,
        })
        resp.raise_for_status()
        write_raw(raw_dir, f"oa_{LOCATIONS.get(location, location)}.html", resp.text)
        return resp.text

    # -- parse ------------------------------------------------------------- #

    @staticmethod
    def _find_grid(html: str):
        soup = BeautifulSoup(html, "lxml")
        for tbl in soup.find_all("table"):
            head = tbl.find("tr")
            if head and any("DesignCapacity" in collapse_ws(c.get_text()) for c in head.find_all(["th", "td"])):
                return tbl
        return None

    def parse_grid(
        self, html: str, direction: str, gas_day: str, cycle_value: int,
        pulled_at: str, raw_ref: Optional[str],
    ) -> list[FlowRecord]:
        tbl = self._find_grid(html)
        if tbl is None:
            return []
        trs = tbl.find_all("tr")
        headers = [collapse_ws(c.get_text()) for c in trs[0].find_all(["th", "td"])]
        idx = {h: i for i, h in enumerate(headers)}

        def cell(cells: list[str], key: str) -> Optional[str]:
            i = idx.get(key)
            return cells[i] if i is not None and i < len(cells) else None

        records: list[FlowRecord] = []
        for tr in trs[1:]:
            cells = [collapse_ws(td.get_text()) for td in tr.find_all("td")]
            if len(cells) < len(headers):
                continue
            name = cell(cells, "LocName") or ""
            loc = cell(cells, "Loc") or ""
            if not name and not loc:
                continue
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=gas_day,
                    cycle=CYCLE_LABEL.get(cycle_value, str(cycle_value)),
                    point_name=name,
                    point_id=loc or None,
                    flow_direction=direction,
                    scheduled_qty=to_float(cell(cells, "TotalScheduledQuantity")),
                    design_capacity=to_float(cell(cells, "DesignCapacity")),
                    operational_capacity=to_float(cell(cells, "OperatingCapacity")),
                    available_capacity=to_float(cell(cells, "OperationallyAvailableCapacity")),
                    units="Dth/d",
                    original_units="Dth/d",
                    original_qty=to_float(cell(cells, "TotalScheduledQuantity")),
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return records

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, cycle: str = "best", *, write: bool = True) -> dict[str, Any]:
        cycle_value = CYCLES.get(cycle.lower())
        if cycle_value is None:
            raise ValueError(f"unknown cycle {cycle!r}; expected one of {sorted(CYCLES)}")
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / f"{gas_day}_{CYCLE_LABEL.get(cycle_value, cycle)}".replace(" ", "_")
        raw_ref = raw_dir.as_posix()

        fields = self._load_form()
        records: list[FlowRecord] = []
        for location, direction in LOCATIONS.items():
            html = self.fetch_location(fields, location, gas_day, cycle_value, raw_dir=raw_dir)
            records.extend(self.parse_grid(html, direction, gas_day, cycle_value, pulled_at, raw_ref))

        result = {
            "source": SOURCE,
            "gas_day": gas_day,
            "cycle": CYCLE_LABEL.get(cycle_value, cycle),
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
            "notices": [],
        }
        if write:
            out_path = self.data_dir / f"{gas_day}_{CYCLE_LABEL.get(cycle_value, cycle)}.normalized.json".replace(" ", "_")
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records)", out_path, len(records))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull Ruby (Tallgrass) operationally available capacity + scheduled quantity."
    )
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-23. Default: latest available.")
    parser.add_argument("--cycle", default="best", help="best | timely | evening | id1 | id2 | id3")
    parser.add_argument("--data-dir", default="data/ruby")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gas_day = args.gas_day or default_gas_day(PACIFIC)
    client = RubyClient(data_dir=args.data_dir)
    try:
        result = client.pull(gas_day, args.cycle, write=not args.no_write)
    except RubyChallengeError as exc:
        log.error("%s", exc)
        return 2
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
