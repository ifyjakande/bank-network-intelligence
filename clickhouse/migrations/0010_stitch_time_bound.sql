-- The stitcher looked up each window's sessions by community_id. Community IDs are hashes,
-- so a window's ~30k touched sessions sat in nearly every granule and every run read the
-- whole day: 105 thousand rows per run at start-up, 27 million twelve hours later.
--
-- The queue now carries each record's first_seen, so a run knows the oldest session it
-- touches (sessions run up to ~45 minutes, never a day), and half-flows get a projection
-- ordered by time, so that bound reads the last hour instead of the day. Rows queued
-- before this migration have no first_seen; the stitch query falls back to its old bound
-- for any window that still holds one.
ALTER TABLE netflow.stitch_queue_local ON CLUSTER netflow
    ADD COLUMN IF NOT EXISTS first_seen DateTime64(6, 'UTC') DEFAULT toDateTime64(0, 6, 'UTC')
    CODEC(Delta, ZSTD(1));

ALTER TABLE netflow.stitch_queue ON CLUSTER netflow
    ADD COLUMN IF NOT EXISTS first_seen DateTime64(6, 'UTC') DEFAULT toDateTime64(0, 6, 'UTC');

-- changed in place: dropping and recreating the view would lose the queue rows of any
-- insert that landed in between
ALTER TABLE netflow.stitch_queue_mv ON CLUSTER netflow
    MODIFY QUERY SELECT ingested_at, community_id, first_seen FROM netflow.halfflows_local;

ALTER TABLE netflow.halfflows_local ON CLUSTER netflow
    ADD PROJECTION IF NOT EXISTS by_first_seen (SELECT * ORDER BY first_seen);

-- builds the projection for parts written before it existed (a background mutation)
ALTER TABLE netflow.halfflows_local ON CLUSTER netflow MATERIALIZE PROJECTION by_first_seen;
