-- Network inventory (the CMDB). Probes never see any of this; it is joined onto flow
-- metadata downstream. Loaded from the generator's CSV export by the etl service.

CREATE SCHEMA IF NOT EXISTS inventory;
SET search_path TO inventory;

CREATE TABLE providers (
    code            text PRIMARY KEY,
    name            text NOT NULL UNIQUE,
    latency_factor  numeric(4, 2) NOT NULL
);

CREATE TABLE pops (
    pop_id       text PRIMARY KEY,
    provider     text NOT NULL REFERENCES providers (code),
    region       text NOT NULL,
    backbone_ms  numeric(6, 2) NOT NULL CHECK (backbone_ms > 0),
    UNIQUE (provider, region)
);

CREATE TABLE gateways (
    gateway_id  text PRIMARY KEY,
    dc          text NOT NULL
);

CREATE TABLE branches (
    branch_id           text PRIMARY KEY,
    index               integer NOT NULL UNIQUE,
    region              text NOT NULL,
    size                text NOT NULL CHECK (size IN ('small', 'medium', 'large', 'flagship')),
    lan_prefix          cidr NOT NULL UNIQUE,
    guest_prefix        cidr NOT NULL UNIQUE,
    router_id           text NOT NULL UNIQUE,
    switch_ids          text NOT NULL,  -- '|' separated, normalised into switches below
    primary_circuit_id  text NOT NULL,
    backup_circuit_id   text NOT NULL
);

CREATE TABLE circuits (
    circuit_id      text PRIMARY KEY,
    tunnel_id       integer NOT NULL UNIQUE,
    branch_id       text NOT NULL REFERENCES branches (branch_id),
    provider        text NOT NULL REFERENCES providers (code),
    pop_id          text NOT NULL REFERENCES pops (pop_id),
    role            text NOT NULL CHECK (role IN ('primary', 'backup')),
    bandwidth_mbps  integer NOT NULL CHECK (bandwidth_mbps > 0),
    access_ms       numeric(6, 2) NOT NULL,
    UNIQUE (branch_id, role)
);

ALTER TABLE branches
    ADD FOREIGN KEY (primary_circuit_id) REFERENCES circuits (circuit_id) DEFERRABLE INITIALLY DEFERRED,
    ADD FOREIGN KEY (backup_circuit_id) REFERENCES circuits (circuit_id) DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE devices (
    device_id    text PRIMARY KEY,
    device_type  text NOT NULL,
    branch_id    text NOT NULL REFERENCES branches (branch_id),
    switch_id    text NOT NULL,
    ip           inet NOT NULL UNIQUE
);
CREATE INDEX ON devices (branch_id);

CREATE TABLE servers (
    server_id  text PRIMARY KEY,
    app        text NOT NULL,
    dc         text NOT NULL,
    ip         inet NOT NULL UNIQUE
);

CREATE TABLE apps (
    name         text PRIMARY KEY,
    category     text NOT NULL,
    criticality  text NOT NULL CHECK (criticality IN ('critical', 'business', 'bulk')),
    ip_proto     smallint NOT NULL,
    l7           text NOT NULL,
    dst_port     integer NOT NULL,
    hosted       text NOT NULL CHECK (hosted IN ('dc', 'internet'))
);

-- what "good" means per application: the quality score is measured against these.
-- owned by the business, not generated.
CREATE TABLE sla_targets (
    app                text PRIMARY KEY REFERENCES apps (name),
    response_good_ms   integer NOT NULL,
    response_bad_ms    integer NOT NULL,
    rtt_good_ms        integer NOT NULL,
    rtt_bad_ms         integer NOT NULL,
    retrans_good_pct   numeric(5, 2) NOT NULL,
    retrans_bad_pct    numeric(5, 2) NOT NULL,
    CHECK (response_bad_ms > response_good_ms),
    CHECK (rtt_bad_ms > rtt_good_ms),
    CHECK (retrans_bad_pct > retrans_good_pct)
);

-- flattened views the ClickHouse dictionaries read from
CREATE VIEW v_device_by_ip AS
SELECT (d.ip - '0.0.0.0'::inet) AS ip_num, d.device_id, d.device_type, d.branch_id, d.switch_id
FROM devices d
WHERE d.device_type <> 'guest';

CREATE VIEW v_branch_by_prefix AS
SELECT host(b.lan_prefix) || '/' || masklen(b.lan_prefix) AS prefix, b.branch_id, 'lan' AS segment
FROM branches b
UNION ALL
SELECT host(b.guest_prefix) || '/' || masklen(b.guest_prefix), b.branch_id, 'guest'
FROM branches b;

CREATE VIEW v_circuit_by_tunnel AS
SELECT c.tunnel_id, c.circuit_id, c.branch_id, c.provider, p.name AS provider_name,
       c.pop_id, c.role, c.bandwidth_mbps
FROM circuits c
JOIN providers p ON p.code = c.provider;

CREATE VIEW v_branch AS
SELECT b.branch_id, b.region, b.size, b.router_id,
       split_part(b.switch_ids, '|', 1) AS payment_switch_id,
       c.provider AS primary_provider,
       c.circuit_id AS primary_circuit_id
FROM branches b
JOIN circuits c ON c.circuit_id = b.primary_circuit_id;

CREATE VIEW v_server_by_ip AS
SELECT (s.ip - '0.0.0.0'::inet) AS ip_num, s.server_id, s.app, s.dc
FROM servers s;

-- every app keeps its category and criticality; one without SLA targets yet is scored
-- against generic targets instead of disappearing from the dictionary
CREATE VIEW v_app AS
SELECT a.name, a.category, a.criticality,
       coalesce(t.response_good_ms, 300) AS response_good_ms,
       coalesce(t.response_bad_ms, 1000) AS response_bad_ms,
       coalesce(t.rtt_good_ms, 40) AS rtt_good_ms,
       coalesce(t.rtt_bad_ms, 100) AS rtt_bad_ms,
       coalesce(t.retrans_good_pct, 1) AS retrans_good_pct,
       coalesce(t.retrans_bad_pct, 3) AS retrans_bad_pct
FROM apps a
LEFT JOIN sla_targets t ON t.app = a.name;
