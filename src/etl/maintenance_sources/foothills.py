"""Foothills (TC Energy) maintenance -> MaintenanceImpact rows.

Foothills has NO standalone EBB feed of its own. Its export-leg restrictions
surface inside the NGTL / NOVA TC Customer Express *outages* table: a handful of
export-gate ``Table`` codes in that CSV correspond to the Foothills border legs.
We therefore reuse NOVA's outage parser verbatim, then keep only the export-gate
rows and tag each with its Foothills leg:

  * ``WGAT`` (Alberta/BC & Alberta/Montana Borders) -> **Foothills BC**
  * ``FHZ8`` (Alberta/BC Border, NPS 36 Zone 8)     -> **Foothills BC**
  * ``EGAT`` (Empress/McNeill Borders, eastbound)   -> **Foothills SK**

Only the Foothills BC leg (WGAT + FHZ8) ultimately reaches PG&E:
Foothills BC -> Kingsgate -> GTN -> Malin -> PG&E. The leg is recorded by
prefixing ``affected_label`` (e.g. ``[Foothills BC] Alberta/BC Border``).

Capacity basis / units are inherited from the NOVA outages source: ``Capability``
is the REMAINING border capability during the outage window (absolute remaining,
not a reduction amount or % cut), in 10^3 m^3/d, normalized to Dth/d with the
original preserved. These rows are not NAESB notices, so this source emits
``MaintenanceImpact`` rows only (no ``NoticeEvent``).

Fetch reuses the frozen ``NovaClient.fetch_outages`` (Foothills has no own
endpoint); parsing delegates to ``etl.maintenance_sources.nova.parse_outages``.
Reference: exploration/extract/foothills_maint.py.
"""
from __future__ import annotations

import pathlib
from typing import Any, Optional

from ebb.nova import NovaClient
from ebb.schema import MaintenanceImpact, NoticeEvent, utc_now_iso

from etl.maintenance_sources.nova import parse_outages as parse_nova_outages

SOURCE = "foothills"

# Export-gate (NOVA outages ``Table`` code) -> Foothills leg. Only these gates are
# kept; WGAT/FHZ8 are the Foothills BC leg that reaches PG&E, EGAT is Foothills SK.
GATE_LEG = {
    "WGAT": "Foothills BC",  # Alberta/BC & Alberta/Montana Borders
    "FHZ8": "Foothills BC",  # Alberta/BC Border (NPS 36 Zone 8)
    "EGAT": "Foothills SK",  # Empress/McNeill Borders (eastbound)
}
EXPORT_GATES = frozenset(GATE_LEG)


def parse_outages(text: str, gas_day: str, pulled_at: str, *, source: str = SOURCE) -> list[MaintenanceImpact]:
    """Reuse NOVA's outage parser, then keep only export-gate rows and tag the leg.

    Delegates to ``nova.parse_outages`` (one MaintenanceImpact per outage x gate
    within the gas-day window), filters to the Foothills export gates, and prefixes
    ``affected_label`` with the leg (``[Foothills BC] ...`` / ``[Foothills SK] ...``).
    """
    rows = parse_nova_outages(text, gas_day, pulled_at, source=source)
    out: list[MaintenanceImpact] = []
    for r in rows:
        leg = GATE_LEG.get(r.segment_or_gate or "")
        if leg is None:
            continue
        r.affected_label = f"[{leg}] {r.affected_label}"
        out.append(r)
    return out


def build(
    gas_day: str,
    *,
    session: Any = None,
    raw_dir: Optional[pathlib.Path] = None,
) -> tuple[list[NoticeEvent], list[MaintenanceImpact]]:
    pulled_at = utc_now_iso()
    client = NovaClient(data_dir=(raw_dir or pathlib.Path("data/nova")), session=session)
    outages_text = client.fetch_outages(raw_dir=raw_dir)
    impacts = parse_outages(outages_text, gas_day, pulled_at)
    return [], impacts
