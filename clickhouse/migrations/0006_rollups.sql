-- Per-minute rollups maintained on insert into sessions_local. Dashboards read these,
-- never the session table, so panel cost does not grow with traffic.

-- service quality by where the traffic came from and what it was
CREATE TABLE IF NOT EXISTS netflow.quality_1m_local ON CLUSTER netflow
(
    minute            DateTime('UTC'),
    branch_id         LowCardinality(String),
    region            LowCardinality(String),
    provider          LowCardinality(String),
    pop_id            LowCardinality(String),
    circuit_id        LowCardinality(String),
    circuit_role      LowCardinality(String),
    app               LowCardinality(String),
    criticality       LowCardinality(String),
    sessions          SimpleAggregateFunction(sum, UInt64),
    degraded          SimpleAggregateFunction(sum, UInt64),
    bytes             SimpleAggregateFunction(sum, UInt64),
    packets           SimpleAggregateFunction(sum, UInt64),
    retrans           SimpleAggregateFunction(sum, UInt64),
    quality_sum       SimpleAggregateFunction(sum, UInt64),
    response_ms       AggregateFunction(quantilesTDigest(0.5, 0.95), Float32),
    client_rtt_ms     AggregateFunction(quantilesTDigest(0.5, 0.95), Float32),
    devices           AggregateFunction(uniq, String),
    degraded_devices  AggregateFunction(uniq, String)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toYYYYMM(minute)
-- region/provider/pop/role/criticality are functions of branch, circuit and app, so they
-- ride along in the sort key without adding rows; the index only uses the prefix
PRIMARY KEY (minute, branch_id, app, circuit_id)
ORDER BY (minute, branch_id, app, circuit_id, region, provider, pop_id, circuit_role, criticality)
TTL minute + INTERVAL 180 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.quality_1m_mv ON CLUSTER netflow
TO netflow.quality_1m_local
-- source columns are qualified (s.) where an output alias reuses their name
AS SELECT
    toStartOfMinute(session_start) AS minute,
    branch_id, region, provider, pop_id, circuit_id, circuit_role, app, criticality,
    count() AS sessions,
    countIf(s.degraded) AS degraded,
    sum(bytes_c2s + bytes_s2c) AS bytes,
    sum(packets_c2s + packets_s2c) AS packets,
    sum(retrans_c2s + retrans_s2c) AS retrans,
    sum(quality) AS quality_sum,
    quantilesTDigestStateIf(0.5, 0.95)(assumeNotNull(response_ms), response_ms IS NOT NULL)
        AS response_ms,
    quantilesTDigestStateIf(0.5, 0.95)(assumeNotNull(client_rtt_ms), client_rtt_ms IS NOT NULL)
        AS client_rtt_ms,
    uniqState(device_id) AS devices,
    uniqStateIf(s.device_id, s.degraded) AS degraded_devices
FROM netflow.sessions_local AS s
GROUP BY minute, branch_id, region, provider, pop_id, circuit_id, circuit_role, app, criticality;

CREATE TABLE IF NOT EXISTS netflow.quality_1m ON CLUSTER netflow AS netflow.quality_1m_local
ENGINE = Distributed(netflow, netflow, quality_1m_local);

-- the same sessions exploded onto every element on their path: the input for "which
-- single element explains the degraded sessions?"
CREATE TABLE IF NOT EXISTS netflow.element_1m_local ON CLUSTER netflow
(
    minute             DateTime('UTC'),
    element_type       LowCardinality(String),
    element_id         LowCardinality(String),
    sessions           SimpleAggregateFunction(sum, UInt64),
    degraded           SimpleAggregateFunction(sum, UInt64),
    response_ms        AggregateFunction(quantilesTDigest(0.5, 0.95), Float32),
    devices            AggregateFunction(uniq, String),
    degraded_devices   AggregateFunction(uniq, String),
    degraded_branches  AggregateFunction(uniq, String),
    degraded_apps      AggregateFunction(groupUniqArray(16), String)
)
ENGINE = ReplicatedAggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (element_type, element_id, minute)
TTL minute + INTERVAL 180 DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS netflow.element_1m_mv ON CLUSTER netflow
TO netflow.element_1m_local
AS SELECT
    toStartOfMinute(session_start) AS minute,
    el.1 AS element_type,
    el.2 AS element_id,
    count() AS sessions,
    countIf(s.degraded) AS degraded,
    quantilesTDigestStateIf(0.5, 0.95)(assumeNotNull(response_ms), response_ms IS NOT NULL)
        AS response_ms,
    uniqState(device_id) AS devices,
    uniqStateIf(s.device_id, s.degraded) AS degraded_devices,
    uniqStateIf(s.branch_id, s.degraded) AS degraded_branches,
    groupUniqArrayStateIf(16)(s.app, s.degraded) AS degraded_apps
FROM netflow.sessions_local AS s
ARRAY JOIN arrayFilter(e -> e.2 != '', arrayDistinct([
    ('switch', switch_id), ('router', router_id), ('circuit', circuit_id),
    ('pop', pop_id), ('provider', provider), ('gateway', probe_c2s), ('gateway', probe_s2c),
    ('server', server_id), ('app', app)
])) AS el
GROUP BY minute, element_type, element_id;

CREATE TABLE IF NOT EXISTS netflow.element_1m ON CLUSTER netflow AS netflow.element_1m_local
ENGINE = Distributed(netflow, netflow, element_1m_local);
