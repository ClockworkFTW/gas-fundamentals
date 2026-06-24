"""Load normalized EBB lineage from disk for the ETL layer (README §9).

The EBB modules each write ``data/<source>/<gas_day>_<cycle>.normalized.json``
(some sources omit the cycle token). This module reads those files back — it is
the *only* place the ETL touches the filesystem for inputs; ``etl/facts.py`` and
``metrics/`` work on the record dicts these loaders return, so they stay pure and
offline-testable.

Two record shapes exist (README §2): the flow/capacity EBBs emit
``FlowRecord``/``Notice`` keyed by gas day; EIA emits a weekly time series keyed
by week-ending period. They are loaded by separate helpers.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import pathlib
from typing import Any, Iterable, Optional

log = logging.getLogger("etl.load")

# Flow/capacity sources that emit the FlowRecord shape, keyed by gas day.
# (EIA is separate — different shape, keyed by week-ending period.)
# Ruby is intentionally excluded: its Incapsula WAF cookie can't be refreshed
# unattended, and Pipe Ranger's onyx_ruby already carries the Ruby→PG&E flow, so
# Ruby only added upstream context. The module remains (src/ebb/ruby.py) for
# manual use. See README §4.
FLOW_SOURCES: tuple[str, ...] = (
    "pipe_ranger",
    "gtn",
    "el_paso",
    "transwestern",
    "kern_river",
    "nova",
    "foothills",
)

# When a gas day has several cycle files and the caller does not pin one, prefer
# the most-settled / latest-posted cycle (it reflects the day's final noms).
# Bare files (no cycle token) and unknown tokens sort last via the .get default.
_CYCLE_RANK: dict[str, int] = {
    "final": 8,
    "id3": 7,
    "id2": 6,
    "id1": 5,
    "evening": 4,
    "timely": 3,
    "best_available": 2,
    "best": 2,
    "all": 1,
    "": 0,
}

_SUFFIX = ".normalized.json"


@dataclasses.dataclass
class SourcePull:
    """One source's normalized output for a gas day, plus where it came from."""

    source: str
    gas_day: str
    cycle: Optional[str]
    pulled_at_utc: Optional[str]
    records: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    path: str

    @property
    def cycle_token(self) -> str:
        """The cycle token parsed from the filename (may differ from ``cycle``)."""
        return _cycle_token(pathlib.Path(self.path).name, self.gas_day)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def prior_gas_day(gas_day: str) -> str:
    """ISO calendar day before ``gas_day`` (used for day-over-day comparisons)."""
    d = dt.date.fromisoformat(gas_day)
    return (d - dt.timedelta(days=1)).isoformat()


def _cycle_token(filename: str, gas_day: str) -> str:
    """Extract the cycle token from ``<gas_day>_<cycle>.normalized.json``.

    Returns "" when the file carries no cycle token (``<gas_day>.normalized.json``).
    """
    stem = filename[: -len(_SUFFIX)] if filename.endswith(_SUFFIX) else filename
    if stem == gas_day:
        return ""
    prefix = f"{gas_day}_"
    if stem.startswith(prefix):
        return stem[len(prefix):]
    return ""


def _candidates(source_dir: pathlib.Path, gas_day: str) -> list[pathlib.Path]:
    """Normalized files for ``gas_day`` in ``source_dir`` (bare + cycle-tagged)."""
    if not source_dir.is_dir():
        return []
    return sorted(source_dir.glob(f"{gas_day}*{_SUFFIX}"))


def _read(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# Flow/capacity sources
# --------------------------------------------------------------------------- #


def load_source(
    source: str,
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    cycle: Optional[str] = None,
) -> Optional[SourcePull]:
    """Load one flow/capacity source for a gas day.

    If ``cycle`` is given, only the matching ``<gas_day>_<cycle>`` file is used.
    Otherwise the most-settled available cycle is chosen (``_CYCLE_RANK``).
    Returns ``None`` when no lineage file exists.
    """
    source_dir = pathlib.Path(data_root) / source
    candidates = _candidates(source_dir, gas_day)
    if not candidates:
        return None

    if cycle is not None:
        wanted = source_dir / f"{gas_day}_{cycle}{_SUFFIX}"
        chosen = wanted if wanted in candidates else None
        if chosen is None:
            log.warning("%s: no file for gas_day=%s cycle=%s", source, gas_day, cycle)
            return None
    else:
        chosen = max(
            candidates,
            key=lambda p: _CYCLE_RANK.get(_cycle_token(p.name, gas_day), -1),
        )

    payload = _read(chosen)
    return SourcePull(
        source=payload.get("source", source),
        gas_day=payload.get("gas_day", gas_day),
        cycle=payload.get("cycle"),
        pulled_at_utc=payload.get("pulled_at_utc"),
        records=payload.get("records", []),
        notices=payload.get("notices", []),
        path=chosen.as_posix(),
    )


def load_flows(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    sources: Iterable[str] = FLOW_SOURCES,
    cycle: Optional[str] = None,
) -> tuple[dict[str, SourcePull], list[str]]:
    """Load every flow/capacity source for a gas day.

    Returns ``(loaded, missing)`` where ``loaded`` maps source -> ``SourcePull``
    and ``missing`` lists sources with no lineage file for the day. Per-source
    ``cycle`` is only applied to Pipe Ranger-style sources via ``cycle``; sources
    that ignore cycles still resolve through the rank fallback.
    """
    loaded: dict[str, SourcePull] = {}
    missing: list[str] = []
    for source in sources:
        pull = load_source(source, gas_day, data_root=data_root, cycle=cycle)
        if pull is None:
            missing.append(source)
        else:
            loaded[source] = pull
    return loaded, missing


# --------------------------------------------------------------------------- #
# EIA (weekly time series — different shape/cadence)
# --------------------------------------------------------------------------- #


def load_eia(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
) -> Optional[dict[str, Any]]:
    """Load the most recent EIA weekly-storage snapshot usable for ``gas_day``.

    EIA is weekly and keyed by week-ending ``period``, not gas day, so we pick the
    snapshot file with the newest ``pulled_at_utc`` and let the band logic select
    the relevant week (as-of the gas day). Returns the raw snapshot dict (regions
    + records + bands), or ``None`` if no EIA lineage exists.
    """
    eia_dir = pathlib.Path(data_root) / "eia"
    if not eia_dir.is_dir():
        return None
    files = sorted(eia_dir.glob(f"*{_SUFFIX}"))
    if not files:
        return None
    # Newest by captured time; fall back to filename order if pulled_at missing.
    snapshots = [_read(p) for p in files]
    snapshots.sort(key=lambda s: s.get("pulled_at_utc") or "")
    return snapshots[-1]
