from __future__ import annotations

import argparse
import logging
import signal
import sys

from prometheus_client import start_http_server

from . import inventory, migrate
from .ch import connect
from .config import Settings
from .stitch import Stitcher


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowetl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply ClickHouse schema migrations")
    sub.add_parser("load-inventory", help="replace the Postgres inventory from CSV")
    sub.add_parser("stitch", help="run the half-flow stitcher until stopped")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = Settings()

    if args.cmd == "migrate":
        host, port = cfg.shards()[0][0]
        client = connect(host, port, cfg.ch_admin_user, cfg.ch_admin_password.get_secret_value())
        migrate.run(client, cfg.migrations_dir)
    elif args.cmd == "load-inventory":
        inventory.load(cfg.pg_dsn.get_secret_value(), cfg.inventory_dir, cfg.seeds_dir)
    else:
        stitcher = Stitcher(cfg)

        def _stop(signum: int, _frame: object) -> None:
            stitcher.stopping = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        if cfg.metrics_port:
            start_http_server(cfg.metrics_port)
        stitcher.run_forever()


if __name__ == "__main__":
    main()
