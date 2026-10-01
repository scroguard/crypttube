-- Small key/value store for system-wide state: maintenance settings, the time the daily
-- maintenance was last queued, and the reports of the last maintenance and cleanup runs.
CREATE TABLE system_state (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
