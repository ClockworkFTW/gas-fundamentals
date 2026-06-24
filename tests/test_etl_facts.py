"""End-to-end tests for the fact-partition builder (src/etl/facts.py).

Builds a small two-gas-day lineage tree under tmp_path and asserts the two
star-schema partitions (fact_operational / fact_storage) carry the right rows and
the pre-computed columns (dod_change; storage band / net-flow series; EIA bands),
and that the dated CSV files are written with a stable header.
"""
import json
import pathlib

import pandas as pd
import pytest

from etl import facts


# --------------------------------------------------------------------------- #
# Record builders (full FlowRecord shape, as the EBB clients serialize them)
# --------------------------------------------------------------------------- #


def _rec(**kw):
    base = {
        "source": None, "dataset_type": None, "gas_day": None, "cycle": None,
        "point_name": None, "point_id": None, "flow_direction": None,
        "scheduled_qty": None, "design_capacity": None, "operational_capacity": None,
        "available_capacity": None, "units": "Dth/d", "original_units": "Dth/d",
        "original_qty": None, "pulled_at_utc": "2026-06-22T22:00:00Z", "raw_ref": None,
    }
    base.update(kw)
    return base


def _sched(gd, point, qty, point_id):
    return _rec(source="pipe_ranger", dataset_type="scheduled_quantity", gas_day=gd, cycle="Timely Schedule",
                point_name=point, point_id=point_id, flow_direction="receipt", scheduled_qty=qty, original_qty=qty)


def _inventory(gd, end, mn, mx):
    return _rec(source="pipe_ranger", dataset_type="inventory", gas_day=gd, point_name="System Ending Inventory",
                point_id="Inv_End", scheduled_qty=end, design_capacity=mx, available_capacity=mn,
                units="Dth", original_units="MMcf", original_qty=end)


def _storage_flow(gd, direction, qty, field):
    return _rec(source="pipe_ranger", dataset_type="storage", gas_day=gd, point_name="Wild Goose",
                point_id=field, flow_direction=direction, scheduled_qty=qty, original_units="MMcf/d", original_qty=qty)


def _supply_demand(gd, label, field, val):
    return _rec(source="pipe_ranger", dataset_type="supply_demand", gas_day=gd, point_name=label,
                point_id=field, scheduled_qty=val, original_units="MMcf/d", original_qty=val)


def _oac(source, gd, point, sched, op, avail):
    return _rec(source=source, dataset_type="operationally_available", gas_day=gd, point_name=point,
                point_id=point, flow_direction="delivery", scheduled_qty=sched, design_capacity=op,
                operational_capacity=op, available_capacity=avail)


def _write(root: pathlib.Path, source: str, name: str, payload: dict) -> None:
    d = root / source
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.normalized.json").write_text(json.dumps(payload), encoding="utf-8")


def _pr_pull(gd, malin, topock, inv_end, inj):
    return {
        "source": "pipe_ranger", "gas_day": gd, "cycle": "id2", "pulled_at_utc": f"{gd}T22:00:00Z",
        "records": [
            _sched(gd, "Malin (GTN)", malin, "malin_gtn"),
            _sched(gd, "Topock (El Paso)", topock, "baja_elpaso"),
            _inventory(gd, inv_end, 20.0, 100.0),
            _storage_flow(gd, "injection", inj, "WG_Net_Inj"),
            _supply_demand(gd, "System Demand", "System_Demand", 3_500_000.0),
        ],
        "notices": [],
    }


@pytest.fixture
def lineage(tmp_path):
    gd, prior = "2026-06-22", "2026-06-21"
    # Pipe Ranger today + prior (drives operational dod + storage band/net-flow dod).
    today = _pr_pull(gd, 1_400_000.0, 200_000.0, 70.0, 5_000.0)
    today["records"].append(_storage_flow(gd, "withdrawal", 1_000.0, "WG_Net_Wd"))
    _write(tmp_path, "pipe_ranger", f"{gd}_id2", today)
    _write(tmp_path, "pipe_ranger", f"{prior}_id2", _pr_pull(prior, 1_300_000.0, 250_000.0, 65.0, 2_000.0))
    # A pipeline OAC source with a delivery point (no prior day → dod stays null).
    _write(tmp_path, "kern_river", gd, {
        "source": "kern_river", "gas_day": gd, "cycle": None,
        "records": [_oac("kern_river", gd, "Daggett - PG&E", 99_000.0, 100_000.0, 1_000.0)], "notices": [],
    })
    # EIA snapshot with a Pacific band (pre-computed at pull time).
    (tmp_path / "eia").mkdir(parents=True, exist_ok=True)
    (tmp_path / "eia" / "2026-01-01_latest.normalized.json").write_text(json.dumps({
        "dataset": "weekly_storage", "pulled_at_utc": f"{gd}T23:00:00Z",
        "bands": {"Pacific": {"as_of_period": "2026-06-19", "current": 280.0, "five_yr_avg": 250.0,
                              "five_yr_min": 210.0, "five_yr_max": 290.0, "vs_5yr_pct": 0.12, "n_years": 5}},
        "records": [{"region": "Pacific", "period": "2026-06-19", "value": 280.0, "wow_change": 10.0, "units": "BCF"}],
    }), encoding="utf-8")
    return tmp_path, gd


