-- Fault ids used to be a per-process counter (sched-00004), so a generator restart reused
-- them and the view paired one run's start with an earlier run's end. Ids now carry a run
-- id (sched-1a2b3c4d-00004). The view also stays correct for the old ids: an end event
-- from before the latest start is ignored. A generator killed mid-incident never sends its
-- end event, so the view falls back to the planned end instead of leaving it open forever.
CREATE OR REPLACE VIEW netflow.incidents ON CLUSTER netflow AS
SELECT
    fault_id, kind, target, target_type, label, source, started_at,
    if(ended_event >= started_at, ended_event, least(planned_end, now64(6))) AS ended_at,
    ended_event < started_at AND planned_end > now64(6) AS ongoing
FROM
(
    SELECT
        fault_id,
        argMaxIf(kind, event_at, state = 'started') AS kind,
        argMaxIf(target, event_at, state = 'started') AS target,
        argMaxIf(target_type, event_at, state = 'started') AS target_type,
        argMaxIf(label, event_at, state = 'started') AS label,
        argMaxIf(source, event_at, state = 'started') AS source,
        maxIf(event_at, state = 'started') AS started_at,
        maxIf(event_at, state = 'ended') AS ended_event,
        argMaxIf(end_at, event_at, state = 'started') AS planned_end,
        countIf(state = 'started') > 0 AS has_start
    FROM netflow.fault_events FINAL
    GROUP BY fault_id
)
WHERE has_start;
