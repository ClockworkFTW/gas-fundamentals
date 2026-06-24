"""Offline tests for the pre-compute metric functions (src/metrics/).

Only the windowed series Python owns under the hybrid compute split are computed
here — operational day-over-day and the storage band / net-flow series. (The DAX-
superseded ratios — utilization, border supply, path util — were dropped in the
Power BI refactor.) All inputs are small synthetic §2 record dicts; the functions
are pure.
"""
import pytest

from metrics import day_over_day, dod_index, storage


def _flow(source, dataset_type, point, qty=None, direction=None, design=None, op=None, avail=None):
    return {
        "source": source,
        "dataset_type": dataset_type,
        "point_name": point,
        "flow_direction": direction,
        "scheduled_qty": qty,
        "design_capacity": design,
        "operational_capacity": op,
        "available_capacity": avail,
        "units": "Dth/d",
    }


def _sched(point, qty):
    return _flow("pipe_ranger", "scheduled_quantity", point, qty=qty, direction="receipt")


# --------------------------------------------------------------------------- #
# Day-over-day (feeds fact_operational.dod_change)
# --------------------------------------------------------------------------- #


def test_day_over_day_biggest_movers():
    today = [_sched("Malin (GTN)", 1_400_000.0), _sched("Topock (El Paso)", 200_000.0)]
    prior = [_sched("Malin (GTN)", 1_300_000.0), _sched("Topock (El Paso)", 250_000.0)]
    movers = day_over_day(today, prior)
    assert movers[0]["point"] == "Malin (GTN)"  # +100k is the biggest absolute move
    assert movers[0]["change"] == pytest.approx(100_000.0)
    assert movers[1]["change"] == pytest.approx(-50_000.0)
    assert movers[1]["pct_change"] == pytest.approx(-0.2)


def test_day_over_day_skips_points_absent_prior():
    today = [_sched("New Point", 100.0), _sched("Malin (GTN)", 200.0)]
    prior = [_sched("Malin (GTN)", 150.0)]
    movers = day_over_day(today, prior)
    assert [m["point"] for m in movers] == ["Malin (GTN)"]


def test_dod_index_keys_by_point_and_spans_dataset_types():
    today = [
        _sched("Malin (GTN)", 1_400_000.0),
        _flow("gtn", "operationally_available", "Kingsgate", qty=2_500_000.0, direction="receipt", op=3_000_000.0),
    ]
    prior = [
        _sched("Malin (GTN)", 1_300_000.0),
        _flow("gtn", "operationally_available", "Kingsgate", qty=2_400_000.0, direction="receipt", op=3_000_000.0),
    ]
    idx = dod_index(today, prior, field="scheduled_qty", dataset_types=None)
    # Both a scheduled point and an operationally-available point carry a delta.
    assert idx[("pipe_ranger", "Malin (GTN)", "receipt", "scheduled_quantity")] == pytest.approx(100_000.0)
    assert idx[("gtn", "Kingsgate", "receipt", "operationally_available")] == pytest.approx(100_000.0)


# --------------------------------------------------------------------------- #
# Storage band + net flow (feeds fact_storage)
# --------------------------------------------------------------------------- #


def _inventory(end, min_band, max_band):
    return {
        "source": "pipe_ranger", "dataset_type": "inventory", "point_name": "System Ending Inventory",
        "flow_direction": None, "scheduled_qty": end, "design_capacity": max_band,
        "operational_capacity": None, "available_capacity": min_band, "units": "Dth",
    }


def _storage_flow(direction, qty):
    return _flow("pipe_ranger", "storage", "Wild Goose", qty=qty, direction=direction)


def test_storage_pge_system_band_and_net_flow():
    today = [
        _inventory(70.0, 20.0, 100.0),
        _storage_flow("injection", 5_000.0),
        _storage_flow("withdrawal", 1_000.0),
    ]
    prior = [_storage_flow("injection", 2_000.0)]
    s = storage(today, prior)
    pge = s["pge_system"]
    assert pge["ending_inventory"] == 70.0
    assert pge["pct_of_band"] == pytest.approx((70 - 20) / (100 - 20))  # 0.625
    assert pge["net_flow"] == pytest.approx(4_000.0)   # 5000 inj − 1000 wd
    assert pge["net_flow_dod"] == pytest.approx(2_000.0)  # 4000 − 2000


def test_storage_eia_region_context():
    snapshot = {
        "bands": {"Pacific": {"as_of_period": "2026-06-19", "current": 280.0, "five_yr_avg": 250.0,
                              "five_yr_min": 210.0, "five_yr_max": 290.0, "vs_5yr_pct": 0.12, "n_years": 5}},
        "records": [
            {"region": "Pacific", "period": "2026-06-12", "value": 270.0, "wow_change": 8.0},
            {"region": "Pacific", "period": "2026-06-19", "value": 280.0, "wow_change": 10.0},
            {"region": "Lower 48", "period": "2026-06-19", "value": 2800.0, "wow_change": 70.0},
        ],
    }
    s = storage([], None, snapshot)
    pac = s["eia_pacific"]
    assert pac["working_gas_bcf"] == 280.0
    assert pac["wow_change"] == 10.0          # from the latest period
    assert pac["vs_5yr_pct"] == pytest.approx(0.12)
    assert pac["band_n_years"] == 5
    l48 = s["eia_lower48"]
    assert l48["working_gas_bcf"] == 2800.0 and l48["wow_change"] == 70.0


def test_storage_handles_missing_inputs():
    s = storage([], None, None)
    assert s == {"pge_system": None, "eia_pacific": None, "eia_lower48": None}
