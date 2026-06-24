"""Pre-computed fundamentals metrics (README §2 hybrid compute split).

Python owns the *windowed/historical-join* series that are awkward in Power BI
Pro DAX — operational **day-over-day** deltas and the EIA **5-year storage bands**
/ net-flow series — and folds them into the star-schema fact tables. Power BI does
the aggregational/ratio math (utilization, basis, rollups) in DAX, so those
presentation metrics are no longer computed here.

Pure functions over the normalized §2 record dicts; ``etl/facts.py`` calls them
while assembling ``fact_operational`` / ``fact_storage``.
"""
from __future__ import annotations

from .operational import day_over_day, dod_index
from .storage import storage

__all__ = ["day_over_day", "dod_index", "storage"]
