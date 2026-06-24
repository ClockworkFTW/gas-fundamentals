"""Offline tests for the ETL lineage loader (src/etl/load.py).

Builds a throwaway ``data/`` tree under tmp_path — the loader's job is purely to
find and read the right normalized JSON, so no network or committed fixtures.
"""
import json
import pathlib

import pytest

from etl import load


def _write(root: pathlib.Path, source: str, name: str, payload: dict) -> None:
    d = root / source
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.normalized.json").write_text(json.dumps(payload), encoding="utf-8")


def _pull(source: str, gas_day: str, cycle, records=None, notices=None, pulled="2026-06-22T00:00:00Z"):
    return {
        "source": source,
        "gas_day": gas_day,
        "cycle": cycle,
        "pulled_at_utc": pulled,
        "records": records or [],
        "notices": notices or [],
    }


def test_prior_gas_day():
    assert load.prior_gas_day("2026-06-22") == "2026-06-21"
    assert load.prior_gas_day("2026-03-01") == "2026-02-28"  # non-leap year


def test_cycle_token_parsing():
    assert load._cycle_token("2026-06-22_id2.normalized.json", "2026-06-22") == "id2"
    assert load._cycle_token("2026-06-22.normalized.json", "2026-06-22") == ""
    assert load._cycle_token("2026-06-22_best_available.normalized.json", "2026-06-22") == "best_available"


def test_load_source_picks_most_settled_cycle(tmp_path):
    gd = "2026-06-22"
    _write(tmp_path, "pipe_ranger", f"{gd}_timely", _pull("pipe_ranger", gd, "timely"))
    _write(tmp_path, "pipe_ranger", f"{gd}_id2", _pull("pipe_ranger", gd, "id2"))
    _write(tmp_path, "pipe_ranger", f"{gd}_evening", _pull("pipe_ranger", gd, "evening"))

    pull = load.load_source("pipe_ranger", gd, data_root=tmp_path)
    assert pull is not None
    # id2 outranks evening and timely.
    assert pull.path.endswith("2026-06-22_id2.normalized.json")
    assert pull.cycle == "id2"


def test_load_source_explicit_cycle(tmp_path):
    gd = "2026-06-22"
    _write(tmp_path, "pipe_ranger", f"{gd}_timely", _pull("pipe_ranger", gd, "timely"))
    _write(tmp_path, "pipe_ranger", f"{gd}_id2", _pull("pipe_ranger", gd, "id2"))

    pull = load.load_source("pipe_ranger", gd, data_root=tmp_path, cycle="timely")
    assert pull is not None and pull.cycle == "timely"

    # A cycle with no file returns None rather than falling back silently.
    assert load.load_source("pipe_ranger", gd, data_root=tmp_path, cycle="final") is None


def test_load_source_bare_file(tmp_path):
    gd = "2026-06-22"
    _write(tmp_path, "kern_river", gd, _pull("kern_river", gd, None))
    pull = load.load_source("kern_river", gd, data_root=tmp_path)
    assert pull is not None
    assert pull.cycle_token == ""


def test_load_source_missing_returns_none(tmp_path):
    assert load.load_source("gtn", "2026-06-22", data_root=tmp_path) is None


def test_load_flows_reports_missing(tmp_path):
    gd = "2026-06-22"
    _write(tmp_path, "pipe_ranger", f"{gd}_id2", _pull("pipe_ranger", gd, "id2"))
    _write(tmp_path, "gtn", f"{gd}_timely", _pull("gtn", gd, "timely"))

    loaded, missing = load.load_flows(gd, data_root=tmp_path)
    assert set(loaded) == {"pipe_ranger", "gtn"}
    # Every other flow source is reported missing, not dropped.
    assert set(missing) == set(load.FLOW_SOURCES) - {"pipe_ranger", "gtn"}


def test_load_eia_picks_newest_snapshot(tmp_path):
    older = {"dataset": "weekly_storage", "pulled_at_utc": "2026-06-15T00:00:00Z", "records": [{"v": 1}]}
    newer = {"dataset": "weekly_storage", "pulled_at_utc": "2026-06-22T00:00:00Z", "records": [{"v": 2}]}
    d = tmp_path / "eia"
    d.mkdir(parents=True)
    (d / "2026-05-01_old.normalized.json").write_text(json.dumps(older), encoding="utf-8")
    (d / "2026-05-29_latest.normalized.json").write_text(json.dumps(newer), encoding="utf-8")

    snap = load.load_eia("2026-06-22", data_root=tmp_path)
    assert snap is not None and snap["records"][0]["v"] == 2


def test_load_eia_missing_returns_none(tmp_path):
    assert load.load_eia("2026-06-22", data_root=tmp_path) is None
