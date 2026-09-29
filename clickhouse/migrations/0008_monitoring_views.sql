-- Cluster health for dashboards and alerts, run with the admin's rights (SQL SECURITY
-- DEFINER) so the read-only grafana user needs neither REMOTE nor system.* grants.
-- skip_unavailable_shards keeps them answering while a node is down: that is exactly
-- when they are needed.

CREATE VIEW IF NOT EXISTS ops.servers_up ON CLUSTER netflow
DEFINER = admin SQL SECURITY DEFINER
AS SELECT count() AS servers
FROM clusterAllReplicas(netflow, system.one)
SETTINGS skip_unavailable_shards = 1;

CREATE VIEW IF NOT EXISTS ops.replica_status ON CLUSTER netflow
DEFINER = admin SQL SECURITY DEFINER
AS SELECT
    hostName() AS host,
    getMacro('shard') AS shard,
    table,
    is_leader,
    absolute_delay,
    queue_size,
    active_replicas
FROM clusterAllReplicas(netflow, system.replicas)
WHERE database = 'netflow'
SETTINGS skip_unavailable_shards = 1;

-- sharded tables: one replica per shard, so each part is counted once. Tables replicated
-- to every node (ingest_errors, fault_events) hold one full copy per shard, so they come
-- from a single replica instead
CREATE VIEW IF NOT EXISTS ops.table_storage ON CLUSTER netflow
DEFINER = admin SQL SECURITY DEFINER
AS SELECT
    table,
    sum(rows) AS rows,
    sum(data_compressed_bytes) AS compressed_bytes,
    sum(data_uncompressed_bytes) AS uncompressed_bytes,
    count() AS active_parts
FROM (
    SELECT table, rows, data_compressed_bytes, data_uncompressed_bytes
    FROM cluster(netflow, system.parts)
    WHERE active AND database = 'netflow' AND table NOT IN ('ingest_errors', 'fault_events')
    UNION ALL
    SELECT table, rows, data_compressed_bytes, data_uncompressed_bytes
    FROM system.parts
    WHERE active AND database = 'netflow' AND table IN ('ingest_errors', 'fault_events')
)
GROUP BY table
SETTINGS skip_unavailable_shards = 1;
