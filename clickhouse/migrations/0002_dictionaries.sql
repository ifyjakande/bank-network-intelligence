-- Inventory lookups, refreshed from Postgres every 1-2 minutes. Every node holds a copy,
-- so enrichment during stitching never leaves the server.

CREATE DICTIONARY IF NOT EXISTS netflow.device_by_ip ON CLUSTER netflow
(
    ip_num       UInt64,
    device_id    String,
    device_type  String,
    branch_id    String,
    switch_id    String
)
PRIMARY KEY ip_num
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_device_by_ip'))
LAYOUT(HASHED())
LIFETIME(MIN 60 MAX 120);

-- longest-prefix match: covers guest wi-fi clients that are not in the device table
CREATE DICTIONARY IF NOT EXISTS netflow.branch_by_prefix ON CLUSTER netflow
(
    prefix     String,
    branch_id  String,
    segment    String
)
PRIMARY KEY prefix
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_branch_by_prefix'))
LAYOUT(IP_TRIE())
LIFETIME(MIN 60 MAX 120);

CREATE DICTIONARY IF NOT EXISTS netflow.circuit_by_tunnel ON CLUSTER netflow
(
    tunnel_id       UInt64,
    circuit_id      String,
    branch_id       String,
    provider        String,
    provider_name   String,
    pop_id          String,
    role            String,
    bandwidth_mbps  UInt32
)
PRIMARY KEY tunnel_id
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_circuit_by_tunnel'))
LAYOUT(HASHED())
LIFETIME(MIN 60 MAX 120);

CREATE DICTIONARY IF NOT EXISTS netflow.branch ON CLUSTER netflow
(
    branch_id          String,
    region             String,
    size               String,
    router_id          String,
    payment_switch_id  String
)
PRIMARY KEY branch_id
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_branch'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(MIN 60 MAX 120);

CREATE DICTIONARY IF NOT EXISTS netflow.server_by_ip ON CLUSTER netflow
(
    ip_num     UInt64,
    server_id  String,
    app        String,
    dc         String
)
PRIMARY KEY ip_num
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_server_by_ip'))
LAYOUT(HASHED())
LIFETIME(MIN 60 MAX 120);

CREATE DICTIONARY IF NOT EXISTS netflow.app ON CLUSTER netflow
(
    name              String,
    category          String,
    criticality       String,
    response_good_ms  Float32,
    response_bad_ms   Float32,
    rtt_good_ms       Float32,
    rtt_bad_ms        Float32,
    retrans_good_pct  Float32,
    retrans_bad_pct   Float32
)
PRIMARY KEY name
SOURCE(POSTGRESQL(NAME pg_inventory TABLE 'v_app'))
LAYOUT(COMPLEX_KEY_HASHED())
LIFETIME(MIN 60 MAX 120);
