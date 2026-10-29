-- Watcher status per library, written by the worker so admins can see it in the UI.
ALTER TABLE library_paths
    ADD COLUMN watch_state text CHECK (watch_state IN ('starting', 'active', 'error')),
    ADD COLUMN watch_error text;
