"""Maintain the star-schema dimension CSVs (README §2 / §9).

Dimensions are static reference tables that Power BI loads once and joins to the
fact partitions. This module (re)writes four CSVs under ``dim/`` (committed, not
gitignored — they are reference data, unlike the ``data/`` partitions):

- ``dim_pipeline`` — pipeline name, owner, role, zones (authored from README §4).
- ``dim_cycle``    — cycle ordering for sort (timely→evening→id1→id2→id3→final).
- ``dim_location`` — schematic nodes: ``point_id → (x,y), type, label, zone``.
- ``dim_segment``  — schematic edges: ``from_node/to_node, path_kind``.

``dim_location`` / ``dim_segment`` are the layout/topology tables that drive the
Deneb schematic. They are **stubbed** (header-only) here: the Transwestern seed
CSVs that README §2 references do not exist yet. When per-pipeline seeds are
authored, drop them in ``dim/seeds/<pipeline>_nodes.csv`` /
``dim/seeds/<pipeline>_segments.csv`` (columns matching ``DIM_LOCATION_COLUMNS`` /
``DIM_SEGMENT_COLUMNS``) and they will be folded in automatically.

``dim_date`` is built in Power BI (Power Query / CALENDAR), not here.
"""
from __future__ import annotations

import argparse
import logging
import pathlib
from typing import Any, Optional

import pandas as pd

log = logging.getLogger("etl.dims")

# --------------------------------------------------------------------------- #
# dim_pipeline — authored from README §4. The ``pipeline`` key matches the EBB
# client ``source`` (and ``fact_operational.pipeline`` / ``fact_storage.source``).
# --------------------------------------------------------------------------- #

DIM_PIPELINE_COLUMNS = ["pipeline", "owner", "role", "zones", "active"]

DIM_PIPELINE_ROWS: list[dict[str, Any]] = [
    {"pipeline": "pipe_ranger", "owner": "PG&E", "role": "CGT backbone + Citygate operator (receipts, storage, inventory)", "zones": "Malin;Topock;Daggett;Citygate", "active": True},
    {"pipeline": "gtn", "owner": "TC Energy", "role": "Malin receipts (W. Canada via Kingsgate)", "zones": "Malin", "active": True},
    {"pipeline": "el_paso", "owner": "Kinder Morgan", "role": "Topock receipts (Southwest)", "zones": "Topock", "active": True},
    {"pipeline": "transwestern", "owner": "Energy Transfer", "role": "Topock receipts (San Juan/Permian/West Texas)", "zones": "Topock", "active": True},
    {"pipeline": "kern_river", "owner": "Berkshire Hathaway Energy", "role": "Daggett receipts (Rockies/Opal)", "zones": "Daggett", "active": True},
    {"pipeline": "nova", "owner": "TC Energy", "role": "AECO/NIT upstream supply (NGTL)", "zones": "AECO;NIT", "active": True},
    {"pipeline": "foothills", "owner": "TC Energy", "role": "AECO -> Kingsgate export border", "zones": "Kingsgate", "active": True},
    {"pipeline": "ruby", "owner": "Tallgrass", "role": "Malin receipts (Rockies) - INACTIVE; onyx_ruby via Pipe Ranger", "zones": "Malin", "active": False},
    {"pipeline": "eia", "owner": "U.S. EIA", "role": "Macro weekly storage context (Pacific/Lower 48)", "zones": "Pacific;Lower 48", "active": True},
]

# --------------------------------------------------------------------------- #
# dim_cycle — canonical nomination-cycle ordering for sort (README §2).
# --------------------------------------------------------------------------- #

DIM_CYCLE_COLUMNS = ["cycle", "sort_order", "label"]

DIM_CYCLE_ROWS: list[dict[str, Any]] = [
    {"cycle": "timely", "sort_order": 1, "label": "Timely"},
    {"cycle": "evening", "sort_order": 2, "label": "Evening"},
    {"cycle": "id1", "sort_order": 3, "label": "Intraday 1"},
    {"cycle": "id2", "sort_order": 4, "label": "Intraday 2"},
    {"cycle": "id3", "sort_order": 5, "label": "Intraday 3"},
    {"cycle": "final", "sort_order": 6, "label": "Final"},
]

# --------------------------------------------------------------------------- #
# dim_location / dim_segment — schematic topology (stubbed; seeded from dim/seeds).
# --------------------------------------------------------------------------- #

DIM_LOCATION_COLUMNS = ["pipeline", "point_id", "x", "y", "type", "label", "zone"]
DIM_SEGMENT_COLUMNS = ["pipeline", "segment_id", "from_node", "to_node", "path_kind"]


# --------------------------------------------------------------------------- #
# Build
# --------------------------------------------------------------------------- #


def _write_csv(rows: list[dict[str, Any]], path: pathlib.Path, columns: list[str]) -> pathlib.Path:
    """Write rows to a CSV with a stable header (idempotent overwrite)."""
    df = pd.DataFrame(rows, columns=columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def _read_seeds(dim_dir: pathlib.Path, suffix: str, columns: list[str]) -> list[dict[str, Any]]:
    """Fold any authored per-pipeline seed CSVs (``dim/seeds/*_<suffix>.csv``).

    Returns [] when no seeds exist (the current stub-only state). Each seed file
    is expected to carry the dim's columns; unknown columns are ignored and
    missing ones come through blank.
    """
    seeds_dir = dim_dir / "seeds"
    if not seeds_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for seed in sorted(seeds_dir.glob(f"*_{suffix}.csv")):
        df = pd.read_csv(seed)
        for rec in df.to_dict(orient="records"):
            rows.append({c: rec.get(c) for c in columns})
        log.info("seeded %d %s rows from %s", len(df), suffix, seed.name)
    return rows


def build_dims(
    dim_dir: pathlib.Path | str = "dim",
    *,
    write: bool = True,
) -> dict[str, Any]:
    """(Re)build the four dimension CSVs under ``dim_dir``.

    Returns ``{name: {"rows": [...], "path": str}}`` for each dim.
    """
    root = pathlib.Path(dim_dir)
    dims: dict[str, tuple[list[dict[str, Any]], list[str]]] = {
        "dim_pipeline": (DIM_PIPELINE_ROWS, DIM_PIPELINE_COLUMNS),
        "dim_cycle": (DIM_CYCLE_ROWS, DIM_CYCLE_COLUMNS),
        "dim_location": (_read_seeds(root, "nodes", DIM_LOCATION_COLUMNS), DIM_LOCATION_COLUMNS),
        "dim_segment": (_read_seeds(root, "segments", DIM_SEGMENT_COLUMNS), DIM_SEGMENT_COLUMNS),
    }

    out: dict[str, Any] = {}
    for name, (rows, columns) in dims.items():
        path = root / f"{name}.csv"
        if write:
            _write_csv(rows, path, columns)
        out[name] = {"rows": rows, "path": path.as_posix()}
    if write:
        log.info(
            "wrote dims to %s (pipeline=%d, cycle=%d, location=%d, segment=%d)",
            root, len(DIM_PIPELINE_ROWS), len(DIM_CYCLE_ROWS),
            len(out["dim_location"]["rows"]), len(out["dim_segment"]["rows"]),
        )
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="(Re)build the star-schema dimension CSVs.")
    parser.add_argument("--dim-dir", default="dim")
    parser.add_argument("--no-write", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = build_dims(args.dim_dir, write=not args.no_write)
    for name, info in out.items():
        print(f"{name}: {len(info['rows'])} rows -> {info['path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
