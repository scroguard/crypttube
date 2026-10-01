-- User-owned libraries, sharing, and personal metadata.
--
-- A library with owner_id IS NULL is a global (admin) library, visible to everyone
-- subject to the per-user deny rules. A library with an owner is visible to the owner,
-- to users it is shared with, or to everyone, per `visibility`. Admins get no special
-- access to other users' private libraries.

ALTER TABLE users
    ADD COLUMN can_upload boolean NOT NULL DEFAULT false,
    ADD COLUMN upload_quota_bytes bigint CHECK (upload_quota_bytes >= 0);  -- NULL = unlimited

ALTER TABLE library_paths
    ADD COLUMN owner_id   bigint REFERENCES users (id) ON DELETE CASCADE,
    ADD COLUMN name       text,
    ADD COLUMN visibility text NOT NULL DEFAULT 'private'
        CHECK (visibility IN ('private', 'users', 'everyone'));
CREATE INDEX library_paths_owner_idx ON library_paths (owner_id) WHERE owner_id IS NOT NULL;

CREATE TABLE library_shares (
    library_id bigint NOT NULL REFERENCES library_paths (id) ON DELETE CASCADE,
    user_id    bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    PRIMARY KEY (library_id, user_id)
);
CREATE INDEX library_shares_user_idx ON library_shares (user_id);

-- ---------------------------------------------------------------------------
-- library_id on content rows, so visibility is a per-row check (no join to files).
-- Maintained by triggers: the scanner and any other writer can't get it wrong.
-- ---------------------------------------------------------------------------

ALTER TABLE scenes    ADD COLUMN library_id bigint REFERENCES library_paths (id) ON DELETE CASCADE;
ALTER TABLE images    ADD COLUMN library_id bigint REFERENCES library_paths (id) ON DELETE CASCADE;
ALTER TABLE galleries ADD COLUMN library_id bigint REFERENCES library_paths (id) ON DELETE CASCADE;

UPDATE scenes s SET library_id = f.library_id FROM files f WHERE f.id = s.file_id;
UPDATE images i SET library_id = f.library_id FROM files f WHERE f.id = i.file_id;
UPDATE galleries g SET library_id = f.library_id FROM files f WHERE f.id = g.zip_file_id;
UPDATE galleries g SET library_id = l.id FROM library_paths l
WHERE g.folder_path IS NOT NULL AND (g.folder_path = l.path OR starts_with(g.folder_path, l.path || '/'));

ALTER TABLE scenes ALTER COLUMN library_id SET NOT NULL;
ALTER TABLE images ALTER COLUMN library_id SET NOT NULL;
-- galleries.library_id stays nullable: a hand-curated (virtual) gallery has no library.

CREATE INDEX scenes_library_idx    ON scenes (library_id);
CREATE INDEX images_library_idx    ON images (library_id);
CREATE INDEX galleries_library_idx ON galleries (library_id);

CREATE FUNCTION content_library_from_file() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'galleries' THEN
        IF NEW.zip_file_id IS NOT NULL THEN
            NEW.library_id := (SELECT library_id FROM files WHERE id = NEW.zip_file_id);
        END IF;
    ELSE
        NEW.library_id := (SELECT library_id FROM files WHERE id = NEW.file_id);
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER scenes_library BEFORE INSERT OR UPDATE OF file_id ON scenes
    FOR EACH ROW EXECUTE FUNCTION content_library_from_file();
CREATE TRIGGER images_library BEFORE INSERT OR UPDATE OF file_id ON images
    FOR EACH ROW EXECUTE FUNCTION content_library_from_file();
CREATE TRIGGER galleries_library BEFORE INSERT OR UPDATE OF zip_file_id ON galleries
    FOR EACH ROW EXECUTE FUNCTION content_library_from_file();

-- A file moved to another library takes its content with it.
CREATE FUNCTION files_library_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    UPDATE scenes    SET library_id = NEW.library_id WHERE file_id = NEW.id;
    UPDATE images    SET library_id = NEW.library_id WHERE file_id = NEW.id;
    UPDATE galleries SET library_id = NEW.library_id WHERE zip_file_id = NEW.id;
    RETURN NULL;
END $$;
CREATE TRIGGER files_library_changed AFTER UPDATE OF library_id ON files
    FOR EACH ROW WHEN (OLD.library_id IS DISTINCT FROM NEW.library_id)
    EXECUTE FUNCTION files_library_changed();

-- ---------------------------------------------------------------------------
-- Personal metadata: tags / performers / studios created by regular users belong to
-- them (owner_id) and are visible to others only through content they can see.
-- owner_id IS NULL = global (created by editors/admins).
-- ---------------------------------------------------------------------------

ALTER TABLE tags       ADD COLUMN owner_id bigint REFERENCES users (id) ON DELETE CASCADE;
ALTER TABLE performers ADD COLUMN owner_id bigint REFERENCES users (id) ON DELETE CASCADE;
ALTER TABLE studios    ADD COLUMN owner_id bigint REFERENCES users (id) ON DELETE CASCADE;

-- Names are unique per owner, so one user's personal names can neither collide with
-- nor reveal (via a "name taken" error) another user's.
DROP INDEX tags_name_key;
CREATE UNIQUE INDEX tags_name_key ON tags (lower(name), coalesce(owner_id, 0));
DROP INDEX studios_name_key;
CREATE UNIQUE INDEX studios_name_key ON studios (lower(name), coalesce(owner_id, 0));
DROP INDEX performers_name_key;
CREATE UNIQUE INDEX performers_name_key
    ON performers (lower(name), coalesce(disambiguation, ''), coalesce(owner_id, 0));
