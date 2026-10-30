-- Folders linked to a performer or studio: media arriving in one gets tagged (app/folder_links.py).
-- created_by decides whether a link still applies: see the access check there.

CREATE TABLE folder_links (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    performer_id bigint REFERENCES performers (id) ON DELETE CASCADE,
    studio_id    bigint REFERENCES studios (id) ON DELETE CASCADE,
    library_id   bigint NOT NULL REFERENCES library_paths (id) ON DELETE CASCADE,
    path         text   NOT NULL,
    created_by   bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CHECK (num_nonnulls(performer_id, studio_id) = 1)
);
CREATE UNIQUE INDEX folder_links_performer_key ON folder_links (performer_id, path) WHERE performer_id IS NOT NULL;
CREATE UNIQUE INDEX folder_links_studio_key    ON folder_links (studio_id, path) WHERE studio_id IS NOT NULL;
-- Any number of performers on a folder, but one studio.
CREATE UNIQUE INDEX folder_links_one_studio ON folder_links (path) WHERE studio_id IS NOT NULL;
CREATE INDEX folder_links_library_idx ON folder_links (library_id);
