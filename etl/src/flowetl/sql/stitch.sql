-- Stitch one window of the work queue into sessions, on one shard's local tables.
--
-- A session is emitted once: when both halves have their final record, or when it is
-- older than the timeout (then marked incomplete). Re-running a window is safe: rows
-- already in sessions_local are excluded, and the insert carries a dedup token.
INSERT INTO netflow.sessions_local (
    session_start, session_end, community_id, stitched_at, complete, asymmetric, records,
    client_ip, server_ip, client_port, server_port, ip_proto, probe_c2s, probe_s2c,
    app, app_category, criticality, l7_proto,
    branch_id, region, branch_size, segment, router_id, switch_id, device_id, device_type,
    tunnel_id, circuit_id, circuit_role, provider, pop_id, server_id, dc,
    bytes_c2s, bytes_s2c, packets_c2s, packets_s2c, retrans_c2s, retrans_s2c, retrans_pct,
    server_rtt_ms, client_rtt_ms, response_ms, quality, degraded
)
WITH
    touched AS (
        SELECT DISTINCT community_id
        FROM netflow.stitch_queue_local
        WHERE (ingested_at > {lo:DateTime64(3)} AND ingested_at <= {hi:DateTime64(3)})
           -- second look at sessions from one timeout ago, to flush ones that never completed
           OR (ingested_at > {lo:DateTime64(3)} - toIntervalSecond({timeout_s:UInt32})
               AND ingested_at <= {hi:DateTime64(3)} - toIntervalSecond({timeout_s:UInt32}))
    ),
    records AS (
        SELECT *
        FROM netflow.halfflows_local
        WHERE community_id IN (SELECT community_id FROM touched)
          AND first_seen >= {hi:DateTime64(3)} - toIntervalSecond({lookback_s:UInt32})
        LIMIT 1 BY record_id  -- replays from the upstream at-least-once hop
    ),
    halves AS (
        SELECT
            community_id,
            first_seen,
            direction,
            any(probe_id) AS probe,
            any(src_ip) AS src_ip,
            any(dst_ip) AS dst_ip,
            any(src_port) AS src_port,
            any(dst_port) AS dst_port,
            any(ip_proto) AS ip_proto,
            any(tunnel_id) AS tunnel_id,
            any(app) AS app,
            any(app_category) AS app_category,
            any(l7_proto) AS l7_proto,
            sum(bytes) AS bytes,
            sum(packets) AS packets,
            sum(retrans_packets) AS retrans,
            max(is_final) AS final,
            count() AS n_records,
            max(flow_end) AS last_seen,
            maxIf(toUnixTimestamp64Micro(tcp_syn), record_seq = 0) AS syn_us,
            maxIf(toUnixTimestamp64Micro(tcp_synack), record_seq = 0) AS synack_us,
            maxIf(toUnixTimestamp64Micro(tcp_ack), record_seq = 0) AS ack_us,
            maxIf(toUnixTimestamp64Micro(first_req), record_seq = 0) AS req_us,
            maxIf(toUnixTimestamp64Micro(first_resp), record_seq = 0) AS resp_us
        FROM records
        GROUP BY community_id, first_seen, direction
    ),
    c AS (SELECT * FROM halves WHERE direction = 'c2s'),
    s AS (SELECT * FROM halves WHERE direction = 's2c'),
    paired AS (
        -- the s2c half is the nearest one seen at or after the c2s half; anything further
        -- than the tolerance belongs to a later session that reused the same 5-tuple
        SELECT
            c.*,
            s.community_id != ''
                AND dateDiff('millisecond', c.first_seen, s.first_seen)
                    <= {pair_tolerance_ms:UInt32} AS matched,
            if(matched, s.probe, '') AS s_probe,
            if(matched, s.bytes, 0) AS s_bytes,
            if(matched, s.packets, 0) AS s_packets,
            if(matched, s.retrans, 0) AS s_retrans,
            if(matched, s.final, false) AS s_final,
            if(matched, s.n_records, 0) AS s_records,
            if(matched, s.last_seen, c.last_seen) AS s_last_seen,
            if(matched, s.synack_us, 0) AS s_synack_us,
            if(matched, s.resp_us, 0) AS s_resp_us
        FROM c
        ASOF LEFT JOIN s ON c.community_id = s.community_id AND c.first_seen <= s.first_seen
    ),
    enriched AS (
        SELECT
            *,
            toUInt64(toUInt32(src_ip)) AS client_key,
            dictGetOrDefault('netflow.branch_by_prefix', 'branch_id', tuple(src_ip), '') AS br,
            dictGetOrDefault('netflow.branch_by_prefix', 'segment', tuple(src_ip), '') AS seg,
            dictGetOrDefault('netflow.device_by_ip', 'device_id', client_key, '') AS dev,
            dictGetOrDefault('netflow.server_by_ip', 'server_id',
                             toUInt64(toUInt32(dst_ip)), '') AS srv,
            if(ip_proto = 6 AND matched AND syn_us > 0 AND s_synack_us > syn_us,
               (s_synack_us - syn_us) / 1000, NULL) AS server_rtt,
            if(ip_proto = 6 AND matched AND s_synack_us > 0 AND ack_us > s_synack_us,
               (ack_us - s_synack_us) / 1000, NULL) AS client_rtt,
            if(ip_proto = 6 AND matched AND req_us > 0 AND s_resp_us > req_us,
               (s_resp_us - req_us) / 1000, NULL) AS response,
            100 * (retrans + s_retrans) / greatest(1, packets + s_packets) AS retrans_pct_v,
            dictGet('netflow.app', ('response_good_ms', 'response_bad_ms', 'rtt_good_ms',
                                    'rtt_bad_ms', 'retrans_good_pct', 'retrans_bad_pct'),
                    app) AS sla
        FROM paired
        WHERE ((matched AND final AND s_final)
               OR first_seen < {hi:DateTime64(3)} - toIntervalSecond({timeout_s:UInt32}))
          AND (community_id, first_seen) NOT IN (
              SELECT community_id, session_start
              FROM netflow.sessions_local
              WHERE community_id IN (SELECT community_id FROM touched)
                AND session_start >= {hi:DateTime64(3)} - toIntervalSecond({lookback_s:UInt32})
          )
    ),
    scored AS (
        SELECT
            *,
            -- each component is 1 at or better than "good", 0 at or worse than "bad"
            if(response IS NULL, NULL,
               greatest(0, least(1, (sla.2 - response) / (sla.2 - sla.1)))) AS resp_score,
            if(client_rtt IS NULL, NULL,
               greatest(0, least(1, (sla.4 - client_rtt) / (sla.4 - sla.3)))) AS rtt_score,
            greatest(0, least(1, (sla.6 - retrans_pct_v) / (sla.6 - sla.5))) AS loss_score,
            -- response time and WAN RTT weigh equally; loss shows up in both, so it gets less
            toUInt8(round(100 * (coalesce(resp_score, 0) * 0.4 + coalesce(rtt_score, 0) * 0.4
                                 + loss_score * 0.2)
                          / (if(resp_score IS NULL, 0, 0.4) + if(rtt_score IS NULL, 0, 0.4)
                             + 0.2))) AS quality_v
        FROM enriched
    )
