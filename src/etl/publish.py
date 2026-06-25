"""Publish the star-schema partitions + dim files to Power Automate (README §1/§9).

The ETL writes fact partitions to ``data/`` and dim CSVs to ``dim/``; this module
POSTs them as JSON to the Power Automate (PA) trigger URL, which writes the files
into the SharePoint document library Power BI reads. Each call carries a
shared-secret header so the PA flow can reject anything else, and stays well under
the ~5 MB/call payload limit (CSV partitions are small).

Secrets come from the environment / ``.env`` (gitignored) + Jenkins Credentials:
``POWER_AUTOMATE_URL`` and ``POWER_AUTOMATE_SHARED_SECRET`` (header name overridable
via ``POWER_AUTOMATE_SECRET_HEADER``, default ``X-Shared-Secret``). Nothing is
committed.

The network call is isolated behind an injectable ``requests.Session`` and a
``dry_run`` switch so the publish plan is fully offline-testable.
"""
from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import pathlib
from typing import Any, Iterable, Optional

import requests
from dotenv import load_dotenv
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger("etl.publish")

DEFAULT_SECRET_HEADER = "X-Shared-Secret"
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024  # PA call cap (README §1)


@dataclasses.dataclass
class PAConfig:
    url: str
    secret: str
    header: str = DEFAULT_SECRET_HEADER


def load_pa_config(
    url: Optional[str] = None,
    secret: Optional[str] = None,
    header: Optional[str] = None,
) -> PAConfig:
    """Resolve the Power Automate trigger URL + shared secret (args, then env/.env)."""
    if not url or not secret:
        load_dotenv()
    url = url or os.getenv("POWER_AUTOMATE_URL")
    secret = secret or os.getenv("POWER_AUTOMATE_SHARED_SECRET")
    header = header or os.getenv("POWER_AUTOMATE_SECRET_HEADER") or DEFAULT_SECRET_HEADER
    if not url or not secret:
        raise RuntimeError(
            "Power Automate config missing. Set POWER_AUTOMATE_URL and "
            "POWER_AUTOMATE_SHARED_SECRET in .env (see .env.example) or pass url=/secret=."
        )
    return PAConfig(url=url, secret=secret, header=header)


# --------------------------------------------------------------------------- #
# File set + payloads
# --------------------------------------------------------------------------- #


def partition_files(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    dim_dir: pathlib.Path | str = "dim",
) -> list[tuple[str, pathlib.Path]]:
    """The (kind, path) set to publish for a gas day: fact partitions + dim CSVs."""
    data_root = pathlib.Path(data_root)
    dim_dir = pathlib.Path(dim_dir)
    return [
        ("fact_operational", data_root / "operational" / f"operational_{gas_day}.csv"),
        ("fact_storage", data_root / "storage" / f"storage_{gas_day}.csv"),
        # Maintenance/notices are current-snapshot facts (not gas-day partitioned);
        # overwritten each run, skipped here if not built this run.
        ("fact_notices", data_root / "notices" / "notices_current.csv"),
        ("fact_maintenance", data_root / "maintenance" / "maintenance_current.csv"),
        ("dim_pipeline", dim_dir / "dim_pipeline.csv"),
        ("dim_cycle", dim_dir / "dim_cycle.csv"),
        ("dim_location", dim_dir / "dim_location.csv"),
        ("dim_segment", dim_dir / "dim_segment.csv"),
    ]


def build_payload(kind: str, path: pathlib.Path, gas_day: str) -> dict[str, Any]:
    """JSON payload for one file: folder + filename + CSV content for the PA flow."""
    content = path.read_text(encoding="utf-8")
    size = len(content.encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        log.warning("%s payload is %.1f MB (> %.0f MB cap)", path.name, size / 1e6, MAX_PAYLOAD_BYTES / 1e6)
    # Fact partitions live in per-fact folders; dims in the dim folder.
    folder = {
        "fact_operational": "operational",
        "fact_storage": "storage",
        "fact_notices": "notices",
        "fact_maintenance": "maintenance",
    }.get(kind, "dim")
    return {
        "kind": kind,
        "folder": folder,
        "filename": path.name,
        "gas_day": gas_day,
        "content_type": "text/csv",
        "content": content,
    }


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=20),
    reraise=True,
)
def _post(session: requests.Session, config: PAConfig, payload: dict[str, Any], timeout: int) -> Any:
    resp = session.post(
        config.url,
        json=payload,
        headers={config.header: config.secret},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp


# --------------------------------------------------------------------------- #
# Orchestrate
# --------------------------------------------------------------------------- #


def publish_gas_day(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    dim_dir: pathlib.Path | str = "dim",
    session: Optional[requests.Session] = None,
    config: Optional[PAConfig] = None,
    kinds: Optional[Iterable[str]] = None,
    timeout: int = 30,
    dry_run: bool = False,
) -> list[dict[str, Any]]:
    """POST the gas day's partitions + dim files to Power Automate.

    Returns one result dict per file: ``{kind, file, ok, status, skipped}``.
    Missing files are skipped (logged), not fatal — so a partial run still
    publishes what exists. ``dry_run`` builds and validates payloads without
    sending. ``config`` defaults to ``load_pa_config()`` unless ``dry_run``.
    """
    files = partition_files(gas_day, data_root=data_root, dim_dir=dim_dir)
    if kinds is not None:
        wanted = set(kinds)
        files = [(k, p) for k, p in files if k in wanted]

    if config is None and not dry_run:
        config = load_pa_config()
    if session is None and not dry_run:
        session = requests.Session()

    results: list[dict[str, Any]] = []
    for kind, path in files:
        if not path.is_file():
            log.warning("skip %s: %s not found", kind, path)
            results.append({"kind": kind, "file": path.as_posix(), "ok": False, "status": None, "skipped": True})
            continue
        payload = build_payload(kind, path, gas_day)
        if dry_run:
            results.append({"kind": kind, "file": path.as_posix(), "ok": True, "status": "dry_run",
                            "skipped": False, "bytes": len(payload["content"].encode("utf-8"))})
            continue
        try:
            resp = _post(session, config, payload, timeout)
            results.append({"kind": kind, "file": path.as_posix(), "ok": True,
                            "status": getattr(resp, "status_code", None), "skipped": False})
        except requests.RequestException as exc:
            log.error("publish failed for %s (%s): %s", kind, path.name, exc)
            results.append({"kind": kind, "file": path.as_posix(), "ok": False, "status": None, "skipped": False})
    log.info("published %d/%d files for %s", sum(1 for r in results if r["ok"]), len(results), gas_day)
    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="POST a gas day's fact + dim files to Power Automate.")
    parser.add_argument("--gas-day", required=True, help="ISO gas day (Pacific).")
    parser.add_argument("--data-root", default="data")
    parser.add_argument("--dim-dir", default="dim")
    parser.add_argument("--dry-run", action="store_true", help="Build payloads but do not POST.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    results = publish_gas_day(args.gas_day, data_root=args.data_root, dim_dir=args.dim_dir, dry_run=args.dry_run)
    for r in results:
        print(f"{'OK ' if r['ok'] else 'ERR'} {r['kind']:<16} {r['file']} ({r['status']})")
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
