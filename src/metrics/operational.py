"""Operational day-over-day deltas (README §2 pre-computed series).

The hybrid compute split keeps the windowed day-over-day math in Python and
folds it into ``fact_operational`` as the ``dod_change`` column; Power BI does
the aggregational/ratio work (utilization, basis) in DAX. ``etl/facts.py`` calls
``day_over_day`` to attach per-point deltas to the operational fact rows.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def day_over_day(
    today_records: Iterable[dict[str, Any]],
    prior_records: Iterable[dict[str, Any]],
    *,
    field: str = "scheduled_qty",
    dataset_types: Optional[Iterable[str]] = ("scheduled_quantity",),
    top_n: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Change in ``field`` vs the prior gas day, per matched point.

    Records are matched on ``(source, point_name, flow_direction, dataset_type)``.
    Returns changes sorted by absolute magnitude (largest first), optionally
    capped at ``top_n``. Points absent from the prior day are skipped. Pass
    ``dataset_types=None`` to compare every dataset type.
    """
    dts = set(dataset_types) if dataset_types is not None else None

    def key(r: dict[str, Any]) -> tuple:
        return (r.get("source"), r.get("point_name"), r.get("flow_direction"), r.get("dataset_type"))

    prior_index: dict[tuple, Any] = {}
    for r in prior_records:
        if dts is not None and r.get("dataset_type") not in dts:
            continue
        prior_index[key(r)] = r.get(field)

    changes: list[dict[str, Any]] = []
    for r in today_records:
        if dts is not None and r.get("dataset_type") not in dts:
            continue
        cur = r.get(field)
        k = key(r)
        prev = prior_index.get(k)
        if cur is None or prev is None:
            continue
        change = cur - prev
        changes.append(
            {
                "source": k[0],
                "point": k[1],
                "flow_direction": k[2],
                "dataset_type": k[3],
                "today": cur,
                "prior": prev,
                "change": change,
                "pct_change": (change / prev) if prev else None,
            }
        )
    changes.sort(key=lambda x: abs(x["change"]), reverse=True)
    return changes[:top_n] if top_n is not None else changes


def dod_index(
    today_records: Iterable[dict[str, Any]],
    prior_records: Iterable[dict[str, Any]],
    *,
    field: str = "scheduled_qty",
    dataset_types: Optional[Iterable[str]] = None,
) -> dict[tuple, float]:
    """``day_over_day`` keyed by ``(source, point_name, flow_direction, dataset_type)``.

    Convenience for ``etl/facts.py``: lets a fact-row builder look up the
    pre-computed ``dod_change`` for each operational point in O(1). Defaults to
    *all* dataset types so capacity/supply-demand rows can carry a delta too.
    """
    return {
        (c["source"], c["point"], c["flow_direction"], c["dataset_type"]): c["change"]
        for c in day_over_day(
            today_records, prior_records, field=field, dataset_types=dataset_types
        )
    }
