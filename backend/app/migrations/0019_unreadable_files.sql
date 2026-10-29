-- Files the scanner found but couldn't read (damaged, incomplete, not really media).
-- Remembered with their size and time, so later scans skip them until they change, and
-- listed for admins (Admin > Maintenance) to retry, ignore or delete.
CREATE TABLE unreadable_files (
    path       text        PRIMARY KEY,
    library_id bigint      NOT NULL REFERENCES library_paths (id) ON DELETE CASCADE,
    size       bigint      NOT NULL,
    mod_time   timestamptz NOT NULL,
    reason     text        NOT NULL,
    ignored    boolean     NOT NULL DEFAULT false,
    first_seen timestamptz NOT NULL DEFAULT now(),
    last_seen  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX unreadable_files_library_idx ON unreadable_files (library_id);
