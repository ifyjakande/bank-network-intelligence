"""Schema and query benchmarks: same data, different designs, measured by ClickHouse.

    make bench                 # dedicated ClickHouse, 100M sessions
    make bench ROWS=5000000    # quick run

Runs against its own single ClickHouse server (compose profile "bench", port 18123), not
the live cluster: benchmarks and the demo would otherwise skew each other. Builds a
synthetic session table (3 days, the live system's density), copies it into each schema
variant, runs each query several times and reads median latency, rows/bytes read and
peak memory back from system.query_log. Results go to bench/results.json.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import clickhouse_connect

OUT = Path(__file__).resolve().parent / "results.json"
RUNS = 5

# the tuned schema: what netflow.sessions_local uses, minus replication
TUNED_COLUMNS = """
    session_start  DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    session_end    DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    community_id   String,
    branch_id      LowCardinality(String),
    region         LowCardinality(String),
    device_id      String,
    device_type    LowCardinality(String),
    circuit_id     LowCardinality(String),
    provider       LowCardinality(String),
    app            LowCardinality(String),
    criticality    LowCardinality(String),
    bytes_c2s      UInt64 CODEC(T64, ZSTD(1)),
    bytes_s2c      UInt64 CODEC(T64, ZSTD(1)),
    packets        UInt32 CODEC(T64, ZSTD(1)),
    retrans        UInt32 CODEC(T64, ZSTD(1)),
    client_rtt_ms  Nullable(Float32),
    response_ms    Nullable(Float32),
    quality        UInt8,
    degraded       Bool
"""
# what a first draft usually looks like: plain strings, no codecs, nullable everywhere
NAIVE_COLUMNS = """
    session_start  Nullable(DateTime64(6, 'UTC')),
    session_end    Nullable(DateTime64(6, 'UTC')),
    community_id   Nullable(String),
    branch_id      Nullable(String),
    region         Nullable(String),
    device_id      Nullable(String),
    device_type    Nullable(String),
    circuit_id     Nullable(String),
    provider       Nullable(String),
    app            Nullable(String),
    criticality    Nullable(String),
    bytes_c2s      Nullable(UInt64),
    bytes_s2c      Nullable(UInt64),
    packets        Nullable(UInt32),
    retrans        Nullable(UInt32),
    client_rtt_ms  Nullable(Float32),
    response_ms    Nullable(Float32),
    quality        Nullable(UInt8),
    degraded       Nullable(UInt8)
"""

# 3 days of sessions over 400 branches with the generator's app mix and ~2% degraded:
# about the density of the live system (per-minute rollups only pay off when dense)
GENERATE = """
INSERT INTO bench.base
SELECT
    start AS session_start,
    start + toIntervalMillisecond(200 + rand(3) % 60000) AS session_end,
    concat('1:', base64Encode(reinterpretAsString(cityHash64(number)))) AS community_id,
    concat('BR-', leftPad(toString(b), 4, '0')) AS branch_id,
    ['North','North-East','East','Central','West','South-West','South','Coast'][b % 8 + 1]
        AS region,
    concat('BR-', leftPad(toString(b), 4, '0'), '-D', toString(rand(4) % 25)) AS device_id,
    ['teller','backoffice','atm','card_terminal','cctv'][rand(5) % 5 + 1] AS device_type,
    concat('CKT-', toString(1001 + 2 * b + (rand(6) % 50 = 0))) AS circuit_id,
    ['MER','SAV','CRS'][b % 3 + 1] AS provider,
    app,
    multiIf(app IN ('core_banking','card_authorisation','swift'), 'critical',
            app IN ('cctv_upload','software_updates','guest_internet'), 'bulk', 'business')
        AS criticality,
    rand(7) % 50000 AS bytes_c2s,
    rand(8) % 500000 AS bytes_s2c,
    10 + rand(9) % 400 AS packets,
    if(rand(10) % 100 < 3, rand(11) % 20, 0) AS retrans,
    toFloat32(25 + (rand(12) % 2000) / 100) AS client_rtt_ms,
    toFloat32(40 + (rand(13) % 30000) / 100) AS response_ms,
    toUInt8(if(rand(14) % 100 < 2, rand(15) % 60, 70 + rand(16) % 31)) AS quality,
    quality < 70 AS degraded
