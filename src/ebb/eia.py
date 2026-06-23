"""EIA Open Data API v2 — Weekly Natural Gas Storage (README §3 macro/storage).

Clean REST/JSON: ``https://api.eia.gov/v2/...?api_key=...``. We read the Weekly
Natural Gas Storage Report (working gas in underground storage, Bcf) for the
Pacific region (PG&E-relevant), Lower 48, and the other regions for context.

Region codes are NOT hardcoded (R31=East, R34=Mountain — easy to get wrong).
Instead we resolve series IDs at runtime from the dataset's ``facet/series``
metadata, matching by region name; a documented fallback map is used only if
discovery is unavailable (e.g. offline tests).

EIA series are weekly time-series, so this module uses its own small record
shape rather than the point/flow §5 schema. Period dates are the report's
week-ending dates (EIA publishes Thursdays for the prior Friday close).

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import pathlib
from typing import Any, Iterable, Optional

import requests
from dotenv import load_dotenv
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = logging.getLogger("eia")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

SOURCE = "eia"
BASE_URL = "https://api.eia.gov/v2"
STORAGE_ROUTE = "natural-gas/stor/wkly"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) gas-fundamentals/0.1 (+ingestion)"
UTC = dt.timezone.utc

# Region name (lowercased substring) -> documented series ID. Used as a fallback
# only; runtime facet discovery is authoritative. Verify against discovery.
DEFAULT_STORAGE_SERIES: dict[str, str] = {
    "lower 48": "NW2_EPG0_SWO_R48_BCF",
    "east": "NW2_EPG0_SWO_R31_BCF",
    "midwest": "NW2_EPG0_SWO_R32_BCF",
    "south central": "NW2_EPG0_SWO_R33_BCF",
    "mountain": "NW2_EPG0_SWO_R34_BCF",
    "pacific": "NW2_EPG0_SWO_R35_BCF",
}

# Default regions to pull (PG&E cares most about Pacific; Lower 48 for macro).
DEFAULT_REGIONS = ["Pacific", "Lower 48", "Mountain", "South Central", "East", "Midwest"]


# --------------------------------------------------------------------------- #
# Normalized record shape (EIA time-series; not the §5 flow/capacity schema)
# --------------------------------------------------------------------------- #


@dataclasses.dataclass
class EIARecord:
    source: str
    dataset: str            # weekly_storage
    series_id: str
    region: str
    period: str             # ISO week-ending date
    value: Optional[float]  # working gas in storage
    units: str              # Bcf
    wow_change: Optional[float]   # week-over-week change (same units), if computable
    pulled_at_utc: str
    raw_ref: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def utc_now_iso() -> str:
    return dt.datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def load_api_key(explicit: Optional[str] = None) -> str:
    """Resolve the EIA API key from an explicit arg, the environment, or .env."""
    if explicit:
        return explicit
    load_dotenv()  # loads .env from cwd / project root if present
    key = os.getenv("EIA_API_KEY")
    if not key:
        raise RuntimeError(
            "EIA_API_KEY not set. Add it to .env (see .env.example) or pass api_key=. "
            "Register a free key at https://www.eia.gov/opendata/register.php"
        )
    return key


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class EIAClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        data_dir: pathlib.Path | str = "data/eia",
        session: Optional[requests.Session] = None,
        timeout: int = 30,
    ) -> None:
        self.api_key = load_api_key(api_key)
        self.data_dir = pathlib.Path(data_dir)
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._series_cache: Optional[dict[str, str]] = None

    # -- fetch ------------------------------------------------------------- #

    @retry(
        retry=retry_if_exception_type(requests.RequestException),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get(self, path: str, params: Any = None) -> dict[str, Any]:
        # Accept a dict OR a list of (key, value) tuples; the latter preserves
        # duplicate keys like facets[series][] (one per region). Building a dict
        # here would silently drop all but the last duplicate.
        if isinstance(params, dict):
            merged: list[tuple[str, Any]] = list(params.items())
        else:
            merged = list(params or [])
        merged.append(("api_key", self.api_key))
        resp = self.session.get(f"{BASE_URL}/{path}", params=merged, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    # -- series discovery -------------------------------------------------- #

    def discover_storage_series(self) -> dict[str, str]:
        """Map series_id -> human name from the dataset's ``facet/series`` metadata."""
        payload = self._get(f"{STORAGE_ROUTE}/facet/series")
        facets = payload.get("response", {}).get("facets", [])
        return {f["id"]: f.get("name", f["id"]) for f in facets}

    def resolve_region_series(self, regions: Iterable[str]) -> dict[str, str]:
        """Resolve requested region names -> series IDs (discovery first, then fallback)."""
        try:
            if self._series_cache is None:
                self._series_cache = self.discover_storage_series()
            id_to_name = self._series_cache
        except requests.RequestException as exc:  # pragma: no cover - network guard
            log.warning("series discovery failed (%s); using documented fallback map", exc)
            id_to_name = {}

        out: dict[str, str] = {}
        for region in regions:
            key = region.lower()
            match = None
            # Prefer a discovered series whose name contains the region word.
            for sid, name in id_to_name.items():
                if key in name.lower():
                    match = sid
                    break
            if match is None:
                match = DEFAULT_STORAGE_SERIES.get(key)
            if match is None:
                log.warning("no series found for region %r", region)
                continue
            out[region] = match
        return out

    # -- storage ----------------------------------------------------------- #

    def fetch_storage(
        self,
        series_ids: Iterable[str],
        start: Optional[str] = None,
        end: Optional[str] = None,
        raw_dir: Optional[pathlib.Path] = None,
    ) -> list[dict[str, Any]]:
        params: list[tuple[str, Any]] = [
            ("frequency", "weekly"),
            ("data[0]", "value"),
            ("sort[0][column]", "period"),
            ("sort[0][direction]", "asc"),
            ("offset", 0),
            ("length", 5000),
        ]
        for sid in series_ids:
            params.append(("facets[series][]", sid))
        if start:
            params.append(("start", start))
        if end:
            params.append(("end", end))
        payload = self._get(f"{STORAGE_ROUTE}/data", params=params)
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
            (raw_dir / "weekly_storage.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return payload.get("response", {}).get("data", [])

    def normalize_storage(
        self,
        rows: list[dict[str, Any]],
        region_by_series: dict[str, str],
        pulled_at: str,
        raw_ref: Optional[str],
    ) -> list[EIARecord]:
        """EIA rows -> EIARecord, with per-series week-over-week change."""
        # Group by series to compute WoW change in period order.
        by_series: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_series.setdefault(row.get("series", ""), []).append(row)

        records: list[EIARecord] = []
        for sid, series_rows in by_series.items():
            series_rows.sort(key=lambda r: r.get("period", ""))
            region = region_by_series.get(sid) or series_rows[0].get("series-description", sid)
            prev: Optional[float] = None
            for row in series_rows:
                value = to_float(row.get("value"))
                wow = (value - prev) if (value is not None and prev is not None) else None
                records.append(
                    EIARecord(
                        source=SOURCE,
                        dataset="weekly_storage",
                        series_id=sid,
                        region=region,
                        period=row.get("period", ""),
                        value=value,
                        units=row.get("units", "BCF"),
                        wow_change=wow,
                        pulled_at_utc=pulled_at,
                        raw_ref=raw_ref,
                    )
                )
                if value is not None:
                    prev = value
        return records

    # -- orchestration ----------------------------------------------------- #

    def pull(
        self,
        regions: Optional[Iterable[str]] = None,
        start: Optional[str] = None,
        end: Optional[str] = None,
        *,
        write: bool = True,
    ) -> dict[str, Any]:
        regions = list(regions or DEFAULT_REGIONS)
        pulled_at = utc_now_iso()
        tag = f"{start or 'all'}_{end or 'latest'}"
        raw_dir = self.data_dir / tag
        raw_ref = raw_dir.as_posix()

        region_series = self.resolve_region_series(regions)
        series_to_region = {sid: region for region, sid in region_series.items()}
        rows = self.fetch_storage(region_series.values(), start=start, end=end, raw_dir=raw_dir)
        records = self.normalize_storage(rows, series_to_region, pulled_at, raw_ref)

        result = {
            "dataset": "weekly_storage",
            "regions": region_series,
            "start": start,
            "end": end,
            "pulled_at_utc": pulled_at,
            "records": [r.to_dict() for r in records],
        }
        if write:
            out_path = self.data_dir / f"{tag}.normalized.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            log.info("wrote %s (%d records across %d regions)", out_path, len(records), len(region_series))
        return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Pull EIA weekly natural gas storage.")
    parser.add_argument("--dataset", default="storage", choices=["storage"], help="Dataset to pull.")
    parser.add_argument("--regions", nargs="*", default=None, help=f"Region names (default: {DEFAULT_REGIONS}).")
    parser.add_argument("--start", default=None, help="Start period YYYY-MM-DD (week-ending).")
    parser.add_argument("--end", default=None, help="End period YYYY-MM-DD.")
    parser.add_argument("--data-dir", default="data/eia")
    parser.add_argument("--api-key", default=None, help="Override EIA_API_KEY (else from env/.env).")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    client = EIAClient(api_key=args.api_key, data_dir=args.data_dir)
    result = client.pull(regions=args.regions, start=args.start, end=args.end, write=not args.no_write)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