SELECT
    first_seen AS session_start,
    greatest(last_seen, s_last_seen) AS session_end,
    community_id,
    now64(3) AS stitched_at,
    matched AND final AND s_final AS complete,
    matched AND probe != s_probe AS asymmetric,
    toUInt16(n_records + s_records) AS records,
    src_ip AS client_ip,
    dst_ip AS server_ip,
    src_port AS client_port,
    dst_port AS server_port,
    ip_proto,
    probe AS probe_c2s,
    s_probe AS probe_s2c,
    app,
    app_category,
    dictGetOrDefault('netflow.app', 'criticality', app, 'unknown') AS criticality,
    l7_proto,
    br AS branch_id,
    dictGetOrDefault('netflow.branch', 'region', br, '') AS region,
    dictGetOrDefault('netflow.branch', 'size', br, '') AS branch_size,
    seg AS segment,
    dictGetOrDefault('netflow.branch', 'router_id', br, '') AS router_id,
    if(dev != '', dictGet('netflow.device_by_ip', 'switch_id', client_key), '') AS switch_id,
    if(dev != '', dev, if(seg = 'guest', concat(br, '-GUEST-', toString(src_ip)), ''))
        AS device_id,
    if(dev != '', dictGet('netflow.device_by_ip', 'device_type', client_key),
       if(seg = 'guest', 'guest', 'unknown')) AS device_type,
    tunnel_id,
    dictGetOrDefault('netflow.circuit_by_tunnel', 'circuit_id', tunnel_id, '') AS circuit_id,
    dictGetOrDefault('netflow.circuit_by_tunnel', 'role', tunnel_id, '') AS circuit_role,
    dictGetOrDefault('netflow.circuit_by_tunnel', 'provider', tunnel_id, '') AS provider,
    dictGetOrDefault('netflow.circuit_by_tunnel', 'pop_id', tunnel_id, '') AS pop_id,
    srv AS server_id,
    if(srv != '', dictGet('netflow.server_by_ip', 'dc', toUInt64(toUInt32(dst_ip))), 'internet')
        AS dc,
    bytes AS bytes_c2s,
    s_bytes AS bytes_s2c,
    packets AS packets_c2s,
    s_packets AS packets_s2c,
    retrans AS retrans_c2s,
    s_retrans AS retrans_s2c,
    toFloat32(retrans_pct_v) AS retrans_pct,
    toNullable(toFloat32(server_rtt)) AS server_rtt_ms,
    toNullable(toFloat32(client_rtt)) AS client_rtt_ms,
    toNullable(toFloat32(response)) AS response_ms,
    quality_v AS quality,
    quality_v < {degraded_below:UInt8} AS degraded
FROM scored