FROM (
    SELECT number,
           rand(1) % 400 AS b,
           toDateTime64('2026-09-18 00:00:00', 6, 'UTC')
               + toIntervalMicrosecond(rand64(2) % (3 * 86400 * 1000000)) AS start,
           ['core_banking','core_banking','core_banking','core_banking','card_authorisation',
            'card_authorisation','email','file_share','guest_internet','atm_management',
            'swift','cctv_upload','teams_media','software_updates'][rand(17) % 14 + 1] AS app
    FROM numbers({rows})
)
"""

VARIANTS: dict[str, str] = {
    "by_time": f"CREATE TABLE bench.by_time ({TUNED_COLUMNS}) ENGINE = MergeTree "
    "PARTITION BY toYYYYMMDD(session_start) ORDER BY session_start",
    "by_branch": f"CREATE TABLE bench.by_branch ({TUNED_COLUMNS}) ENGINE = MergeTree "
    "PARTITION BY toYYYYMMDD(session_start) "
    "ORDER BY (branch_id, session_start, community_id)",
    "naive_types": f"CREATE TABLE bench.naive_types ({NAIVE_COLUMNS}) ENGINE = MergeTree "
    "PARTITION BY toYYYYMMDD(assumeNotNull(session_start)) "
    "ORDER BY tuple() SETTINGS allow_nullable_key = 1",
}

ROLLUP = """
CREATE TABLE bench.rollup_1m (
    minute       DateTime('UTC'),
    branch_id    LowCardinality(String),
    app          LowCardinality(String),
    circuit_id   LowCardinality(String),
    criticality  LowCardinality(String),
    sessions     SimpleAggregateFunction(sum, UInt64),
    degraded     SimpleAggregateFunction(sum, UInt64),
    response_ms  AggregateFunction(quantilesTDigest(0.5, 0.95), Float32)
)
ENGINE = AggregatingMergeTree PARTITION BY toYYYYMM(minute)
ORDER BY (minute, branch_id, app, circuit_id, criticality)
"""
ROLLUP_FILL = """
INSERT INTO bench.rollup_1m
SELECT toStartOfMinute(session_start) AS minute, branch_id, app, circuit_id, criticality,
       count() AS sessions, countIf(degraded) AS degraded,
       quantilesTDigestState(0.5, 0.95)(assumeNotNull(response_ms)) AS response_ms
