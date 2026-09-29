"""ClickHouse connections with per-shard replica failover."""

from __future__ import annotations

import logging
from typing import Any

import clickhouse_connect
from clickhouse_connect.driver.client import Client

log = logging.getLogger(__name__)


def connect(host: str, port: int, user: str, password: str, **settings: Any) -> Client:
    return clickhouse_connect.get_client(
        host=host,
        port=port,
        username=user,
        password=password,
        connect_timeout=5,
        send_receive_timeout=300,
        settings=settings or None,
    )


class ShardClient:
    """Sticks to one healthy replica of a shard and fails over to the next one.

    After a failover the new replica is synced before use, so reads that decide what
    has already been written (the stitcher's emit-once check) see the old replica's
    inserts.
    """

    def __init__(
        self,
        shard_no: int,
        replicas: list[tuple[str, int]],
        user: str,
        password: str,
        sync_tables: tuple[str, ...] = (),
    ) -> None:
        self.shard_no = shard_no
        self.replicas = replicas
        self.user = user
        self.password = password
        self.sync_tables = sync_tables
        self._client: Client | None = None
        self._current: int | None = None
        self._last_used: int | None = None

    @property
    def replica(self) -> str:
        return "none" if self._current is None else self.replicas[self._current][0]

    def get(self) -> Client:
        if self._client is not None:
            try:
                self._client.ping()
                return self._client
            except Exception:  # any failure means try the next replica
                log.warning("shard %s: replica %s unhealthy", self.shard_no, self.replica)
                self._client = None
        errors = []
        for i, (host, port) in enumerate(self.replicas):
            try:
                client = connect(host, port, self.user, self.password)
                client.ping()
            except Exception as exc:
                errors.append(f"{host}: {exc}")
                continue
            if self._last_used is not None and self._last_used != i:
                log.warning("shard %s: failing over to %s", self.shard_no, host)
                for table in self.sync_tables:
                    client.command(
                        f"SYSTEM SYNC REPLICA {table}", settings={"receive_timeout": 120}
                    )
            self._client, self._current, self._last_used = client, i, i
            return client
        raise ConnectionError(f"shard {self.shard_no}: no healthy replica ({'; '.join(errors)})")

    def reset(self) -> None:
        self._client = None
