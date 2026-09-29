-- Fault localisation: which single element best explains the degraded sessions?
--
-- For every element on a session path (switch, router, circuit, PoP, provider, gateway,
-- server, app) over the window:
--   precision = degraded sessions through it / all sessions through it
--   coverage  = degraded sessions through it / all degraded sessions
-- and rank by their harmonic mean. A router and its circuit often carry identical
-- sessions and tie; both are reported, as the operator would want to see.
--
-- params: window_min (UInt16), min_degraded (UInt32)
WITH
    per_element AS (
        SELECT
            element_type,
            element_id,
            sum(sessions) AS sessions,
            sum(degraded) AS degraded,
            uniqMerge(degraded_devices) AS devices_hit,
            uniqMerge(degraded_branches) AS branches_hit,
            groupUniqArrayMerge(16)(degraded_apps) AS apps_hit,
            minIf(e.minute, e.degraded > 0) AS first_degraded_minute
        FROM netflow.element_1m AS e
        WHERE minute >= toStartOfMinute(now() - toIntervalMinute({window_min:UInt16}))
        GROUP BY element_type, element_id
    ),
    total AS (
        SELECT sum(degraded) AS all_degraded
        FROM per_element
        WHERE element_type = 'app'  -- every session has exactly one app: no double count
    )
SELECT
    element_type,
    element_id,
    degraded,
    sessions,
    round(degraded / sessions, 3) AS precision,
    round(degraded / (SELECT all_degraded FROM total), 3) AS coverage,
    round(2 * precision * coverage / (precision + coverage), 3) AS score,
    devices_hit,
    branches_hit,
    apps_hit,
    first_degraded_minute AS since
FROM per_element
WHERE degraded >= {min_degraded:UInt32}
ORDER BY score DESC, precision DESC
LIMIT 10
