-- How far each shard's stitcher has read the work queue. Replicated to every node so a
-- stitcher can resume from any replica.
CREATE TABLE IF NOT EXISTS ops.etl_watermarks ON CLUSTER netflow
(
    job         LowCardinality(String),
    shard       UInt8,
    watermark   DateTime64(3, 'UTC'),
    updated_at  DateTime64(3, 'UTC') DEFAULT now64(3)
)
ENGINE = ReplicatedReplacingMergeTree('/clickhouse/tables/all/ops/etl_watermarks', '{replica}', updated_at)
ORDER BY (job, shard);
