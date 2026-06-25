"""Offline tests for the Power Automate publisher (src/etl/publish.py).

No network: a fake session records the POSTs, and dry-run exercises payload
assembly. The shared-secret header and per-fact folder routing are asserted.
"""
import pathlib

import pytest

from etl import publish


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        pass


class _FakeSession:
    """Records POST calls; returns 200 (or a configured status)."""

    def __init__(self, status_code=200):
        self.calls = []
        self.status_code = status_code

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        return _FakeResponse(self.status_code)


@pytest.fixture
def staged(tmp_path):
    """A data/ + dim/ tree with the partitions and dims a publish expects."""
    gd = "2026-06-22"
    (tmp_path / "data" / "operational").mkdir(parents=True)
    (tmp_path / "data" / "storage").mkdir(parents=True)
    (tmp_path / "data" / "notices").mkdir(parents=True)
    (tmp_path / "data" / "maintenance").mkdir(parents=True)
    (tmp_path / "dim").mkdir(parents=True)
    (tmp_path / "data" / "operational" / f"operational_{gd}.csv").write_text("pipeline,gas_day\npipe_ranger,2026-06-22\n", encoding="utf-8")
    (tmp_path / "data" / "storage" / f"storage_{gd}.csv").write_text("region,gas_day\nPG&E System,2026-06-22\n", encoding="utf-8")
    # Maintenance/notices are current-snapshot facts (not gas-day partitioned).
    (tmp_path / "data" / "notices" / "notices_current.csv").write_text("source,notice_id\ngtn,1585\n", encoding="utf-8")
    (tmp_path / "data" / "maintenance" / "maintenance_current.csv").write_text("source,maintenance_id\nnova,nova:1\n", encoding="utf-8")
    for name in ("dim_pipeline", "dim_cycle", "dim_location", "dim_segment"):
        (tmp_path / "dim" / f"{name}.csv").write_text("col\n", encoding="utf-8")
    return tmp_path, gd


def test_load_pa_config_from_args():
    cfg = publish.load_pa_config(url="https://pa.example/trigger", secret="s3cret")
    assert cfg.url.endswith("/trigger") and cfg.secret == "s3cret"
    assert cfg.header == publish.DEFAULT_SECRET_HEADER


def test_load_pa_config_missing_raises(monkeypatch):
    monkeypatch.delenv("POWER_AUTOMATE_URL", raising=False)
    monkeypatch.delenv("POWER_AUTOMATE_SHARED_SECRET", raising=False)
    # Avoid picking up a real .env on the dev box.
    monkeypatch.setattr(publish, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError):
        publish.load_pa_config()


def test_build_payload_routes_folder_and_carries_csv(staged):
    root, gd = staged
    op_path = root / "data" / "operational" / f"operational_{gd}.csv"
    payload = publish.build_payload("fact_operational", op_path, gd)
    assert payload["folder"] == "operational"
    assert payload["filename"] == f"operational_{gd}.csv"
    assert payload["gas_day"] == gd
    assert "pipe_ranger" in payload["content"]

    dim_payload = publish.build_payload("dim_pipeline", root / "dim" / "dim_pipeline.csv", gd)
    assert dim_payload["folder"] == "dim"

    # Current-snapshot facts route to their own per-fact folders.
    notices_payload = publish.build_payload("fact_notices", root / "data" / "notices" / "notices_current.csv", gd)
    assert notices_payload["folder"] == "notices"
    maint_payload = publish.build_payload("fact_maintenance", root / "data" / "maintenance" / "maintenance_current.csv", gd)
    assert maint_payload["folder"] == "maintenance"


def test_publish_posts_all_files_with_secret_header(staged):
    root, gd = staged
    session = _FakeSession()
    cfg = publish.PAConfig(url="https://pa.example/trigger", secret="s3cret", header="X-Shared-Secret")
    results = publish.publish_gas_day(
        gd, data_root=root / "data", dim_dir=root / "dim", session=session, config=cfg
    )
    assert len(results) == 8 and all(r["ok"] for r in results)
    # Eight POSTs, each with the shared-secret header and the right URL.
    assert len(session.calls) == 8
    for call in session.calls:
        assert call["url"] == "https://pa.example/trigger"
        assert call["headers"]["X-Shared-Secret"] == "s3cret"
    kinds = {c["json"]["kind"] for c in session.calls}
    assert kinds == {"fact_operational", "fact_storage", "fact_notices", "fact_maintenance",
                     "dim_pipeline", "dim_cycle", "dim_location", "dim_segment"}


def test_publish_skips_missing_partition(staged):
    root, gd = staged
    (root / "data" / "storage" / f"storage_{gd}.csv").unlink()  # storage partition absent
    session = _FakeSession()
    cfg = publish.PAConfig(url="https://pa.example/trigger", secret="s3cret")
    results = publish.publish_gas_day(gd, data_root=root / "data", dim_dir=root / "dim", session=session, config=cfg)
    storage_res = next(r for r in results if r["kind"] == "fact_storage")
    assert storage_res["skipped"] is True and storage_res["ok"] is False
    assert len(session.calls) == 7  # 8 known files, storage not posted


def test_publish_dry_run_builds_without_posting(staged):
    root, gd = staged
    results = publish.publish_gas_day(gd, data_root=root / "data", dim_dir=root / "dim", dry_run=True)
    assert all(r["status"] == "dry_run" and r["ok"] for r in results)
    assert all(r["bytes"] > 0 for r in results if not r["skipped"])
