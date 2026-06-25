"""gas-fundamentals operational-data engine CLI (README §9).

    python -m src --gas-day 2026-06-22 --cycle id2 --pull --publish
    python -m src                         # latest available gas day, etl only
    python -m src --gas-day 2026-06-22 --pull --no-publish

One gas day, three stages:

    pull   — refresh normalized EBB lineage via the source clients (network).
    etl    — build the star-schema fact partitions (data/operational, data/storage)
             and (re)build the dimension CSVs (dim/).
    publish — POST the partitions + dim files to Power Automate (shared-secret).

Gas day is Pacific; ``pulled_at`` is UTC. ICE pricing and news are out of scope
(Power Query / Power Automate handle those).
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib
import inspect
import logging
import pathlib
import sys
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

# Trust the OS certificate store rather than certifi's bundle, so requests works
# behind a corporate TLS-inspection proxy (the company root CA is installed in the
# Windows store via group policy but not in certifi). No-op if truststore is
# absent; nothing here disables verification. Must run before any TLS connection.
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:
    pass

# Make the sibling packages (ebb / etl / metrics) importable as top-level, the
# same way tests/conftest.py does — so this runs cleanly as `python -m src`.
_SRC = pathlib.Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from etl import facts, dims, maintenance, publish  # noqa: E402
from etl.load import FLOW_SOURCES  # noqa: E402

log = logging.getLogger("gas_fundamentals")

PACIFIC = ZoneInfo("America/Los_Angeles")

# source -> (module, client class). EIA is handled separately (different shape).
# Ruby is excluded from the default pull (un-automatable WAF cookie; redundant
# with Pipe Ranger's onyx_ruby) but can be requested explicitly via --sources.
_FLOW_CLIENTS: dict[str, tuple[str, str]] = {
    "pipe_ranger": ("ebb.pipe_ranger", "PipeRangerClient"),
    "gtn": ("ebb.gtn", "GTNClient"),
    "el_paso": ("ebb.el_paso", "ElPasoClient"),
    "transwestern": ("ebb.transwestern", "TranswesternClient"),
    "kern_river": ("ebb.kern_river", "KernRiverClient"),
    "nova": ("ebb.nova", "NovaClient"),
    "foothills": ("ebb.foothills", "FoothillsClient"),
    "ruby": ("ebb.ruby", "RubyClient"),  # not in defaults; available on explicit request
}


def _default_gas_day() -> str:
    """Prior gas day before 08:00 PT (prior-day final not yet posted), else today."""
    now = dt.datetime.now(PACIFIC)
    day = now.date()
    if now.hour < 8:
        day = day - dt.timedelta(days=1)
    return day.isoformat()


def pull_sources(
    gas_day: str,
    *,
    data_root: pathlib.Path | str = "data",
    cycle: Optional[str] = None,
    sources: Optional[list[str]] = None,
) -> dict[str, bool]:
    """Best-effort: invoke each EBB client to refresh lineage for ``gas_day``.

    Network-bound (not exercised by offline tests); each source is wrapped so one
    failure (auth, WAF, outage) doesn't sink the rest. ``cycle`` is passed only to
    clients whose ``pull`` accepts it (Pipe Ranger). Returns ``{source: ok}``.
    """
    targets = sources or (list(FLOW_SOURCES) + ["eia"])
    results: dict[str, bool] = {}
    root = pathlib.Path(data_root)
    for source in targets:
        try:
            if source == "eia":
                mod = importlib.import_module("ebb.eia")
                mod.EIAClient(data_dir=root / "eia").pull()
            else:
                mod_path, cls_name = _FLOW_CLIENTS[source]
                mod = importlib.import_module(mod_path)
                client = getattr(mod, cls_name)(data_dir=root / source)
                kwargs: dict[str, Any] = {}
                if (
                    cycle is not None
                    and source == "pipe_ranger"
                    and "cycle" in inspect.signature(client.pull).parameters
                ):
                    kwargs["cycle"] = cycle
                client.pull(gas_day, **kwargs)
            results[source] = True
        except Exception as exc:  # noqa: BLE001 - convenience path; log and continue
            log.warning("pull failed for %s: %s", source, exc)
            results[source] = False
    return results


def run(
    gas_day: str,
    *,
    data_root: str = "data",
    dim_dir: str = "dim",
    cycle: Optional[str] = None,
    do_pull: bool = False,
    do_publish: bool = False,
    do_maintenance: bool = False,
    sources: Optional[list[str]] = None,
    write: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """pull -> etl -> publish for one gas day. Returns a summary dict."""
    summary: dict[str, Any] = {"gas_day": gas_day, "cycle": cycle}

    if do_pull:
        summary["pull"] = pull_sources(gas_day, data_root=data_root, cycle=cycle, sources=sources)
        log.info("pull results: %s", summary["pull"])

    summary["facts"] = facts.build_facts(gas_day, data_root=data_root, cycle=cycle, write=write)
    summary["dims"] = dims.build_dims(dim_dir, write=write)

    # Maintenance/notices snapshot is its own forward-looking feed (network-bound);
    # opt-in so a plain ETL of on-disk lineage stays offline. Pass the just-built
    # operational design capacities so point_id-joined impacts (e.g. GTN LOC#) get
    # base capacity + pct_of_capacity backfilled.
    if do_maintenance:
        design_by_point = {
            (r["pipeline"], str(r["point_id"])): r["design_capacity"]
            for r in summary["facts"]["operational"]
            if r.get("point_id") and r.get("design_capacity")
        }
        summary["maintenance"] = maintenance.build_maintenance(
            gas_day, data_root=data_root, sources=sources,
            design_by_point=design_by_point, write=write,
        )

    if do_publish:
        summary["publish"] = publish.publish_gas_day(
            gas_day, data_root=data_root, dim_dir=dim_dir, dry_run=dry_run
        )
    return summary


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="gas-fundamentals: pull -> etl -> publish for a gas day.")
    parser.add_argument("--gas-day", default=None, help="ISO gas day (Pacific). Default: latest available.")
    parser.add_argument("--cycle", default=None, help="Pin a Pipe Ranger cycle (else most-settled).")
    parser.add_argument("--data-root", default="data", help="Root of the lineage + partition tree.")
    parser.add_argument("--dim-dir", default="dim", help="Directory of the committed dimension CSVs.")
    parser.add_argument("--pull", action="store_true", help="Refresh source lineage via the EBB clients first (network).")
    parser.add_argument("--sources", nargs="*", default=None, help="Limit the pull to these sources (e.g. pipe_ranger eia).")
    parser.add_argument("--maintenance", action="store_true", help="Also build the maintenance + notices snapshot facts (network).")
    parser.add_argument("--publish", action="store_true", help="POST partitions + dims to Power Automate.")
    parser.add_argument("--dry-run", action="store_true", help="With --publish, build payloads but do not POST.")
    parser.add_argument("--no-write", action="store_true", help="Do not write partition/dim files.")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    gas_day = args.gas_day or _default_gas_day()
    summary = run(
        gas_day,
        data_root=args.data_root,
        dim_dir=args.dim_dir,
        cycle=args.cycle,
        do_pull=args.pull,
        do_publish=args.publish,
        do_maintenance=args.maintenance,
        sources=args.sources,
        write=not args.no_write,
        dry_run=args.dry_run,
    )

    f = summary["facts"]
    print(
        f"gas_day {gas_day} | fact_operational {len(f['operational'])} rows, "
        f"fact_storage {len(f['storage'])} rows | sources {f['sources_loaded']} "
        f"(missing {f['sources_missing']})"
    )
    if args.maintenance:
        m = summary["maintenance"]
        print(
            f"fact_notices {len(m['notices'])} rows, fact_maintenance {len(m['impacts'])} rows | "
            f"maint sources ok {m['sources_ok']} (failed {list(m['sources_failed'])})"
        )
    if args.publish:
        ok = sum(1 for r in summary["publish"] if r["ok"])
        print(f"published {ok}/{len(summary['publish'])} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
