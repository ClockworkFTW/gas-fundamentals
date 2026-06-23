"""PG&E CGT — Pipe Ranger ingestion (README §8).

Pipe Ranger's public site (pge.com/pipeline) renders empty table cells that a
JavaScript clientlib fills by GET-ing plain JSON servlets under
``https://www.pge.com/bin/pipeline/``.  There is **no** CSV download or ASP.NET
WebForms POST to replicate — the in-browser "Download to Excel" is built
client-side from these same JSON responses.  So we fetch the JSON directly.

This module fetches the operational datasets for a gas day + cycle, normalizes
them to the common record shape (README §5), and returns ``records`` + ``notices``.

Units: scheduled volumes and physical capacity are already in **Dth**; the wide
``PlanData`` datasets (supply/demand, storage, inventory) are in **MMcf** and are
converted to Dth on ingest (README §5 unit gotcha).  Canonical internal unit is
Dth/d; the original unit/value is always preserved on the record.

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
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .schema import FlowRecord, Notice, norm_date, to_float, utc_now_iso

log = logging.getLogger("pipe_ranger")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "pipe_ranger"
BASE_URL = "https://www.pge.com/bin/pipeline"
REFERER = "https://www.pge.com/pipeline/operations/cgt_pipeline_status.page"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"

PACIFIC = ZoneInfo("America/Los_Angeles")

# Heat content for MMcf -> Dth.  1 MMcf at h BTU/cf == h Dth (1 Dth = 1 MMBtu).
# Default ~1000 BTU/cf => 1 MMcf ~= 1000 Dth.  Configurable; per-point values
# from the scheduledvolumedata servlet override this where available.
DEFAULT_HEAT_CONTENT_BTU_PER_CF = 1000.0

# JSON servlets we read (README §8).  POST endpoints are noted in fetch methods.
ENDPOINTS = {
    "scheduled_volumes": "scheduledvolumes",          # scheduled flows by path/cycle (Dth)
    "physical_capacity": "dthphysicalpipeline",       # physical capacity (Dth)
    "supply_demand": "supplydemand",                  # 7-day supply/demand PlanData (MMcf)
    "storage": "storageactivity",                     # storage inj/withdrawal PlanData (MMcf)
    "inventory_summary": "systeminventorysummary",    # forecast inventory PlanData (MMcf)
    "inventory_status": "systemInventoryStatus",      # ending-inventory series (MMcf)
    "daily_btu": "scheduledvolumedata",               # per-point heat content (BTU/cf)
    "ofo": "ofoefoarchive",                           # POST ofotype=ofo
    "efo": "ofoefoarchive",                           # POST ofotype=efo
}

# scheduledvolumes field -> (point_name, flow_direction).  Border zone in name.
# Totals are kept (point_name "* Total") for convenient cross-checks.
SCHEDULED_POINTS: dict[str, tuple[str, str]] = {
    "malin_gtn": ("Malin (GTN)", "receipt"),
    "onyx_ruby": ("Malin (Ruby)", "receipt"),
    "redwood_total": ("Redwood Path Total", "receipt"),
    "baja_elpaso": ("Topock (El Paso)", "receipt"),
    "baja_transw": ("Topock (Transwestern)", "receipt"),
    "baja_strails": ("Topock (Southern Trails)", "receipt"),
    "baja_hdl": ("KRGT-HDL (Baja)", "receipt"),
    "baja_daggett": ("Daggett (KRGT)", "receipt"),
    "baja_total": ("Baja Path Total", "receipt"),
    "on_krs": ("On-System KRS", "receipt"),
    "off_krs": ("Off-System KRS", "delivery"),
    "off_frp": ("Off-System FRP", "delivery"),
}

# dthphysicalpipeline PlanData field -> point_name (physical/design capacity, Dth).
CAPACITY_POINTS: dict[str, str] = {
    "Redwood_Phys_Cap": "Redwood Path",
    "Phys_PGT_NW": "Malin (GTN)",
    "Ruby_Phys_Cap": "Malin (Ruby)",
    "Topock_Phys_Cap": "Topock",
    "Baja_HinkleyNorth_Capacity": "Baja Hinkley North",
    "KRGT_Daggett_Phys": "Daggett (KRGT)",
    "El_Paso_Phys": "Topock (El Paso)",
    "TW_Phys_Cap": "Topock (Transwestern)",
}

# Storage facilities: (point_name, injection_field, withdrawal_field) in PlanData (MMcf).
STORAGE_FACILITIES: list[tuple[str, str, str]] = [
    ("Wild Goose", "WG_Net_Inj", "WG_Net_Wd"),
    ("Lodi", "Lodi_Net_Inj", "Lodi_Net_Wd"),
    ("Central Valley", "CVGS_Inj_Phys", "CVGS_WD_Phys"),
    ("Gill Ranch", "GRS_LLC_Inj", "GRS_LLC_WD"),
]

# supplydemand PlanData: a curated set of system metrics worth emitting as records.
SUPPLY_DEMAND_METRICS: dict[str, str] = {
    "Supply_Sys": "System Supply",
    "System_Demand": "System Demand",
    "Core": "Core Demand",
    "MeanTemp": "Mean Temperature",
}

# Map the daily_btu PipeRanger_Name to a point heat content lookup key.
BTU_NAME_TO_POINT = {
    "From Gas Transmission Northwest": "Malin (GTN)",
    "From Ruby": "Malin (Ruby)",
    "From El Paso Natural Gas": "Topock (El Paso)",
    "From Transwestern": "Topock (Transwestern)",
    "From Southern Trails": "Topock (Southern Trails)",
    "From KRGT-Daggett": "Daggett (KRGT)",
    "From/To KRGT-HDL": "KRGT-HDL (Baja)",
    "To SoCalGas": "Off-System KRS",
}

# Cycle aliases -> substring matched against the servlet's cycle label.
CYCLE_ALIASES = {
    "timely": "Timely",
    "evening": "Evening",
    "id1": "ID1",
    "id2": "ID2",
    "id3": "ID3",
    "final": "Final",
}


# --------------------------------------------------------------------------- #
# Small helpers  (FlowRecord/Notice/to_float/norm_date/utc_now_iso: see schema.py)
# --------------------------------------------------------------------------- #


def pacific_today() -> dt.date:
    return dt.datetime.now(PACIFIC).date()


def mmcf_to_dth(mmcf: Optional[float], btu_per_cf: float = DEFAULT_HEAT_CONTENT_BTU_PER_CF) -> Optional[float]:
    """1 MMcf at h BTU/cf == h Dth (1 Dth = 1 MMBtu)."""
    if mmcf is None:
        return None
    return mmcf * btu_per_cf


def _plan_day_index(plan_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map a PlanData item's Auto_Generated.Day entries by normalized Fcst_Date.

    Each entry carries ``id`` (Yesterday/Today/Today+1…) and ``Plan_Num``
    ('F' == Final cycle; integers are plan numbers).
    """
    out: dict[str, dict[str, Any]] = {}
    days = (
        plan_data.get("Additional_Data", {})
        .get("Auto_Generated", {})
        .get("Day", [])
    )
    for d in days:
        iso = norm_date(d.get("Fcst_Date"))
        if iso:
            out[iso] = d
    return out


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class PipeRangerClient:
    def __init__(
        self,
        data_dir: pathlib.Path | str = "data/pipe_ranger",
        heat_content_btu_per_cf: float = DEFAULT_HEAT_CONTENT_BTU_PER_CF,
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ) -> None:
        self.data_dir = pathlib.Path(data_dir)
        self.heat_content = heat_content_btu_per_cf
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": REFERER,
            }
        )

    # -- fetch ------------------------------------------------------------- #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _request(self, method: str, endpoint: str, **kwargs: Any) -> str:
        url = f"{BASE_URL}/{endpoint}"
        resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        resp.raise_for_status()
        return resp.text

    @staticmethod
    def _decode(text: str) -> Any:
        """Some servlets return JSON, some return a JSON string-of-JSON."""
        obj = json.loads(text)
        if isinstance(obj, str):
            obj = json.loads(obj)
        return obj

    def fetch(self, key: str, *, raw_dir: Optional[pathlib.Path] = None) -> Any:
        """Fetch one logical dataset by ENDPOINTS key, saving the raw response."""
        endpoint = ENDPOINTS[key]
        if key in ("ofo", "efo"):
            text = self._request("POST", endpoint, data={"ofotype": key})
        else:
            text = self._request("GET", endpoint)
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / f"{key}.json").write_text(text, encoding="utf-8")
        return self._decode(text)

    # -- parse ------------------------------------------------------------- #

    def parse_scheduled_volumes(
        self, payload: dict[str, Any], gas_day: str, cycle: Optional[str], pulled_at: str, raw_ref: str
    ) -> list[FlowRecord]:
        rows = payload.get("schd_values", {}).get("v_schd_value", [])
        alias = CYCLE_ALIASES.get((cycle or "").lower(), cycle)
        out: list[FlowRecord] = []
        for row in rows:
            if norm_date(row.get("gas_day")) != gas_day:
                continue
            row_cycle = str(row.get("cycle", "")).strip()
            if alias and alias.lower() not in row_cycle.lower():
                continue
            for field, (point, direction) in SCHEDULED_POINTS.items():
                qty = to_float(row.get(field))
                if qty is None:
                    continue
                out.append(
                    FlowRecord(
                        source=SOURCE,
                        dataset_type="scheduled_quantity",
                        gas_day=gas_day,
                        cycle=row_cycle,
                        point_name=point,
                        point_id=field,
                        flow_direction=direction,
                        scheduled_qty=qty,            # already Dth
                        design_capacity=None,
                        operational_capacity=None,
                        available_capacity=None,
                        units="Dth/d",
                        original_units="Dth/d",
                        original_qty=qty,
                        pulled_at_utc=pulled_at,
                        raw_ref=raw_ref,
                    )
                )
        return out

    def parse_physical_capacity(
        self, payload: list[dict[str, Any]], gas_day: str, pulled_at: str, raw_ref: str
    ) -> list[FlowRecord]:
        out: list[FlowRecord] = []
        for item in payload:
            plan = item.get("PlanData", {})
            if norm_date(plan.get("Date")) != gas_day:
                continue
            day_meta = _plan_day_index(plan).get(gas_day, {})
            cycle = "Final" if str(day_meta.get("Plan_Num")) == "F" else day_meta.get("id")
            for field, point in CAPACITY_POINTS.items():
                cap = to_float(plan.get(field))
                if cap is None:
                    continue
                out.append(
                    FlowRecord(
                        source=SOURCE,
                        dataset_type="operationally_available",
                        gas_day=gas_day,
                        cycle=cycle,
                        point_name=point,
                        point_id=field,
                        flow_direction="receipt",
                        scheduled_qty=None,
                        design_capacity=cap,           # already Dth
                        operational_capacity=cap,
                        available_capacity=None,
                        units="Dth/d",
                        original_units="Dth/d",
                        original_qty=cap,
                        pulled_at_utc=pulled_at,
                        raw_ref=raw_ref,
                    )
                )
        return out

    def parse_storage(
        self, payload: list[dict[str, Any]], gas_day: str, pulled_at: str, raw_ref: str
    ) -> list[FlowRecord]:
        out: list[FlowRecord] = []
        for item in payload:
            plan = item.get("PlanData", {})
            if norm_date(plan.get("Date")) != gas_day:
                continue
            for facility, inj_field, wd_field in STORAGE_FACILITIES:
                for direction, field in (("injection", inj_field), ("withdrawal", wd_field)):
                    mmcf = to_float(plan.get(field))
                    if mmcf is None:
                        continue
                    out.append(
                        FlowRecord(
                            source=SOURCE,
                            dataset_type="storage",
                            gas_day=gas_day,
                            cycle=None,
                            point_name=facility,
                            point_id=field,
                            flow_direction=direction,
                            scheduled_qty=mmcf_to_dth(mmcf, self.heat_content),
                            design_capacity=None,
                            operational_capacity=None,
                            available_capacity=None,
                            units="Dth/d",
                            original_units="MMcf/d",
                            original_qty=mmcf,
                            pulled_at_utc=pulled_at,
                            raw_ref=raw_ref,
                        )
                    )
        return out

    def parse_supply_demand(
        self, payload: list[dict[str, Any]], gas_day: str, pulled_at: str, raw_ref: str
    ) -> list[FlowRecord]:
        out: list[FlowRecord] = []
        for item in payload:
            plan = item.get("PlanData", {})
            if norm_date(plan.get("Date")) != gas_day:
                continue
            for field, label in SUPPLY_DEMAND_METRICS.items():
                val = to_float(plan.get(field))
                if val is None:
                    continue
                is_temp = field == "MeanTemp"
                out.append(
                    FlowRecord(
                        source=SOURCE,
                        dataset_type="supply_demand",
                        gas_day=gas_day,
                        cycle=None,
                        point_name=label,
                        point_id=field,
                        flow_direction=None,
                        scheduled_qty=val if is_temp else mmcf_to_dth(val, self.heat_content),
                        design_capacity=None,
                        operational_capacity=None,
                        available_capacity=None,
                        units="degF" if is_temp else "Dth/d",
                        original_units="degF" if is_temp else "MMcf/d",
                        original_qty=val,
                        pulled_at_utc=pulled_at,
                        raw_ref=raw_ref,
                    )
                )
        return out

    def parse_inventory(
        self, payload: dict[str, Any], gas_day: str, pulled_at: str, raw_ref: str
    ) -> list[FlowRecord]:
        """systemInventoryStatus: parallel arrays Date[]/Inv_End[]/Min[]/Max[]."""
        dates = payload.get("Date", [])
        inv_end = payload.get("Inv_End", [])
        inv_min = payload.get("MinInventory", [])
        inv_max = payload.get("MaxInventory", [])
        out: list[FlowRecord] = []
        for i, raw in enumerate(dates):
            if norm_date(raw) != gas_day:
                continue
            end = to_float(inv_end[i]) if i < len(inv_end) else None
            mmcf = end
            out.append(
                FlowRecord(
                    source=SOURCE,
                    dataset_type="inventory",
                    gas_day=gas_day,
                    cycle=None,
                    point_name="System Ending Inventory",
                    point_id="Inv_End",
                    flow_direction=None,
                    scheduled_qty=mmcf_to_dth(mmcf, self.heat_content),
                    design_capacity=mmcf_to_dth(to_float(inv_max[i]), self.heat_content) if i < len(inv_max) else None,
                    operational_capacity=None,
                    available_capacity=mmcf_to_dth(to_float(inv_min[i]), self.heat_content) if i < len(inv_min) else None,
                    units="Dth",
                    original_units="MMcf",
                    original_qty=mmcf,
                    pulled_at_utc=pulled_at,
                    raw_ref=raw_ref,
                )
            )
        return out

    def parse_notices(self, ofo_payload: Any, efo_payload: Any, pulled_at: str, url: str) -> list[Notice]:
        out: list[Notice] = []
        for payload, ntype in ((ofo_payload, "OFO"), (efo_payload, "EFO")):
            for item in payload or []:
                gas_day = norm_date(item.get("gasDay")) or str(item.get("gasDay", ""))
                desc = item.get("typeDesc", ntype)
                reason = item.get("reason", "")
                stage = item.get("stage")
                charge = item.get("nonComplianceCharge")
                out.append(
                    Notice(
                        source=SOURCE,
                        gas_day=gas_day,
                        posted_at=None,
                        notice_type=ntype,
                        stage=stage,
                        headline=f"{desc} — {reason}".strip(" —"),
                        body=(
                            f"{desc} for gas day {gas_day}. Reason: {reason}. "
                            f"Stage: {stage}. Noncompliance charge: {charge}. "
                            f"Customers affected: {item.get('numCustomer')}."
                        ),
                        url=url,
                    )
                )
        return out

    def per_point_heat_content(self, daily_btu_payload: dict[str, Any]) -> dict[str, float]:
        """Map point_name -> heat content (BTU/cf) from scheduledvolumedata."""
        out: dict[str, float] = {}
        for row in daily_btu_payload.get("Daily_BTU_Values", {}).get("v_BTU_values", []):
            point = BTU_NAME_TO_POINT.get(str(row.get("PipeRanger_Name", "")).strip())
            btu = to_float(row.get("Daily_BTU"))
            if point and btu:
                out[point] = btu
        return out

    # -- orchestration ----------------------------------------------------- #

    def pull(self, gas_day: str, cycle: Optional[str] = None, *, write: bool = True) -> dict[str, Any]:
        """Fetch + normalize all datasets for one gas day / cycle.

        Returns ``{"gas_day", "cycle", "records": [...], "notices": [...]}``.
        """
        pulled_at = utc_now_iso()
        raw_dir = self.data_dir / f"{gas_day}_{cycle or 'all'}"
        raw_ref = raw_dir.as_posix()

        sched = self.fetch("scheduled_volumes", raw_dir=raw_dir)
        cap = self.fetch("physical_capacity", raw_dir=raw_dir)
        sd = self.fetch("supply_demand", raw_dir=raw_dir)
        storage = self.fetch("storage", raw_dir=raw_dir)
        inv_status = self.fetch("inventory_status", raw_dir=raw_dir)
        self.fetch("inventory_summary", raw_dir=raw_dir)  # saved for lineage; status drives records
        self.fetch("daily_btu", raw_dir=raw_dir)          # saved for lineage / future per-point conversion
        ofo = self.fetch("ofo", raw_dir=raw_dir)
        efo = self.fetch("efo", raw_dir=raw_dir)

        records: list[FlowRecord] = []
        records += self.parse_scheduled_volumes(sched, gas_day, cycle, pulled_at, raw_ref)
        records += self.parse_physical_capacity(cap, gas_day, pulled_at, raw_ref)
        records += self.parse_storage(storage, gas_day, pulled_at, raw_ref)
        records += self.parse_supply_demand(sd, gas_day, pulled_at, raw_ref)
        records += self.parse_inventory(inv_status, gas_day, pulled_at, raw_ref)
        # ofoefoarchive returns the full historical archive; keep only notices
        # active for this gas day or later (forward-dated orders) so a daily
        # feed isn't polluted by years-old events.
        notices = [
            n for n in self.parse_notices(ofo, efo, pulled_at, REFERER)
            if (norm_date(n.gas_day) or "") >= gas_day
        ]

        result = {
            "gas_day": gas_day,
            "cycle": cycle,
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
            "notices": [n.to_dict() for n in notices],
        }
        if write:
            out_path = self.data_dir / f"{gas_day}_{cycle or 'all'}.normalized.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records, %d notices)", out_path, len(records), len(notices))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _default_gas_day() -> str:
    """Prior gas day before 08:00 PT (final not yet posted), else today (PT)."""
    now = dt.datetime.now(PACIFIC)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pull PG&E Pipe Ranger data for a gas day/cycle.")
    parser.add_argument("--gas-day", default=None, help="ISO gas day (Pacific), e.g. 2026-06-21. Default: latest available.")
    parser.add_argument("--cycle", default=None, help="timely | evening | id1 | id2 | id3 | final")
    parser.add_argument("--data-dir", default="data/pipe_ranger")
    parser.add_argument("--heat-content", type=float, default=DEFAULT_HEAT_CONTENT_BTU_PER_CF, help="BTU/cf for MMcf->Dth")
    parser.add_argument("--no-write", action="store_true", help="Do not write lineage/normalized files.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    gas_day = args.gas_day or _default_gas_day()
    client = PipeRangerClient(data_dir=args.data_dir, heat_content_btu_per_cf=args.heat_content)
    result = client.pull(gas_day, args.cycle, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
