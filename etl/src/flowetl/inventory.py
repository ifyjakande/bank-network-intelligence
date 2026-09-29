"""Load the network inventory into Postgres.

The whole inventory is replaced inside one transaction, so the ClickHouse dictionaries
that read it only ever see a complete, consistent version.
"""

from __future__ import annotations

import csv
import logging
from pathlib import Path

import psycopg

log = logging.getLogger(__name__)

# load order respects foreign keys (branches <-> circuits is deferred)
TABLES: tuple[tuple[str, str], ...] = (
    ("providers", "inventory"),
    ("pops", "inventory"),
    ("gateways", "inventory"),
    ("branches", "inventory"),
    ("circuits", "inventory"),
    ("devices", "inventory"),
    ("servers", "inventory"),
    ("apps", "inventory"),
    ("sla_targets", "seeds"),
)


def _header(path: Path) -> list[str]:
    with path.open(newline="") as fh:
        return next(csv.reader(fh))


def load(dsn: str, inventory_dir: Path, seeds_dir: Path) -> dict[str, int]:
    sources = {"inventory": inventory_dir, "seeds": seeds_dir}
    paths = {t: sources[src] / f"{t}.csv" for t, src in TABLES}
    missing = [str(p) for p in paths.values() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"inventory files missing: {missing}")

    counts: dict[str, int] = {}
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SET search_path TO inventory")
        cur.execute("SET CONSTRAINTS ALL DEFERRED")
        names = ", ".join(t for t, _ in TABLES)
        cur.execute(f"TRUNCATE {names} CASCADE")
        for table, _ in TABLES:
            cols = ", ".join(_header(paths[table]))
            with (
                paths[table].open("rb") as fh,
                cur.copy(
                    f"COPY {table} ({cols}) FROM STDIN WITH (FORMAT csv, HEADER true)"
                ) as copy,
            ):
                while chunk := fh.read(1 << 16):
                    copy.write(chunk)
            counts[table] = cur.rowcount
            log.info("%s: %d rows", table, cur.rowcount)
    return counts
