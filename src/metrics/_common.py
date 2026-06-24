"""Shared helpers for the metrics package.

These are the small record-dict utilities reused by the windowed/band metric
functions that feed the ETL fact writers (README §2 hybrid compute split).
"""
from __future__ import annotations

from typing import Any, Iterable, Optional


def _filter(
    records: Iterable[dict[str, Any]],
    *,
    dataset_type: Optional[str] = None,
    source: Optional[str] = None,
    direction: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Subset normalized §2 record dicts by dataset_type / source / flow_direction."""
    out = []
    for r in records:
        if dataset_type is not None and r.get("dataset_type") != dataset_type:
            continue
        if source is not None and r.get("source") != source:
            continue
        if direction is not None and r.get("flow_direction") != direction:
            continue
        out.append(r)
    return out
