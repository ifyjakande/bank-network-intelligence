-- What the generator actually broke, and when. Used to annotate dashboards and to score
-- the localisation query in CI. Tiny, so every node keeps a full replica ("all" path).

CREATE TABLE IF NOT EXISTS netflow.fault_events ON CLUSTER netflow
(
    fault_id        String,
    state           Enum8('started' = 1, 'ended' = 2),
    event_at        DateTime64(6, 'UTC'),
    kind            LowCardinality(String),
    target          String,
    target_type     LowCardinality(String),
    label           String,
    source          LowCardinality(String),
    start_at        DateTime64(6, 'UTC'),
    end_at          DateTime64(6, 'UTC'),
    latency_ms      Float32,
    loss            Float32,
    server_factor   Float32
)
ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/all/netflow/fault_events', '{replica}')
ORDER BY (fault_id, state);

CREATE TABLE IF NOT EXISTS netflow.faults_kafka ON CLUSTER netflow
(
    fault_id       String,
    state          String,
    event_at_us    Int64,
    kind           String,
    target         String,
    target_type    String,
    label          String,
    source         String,
    start_us       Int64,
    end_us         Int64,
    latency_ms     Float32,
    loss           Float32,
    server_factor  Float32
)
ENGINE = Kafka(kafka_faults)
SETTINGS input_format_skip_unknown_fields = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.faults_kafka_mv ON CLUSTER netflow
TO netflow.fault_events
AS SELECT
    fault_id,
    -- cannot throw (see halfflows_kafka_mv): rows WHERE rejects are dropped
    CAST(if(state = 'ended', 'ended', 'started'), 'Enum8(\'started\' = 1, \'ended\' = 2)')
        AS state,
    fromUnixTimestamp64Micro(event_at_us, 'UTC') AS event_at,
    kind, target, target_type, label, source,
    fromUnixTimestamp64Micro(start_us, 'UTC') AS start_at,
    fromUnixTimestamp64Micro(end_us, 'UTC') AS end_at,
    latency_ms, loss, server_factor
FROM netflow.faults_kafka AS k
-- k.state, not state: the unqualified name is the sanitised output alias
WHERE length(k._error) = 0 AND k.state IN ('started', 'ended') AND k.fault_id != '';

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.faults_errors_mv ON CLUSTER netflow
TO netflow.ingest_errors
AS SELECT
    now() AS received_at, k._topic AS topic, k._offset AS kafka_offset,
    k._partition AS partition,
    if(length(k._error) > 0, k._error,
       if(k.fault_id = '', 'missing fault_id', 'invalid state')) AS error,
    if(length(k._error) > 0, k._raw_message,
       toJSONString(map('fault_id', k.fault_id, 'state', k.state))) AS raw_message
FROM netflow.faults_kafka AS k
WHERE length(k._error) > 0 OR k.state NOT IN ('started', 'ended') OR k.fault_id = '';

-- one row per incident with its real start and end
CREATE VIEW IF NOT EXISTS netflow.incidents ON CLUSTER netflow AS
SELECT
    fault_id,
    any(kind) AS kind,
    any(target) AS target,
    any(target_type) AS target_type,
    any(label) AS label,
    any(source) AS source,
    minIf(event_at, state = 'started') AS started_at,
    maxIf(event_at, state = 'ended') AS ended_at,
    countIf(state = 'ended') = 0 AS ongoing
FROM netflow.fault_events FINAL
GROUP BY fault_id;
