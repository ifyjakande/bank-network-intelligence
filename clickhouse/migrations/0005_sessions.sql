-- Stitched, enriched sessions: one row per TCP/UDP session with both halves joined,
-- network and application timings derived, and a 0-100 quality score against the
-- app's SLA targets. Written once per session by the stitcher. ReplacingMergeTree is a
-- safety net only: the stitcher never writes a session twice on purpose.

CREATE TABLE IF NOT EXISTS netflow.sessions_local ON CLUSTER netflow
(
    session_start    DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    session_end      DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    community_id     String,
    stitched_at      DateTime64(3, 'UTC'),
    complete         Bool,
    asymmetric       Bool,
    records          UInt16,

    client_ip        IPv4,
    server_ip        IPv4,
    client_port      UInt16,
    server_port      UInt16,
    ip_proto         UInt8,
    probe_c2s        LowCardinality(String),
    probe_s2c        LowCardinality(String),

    app              LowCardinality(String),
    app_category     LowCardinality(String),
    criticality      LowCardinality(String),
    l7_proto         LowCardinality(String),

    branch_id        LowCardinality(String),
    region           LowCardinality(String),
    branch_size      LowCardinality(String),
    segment          LowCardinality(String),
    router_id        LowCardinality(String),
    switch_id        LowCardinality(String),
    device_id        String,
    device_type      LowCardinality(String),
    tunnel_id        UInt32,
    circuit_id       LowCardinality(String),
    circuit_role     LowCardinality(String),
    provider         LowCardinality(String),
    pop_id           LowCardinality(String),
    server_id        LowCardinality(String),
    dc               LowCardinality(String),

    bytes_c2s        UInt64 CODEC(T64, ZSTD(1)),
    bytes_s2c        UInt64 CODEC(T64, ZSTD(1)),
    packets_c2s      UInt32 CODEC(T64, ZSTD(1)),
    packets_s2c      UInt32 CODEC(T64, ZSTD(1)),
    retrans_c2s      UInt32 CODEC(T64, ZSTD(1)),
    retrans_s2c      UInt32 CODEC(T64, ZSTD(1)),
    retrans_pct      Float32,
    -- NULL when not measurable (UDP, or the other half never arrived)
    server_rtt_ms    Nullable(Float32),
    client_rtt_ms    Nullable(Float32),
    response_ms      Nullable(Float32),
    quality          UInt8,
    degraded         Bool,           -- quality under the threshold, whatever the cause
    net_degraded     Bool,           -- the network path failed its SLA (RTT, loss, no return)
    -- the stitcher's emit-once check looks sessions up by id
    INDEX idx_community_id community_id TYPE bloom_filter(0.01) GRANULARITY 4,
    -- freshness monitoring filters on stitched_at, which is not in the sort key
    INDEX idx_stitched_at stitched_at TYPE minmax GRANULARITY 1
)
ENGINE = ReplicatedReplacingMergeTree(stitched_at)
PARTITION BY toYYYYMMDD(session_start)
ORDER BY (branch_id, session_start, community_id)
TTL toDateTime(session_start) + INTERVAL 30 DAY DELETE
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS netflow.sessions ON CLUSTER netflow AS netflow.sessions_local
ENGINE = Distributed(netflow, netflow, sessions_local, cityHash64(community_id));
