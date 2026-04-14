-- ps_contacts: stores WebRTC/SIP client registrations
CREATE TABLE IF NOT EXISTS ps_contacts (
    id                    VARCHAR(255) NOT NULL,
    uri                   VARCHAR(511),
    expiration_time       VARCHAR(30),
    qualify_frequency     INTEGER,
    outbound_proxy        VARCHAR(255),
    path                  TEXT,
    user_agent            VARCHAR(255),
    qualify_timeout       FLOAT,
    reg_server            VARCHAR(255),
    authenticate_qualify  VARCHAR(40),
    via_addr              VARCHAR(40),
    via_port              INTEGER,
    call_id               VARCHAR(255),
    endpoint              VARCHAR(255),
    qualify_2xx_only      VARCHAR(40),
    prune_on_boot         VARCHAR(40),
    PRIMARY KEY (id)
);

-- ps_endpoint_id_ips: identifies endpoints by IP
CREATE TABLE IF NOT EXISTS ps_endpoint_id_ips (
    id            VARCHAR(40) NOT NULL,
    endpoint      VARCHAR(40),
    match         VARCHAR(80),
    srv_lookups   VARCHAR(3),
    match_header  VARCHAR(255),
    PRIMARY KEY (id)
);

-- Index for contacts lookup by endpoint
CREATE INDEX IF NOT EXISTS ps_contacts_endpoint_idx ON ps_contacts (endpoint);

-- Show all ps_ tables to confirm
SELECT tablename FROM pg_tables WHERE tablename LIKE 'ps_%' ORDER BY tablename;
