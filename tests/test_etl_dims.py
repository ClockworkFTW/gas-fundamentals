"""Offline tests for the dimension builder (src/etl/dims.py).

Writes the four dim CSVs to tmp_path and asserts the authored dim_pipeline /
dim_cycle content, the stub-only (header-only) dim_location / dim_segment, and the
seed-ingestion hook for when per-pipeline topology CSVs are authored.
"""
import pandas as pd

from etl import dims


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


def test_dim_location_segment_are_header_only_stubs(tmp_path):
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