def _find(rows, **kw):
    for r in rows:
        if all(r.get(k) == v for k, v in kw.items()):
            return r
    return None


# --------------------------------------------------------------------------- #
# fact_operational
# --------------------------------------------------------------------------- #


def test_fact_operational_rows_and_dod(lineage):
    root, gd = lineage
    result = facts.build_facts(gd, data_root=root, cycle="id2", write=False)
    op = result["operational"]

    malin = _find(op, pipeline="pipe_ranger", dataset_type="scheduled_quantity", point_name="Malin (GTN)")
    assert malin is not None
    assert malin["scheduled_qty"] == pytest.approx(1_400_000.0)
    assert malin["dod_change"] == pytest.approx(100_000.0)   # 1.40M − 1.30M

    topock = _find(op, point_name="Topock (El Paso)")
    assert topock["dod_change"] == pytest.approx(-50_000.0)  # 200k − 250k

    # An OAC point from another pipeline carries its capacities; no prior → dod null.
    daggett = _find(op, pipeline="kern_river", point_name="Daggett - PG&E")
    assert daggett["operational_capacity"] == 100_000.0 and daggett["available_capacity"] == 1_000.0
    assert daggett["dod_change"] is None

    # supply_demand rides along on fact_operational (Power BI builds CGT demand in DAX).
    assert _find(op, dataset_type="supply_demand", point_name="System Demand") is not None


def test_fact_operational_excludes_storage_and_inventory(lineage):
    root, gd = lineage
    op = facts.build_facts(gd, data_root=root, cycle="id2", write=False)["operational"]
    assert {r["dataset_type"] for r in op} <= facts.OPERATIONAL_DATASETS
    assert not any(r["dataset_type"] in ("storage", "inventory") for r in op)


# --------------------------------------------------------------------------- #
# fact_storage
# --------------------------------------------------------------------------- #


def test_fact_storage_pge_band_and_net_flow(lineage):
    root, gd = lineage
    st = facts.build_facts(gd, data_root=root, cycle="id2", write=False)["storage"]
    pge = _find(st, region="PG&E System")
    assert pge["source"] == "pipe_ranger"
    assert pge["working_gas"] == 70.0
    assert pge["pct_of_band"] == pytest.approx((70 - 20) / (100 - 20))  # 0.625
    assert pge["net_flow"] == pytest.approx(4_000.0)        # 5000 inj − 1000 wd
    assert pge["net_flow_dod"] == pytest.approx(2_000.0)    # 4000 − 2000 (prior inj only)


def test_fact_storage_eia_band_row(lineage):
    root, gd = lineage
    st = facts.build_facts(gd, data_root=root, cycle="id2", write=False)["storage"]
    pac = _find(st, region="EIA Pacific")
    assert pac["source"] == "eia" and pac["units"] == "BCF"
    assert pac["working_gas"] == 280.0
    assert pac["vs_5yr_pct"] == pytest.approx(0.12)
    assert pac["wow_change"] == 10.0
    assert pac["pct_of_band"] == pytest.approx((280 - 210) / (290 - 210))  # 0.875
    assert pac["as_of_period"] == "2026-06-19"


# --------------------------------------------------------------------------- #
# Partition files
# --------------------------------------------------------------------------- #


def test_build_facts_writes_partitions_with_stable_header(lineage):
    root, gd = lineage
    result = facts.build_facts(gd, data_root=root, cycle="id2", write=True)

    op_path = root / "operational" / f"operational_{gd}.csv"
    st_path = root / "storage" / f"storage_{gd}.csv"
    assert op_path.is_file() and st_path.is_file()

    op_df = pd.read_csv(op_path)
    st_df = pd.read_csv(st_path)
    assert list(op_df.columns) == facts.FACT_OPERATIONAL_COLUMNS
    assert list(st_df.columns) == facts.FACT_STORAGE_COLUMNS
    assert len(op_df) == len(result["operational"])
    assert len(st_df) == len(result["storage"])
    # Idempotent: a second run overwrites, not appends.
    facts.build_facts(gd, data_root=root, cycle="id2", write=True)
    assert len(pd.read_csv(op_path)) == len(result["operational"])


def test_build_facts_tolerates_no_data(tmp_path):
    # No lineage at all → both partitions still write with header-only (no rows).
    result = facts.build_facts("2026-06-22", data_root=tmp_path, write=True)
    assert result["operational"] == [] and result["storage"] == []
    assert result["sources_loaded"] == []
    op_df = pd.read_csv(tmp_path / "operational" / "operational_2026-06-22.csv")
    assert list(op_df.columns) == facts.FACT_OPERATIONAL_COLUMNS and len(op_df) == 0
