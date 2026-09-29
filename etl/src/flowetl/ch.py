"""ClickHouse connections with per-shard replica failover."""

from __future__ import annotations

import logging

import clickhouse_connect
from clickhouse_connect.driver.client import Client

log = logging.getLogger(__name__)


def connect(host: str, port: int, user: str, password: str) -> Client:
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        connect_timeout=5,
        send_receive_timeout=300,
    )


class ShardClient:
    """Sticks to one healthy replica of a shard and fails over to the next one.

    `sync()` makes the replica fetch what its partner already has, so reads that decide
    what has already been written (the stitcher's emit-once check) see inserts that went
    to the other replica. It is bounded: a dead partner costs a few seconds and a
    warning, never a stall.
    """

    def __init__(
        self,
        shard_no: int,
        replicas: list[tuple[str, int]],
        user: str,
        password: str,
        sync_timeout_s: int = 10,
    ) -> None:
        self.shard_no = shard_no
        self.replicas = replicas
        self.user = user
        self.password = password
        self.sync_timeout_s = sync_timeout_s
        self._client: Client | None = None
        self._current: int | None = None

    @property
    def replica(self) -> str:
        return "none" if self._current is None else self.replicas[self._current][0]

    @staticmethod
    def _healthy(client: Client) -> bool:
        try:
            return bool(client.ping())  # ping() reports failure as False, it does not raise
        except Exception:
            return False

    def sync(self, client: Client, tables: tuple[str, ...]) -> bool:
        """Fetch what the partner replica already has; False if the wait ran out.

        Skipped while a partner is down: parts only it holds cannot be fetched, so the
        wait would run out on every table every run for nothing.
        """
        try:
            # only the tables being synced: the all-node tables would report a node down
            # on the other shard as if this shard's partner had gone
            degraded = client.query(
                "SELECT count() FROM system.replicas WHERE concat(database, '.', table) IN "
                "{tables:Array(String)} AND active_replicas < total_replicas",
                parameters={"tables": list(tables)},
            ).result_rows[0][0]
        except Exception:
            degraded = 0
        if degraded:
            log.info("shard %s: partner replica down, skipping sync", self.shard_no)
            return False
        ok = True
        for table in tables:
            try:
                client.command(
                    f"SYSTEM SYNC REPLICA {table} LIGHTWEIGHT",
                    settings={"receive_timeout": self.sync_timeout_s},
                )
            except Exception as exc:
                ok = False
                log.warning("shard %s: %s not fully synced (%s)", self.shard_no, table, exc)
        return ok

    def get(self) -> Client:
        if self._client is not None:
            if self._healthy(self._client):
                return self._client
            log.warning("shard %s: replica %s unhealthy", self.shard_no, self.replica)
            self._client = None
        errors = []
        for i, (host, port) in enumerate(self.replicas):
            try:
                client = connect(host, port, self.user, self.password)
            except Exception as exc:
                errors.append(f"{host}: {exc}")
                continue
            if not self._healthy(client):
                errors.append(f"{host}: ping failed")
                continue
            if self._current is not None and self._current != i:
                log.warning("shard %s: failing over to %s", self.shard_no, host)
            self._client, self._current = client, i
            return client
        raise ConnectionError(f"shard {self.shard_no}: no healthy replica ({'; '.join(errors)})")

    def reset(self) -> None:
        self._client = None
