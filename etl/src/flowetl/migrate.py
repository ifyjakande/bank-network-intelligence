"""Versioned, checksummed schema migrations for the ClickHouse cluster.

Files are `NNNN_name.sql`, applied in order, each exactly once. Editing a file after it
was applied is an error: write a new migration instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from clickhouse_connect.driver.client import Client

log = logging.getLogger(__name__)

FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
DDL_SETTINGS = {"distributed_ddl_task_timeout": 300, "distributed_ddl_output_mode": "throw"}

BOOTSTRAP = (
    "CREATE DATABASE IF NOT EXISTS ops ON CLUSTER netflow",
    """
    CREATE TABLE IF NOT EXISTS ops.schema_migrations ON CLUSTER netflow
    (
        version     UInt32,
        name        String,
        checksum    FixedString(64),
        applied_at  DateTime DEFAULT now()
    )
    ENGINE = ReplicatedMergeTree('/clickhouse/tables/all/ops/schema_migrations', '{replica}')
    ORDER BY version
    """,
)


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode()).hexdigest()

    def statements(self) -> list[str]:
        return split_statements(self.sql)


def split_statements(sql: str) -> list[str]:
    """Split on statement-ending semicolons; our migrations never put ';' in literals."""
    no_comments = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    parts = re.split(r";\s*(?:\n|$)", no_comments)
    return [p.strip() for p in parts if p.strip()]


def discover(directory: Path) -> list[Migration]:
    found = []
    for path in sorted(directory.glob("*.sql")):
        m = FILE_RE.match(path.name)
        if not m:
            raise ValueError(f"bad migration file name: {path.name}")
        found.append(Migration(int(m.group(1)), m.group(2), path.read_text()))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration version")
    return found


def run(client: Client, directory: Path) -> int:
    for stmt in BOOTSTRAP:
        client.command(stmt, settings=DDL_SETTINGS)
    applied = {
        row[0]: row[1]
        for row in client.query(
            "SELECT version, toString(checksum) FROM ops.schema_migrations"
        ).result_rows
    }
    count = 0
    for mig in discover(directory):
        if mig.version in applied:
            if applied[mig.version] != mig.checksum:
                raise RuntimeError(
                    f"migration {mig.version:04d}_{mig.name} changed after it was applied"
                )
            continue
        log.info("applying %04d_%s", mig.version, mig.name)
        for stmt in mig.statements():
            client.command(stmt, settings=DDL_SETTINGS)
        client.insert(
            "ops.schema_migrations",
            [[mig.version, mig.name, mig.checksum]],
            column_names=["version", "name", "checksum"],
        )
        count += 1
    log.info("migrations: %d applied, %d already present", count, len(applied))
    return count
