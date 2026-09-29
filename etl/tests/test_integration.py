"""End-to-end checks against a running stack (`make up`), run with `pytest -m integration`.

Needs CH_ADMIN_PASSWORD in the environment. For a fast CI run start the generator with a
short lateness bound (NETGEN_LATE_MAX_S=30) and set INTEGRATION_SETTLE_MIN=2.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from clickhouse_connect.driver.client import Client

from flowetl.ch import connect

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]
CONTROL = os.environ.get("NETGEN_CONTROL_URL", "http://localhost:8088")
SETTLE_MIN = int(os.environ.get("INTEGRATION_SETTLE_MIN", "10"))


@pytest.fixture(scope="module")
def ch() -> Iterator[Client]:
    client = connect(
        os.environ.get("CH_HOST", "localhost"),
        int(os.environ.get("CH_PORT", "8123")),
        "admin",
        os.environ["CH_ADMIN_PASSWORD"],
    )
    yield client
    client.close()


def scalar(ch: Client, sql: str, **params: Any) -> Any:
    return ch.query(sql, parameters=params).result_rows[0][0]


def test_records_flow_and_nothing_is_quarantined(ch: Client) -> None:
    assert scalar(ch, "SELECT count() FROM netflow.halfflows") > 0
    assert scalar(ch, "SELECT count() FROM netflow.sessions") > 0
    assert scalar(ch, "SELECT count() FROM netflow.ingest_errors") == 0


def test_replicas_agree(ch: Client) -> None:
    ch.command(
        "SYSTEM SYNC REPLICA ON CLUSTER netflow netflow.halfflows_local",
        settings={"receive_timeout": 120},
    )
    # one cutoff for every node: now() differs by a few ms per replica
    cutoff = scalar(ch, "SELECT now64(3) - INTERVAL 60 SECOND")
    rows = ch.query(
        "SELECT shard, uniqExact(n) AS distinct_counts FROM clusterAllReplicas(netflow, view("
        "  SELECT getMacro('shard') AS shard, count() AS n FROM netflow.halfflows_local"
        "  WHERE ingested_at < {cutoff:DateTime64(3)})) GROUP BY shard",
        parameters={"cutoff": cutoff},
    ).result_rows
    assert rows and all(n == 1 for _, n in rows), rows


def test_sessions_are_unique_complete_and_enriched(ch: Client) -> None:
    row = ch.query(f"""
        SELECT count() - uniqExact(community_id, session_start),
               countIf(NOT complete) / count(),
               countIf(branch_id = '' OR circuit_id = '' OR device_type = 'unknown')
        FROM netflow.sessions
        WHERE session_start < now() - INTERVAL {SETTLE_MIN} MINUTE
    """).result_rows[0]
    duplicates, incomplete_share, unenriched = row
    assert duplicates == 0
    assert incomplete_share < 0.001
    assert unenriched == 0


def test_no_finished_session_is_lost(ch: Client) -> None:
    # every client->server half whose final record arrived before the settle horizon has
    # a session. Selected by arrival, not start: a 10 minute upload that ended a second
    # ago is still in flight. Both tables share a sharding key, so the join is local.
    lost = scalar(
        ch,
        f"""
        SELECT sum(c) FROM cluster(netflow, view(
            SELECT count() AS c
            FROM (
                SELECT DISTINCT community_id, first_seen
                FROM netflow.halfflows_local
                WHERE direction = 'c2s' AND is_final
                  AND ingested_at BETWEEN now() - INTERVAL {SETTLE_MIN + 30} MINUTE
                                      AND now() - INTERVAL {SETTLE_MIN} MINUTE
            ) AS h
            LEFT ANTI JOIN netflow.sessions_local AS s
                ON s.community_id = h.community_id AND s.session_start = h.first_seen
        ))
        """,
    )
    assert lost == 0


def _post(path: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        f"{CONTROL}{path}",
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())  # type: ignore[no-any-return]


def _delete(path: str) -> None:
    urllib.request.urlopen(
        urllib.request.Request(f"{CONTROL}{path}", method="DELETE"), timeout=10
    ).close()


def test_injected_fault_is_localised(ch: Client) -> None:
    target = "server:cas-02"
    fault = _post(
        "/faults",
        {
            "kind": "slow",
            "target": target,
            "server_factor": 8,
            "duration_s": 300,
            "label": "integration test",
        },
    )
    try:
        localise = (REPO / "clickhouse" / "queries" / "localise.sql").read_text()
        deadline = time.monotonic() + 240
        top: list[tuple[str, str]] = []
        while time.monotonic() < deadline:
            time.sleep(20)
            rows = ch.query(localise, parameters={"window_min": 2, "min_degraded": 20}).result_rows
            if rows:
                best = rows[0][6]
                top = [(r[0], r[1]) for r in rows if r[6] == best]
                if ("server", "cas-02") in top:
                    break
        assert ("server", "cas-02") in top, f"localisation said {top}"
        truth = scalar(
            ch,
            "SELECT count() FROM netflow.fault_events WHERE fault_id = {f:String}",
            f=fault["fault_id"],
        )
        assert truth >= 1, "ground-truth event did not reach ClickHouse"
    finally:
        _delete(f"/faults/{fault['fault_id']}")
