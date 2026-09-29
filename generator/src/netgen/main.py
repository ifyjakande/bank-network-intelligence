from __future__ import annotations

import argparse
import random
import signal
import sys
from pathlib import Path

from prometheus_client import start_http_server

from . import control
from .config import Settings
from .demand import Demand
from .engine import Engine
from .inventory import write_inventory
from .sinks import SinkStalled, make_sink
from .topology import Estate, build_estate


def _info(cfg: Settings, estate: Estate) -> None:
    counts = {t: len(d) for t, d in estate.devices_by_type.items()}
    demand = Demand(estate, random.Random(0), cfg.load_factor, cfg.utc_offset_hours)
    print(
        f"branches: {len(estate.branches)}  circuits: {len(estate.circuits)}  "
        f"pops: {len(estate.pops)}  gateways: {len(estate.gateways)}  "
        f"servers: {len(estate.servers)}"
    )
    print("devices:", counts)
    print(f"peak sessions/s at load_factor={cfg.load_factor}: {demand.peak_sessions_per_s():,.0f}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="netgen")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="generate traffic (default)")
    inv = sub.add_parser("inventory", help="export the estate as CSV")
    inv.add_argument("--out", type=Path, default=Path("inventory"))
    sub.add_parser("info", help="print estate size and expected rate")
    args = parser.parse_args(argv)

    cfg = Settings()
    estate = build_estate(cfg.seed, cfg.branches)
    if args.cmd == "inventory":
        write_inventory(estate, args.out)
        return
    if args.cmd == "info":
        _info(cfg, estate)
        return

    sink = make_sink(cfg.sink, cfg.kafka_bootstrap)
    engine = Engine(cfg, estate, sink)

    def _stop(signum: int, _frame: object) -> None:
        engine.stopping = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if cfg.metrics_port:
        start_http_server(cfg.metrics_port)
    log = sys.stderr
    try:
        if cfg.mode == "backfill":
            if not (cfg.backfill_start and cfg.backfill_end):
                raise SystemExit("backfill needs NETGEN_BACKFILL_START and NETGEN_BACKFILL_END")
            start = int(cfg.backfill_start.timestamp() * 1e6)
            end = int(cfg.backfill_end.timestamp() * 1e6)
            print(f"netgen: backfill {cfg.backfill_start} -> {cfg.backfill_end}", file=log)
            engine.run_backfill(start, end)
        else:
            if cfg.control_port:
                control.serve(cfg.control_port, estate, engine.faults, engine.demand)
            print(
                f"netgen: live, peak ~{engine.demand.peak_sessions_per_s():,.0f} sessions/s, "
                f"sink={cfg.sink}",
                file=log,
            )
            engine.run_live()
    except SinkStalled as exc:
        # exit non-zero: Docker restarts the generator with a fresh producer
        raise SystemExit(f"netgen: {exc}; exiting so the container restarts") from None
    finally:
        sink.close()


if __name__ == "__main__":
    main()
