"""NOVA / NGTL — NOVA Gas Transmission Ltd. (TC Energy) ingestion.

NGTL is TC Energy's Alberta intra-basin system — the gathering/transmission grid
that moves AECO/NIT-basin supply to its borders. The fundamentals-relevant export
is the **Alberta/BC border** (AB/BC), which hands gas to **Foothills BC** →
**Kingsgate** → **GTN** → **Malin** → **PG&E**. NGTL is therefore the upstream
visibility into the AECO supply that ultimately reaches northern California.

Unlike the US TC Energy "ganesha" platform (``tcplus.com``, see ``gtn.py``), the
Canadian systems publish through **TC Customer Express** (``tccustomerexpress.com``),
whose ``my.`` SPA is backed by a public AWS API Gateway that serves plain CSV via
GET — exactly the README §4 ideal (no auth, no WebForms):

  * ``chart/csv``                      Capability & Historical Flow at NGTL's key
                                       operational zones (USJR, AB/BC, EGAT, OSDA):
                                       actual flow + base/outage capability + firm
                                       design (FT-D) + assumed heat value. Daily.
  * ``csr/csv/?unit=&duration=N``      Current System Report: intraday system-balance
                                       snapshots — receipts, intraprovincial demand,
                                       every export border (incl. Alberta-BC),
                                       linepack, net storage flow.
  * ``csv/outages/``                   Daily Operating Plan outages (maintenance /
                                       capability restrictions) → notices.
  * ``plantturnaroundactivity/csv/``   Upstream plant turnaround receipt/delivery
                                       impacts (supply outages) → notices.

Login-gated (Cognito) and therefore skipped: the general ``bulletin`` notices and
the ``chart/summary`` / Gas Day Summary JSON. The operationally-relevant
maintenance notices are public via the outages CSV.

**Units.** NGTL publishes volumetric **10^3 m^3/d** (plus assumed heat value
GJ/10^3m^3) and firm design as energy **TJ/d**. We normalize everything to the
canonical **Dth/d** (see README §5): chart flows/capabilities use the per-zone heat
value (precise); CSR is fetched in MMcf and converted with a configurable heat
content (consistent with ``pipe_ranger``). Originals are always preserved.

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
from tenacity import retry

from .base import RETRY, BaseEBBClient, write_raw
from .schema import FlowRecord, Notice, default_gas_day, norm_date, to_float, utc_now_iso

log = logging.getLogger("nova")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "nova"
TSP = "NGTL"
API_BASE = "https://f51561ras5.execute-api.us-west-2.amazonaws.com/production"
SPA_URL = "https://my.tccustomerexpress.com/"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"
# NGTL operates on the Alberta (Mountain) gas day.
MOUNTAIN = ZoneInfo("America/Edmonton")

# Energy conversions to the canonical Dth.
#   1 GJ  = 0.94781712 MMBtu = 0.94781712 Dth
#   1 TJ  = 1000 GJ
GJ_TO_DTH = 0.94781712
TJ_TO_DTH = 1000.0 * GJ_TO_DTH
# Default heat content for MMcf -> Dth (BTU/cf); see pipe_ranger. 1 MMcf @ 1000
# BTU/cf == 1000 Dth. Configurable via --heat-content.
DEFAULT_HEAT_BTU_PER_CF = 1000.0

# chart/csv operational zones. Direction is from NGTL's perspective: USJR is the
# upstream supply corridor (receipt); the others are border/area deliveries. AB/BC
# is the export toward Foothills BC / Kingsgate / GTN / Malin / PG&E.
CHART_ZONES = {
    "USJR": {"name": "USJR (Upstream James River corridor)", "direction": "receipt"},
    "AB/BC": {"name": "AB/BC Border (to Foothills BC → Kingsgate)", "direction": "delivery"},
    "EGAT": {"name": "EGAT (East Gate)", "direction": "delivery"},
    "OSDA": {"name": "OSDA", "direction": "delivery"},
}

# csr/csv system-balance columns -> (normalized point name, direction|None,
# dataset_type). Linepack / storage / target columns carry no flow direction and
# are emitted as supply_demand context.
CSR_COLUMNS = {
    "NGTL-Field Receipts": ("NGTL Field Receipts", "receipt", "scheduled_quantity"),
    "Groundbirch East Receipt": ("Groundbirch East Receipt", "receipt", "scheduled_quantity"),
    "Gordondale Receipt": ("Gordondale Receipt", "receipt", "scheduled_quantity"),
    "Total Receipts": ("Total Receipts", "receipt", "scheduled_quantity"),
    "Intraprovincial Demand": ("Intraprovincial Demand", "delivery", "scheduled_quantity"),
    "Empress Border Flow": ("Empress Border (to Canadian Mainline)", "delivery", "scheduled_quantity"),
    "Mcneil Border Flow": ("McNeill Border", "delivery", "scheduled_quantity"),
    "Alberta-BC Border Flow": ("Alberta-BC Border (to Foothills BC → Kingsgate)", "delivery", "scheduled_quantity"),
    "Willow Valley Interconnect": ("Willow Valley Interconnect", "delivery", "scheduled_quantity"),
    "Total Deliveries": ("Total Deliveries", "delivery", "scheduled_quantity"),
    "Current Linepack": ("Current Linepack", None, "supply_demand"),
    "Net Storage Flow": ("Net Storage Flow", None, "supply_demand"),
    "Linepack Target": ("Linepack Target", None, "supply_demand"),
}

OUTAGES_URL = f"{SPA_URL}#Outages"
# Notice window around the gas day: keep outages still active (ended on/after
# gas_day - LOOKBACK) and starting within gas_day + LOOKAHEAD.
NOTICE_LOOKBACK_DAYS = 7
NOTICE_LOOKAHEAD_DAYS = 45


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class NovaClient(BaseEBBClient):
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/nova",
        session: Optional[requests.Session] = None,
        timeout: int = 60,
        heat_btu_per_cf: float = DEFAULT_HEAT_BTU_PER_CF,
    ) -> None:
        super().__init__(
            data_dir,
            session,
            timeout,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/csv, */*",
                "Origin": "https://my.tccustomerexpress.com",
                "Referer": SPA_URL,
            },
        )
        self.heat_btu_per_cf = heat_btu_per_cf

    @retry(**RETRY)
    def _get_csv(self, path: str, params: Optional[dict[str, Any]] = None) -> str:
        resp = self.session.get(f"{API_BASE}/{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.text

    # -- fetch ------------------------------------------------------------- #

    def fetch_capability_flow(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        text = self._get_csv("chart/csv")
        write_raw(raw_dir, "chart.csv", text)
        return text

    def fetch_system_report(self, *, duration: int = 2, raw_dir: Optional[pathlib.Path] = None) -> str:
        # Fetch in MMcf so the conversion reuses the heat-content assumption.
        text = self._get_csv("csr/csv/", params={"unit": "MMcf", "duration": duration})
        write_raw(raw_dir, "csr.csv", text)
        return text

    def fetch_outages(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        text = self._get_csv("csv/outages/")
        write_raw(raw_dir, "outages.csv", text)
        return text

    def fetch_plant_turnarounds(self, *, raw_dir: Optional[pathlib.Path] = None) -> str:
        text = self._get_csv("plantturnaroundactivity/csv/")
        write_raw(raw_dir, "plant_turnarounds.csv", text)
        return text

    # -- conversion helpers ------------------------------------------------ #

    @staticmethod
    def _vol_to_dth(vol_10e3m3: Optional[float], heat_gj: Optional[float]) -> Optional[float]:
        """10^3 m^3/d * (GJ/10^3 m^3) -> GJ/d -> Dth/d."""
        if vol_10e3m3 is None or heat_gj is None:
            return None
        return round(vol_10e3m3 * heat_gj * GJ_TO_DTH, 1)

    def _mmcf_to_dth(self, mmcf: Optional[float]) -> Optional[float]:
        if mmcf is None:
            return None
        return round(mmcf * self.heat_btu_per_cf, 1)

    # -- parse: capability & historical flow (chart) ----------------------- #

    @staticmethod
    def _col(row: dict[str, str], zone: str, metric: str) -> Optional[float]:
        """Find a chart column for ``zone``/``metric`` regardless of unit suffix."""
        for key, val in row.items():
            k = key.strip()
            if k.startswith(f"{zone} {metric}"):
                return to_float(val)
        return None

    def parse_capability_flow(
        self, text: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> tuple[list[FlowRecord], Optional[str]]:
        """Emit one FlowRecord per zone for ``gas_day`` (or the latest row with
        actual flow at or before it). Returns (records, effective_gas_day)."""
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        chosen, eff_day = self._select_chart_row(rows, gas_day)
        if chosen is None:
            return [], None

        records: list[FlowRecord] = []
        for zone, meta in CHART_ZONES.items():
            actual = self._col(chosen, zone, "Actual Flow")
            base = self._col(chosen, zone, "Base Capability")
            outage = self._col(chosen, zone, "Outage Capability")
            heat = self._col(chosen, zone, "Assumed Heat Value")
            ftd_tj = self._col(chosen, zone, "FT-D")
            impact = self._col(chosen, zone, "Impact")

            sched = self._vol_to_dth(actual, heat)
            operating_vol = outage if outage is not None else base
            operating = self._vol_to_dth(operating_vol, heat)
            design = round(ftd_tj * TJ_TO_DTH, 1) if ftd_tj is not None else None
            available = None
            if operating is not None and sched is not None:
                available = round(operating - sched, 1)

            outage_desc = ""
            for key, val in chosen.items():
                if key.strip().startswith(f"{zone} Outage Description") and (val or "").strip():
                    outage_desc = val.strip()
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="operationally_available",
                    gas_day=eff_day,
                    cycle="daily",
                    point_name=meta["name"],
                    point_id=zone,
                    flow_direction=meta["direction"],
                    scheduled_qty=sched,
                    design_capacity=design,
                    operational_capacity=operating,
                    available_capacity=available,
                    units="Dth/d",
                    original_units="10^3m^3/d",
                    original_qty=actual,
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
            if impact:
                log.debug("zone %s outage impact %s: %s", zone, impact, outage_desc)
        return records, eff_day

    @staticmethod
    def _select_chart_row(rows: list[dict[str, str]], gas_day: str) -> tuple[Optional[dict[str, str]], Optional[str]]:
        """Pick the row for gas_day; else the latest row at/before gas_day that
        carries an actual flow (forward rows hold only capability)."""
        exact = None
        fallback = None
        fallback_day = None
        for row in rows:
            day = norm_date(row.get("Gas Day"))
            if day is None:
                continue
            if day == gas_day:
                exact = row
            if day <= gas_day:
                has_actual = any(
                    (v or "").strip() for k, v in row.items() if "Actual Flow" in k
                )
                if has_actual and (fallback_day is None or day > fallback_day):
                    fallback, fallback_day = row, day
        if exact is not None:
            # Prefer exact even if actuals are still blank (intraday/current day).
            has_actual = any((v or "").strip() for k, v in exact.items() if "Actual Flow" in k)
            if has_actual or fallback is None:
                return exact, gas_day
        if fallback is not None:
            return fallback, fallback_day
        return (rows[-1], norm_date(rows[-1].get("Gas Day"))) if rows else (None, None)

    # -- parse: current system report (csr) -------------------------------- #

    def parse_system_report(
        self, text: str, gas_day: str, pulled_at: str, raw_ref: Optional[str]
    ) -> list[FlowRecord]:
        """Emit the latest CSR snapshot for ``gas_day`` (else the latest available)
        as one FlowRecord per balance column. Values are MMcf -> Dth/d."""
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows:
            return []

        def row_day(row: dict[str, str]) -> Optional[str]:
            return norm_date(row.get("Timestamp"))

        same_day = [r for r in rows if row_day(r) == gas_day]
        snapshot = same_day[-1] if same_day else rows[-1]
        eff_day = row_day(snapshot) or gas_day
        ts = (snapshot.get("Timestamp") or "").strip()

        records: list[FlowRecord] = []
        for col, (name, direction, dtype) in CSR_COLUMNS.items():
            if col not in snapshot:
                continue
            mmcf = to_float(snapshot.get(col))
            records.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type=dtype,
                    gas_day=eff_day,
                    cycle="current",
                    point_name=name,
                    point_id=col,
                    flow_direction=direction,
                    scheduled_qty=self._mmcf_to_dth(mmcf),
                    design_capacity=None,
                    operational_capacity=None,
                    available_capacity=None,
                    units="Dth/d",
                    original_units="MMcf/d",
                    original_qty=mmcf,
                    pulled_at_utc=pulled_at,
                    raw_ref=f"{raw_ref} @ {ts}" if raw_ref else None,
                )
            )
        return records

    # -- parse: outages + plant turnarounds -> notices --------------------- #

    def group_outages(self, text: str, gas_day: str) -> list[dict[str, Any]]:
        """Group the outages CSV into one record per outage.

        The feed has **one row per (outage × affected operational-area gate)** —
        the same physical outage repeats with a different ``Table`` (gate) and the
        ``Area for Stated Capability`` it applies to. We collapse to one record per
        Outage Id, collecting the affected gates/areas, within the
        −LOOKBACK/+LOOKAHEAD window. Reused by ``foothills.py`` to select the
        export-gate outages.
        """
        reader = csv.DictReader(io.StringIO(text))
        day = dt.date.fromisoformat(gas_day)
        cutoff = (day - dt.timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
        horizon = (day + dt.timedelta(days=NOTICE_LOOKAHEAD_DAYS)).isoformat()
        groups: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        for row in reader:
            start = norm_date(row.get("Start"))
            end = norm_date(row.get("End"))
            # Keep outages overlapping the window [gas_day - LOOKBACK, gas_day + LOOKAHEAD].
            if end is not None and end < cutoff:
                continue
            if start is not None and start > horizon:
                continue
            oid = (row.get("Outage Id") or "").strip() or (row.get("Description") or "").strip()
            g = groups.get(oid)
            if g is None:
                g = {
                    "outage_id": oid,
                    "description": (row.get("Description") or "").strip(),
                    "restriction": (row.get("Type of Restriction") or "").strip(),
                    "start": start,
                    "end": end,
                    "tables": [],
                    "areas": [],
                }
                groups[oid] = g
                order.append(oid)
            tbl = (row.get("Table") or "").strip()
            if tbl and tbl not in g["tables"]:
                g["tables"].append(tbl)
            area = (row.get("Area for Stated Capability") or "").strip()
            if area and area not in g["areas"]:
                g["areas"].append(area)
        return [groups[o] for o in order]

    @staticmethod
    def outage_notice(source: str, g: dict[str, Any], gas_day: str, *, extra: str = "") -> Notice:
        areas = "; ".join(g["areas"]) if g["areas"] else ", ".join(g["tables"])
        body = (
            f"{g['restriction']}. {extra}Affected areas: {areas}. "
            f"Effective {g['start']} through {g['end']}."
        ).strip()
        return Notice(
            source=source,
            gas_day=g["start"] or gas_day,
            posted_at=None,
            notice_type="maintenance",
            stage=", ".join(g["tables"]) or None,
            headline=g["description"] or "NGTL outage",
            body=body,
            url=OUTAGES_URL,
        )

    def parse_outages(self, text: str, gas_day: str, pulled_at: str) -> list[Notice]:
        return [self.outage_notice(SOURCE, g, gas_day) for g in self.group_outages(text, gas_day)]

    def parse_plant_turnarounds(self, text: str, gas_day: str, pulled_at: str) -> list[Notice]:
        reader = csv.DictReader(io.StringIO(text))
        day = dt.date.fromisoformat(gas_day)
        cutoff = (day - dt.timedelta(days=NOTICE_LOOKBACK_DAYS)).isoformat()
        horizon = (day + dt.timedelta(days=NOTICE_LOOKAHEAD_DAYS)).isoformat()
        out: list[Notice] = []
        for row in reader:
            start = norm_date(row.get("Start"))
            end = norm_date(row.get("End"))
            if end is not None and end < cutoff:
                continue
            if start is not None and start > horizon:
                continue
            kind = (row.get("Type") or "").strip()  # Receipt | Delivery
            impact = (row.get("Impact") or "").strip()
            out.append(
                Notice(
                    source=SOURCE,
                    gas_day=start or gas_day,
                    posted_at=None,
                    notice_type="maintenance",
                    stage="plant turnaround",
                    headline=f"Upstream plant turnaround — {kind} impact {impact} (10^3m^3/d)",
                    body=f"NGTL upstream plant turnaround. {kind} impact {impact} 10^3m^3/d, effective {start} through {end}.",
                    url=OUTAGES_URL,
                )
            )
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, *, duration: int = 2, write: bool = True) -> dict[str, Any]:
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / gas_day
        raw_ref = raw_dir.as_posix()

        chart_text = self.fetch_capability_flow(raw_dir=raw_dir)
        csr_text = self.fetch_system_report(duration=duration, raw_dir=raw_dir)
        outages_text = self.fetch_outages(raw_dir=raw_dir)
        plant_text = self.fetch_plant_turnarounds(raw_dir=raw_dir)

        chart_records, eff_day = self.parse_capability_flow(chart_text, gas_day, pulled_at, raw_ref)
        csr_records = self.parse_system_report(csr_text, gas_day, pulled_at, raw_ref)
        notices = self.parse_outages(outages_text, gas_day, pulled_at)
        notices += self.parse_plant_turnarounds(plant_text, gas_day, pulled_at)

        records = chart_records + csr_records
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


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull NOVA/NGTL (TC Energy) capability+flow, system balance, and outages."
    )
    parser.add_argument("--gas-day", default=None, help="ISO gas day, e.g. 2026-06-22. Default: latest available (MT).")
    parser.add_argument("--duration", type=int, default=2, help="CSR history window in days (default 2).")
    parser.add_argument("--heat-content", type=float, default=DEFAULT_HEAT_BTU_PER_CF, help="BTU/cf for MMcf->Dth (default 1000).")
    parser.add_argument("--data-dir", default="data/nova")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    gas_day = args.gas_day or default_gas_day(MOUNTAIN)
    client = NovaClient(data_dir=args.data_dir, heat_btu_per_cf=args.heat_content)
    result = client.pull(gas_day, duration=args.duration, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
