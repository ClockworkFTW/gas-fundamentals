"""GTN — Gas Transmission Northwest (TC Energy) ingestion.

GTN feeds PG&E at Malin (Canadian gas via Kingsgate). Its EBB is TC Energy's
"ganesha" platform at ``tcplus.com/GTN``. Like Pipe Ranger, the data is reachable
as JSON via a plain GET — the Operationally Available Capacity posting comes from
``/GTN/OperationalCapacity/Generate?GasDay=&CycleType=&ExportEnum=0`` and includes
both capacity AND scheduled quantity at every location (Kingsgate, Malin, …).

Measurement basis is "Million BTU's" (MMBtu/d == Dth/d), so values are already in
the canonical Dth unit — no conversion, only formatting cleanup.

Notices (``/GTN/Notice/Retrieve``) use a stateful grid endpoint and are a
documented follow-up (see README). This module covers OAC + scheduled quantity.

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import pathlib
import re
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .schema import FlowRecord, Notice, norm_date, to_float, utc_now_iso

log = logging.getLogger("gtn")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "gtn"
TSP = "GTN"
BASE_URL = "https://www.tcplus.com"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"
PACIFIC = ZoneInfo("America/Los_Angeles")

# GTN CycleType select values (from the OperationalCapacity page).
CYCLE_TYPES = {
    "timely": 1,
    "intraday1": 2,
    "id1": 2,
    "intraday2": 3,
    "id2": 3,
    "evening": 4,
    "intraday3": 5,
    "id3": 5,
}
CYCLE_LABEL = {1: "Timely", 2: "Intraday 1", 3: "Intraday 2", 4: "Evening", 5: "Intraday 3"}

# Flow indicator -> normalized direction.
FLOW_DIRECTION = {"R": "receipt", "D": "delivery"}

# Notices grid (resourcetable). The view overrides FilterTemplate to "filter.<key>";
# sort_direction must be the verbose "Descending"/"Ascending". indicator "" = all
# categories (Critical, Non-Critical, Planned Service Outage). EffDate/EndDate are
# the effective-date window and are required (MM/DD/YYYY).
NOTICE_RETRIEVE = f"{TSP}/Notice/Retrieve"
NOTICE_DETAIL_URL = f"{BASE_URL}/{TSP}/Notice/ShowDetails/{{notice_id}}"
NOTICE_INDICATOR_TO_TYPE = {
    "Critical": "critical",
    "Planned Service Outage": "maintenance",
    "Non-Critical": "other",
}
# How far around the gas day to look for active/upcoming notices, in days.
NOTICE_LOOKBACK_DAYS = 14
NOTICE_LOOKAHEAD_DAYS = 45


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class GTNClient:
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/gtn",
        session: Optional[requests.Session] = None,
        timeout: int = 40,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": f"{BASE_URL}/{TSP}/OperationalCapacity",
            }
        )

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        resp = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # -- fetch ------------------------------------------------------------- #

    def fetch_operational_capacity(
        self, gas_day: str, cycle: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> dict[str, Any]:
        """Fetch the OAC posting (capacity + scheduled qty) for a gas day/cycle.

        ``gas_day`` is ISO (YYYY-MM-DD); GTN expects MM/DD/YYYY.
        ``cycle`` is a CYCLE_TYPES alias (timely, id1, id2, id3, evening).
        """
        cycle_type = CYCLE_TYPES.get(cycle.lower())
        if cycle_type is None:
            raise ValueError(f"unknown cycle {cycle!r}; expected one of {sorted(set(CYCLE_TYPES))}")
        y, m, d = gas_day.split("-")
        payload = self._get(
            f"{TSP}/OperationalCapacity/Generate",
            params={"GasDay": f"{m}/{d}/{y}", "CycleType": cycle_type, "ExportEnum": 0},
        )
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "operational_capacity.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload

    # -- parse ------------------------------------------------------------- #

    def parse_operational_capacity(
        self, payload: dict[str, Any], gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        data = payload.get("data", {})
        cycle = data.get("Cycle")
        # MeasurementBasis is "Million BTU's" == MMBtu/d == Dth/d (canonical).
        basis = (data.get("MeasurementBasis") or "").lower()
        original_units = "MMBtu/d" if "btu" in basis else (data.get("MeasurementBasis") or "Dth/d")
        eff_gas_day = norm_date(data.get("EffectiveGasDay")) or gas_day

        records: list[FlowRecord] = []
        for row in data.get("Content", []):
            direction = FLOW_DIRECTION.get((row.get("FlowIndicatorDescription") or "").strip().upper())
            if direction is None:
                purpose = (row.get("LocationPurposeDescription") or "").lower()
                direction = "receipt" if "receipt" in purpose else ("delivery" if "delivery" in purpose else None)
            sched = to_float(row.get("TotalScheduledQuantity"))
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=eff_gas_day,
                    cycle=cycle,
                    point_name=row.get("LocationName", ""),
                    point_id=str(row.get("LocationID", "")) or None,
                    flow_direction=direction,
                    scheduled_qty=sched,                                   # Dth (no conversion)
                    design_capacity=to_float(row.get("DesignCapacity")),
                    operational_capacity=to_float(row.get("OperatingCapacity")),
                    available_capacity=to_float(row.get("OperationallyAvailableCapacity")),
                    units="Dth/d",
                    original_units=original_units,
                    original_qty=sched,
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return records

    # -- notices ----------------------------------------------------------- #

    def fetch_notices(
        self,
        gas_day: str,
        *,
        eff_start: Optional[str] = None,
        eff_end: Optional[str] = None,
        indicator: str = "",
        raw_dir: Optional[pathlib.Path] = None,
    ) -> list[dict[str, Any]]:
        """Fetch notices effective within a window around the gas day.

        ``indicator=""`` returns all categories. Dates default to a window of
        [gas_day - NOTICE_LOOKBACK_DAYS, gas_day + NOTICE_LOOKAHEAD_DAYS].
        """
        day = dt.date.fromisoformat(gas_day)
        start = eff_start or (day - dt.timedelta(days=NOTICE_LOOKBACK_DAYS)).strftime("%m/%d/%Y")
        end = eff_end or (day + dt.timedelta(days=NOTICE_LOOKAHEAD_DAYS)).strftime("%m/%d/%Y")
        payload = self._get(
            NOTICE_RETRIEVE,
            params={
                "filter.SelectedIndicator": indicator,
                "filter.SelectedStatus": "",
                "filter.SelectedTypeIds": "",
                "filter.EffDate": start,
                "filter.EndDate": end,
                "page": 1,
                "sort": "PostingDate",
                "sort_direction": "Descending",  # verbose form required (not "desc")
            },
        )
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "notices.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload.get("data", [])

    @staticmethod
    def _clean_text(value: Optional[str]) -> str:
        """Unescape HTML entities and strip tags from notice body text."""
        if not value:
            return ""
        text = html.unescape(value)
        text = re.sub(r"<[^>]+>", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def parse_notices(self, rows: list[dict[str, Any]], pulled_at: str) -> list[Notice]:
        out: list[Notice] = []
        for row in rows:
            indicator = (row.get("NoticeIndicator") or "").strip()
            posted_date = norm_date(row.get("PostingDate")) or (row.get("PostingDate") or "")
            posted_at = f"{posted_date} {row.get('PostingTime', '')}".strip()
            window = (
                f"Effective {row.get('EffDate', '')} {row.get('EffTime', '')} "
                f"through {row.get('EndDate', '')} {row.get('EndTime', '')}.".strip()
            )
            body = self._clean_text(row.get("Text"))
            out.append(
                Notice(
                    source=SOURCE,
                    gas_day=norm_date(row.get("EffDate")) or "",
                    posted_at=posted_at,
                    notice_type=NOTICE_INDICATOR_TO_TYPE.get(indicator, "other"),
                    stage=row.get("NoticeStatus"),
                    headline=self._clean_text(row.get("Subject")),
                    body=f"[{indicator} / {row.get('NoticeType', '')}] {window} {body}".strip(),
                    url=NOTICE_DETAIL_URL.format(notice_id=row.get("NoticeId")),
                )
            )
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, cycle: str = "timely", *, write: bool = True) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / f"{gas_day}_{cycle}"
        raw_ref = raw_dir.as_posix()

        payload = self.fetch_operational_capacity(gas_day, cycle, raw_dir=raw_dir)
        records = self.parse_operational_capacity(payload, gas_day, pulled_at, raw_ref)
        notices = self.parse_notices(self.fetch_notices(gas_day, raw_dir=raw_dir), pulled_at)

        result = {
            "source": SOURCE,
            "gas_day": gas_day,
            "cycle": cycle,
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
            "notices": [n.to_dict() for n in notices],
        }
        if write:
            out_path = self.data_dir / f"{gas_day}_{cycle}.normalized.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records, %d notices)", out_path, len(records), len(notices))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_gas_day() -> str:
    """Prior gas day before 08:00 PT, else today (PT)."""
    now = dt.datetime.now(PACIFIC)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pull GTN operationally available capacity + scheduled quantity.")
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-21. Default: latest available.")
    parser.add_argument("--cycle", default="timely", help="timely | id1 | id2 | id3 | evening")
    parser.add_argument("--data-dir", default="data/gtn")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    gas_day = args.gas_day or _default_gas_day()
    client = GTNClient(data_dir=args.data_dir)
    result = client.pull(gas_day, args.cycle, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
