-- Ingestion: job queue, library options, natural filename ordering.

-- Numeric-aware ordering so "img2" sorts before "img10".
CREATE COLLATION natural_sort (provider = icu, locale = 'und-u-kn');

ALTER TABLE library_paths
    ADD COLUMN create_folder_galleries boolean NOT NULL DEFAULT true;

CREATE TYPE job_status AS ENUM ('queued', 'running', 'done', 'failed', 'cancelled');

CREATE TABLE jobs (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind             text        NOT NULL,
    payload          jsonb       NOT NULL DEFAULT '{}',
    status           job_status  NOT NULL DEFAULT 'queued',
    -- At most one *queued* job per key; a new request for the same key while one
    -- is queued collapses into it. A running job does not block a new queued one,
    -- so changes that land mid-run are still picked up afterwards.
    dedupe_key       text,
    run_after        timestamptz NOT NULL DEFAULT now(),
    progress         real,
    message          text,
    error            text,
    attempts         smallint    NOT NULL DEFAULT 0,
    cancel_requested boolean     NOT NULL DEFAULT false,
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz
);
CREATE UNIQUE INDEX jobs_dedupe_key ON jobs (dedupe_key) WHERE status = 'queued';
CREATE INDEX jobs_queue_idx   ON jobs (run_after, id) WHERE status = 'queued';
CREATE INDEX jobs_running_idx ON jobs (dedupe_key) WHERE status = 'running';
CREATE INDEX jobs_created_idx ON jobs (created_at DESC, id DESC);
