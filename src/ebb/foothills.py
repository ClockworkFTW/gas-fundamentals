"""Foothills — Foothills Pipe Lines (TC Energy) ingestion.

Foothills is the export leg downstream of NGTL: it carries Alberta (AECO/NIT) gas
from NGTL's borders to the international boundary —
  * **Foothills BC**: NGTL's **Alberta/BC border** → **Kingsgate** → GTN → Malin →
    **PG&E** (the leg that delivers AECO supply to northern California), and
  * **Foothills SK**: NGTL's **Empress / McNeill** borders → Canadian Mainline / US.

Foothills has **no separate public operational EBB** of its own — TC Energy reports
its throughput *as NGTL's border flows*. So this module reuses the same public TC
Customer Express CSV feeds as ``nova.py`` (``NovaClient``) and presents the
**export-border subset** as the ``foothills`` source: the AB/BC firm + flow from the
``chart`` feed, and the Alberta-BC / Empress / McNeill border flows from the Current
System Report. Units are normalized to the canonical **Dth/d** by the reused
``NovaClient`` conversions (originals preserved). See README §13–14.

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import pathlib
from typing import Any, Iterable, Optional

import requests

from .nova import DEFAULT_HEAT_BTU_PER_CF, MOUNTAIN, NovaClient
from .schema import FlowRecord, Notice, utc_now_iso

log = logging.getLogger("foothills")

SOURCE = "foothills"

# chart zone (NGTL AB/BC) re-presented as Foothills BC capacity + flow.
FOOTHILLS_BC_ZONE = "AB/BC"
FOOTHILLS_BC_NAME = "Foothills BC (AB/BC → Kingsgate → GTN → Malin → PG&E)"

# CSR border columns that are Foothills export points -> (renamed point, leg).
FOOTHILLS_CSR_POINTS = {
    "Alberta-BC Border Flow": "Foothills BC border (→ Kingsgate → PG&E)",
    "Empress Border Flow": "Empress border (Foothills SK / Canadian Mainline)",
    "Mcneil Border Flow": "McNeill border (Foothills SK)",
}

# NGTL operational-area gates that feed the Foothills export legs, confirmed from
# the outages' "Area for Stated Capability": WGAT ("Alberta/BC and Alberta/Montana
# Borders") and FHZ8 ("Alberta/BC Border") -> Foothills BC -> Kingsgate -> PG&E;
# EGAT ("Empress/McNeill Borders") -> Foothills SK / Canadian Mainline.
EXPORT_GATE_LEG = {"WGAT": "Foothills BC", "FHZ8": "Foothills BC", "EGAT": "Foothills SK"}


class FoothillsClient:
    """Thin export-border view over the shared TC Customer Express feeds."""

    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/foothills",
        session: Optional[requests.Session] = None,
        timeout: int = 60,
        heat_btu_per_cf: float = DEFAULT_HEAT_BTU_PER_CF,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        # Reuse NovaClient for fetching + unit conversion (single source of truth).
        self._nova = NovaClient(
            data_dir=data_dir, session=session, timeout=timeout, heat_btu_per_cf=heat_btu_per_cf
        )

    # -- fetch (delegates to NovaClient) ----------------------------------- #

    def fetch_capability_flow(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        return self._nova.fetch_capability_flow(raw_dir=raw_dir)

    def fetch_system_report(self, *, duration: int = 2, raw_dir: Optional[pathlib.Path] = None) -> str:
        return self._nova.fetch_system_report(duration=duration, raw_dir=raw_dir)

    def fetch_outages(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        return self._nova.fetch_outages(raw_dir=raw_dir)

    # -- parse: select + re-tag the export-border subset ------------------- #

    @staticmethod
    def _retag(rec: FlowRecord, point_name: str) -> FlowRecord:
        return dataclasses.replace(rec, source=SOURCE, point_name=point_name)

    def parse_capability_flow(
        self, text: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> tuple[list[FlowRecord], Optional[str]]:
        """Foothills BC firm + flow + capability, from NGTL's AB/BC chart zone."""
        recs, eff = self._nova.parse_capability_flow(text, gas_day, pulled_at, raw_ref)
        out = [self._retag(r, FOOTHILLS_BC_NAME) for r in recs if r.point_id == FOOTHILLS_BC_ZONE]
        return out, eff

    def parse_system_report(
        self, text: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        """The three Foothills export borders from the Current System Report."""
        recs = self._nova.parse_system_report(text, gas_day, pulled_at, raw_ref)
        out: list[FlowRecord] = []
        for r in recs:
            name = FOOTHILLS_CSR_POINTS.get(r.point_id)
            if name:
                out.append(self._retag(r, name))
        return out

    def parse_outages(self, text: str, gas_day: str, pulled_at: str) -> list[Notice]:
        """NGTL outages restricting a Foothills export gate (WGAT/FHZ8 → BC,
        EGAT → SK), grouped per outage and tagged with the affected leg."""
        out: list[Notice] = []
        for g in self._nova.group_outages(text, gas_day):
            legs = sorted({EXPORT_GATE_LEG[t] for t in g["tables"] if t in EXPORT_GATE_LEG})
            if not legs:
                continue
            out.append(self._nova.outage_notice(SOURCE, g, gas_day, extra=f"Affects {', '.join(legs)}. "))
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, *, duration: int = 2, write: bool = True) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / gas_day
        raw_ref = raw_dir.as_posix()

        chart_text = self.fetch_capability_flow(raw_dir=raw_dir)
        csr_text = self.fetch_system_report(duration=duration, raw_dir=raw_dir)
        outages_text = self.fetch_outages(raw_dir=raw_dir)

        bc_records, eff_day = self.parse_capability_flow(chart_text, gas_day, pulled_at, raw_ref)
        border_records = self.parse_system_report(csr_text, gas_day, pulled_at, raw_ref)
        notices = self.parse_outages(outages_text, gas_day, pulled_at)

        records = bc_records + border_records
        result = {
            "source": SOURCE,
            "gas_day": eff_day or gas_day,
            "cycle": "daily",
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
    now = dt.datetime.now(MOUNTAIN)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull Foothills export-border throughput/capacity (TC Customer Express feeds)."
    )
    parser.add_argument("--gas-day", default=None, help="ISO gas day. Default: latest available (MT).")
    parser.add_argument("--duration", type=int, default=2, help="CSR history window in days (default 2).")
    parser.add_argument("--heat-content", type=float, default=DEFAULT_HEAT_BTU_PER_CF, help="BTU/cf for MMcf->Dth (default 1000).")
    parser.add_argument("--data-dir", default="data/foothills")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gas_day = args.gas_day or _default_gas_day()
    client = FoothillsClient(data_dir=args.data_dir, heat_btu_per_cf=args.heat_content)
    result = client.pull(gas_day, duration=args.duration, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
