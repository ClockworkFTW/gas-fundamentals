"""Per-pipe maintenance parsers (one module per source).

Each module exposes:

    build(gas_day, *, session=None, raw_dir=None)
        -> tuple[list[NoticeEvent], list[MaintenanceImpact]]
        Fetch (reusing the frozen ebb client's request methods, plus only the small
        additive fetches a maintenance feed needs) + parse.

    parse_* (pure functions over raw text/dicts) — exercised by the offline tests
        against committed fixtures, no network.

Shapes come from ``ebb.schema`` (NoticeEvent / MaintenanceImpact); shared
pre-compute (unit conversion, classification, pct_of_capacity, date parsing) from
``etl.maintenance``. The ebb clients are imported as fetchers only — their request
logic is not modified.
"""
