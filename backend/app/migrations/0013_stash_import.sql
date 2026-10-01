-- Staging for importing tags, performers and studios from a Stash database.
-- Nothing staged here is visible anywhere else: an admin reviews the list and each
-- item becomes real only when approved. Finishing or rejecting deletes the staging rows.

CREATE TABLE stash_imports (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_by    bigint      REFERENCES users (id) ON DELETE SET NULL,
    filename      text,
    stash_version integer,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE stash_import_items (
    import_id bigint  NOT NULL REFERENCES stash_imports (id) ON DELETE CASCADE,
    kind      text    NOT NULL CHECK (kind IN ('tag', 'performer', 'studio')),
    source_id bigint  NOT NULL,              -- the row's id in the Stash database
    name      text    NOT NULL,
    usage     integer NOT NULL DEFAULT 0,    -- how many scenes/images/galleries used it there
    data      jsonb   NOT NULL,              -- already mapped to this system's fields
    -- NULL = waiting for review; 'imported' = created by this import;
    -- 'existing' = approved, but an item with that name was already here (left untouched)
    decision  text    CHECK (decision IN ('imported', 'existing')),
    target_id bigint,                        -- the tag/performer/studio it became or matched
    PRIMARY KEY (import_id, kind, source_id)
);
