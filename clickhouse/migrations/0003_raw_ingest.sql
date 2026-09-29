-- Raw half-flows exactly as the probes exported them (duplicates and late arrivals
-- included). Sharded by community id so both halves of a session share a shard.

CREATE TABLE IF NOT EXISTS netflow.halfflows_local ON CLUSTER netflow
(
    record_id        FixedString(16),
    community_id     String,
    first_seen       DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    direction        Enum8('c2s' = 1, 's2c' = 2),
    record_seq       UInt16,
    is_final         Bool,
    probe_id         LowCardinality(String),
    exported_at      DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    ingested_at      DateTime64(3, 'UTC') DEFAULT now64(3),
    flow_start       DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    flow_end         DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    src_ip           IPv4,
    dst_ip           IPv4,
    src_port         UInt16,
    dst_port         UInt16,
    ip_proto         UInt8,
    tunnel_id        UInt32,
    app              LowCardinality(String),
    app_category     LowCardinality(String),
    l7_proto         LowCardinality(String),
    bytes            UInt64 CODEC(T64, ZSTD(1)),
    packets          UInt32 CODEC(T64, ZSTD(1)),
    retrans_packets  UInt32 CODEC(T64, ZSTD(1)),
    -- handshake / first-byte timestamps; 0 when this probe did not see that packet
    tcp_syn          DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    tcp_synack       DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    tcp_ack          DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    first_req        DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1)),
    first_resp       DateTime64(6, 'UTC') CODEC(Delta, ZSTD(1))
)
ENGINE = ReplicatedMergeTree
PARTITION BY toYYYYMMDD(first_seen)
-- the stitcher looks sessions up by id, so the id leads the sort key
ORDER BY (community_id, first_seen, direction, record_seq)
TTL toDateTime(first_seen) + INTERVAL 7 DAY DELETE
SETTINGS ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS netflow.halfflows ON CLUSTER netflow AS netflow.halfflows_local
ENGINE = Distributed(netflow, netflow, halfflows_local, cityHash64(community_id));

-- Work queue for the stitcher: which session ids received records, by arrival time.
-- Written by an MV on the local table, so it is always on the same shard as the data.
CREATE TABLE IF NOT EXISTS netflow.stitch_queue_local ON CLUSTER netflow
(
    ingested_at   DateTime64(3, 'UTC'),
    community_id  String
)
ENGINE = ReplicatedMergeTree
ORDER BY (ingested_at, community_id)
TTL toDateTime(ingested_at) + INTERVAL 1 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.stitch_queue_mv ON CLUSTER netflow
TO netflow.stitch_queue_local
AS SELECT ingested_at, community_id
FROM netflow.halfflows_local;

-- Records that failed to parse: kept with the raw payload instead of stalling the topic.
CREATE TABLE IF NOT EXISTS netflow.ingest_errors ON CLUSTER netflow
(
    received_at  DateTime DEFAULT now(),
    topic        LowCardinality(String),
    kafka_offset UInt64,
    partition    UInt32,
    error        String,
    raw_message  String
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/all/netflow/ingest_errors', '{replica}')
ORDER BY received_at
TTL received_at + INTERVAL 14 DAY DELETE;

-- Kafka consumers: one per node, same group, so partitions spread over the cluster.
-- Timestamps arrive as integer microseconds and are converted explicitly.
CREATE TABLE IF NOT EXISTS netflow.halfflows_kafka ON CLUSTER netflow
(
    record_id        String,
    community_id     String,
    first_seen_us    Int64,
    direction        String,
    record_seq       UInt16,
    is_final         UInt8,
    probe_id         String,
    exported_at_us   Int64,
    flow_start_us    Int64,
    flow_end_us      Int64,
    src_ip           IPv4,
    dst_ip           IPv4,
    src_port         UInt16,
    dst_port         UInt16,
    ip_proto         UInt8,
    tunnel_id        UInt32,
    app              String,
    app_category     String,
    l7_proto         String,
    bytes            UInt64,
    packets          UInt32,
    retrans_packets  UInt32,
    tcp_syn_us       Int64,
    tcp_synack_us    Int64,
    tcp_ack_us       Int64,
    first_req_us     Int64,
    first_resp_us    Int64
)
ENGINE = Kafka(kafka_flows)
SETTINGS input_format_skip_unknown_fields = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.halfflows_kafka_mv ON CLUSTER netflow
TO netflow.halfflows
AS SELECT
    toFixedString(record_id, 16) AS record_id,
    community_id,
    fromUnixTimestamp64Micro(first_seen_us, 'UTC') AS first_seen,
    CAST(direction, 'Enum8(\'c2s\' = 1, \'s2c\' = 2)') AS direction,
    record_seq,
    is_final = 1 AS is_final,
    probe_id,
    fromUnixTimestamp64Micro(exported_at_us, 'UTC') AS exported_at,
    fromUnixTimestamp64Micro(flow_start_us, 'UTC') AS flow_start,
    fromUnixTimestamp64Micro(flow_end_us, 'UTC') AS flow_end,
    src_ip, dst_ip, src_port, dst_port, ip_proto, tunnel_id,
    app, app_category, l7_proto, bytes, packets, retrans_packets,
    fromUnixTimestamp64Micro(tcp_syn_us, 'UTC') AS tcp_syn,
    fromUnixTimestamp64Micro(tcp_synack_us, 'UTC') AS tcp_synack,
    fromUnixTimestamp64Micro(tcp_ack_us, 'UTC') AS tcp_ack,
    fromUnixTimestamp64Micro(first_req_us, 'UTC') AS first_req,
    fromUnixTimestamp64Micro(first_resp_us, 'UTC') AS first_resp
FROM netflow.halfflows_kafka
WHERE length(_error) = 0;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.halfflows_errors_mv ON CLUSTER netflow
TO netflow.ingest_errors
AS SELECT
    now() AS received_at, _topic AS topic, _offset AS kafka_offset, _partition AS partition,
    _error AS error, _raw_message AS raw_message
FROM netflow.halfflows_kafka
WHERE length(_error) > 0;