FROM bench.by_branch
GROUP BY minute, branch_id, app, circuit_id, criticality
"""

DAY = "session_start >= '2026-09-20 00:00:00' AND session_start < '2026-09-21 00:00:00'"


@dataclass
class Result:
    experiment: str
    variant: str
    query: str
    median_ms: float
    rows_read: int
    bytes_read: int
    peak_memory: int


def client() -> Any:
    return clickhouse_connect.get_client(
        host=os.environ.get("BENCH_HOST", "localhost"),
        port=int(os.environ.get("BENCH_PORT", "18123")),
        username="bench",
        password=os.environ["BENCH_PASSWORD"],
        send_receive_timeout=3600,
    )


def measure(ch: Any, experiment: str, variant: str, sql: str) -> Result:
    tag = f"bench-{uuid.uuid4().hex[:8]}"
    # no result cache and no query condition cache: runs 2..5 must do the same work as
    # run 1 (the condition cache would otherwise remember which granules matched and turn
    # a full scan into a few milliseconds)
    for _ in range(RUNS):
        ch.query(
            sql, settings={"log_comment": tag, "use_query_cache": 0, "use_query_condition_cache": 0}
        )
    ch.command("SYSTEM FLUSH LOGS")
    rows = ch.query(
        "SELECT query_duration_ms, read_rows, read_bytes, memory_usage FROM system.query_log "
        "WHERE log_comment = {t:String} AND type = 'QueryFinish'",
        parameters={"t": tag},
    ).result_rows
    assert len(rows) == RUNS, f"{experiment}/{variant}: {len(rows)} runs logged"
    r = Result(
        experiment,
        variant,
        " ".join(sql.split()),
        statistics.median(x[0] for x in rows),
        int(statistics.median(x[1] for x in rows)),
        int(statistics.median(x[2] for x in rows)),
        max(x[3] for x in rows),
    )
    print(
        f"  {experiment:12} {variant:22} {r.median_ms:8.0f} ms  "
        f"{r.rows_read:>13,} rows  {r.bytes_read / 1e6:9.1f} MB  {r.peak_memory / 1e6:7.1f} MB mem"
    )
    return r


def storage(ch: Any, table: str) -> dict[str, Any]:
    row = ch.query(
        "SELECT sum(rows), sum(data_compressed_bytes), sum(data_uncompressed_bytes) "
        "FROM system.parts WHERE active AND database = 'bench' AND table = {t:String}",
        parameters={"t": table},
    ).result_rows[0]
    return {
        "table": table,
        "rows": row[0],
        "compressed_bytes": row[1],
        "uncompressed_bytes": row[2],
        "ratio": round(row[2] / max(row[1], 1), 2),
    }


def load(ch: Any, rows: int) -> dict[str, float]:
    ch.command("DROP DATABASE IF EXISTS bench SYNC")
    ch.command("CREATE DATABASE bench")
    ch.command(
        f"CREATE TABLE bench.base ({TUNED_COLUMNS}) ENGINE = MergeTree "
        "PARTITION BY toYYYYMMDD(session_start) ORDER BY session_start"
    )
    timings: dict[str, float] = {}
    started = time.monotonic()
    ch.command(GENERATE.format(rows=rows), settings={"max_insert_threads": 4})
    timings["generate_s"] = round(time.monotonic() - started, 1)
    for name, ddl in VARIANTS.items():
        ch.command(ddl)
        started = time.monotonic()
        ch.command(
            f"INSERT INTO bench.{name} SELECT * FROM bench.base", settings={"max_insert_threads": 4}
        )
        timings[f"load_{name}_s"] = round(time.monotonic() - started, 1)
        ch.command(f"OPTIMIZE TABLE bench.{name} FINAL")
    started = time.monotonic()
    ch.command(ROLLUP)
    ch.command(ROLLUP_FILL)
    ch.command("OPTIMIZE TABLE bench.rollup_1m FINAL")
    timings["build_rollup_s"] = round(time.monotonic() - started, 1)
    ch.command("DROP TABLE bench.base SYNC")
    print("load:", timings)
    return timings


def experiments(ch: Any) -> list[Result]:
    # a reused dataset may still carry the previous run's projection and index: every
    # "before" must really be before
    ch.command(
        "ALTER TABLE bench.by_branch DROP PROJECTION IF EXISTS by_circuit",
        settings={"mutations_sync": 2},
    )
    ch.command(
        "ALTER TABLE bench.by_time DROP INDEX IF EXISTS idx_cid", settings={"mutations_sync": 2}
    )
    results: list[Result] = []
    branch_day = (
        f"SELECT app, count(), countIf(degraded), quantile(0.95)(response_ms) "
        f"FROM bench.{{t}} WHERE branch_id = 'BR-0017' AND {DAY} GROUP BY app"
    )
    print("E1 sort key: one branch, one day (the drill-down)")
    for t in ("by_time", "by_branch"):
        results.append(measure(ch, "E1_sort_key", t, branch_day.format(t=t)))

    print("E2 rollup: degraded share per minute by criticality, one day (a dashboard panel)")
    results.append(
        measure(
            ch,
            "E2_rollup",
            "raw_sessions",
            f"""
        SELECT toStartOfMinute(session_start) AS m, criticality, countIf(degraded) / count()
        FROM bench.by_branch WHERE {DAY} GROUP BY m, criticality ORDER BY m""",
        )
    )
    results.append(
        measure(
            ch,
            "E2_rollup",
            "rollup_1m",
            """
        SELECT minute AS m, criticality, sum(degraded) / sum(sessions)
        FROM bench.rollup_1m
        WHERE minute >= '2026-09-20 00:00:00' AND minute < '2026-09-21 00:00:00'
        GROUP BY m, criticality ORDER BY m""",
        )
    )

    print("E3 types and codecs: full scan of the columns a quality query touches")
    scan = "SELECT app, count(), avg(client_rtt_ms), countIf(degraded) FROM bench.{t} GROUP BY app"
    for t in ("naive_types", "by_branch"):
        results.append(measure(ch, "E3_types", t, scan.format(t=t)))

    print("E4 projection: one circuit over three days (the SLA evidence view)")
    circuit_week = (
        "SELECT toStartOfHour(session_start) AS h, count(), countIf(degraded) "
        "FROM bench.by_branch WHERE circuit_id = 'CKT-1035' "
        "AND session_start >= '2026-09-18' AND session_start < '2026-09-21' "
        "GROUP BY h ORDER BY h"
    )
    results.append(measure(ch, "E4_projection", "no_projection", circuit_week))
    ch.command(
        "ALTER TABLE bench.by_branch ADD PROJECTION by_circuit "
        "(SELECT * ORDER BY circuit_id, session_start)"
    )
    ch.command(
        "ALTER TABLE bench.by_branch MATERIALIZE PROJECTION by_circuit",
        settings={"mutations_sync": 2},
    )
    results.append(measure(ch, "E4_projection", "with_projection", circuit_week))

    print("E5 skip index: look one session up by community id (the stitcher's check)")
    sample = ch.query("SELECT community_id FROM bench.by_time LIMIT 1 OFFSET 12345").result_rows
    lookup = f"SELECT count() FROM bench.by_time WHERE community_id = '{sample[0][0]}'"
    results.append(measure(ch, "E5_skip_index", "no_index", lookup))
    ch.command(
        "ALTER TABLE bench.by_time ADD INDEX idx_cid community_id "
        "TYPE bloom_filter(0.01) GRANULARITY 4"
    )
    ch.command(
        "ALTER TABLE bench.by_time MATERIALIZE INDEX idx_cid", settings={"mutations_sync": 2}
    )
    results.append(measure(ch, "E5_skip_index", "bloom_filter", lookup))
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000_000)
    parser.add_argument("--reuse", action="store_true", help="keep the loaded bench tables")
    args = parser.parse_args()
    ch = client()
    # a reuse run measures queries only: keep the load timings of the run that loaded
    previous = json.loads(OUT.read_text()).get("load", {}) if OUT.exists() else {}
    timings = previous if args.reuse else load(ch, args.rows)
    storages = [storage(ch, t) for t in (*VARIANTS, "rollup_1m")]
    for s in storages:
        print(
            f"  storage {s['table']:12} {s['rows']:>13,} rows  "
            f"{s['compressed_bytes'] / 1e6:9.1f} MB on disk  ratio {s['ratio']}"
        )
    results = experiments(ch)
    version = ch.query("SELECT version()").result_rows[0][0]
    OUT.write_text(
        json.dumps(
            {
                "rows": args.rows,
                "clickhouse": version,
                "runs_per_query": RUNS,
                "setup": "dedicated single ClickHouse server, 5 GB container, laptop (Docker)",
                "load": timings,
                "storage": storages,
                "queries": [asdict(r) for r in results],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
