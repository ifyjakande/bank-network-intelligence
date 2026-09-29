from __future__ import annotations

import argparse
import logging
import signal
import sys
from datetime import UTC, datetime

from prometheus_client import start_http_server

from . import inventory, migrate
from .ch import connect
from .config import Settings
from .stitch import Stitcher


def _utc(ts: datetime) -> datetime:
    """Naive UTC, like the watermarks: aware inputs are converted, naive ones are UTC."""
    return ts.astimezone(UTC).replace(tzinfo=None) if ts.tzinfo else ts


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="flowetl")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply ClickHouse schema migrations")
    sub.add_parser("load-inventory", help="replace the Postgres inventory from CSV")
    sub.add_parser("stitch", help="run the half-flow stitcher until stopped")
    re = sub.add_parser("restitch", help="re-run a past range; only fills missing sessions")
    re.add_argument("--since", type=datetime.fromisoformat, required=True)
    re.add_argument("--until", type=datetime.fromisoformat, required=True)
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
    elif args.cmd == "restitch":
        written = Stitcher(cfg).restitch(_utc(args.since), _utc(args.until))
        logging.getLogger(__name__).info("restitch wrote %d missing sessions", written)
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
