"""Transwestern — Transwestern Pipeline (Energy Transfer) ingestion.

Transwestern carries San Juan / Permian / West Texas supply west and feeds PG&E at
**Topock** (the AZ/CA border), alongside SoCalGas and Mojave interconnects. Its EBB
is Energy Transfer's **iPost** platform at ``twtransfer.energytransfer.com`` (asset
code ``TW``) — and, unlike El Paso's WebForms, iPost exposes every posting as a
**plain CSV via GET** (the README §4 ideal, no viewstate, no auth):

  * OAC + scheduled: ``/ipost/capacity/operationally-available?asset=TW&gasDay=&cycle=&f=csv``
    — one CSV carries Design Capacity (DC), Operating Capacity (OPC), Total Scheduled
    Quantity (TSQ) and Operationally Available Capacity (OAC) for every location,
    including **PG&E TOPOCK** (loc 56698, the delivery to PG&E).
  * Notices: ``/ipost/notice/{critical|non-critical|planned-service-outage}?asset=TW&f=csv``
    — Notice Type / Posted / Eff / End / Notice ID / Subject.

Quantities are expressed in **DTH** (Measurement Basis MMBtu) — already the canonical
Dth/d, no conversion. Two nomination cycles are posted: Timely (0) and Evening (1).

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import logging
import pathlib
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests
from dateutil import parser as dtparser
from tenacity import retry

from .base import RETRY, BaseEBBClient, write_raw
from .schema import (
    FLOW_DIRECTION,
    FlowRecord,
    Notice,
    default_gas_day,
    to_float,
    utc_now_iso,
)

log = logging.getLogger("transwestern")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "transwestern"
ASSET = "TW"
BASE_URL = "https://twtransfer.energytransfer.com"
OAC_PATH = "/ipost/capacity/operationally-available"
NOTICE_PATH = "/ipost/notice/{category}"
NOTICE_DETAIL_URL = f"{BASE_URL}/ipost/notice/show/{{notice_id}}?asset={ASSET}"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"
PACIFIC = ZoneInfo("America/Los_Angeles")

# iPost posts two nomination cycles (Timely, Evening). Aliases -> select value.
CYCLES = {"timely": 0, "evening": 1}
CYCLE_LABEL = {0: "timely", 1: "evening"}

# Flow indicator -> direction: schema.FLOW_DIRECTION (shared).

# Notice categories (each its own CSV endpoint) and normalized type mapping.
NOTICE_CATEGORIES = ("critical", "non-critical", "planned-service-outage")
CATEGORY_DEFAULT_TYPE = {
    "critical": "critical",
    "planned-service-outage": "maintenance",
    "non-critical": "other",
}
# Notice Type string -> normalized §5 type (overrides the category default when it
# names a fundamentals-relevant condition).
NOTICE_TYPE_MAP = {
    "force majeure": "critical",
    "capacity constraint": "critical",
    "operational alert": "critical",
    "operational flow order": "OFO",
    "planned service outage": "maintenance",
    "maintenance": "maintenance",
}
# Keep notices that haven't ended before gas_day - this many days.
NOTICE_LOOKBACK_DAYS = 3


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class TranswesternClient(BaseEBBClient):
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/transwestern",
        session: Optional[requests.Session] = None,
        timeout: int = 60,
    ) -> None:
        super().__init__(
            data_dir, session, timeout, headers={"User-Agent": USER_AGENT, "Accept": "text/csv, */*"}
        )

    @retry(**RETRY)
    def _get_csv(self, path: str, params: dict[str, Any]) -> str:
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    # -- fetch ------------------------------------------------------------- #

    def fetch_operational_capacity(
        self, gas_day: str, cycle: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> str:
        """OAC + scheduled CSV for a gas day/cycle.

        ``gas_day`` is ISO (YYYY-MM-DD); iPost expects MM/DD/YYYY.
        ``cycle`` is a CYCLES alias (timely, evening).
        """
        cyc = CYCLES.get(cycle.lower())
        if cyc is None:
            raise ValueError(f"unknown cycle {cycle!r}; expected one of {sorted(CYCLES)}")
        y, m, d = gas_day.split("-")
        text = self._get_csv(
            OAC_PATH,
            {
                "asset": ASSET,
                "gasDay": f"{m}/{d}/{y}",
                "cycle": cyc,
                "searchType": "ALL",
                "locType": "ALL",
                "locZone": "ALL",
                "max": "ALL",
                "f": "csv",
                "extension": "csv",
            },
        )
        write_raw(raw_dir, "operational_capacity.csv", text)
        return text

    def fetch_notice_category(
        self, category: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> str:
        text = self._get_csv(
            NOTICE_PATH.format(category=category),
            {"asset": ASSET, "f": "csv", "extension": "csv"},
        )
        write_raw(raw_dir, f"notices_{category}.csv", text)
        return text

    # -- parse ------------------------------------------------------------- #

    def parse_operational_capacity(
        self, text: str, gas_day: str, cycle: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        reader = csv.DictReader(io.StringIO(text))
        records: list[FlowRecord] = []
        for row in reader:
            loc = (row.get("Loc") or "").strip()
            name = (row.get("Loc Name") or "").strip()
            if not loc and not name:
                continue
            tsq = to_float(row.get("TSQ"))
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=gas_day,
                    cycle=CYCLE_LABEL.get(CYCLES.get(cycle.lower()), cycle),
                    point_name=name,
                    point_id=loc or None,
                    flow_direction=FLOW_DIRECTION.get((row.get("Flow Ind") or "").strip().upper()),
                    scheduled_qty=tsq,                                    # Dth (no conversion)
                    design_capacity=to_float(row.get("DC")),
                    operational_capacity=to_float(row.get("OPC")),
                    available_capacity=to_float(row.get("OAC")),
                    units="Dth/d",
                    original_units="Dth/d",
                    original_qty=tsq,
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return records

    @staticmethod
    def _parse_dt(value: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        """iPost 'Jun 23 2026  7:34AM' -> (ISO date, 'YYYY-MM-DD HH:MM')."""
        s = (value or "").strip()
        if not s:
            return None, None
        try:
            parsed = dtparser.parse(s)
        except (ValueError, OverflowError):
            return None, s
        return parsed.date().isoformat(), parsed.strftime("%Y-%m-%d %H:%M")

    @classmethod
    def _notice_type(cls, category: str, notice_type: str) -> str:
        mapped = NOTICE_TYPE_MAP.get((notice_type or "").strip().lower())
        return mapped or CATEGORY_DEFAULT_TYPE.get(category, "other")

    def parse_notices(
        self, text: str, category: str, gas_day: str, pulled_at: str
    ) -> list[Notice]:
        reader = csv.DictReader(io.StringIO(text))
        cutoff = (dt.date.fromisoformat(gas_day) - dt.timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
        out: list[Notice] = []
        for row in reader:
            notice_id = (row.get("Notice ID") or "").strip()
            subject = (row.get("Subject") or "").strip()
            if not notice_id and not subject:
                continue
            eff_date, _ = self._parse_dt(row.get("Notice Eff Date/Time"))
            end_date, _ = self._parse_dt(row.get("Notice End Date/Time"))
            _, posted = self._parse_dt(row.get("Posted Date/Time"))
            # Drop notices that already ended before the lookback cutoff.
            if end_date is not None and end_date < cutoff:
                continue
            ntype = (row.get("Notice Type") or "").strip()
            out.append(
                Notice(
                    source=SOURCE,
                    gas_day=eff_date or gas_day,
                    posted_at=posted,
                    notice_type=self._notice_type(category, ntype),
                    stage=category,
                    headline=subject,
                    body=f"[{category} / {ntype}] Effective {eff_date} through {end_date}.".strip(),
                    url=NOTICE_DETAIL_URL.format(notice_id=notice_id) if notice_id else f"{BASE_URL}{NOTICE_PATH.format(category=category)}?asset={ASSET}",
                )
            )
        return out

    def fetch_all_notices(
        self, gas_day: str, pulled_at: str, *, raw_dir: Optional[pathlib.Path] = None
    ) -> list[Notice]:
        seen: set[str] = set()
        out: list[Notice] = []
        for category in NOTICE_CATEGORIES:
            text = self.fetch_notice_category(category, raw_dir=raw_dir)
            for n in self.parse_notices(text, category, gas_day, pulled_at):
                key = n.url
                if key in seen:
                    continue
                seen.add(key)
                out.append(n)
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, cycle: str = "timely", *, write: bool = True) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / f"{gas_day}_{cycle}"
        raw_ref = raw_dir.as_posix()

        oac_text = self.fetch_operational_capacity(gas_day, cycle, raw_dir=raw_dir)
        records = self.parse_operational_capacity(oac_text, gas_day, cycle, pulled_at, raw_ref)
        notices = self.fetch_all_notices(gas_day, pulled_at, raw_dir=raw_dir)

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


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull Transwestern (Energy Transfer / iPost) OAC + scheduled quantity + notices."
    )
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-22. Default: latest available.")
    parser.add_argument("--cycle", default="timely", help="timely | evening")
    parser.add_argument("--data-dir", default="data/transwestern")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gas_day = args.gas_day or default_gas_day(PACIFIC)
    client = TranswesternClient(data_dir=args.data_dir)
    result = client.pull(gas_day, args.cycle, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
