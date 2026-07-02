"""Offline tests for the dimension builder (src/etl/dims.py).

Writes the four dim CSVs to tmp_path and asserts the authored dim_pipeline /
dim_cycle content, the empty-without-seeds behaviour of dim_location / dim_segment,
the seed-ingestion hook, and the committed per-pipeline topology seeds under
``dim/seeds/`` (the schematic node/edge tables).
"""
import pathlib

import pandas as pd

from etl import dims

# Pipelines with topology nodes in dim/seeds/. The condensed schematic is the
# major interconnect spine, so El Paso (no delivery record of its own) is carried
# on Pipe Ranger's Topock border and NGTL/NOVA collapses to the foothills AB/BC
# export node — neither has a standalone seed file.
SEEDED_PIPELINES = {"pipe_ranger", "gtn", "transwestern", "kern_river", "foothills"}


def test_build_dims_writes_four_csvs(tmp_path):
    out = dims.build_dims(tmp_path, write=True)
    for name in ("dim_pipeline", "dim_cycle", "dim_location", "dim_segment"):
        assert (tmp_path / f"{name}.csv").is_file()
        assert out[name]["path"].endswith(f"{name}.csv")


def test_dim_pipeline_content(tmp_path):
    dims.build_dims(tmp_path, write=True)
    df = pd.read_csv(tmp_path / "dim_pipeline.csv")
    assert list(df.columns) == dims.DIM_PIPELINE_COLUMNS
    # The pipeline key joins fact_operational.pipeline (= the EBB source).
    assert set(df["pipeline"]) >= {"pipe_ranger", "gtn", "el_paso", "transwestern",
                                   "kern_river", "nova", "foothills", "ruby", "eia"}
    # Ruby is present but flagged inactive (kept-but-inactive per README §4).
    ruby = df[df["pipeline"] == "ruby"].iloc[0]
    assert bool(ruby["active"]) is False


def test_dim_cycle_ordering(tmp_path):
    dims.build_dims(tmp_path, write=True)
    df = pd.read_csv(tmp_path / "dim_cycle.csv")
    ordered = list(df.sort_values("sort_order")["cycle"])
    assert ordered == ["timely", "evening", "id1", "id2", "id3", "final"]


def test_dim_location_segment_empty_without_seeds(tmp_path):
    # With no dim/seeds/ directory, location/segment are header-only (still a
    # stable header for Power BI's folder connector to append against).
    out = dims.build_dims(tmp_path, write=True)
    loc = pd.read_csv(tmp_path / "dim_location.csv")
    seg = pd.read_csv(tmp_path / "dim_segment.csv")
    assert list(loc.columns) == dims.DIM_LOCATION_COLUMNS and len(loc) == 0
    assert list(seg.columns) == dims.DIM_SEGMENT_COLUMNS and len(seg) == 0
    assert out["dim_location"]["rows"] == [] and out["dim_segment"]["rows"] == []


def test_seed_hook_folds_in_authored_topology(tmp_path):
    # When a Transwestern node seed is dropped in dim/seeds/, it is folded in.
    seeds = tmp_path / "seeds"
    seeds.mkdir(parents=True)
    pd.DataFrame(
        [{"pipeline": "transwestern", "point_id": "56698", "x": 10, "y": 20,
          "type": "delivery", "label": "Topock (PG&E)", "zone": "Topock"}]
    ).to_csv(seeds / "transwestern_nodes.csv", index=False)

    out = dims.build_dims(tmp_path, write=True)
    loc = pd.read_csv(tmp_path / "dim_location.csv")
    assert len(loc) == 1
    assert loc.iloc[0]["point_id"] == 56698
    assert loc.iloc[0]["label"] == "Topock (PG&E)"
    assert list(loc.columns) == dims.DIM_LOCATION_COLUMNS


def test_committed_topology_seeds_are_coherent():
    """Guard the checked-in dim/seeds/ topology that drives the Deneb schematic.

    Asserts structural invariants (not exact coordinates, so layout can be tuned):
    every flow pipeline has nodes, key fact-join point_ids are present, segment
    endpoints all resolve to a node, and the literal-text point_ids survive
    byte-exact (any normalization would break the Power BI join).
    """
    repo = pathlib.Path(__file__).resolve().parents[1]
    out = dims.build_dims(repo / "dim", write=False)
    loc = out["dim_location"]["rows"]
    seg = out["dim_segment"]["rows"]
    assert loc and seg, "committed seeds should fold into non-empty location/segment"

    # Every seeded flow pipeline contributes nodes.
    assert {n["pipeline"] for n in loc} == SEEDED_PIPELINES

    # Spot-check the load-bearing fact-join keys (point_id == fact_operational.point_id).
    keys = {(n["pipeline"], str(n["point_id"])) for n in loc}
    for must in [
        ("pipe_ranger", "malin_gtn"), ("pipe_ranger", "baja_elpaso"),
        ("pipe_ranger", "baja_daggett"), ("gtn", "1820"), ("gtn", "3498"),
        ("transwestern", "56698"), ("kern_river", "68522"), ("foothills", "AB/BC"),
    ]:
        assert must in keys, f"missing schematic join key {must}"

    # The literal-text id must be preserved exactly (no trim/case/slash mangling).
    assert "AB/BC" in {str(n["point_id"]) for n in loc}, "text point_id 'AB/BC' altered/dropped"

    # The synthetic Citygate hub intentionally joins to nothing.
    hub = [n for n in loc if n["type"] == "hub"]
    assert len(hub) == 1 and hub[0]["point_id"] == "cgt_citygate"

    # Every segment endpoint references a real node id.
    node_ids = {str(n["point_id"]) for n in loc}
    for s in seg:
        assert str(s["from_node"]) in node_ids, f"{s['segment_id']} from_node dangling"
        assert str(s["to_node"]) in node_ids, f"{s['segment_id']} to_node dangling"
