-- Core schema: files, media entities, metadata entities, users and ACLs.
-- Surrogate keys are bigint identities (compact, index-friendly, keyset-pagination friendly).

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE FUNCTION set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END $$;

-- ---------------------------------------------------------------------------
-- Users
-- ---------------------------------------------------------------------------

CREATE TABLE users (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    username      text        NOT NULL,
    password_hash text        NOT NULL,
    is_admin      boolean     NOT NULL DEFAULT false,
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_username_key ON users (lower(username));

-- ---------------------------------------------------------------------------
-- Filesystem
-- ---------------------------------------------------------------------------

CREATE TABLE library_paths (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    path             text        NOT NULL UNIQUE,
    enabled          boolean     NOT NULL DEFAULT true,
    watch            boolean     NOT NULL DEFAULT false,
    exclude_patterns text[]      NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- One row per physical file. Files inside a zip archive reference the archive
-- row via zip_file_id and store their path relative to the archive root.
CREATE TABLE files (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    library_id    bigint      NOT NULL REFERENCES library_paths (id) ON DELETE CASCADE,
    zip_file_id   bigint      REFERENCES files (id) ON DELETE CASCADE,
    path          text        NOT NULL,
    size          bigint      NOT NULL,
    mod_time      timestamptz NOT NULL,
    oshash        text,                   -- fast content fingerprint; used to follow moved files
    missing_since timestamptz,            -- set when a scan no longer finds the file
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX files_path_key     ON files (path) WHERE zip_file_id IS NULL;
CREATE UNIQUE INDEX files_zip_path_key ON files (zip_file_id, path) WHERE zip_file_id IS NOT NULL;
CREATE INDEX files_library_idx ON files (library_id);
CREATE INDEX files_oshash_idx  ON files (oshash);
CREATE INDEX files_missing_idx ON files (missing_since) WHERE missing_since IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Metadata entities
-- ---------------------------------------------------------------------------

CREATE TABLE studios (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name          text        NOT NULL,
    parent_id     bigint      REFERENCES studios (id) ON DELETE SET NULL,
    url           text,
    details       text,
    image_path    text,                   -- relative to the generated dir
    rating        smallint    CHECK (rating BETWEEN 0 AND 100),
    custom_fields jsonb       NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_id <> id)
);
CREATE UNIQUE INDEX studios_name_key ON studios (lower(name));
CREATE INDEX studios_parent_idx   ON studios (parent_id);
CREATE INDEX studios_name_trgm    ON studios USING gin (name gin_trgm_ops);

CREATE TYPE gender AS ENUM (
    'female', 'male', 'transgender_female', 'transgender_male', 'intersex', 'non_binary'
);

CREATE TABLE performers (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name           text        NOT NULL,
    disambiguation text,
    gender         gender,
    birthdate      date,
    deathdate      date,
    country        text,
    ethnicity      text,
    eye_color      text,
    hair_color     text,
    height_cm      smallint    CHECK (height_cm > 0),
    weight_kg      smallint    CHECK (weight_kg > 0),
    measurements   text,
    tattoos        text,
    piercings      text,
    career_start   smallint,
    career_end     smallint,
    details        text,
    image_path     text,
    rating         smallint    CHECK (rating BETWEEN 0 AND 100),
    favorite       boolean     NOT NULL DEFAULT false,
    custom_fields  jsonb       NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX performers_name_key ON performers (lower(name), coalesce(disambiguation, ''));
CREATE INDEX performers_name_trgm ON performers USING gin (name gin_trgm_ops);

CREATE TABLE performer_aliases (
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    alias        text   NOT NULL,
    PRIMARY KEY (performer_id, alias)
);
CREATE INDEX performer_aliases_trgm ON performer_aliases USING gin (alias gin_trgm_ops);

CREATE TABLE tags (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text        NOT NULL,
    description text,
    image_path  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX tags_name_key ON tags (lower(name));
CREATE INDEX tags_name_trgm ON tags USING gin (name gin_trgm_ops);

-- Tag hierarchy is a DAG: a tag may have several parents ("Outdoor" under both
-- "Location" and "Setting"). Cycles are rejected by trigger below.
CREATE TABLE tag_relations (
    parent_id bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    child_id  bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (parent_id, child_id),
    CHECK (parent_id <> child_id)
);
CREATE INDEX tag_relations_child_idx ON tag_relations (child_id);

CREATE FUNCTION tag_relations_prevent_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (
        WITH RECURSIVE descendants(id) AS (
            SELECT NEW.child_id
            UNION
            SELECT tr.child_id FROM tag_relations tr JOIN descendants d ON tr.parent_id = d.id
        )
        SELECT 1 FROM descendants WHERE id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION 'tag hierarchy cycle: % -> %', NEW.parent_id, NEW.child_id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER tag_relations_prevent_cycle BEFORE INSERT OR UPDATE ON tag_relations
    FOR EACH ROW EXECUTE FUNCTION tag_relations_prevent_cycle();

CREATE FUNCTION studios_prevent_cycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NOT NULL AND EXISTS (
        WITH RECURSIVE ancestors(id) AS (
            SELECT NEW.parent_id
            UNION
            SELECT s.parent_id FROM studios s JOIN ancestors a ON s.id = a.id
            WHERE s.parent_id IS NOT NULL
        )
        SELECT 1 FROM ancestors WHERE id = NEW.id
    ) THEN
        RAISE EXCEPTION 'studio hierarchy cycle: % -> %', NEW.parent_id, NEW.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER studios_prevent_cycle BEFORE INSERT OR UPDATE OF parent_id ON studios
    FOR EACH ROW EXECUTE FUNCTION studios_prevent_cycle();

-- ---------------------------------------------------------------------------
-- Media entities
-- ---------------------------------------------------------------------------

CREATE TABLE scenes (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_id        bigint      NOT NULL UNIQUE REFERENCES files (id) ON DELETE CASCADE,
    title          text,
    details        text,
    release_date   date,
    urls           text[]      NOT NULL DEFAULT '{}',
    studio_id      bigint      REFERENCES studios (id) ON DELETE SET NULL,
    rating         smallint    CHECK (rating BETWEEN 0 AND 100),
    organized      boolean     NOT NULL DEFAULT false,
    -- probe results (ffprobe)
    duration       real,                  -- seconds
    width          integer,
    height         integer,
    framerate      real,
    bitrate        bigint,
    video_codec    text,
    audio_codec    text,
    container      text,
    -- playback state
    play_count     integer     NOT NULL DEFAULT 0,
    resume_time    real,
    last_played_at timestamptz,
    custom_fields  jsonb       NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
-- (sort_key, id) pairs back keyset pagination for the common sort orders.
CREATE INDEX scenes_created_idx  ON scenes (created_at DESC, id DESC);
CREATE INDEX scenes_release_idx  ON scenes (release_date DESC NULLS LAST, id DESC);
CREATE INDEX scenes_duration_idx ON scenes (duration, id);
CREATE INDEX scenes_height_idx   ON scenes (height, id);
CREATE INDEX scenes_studio_idx   ON scenes (studio_id);
CREATE INDEX scenes_title_trgm   ON scenes USING gin (title gin_trgm_ops);

CREATE TABLE images (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    file_id       bigint      NOT NULL UNIQUE REFERENCES files (id) ON DELETE CASCADE,
    title         text,
    studio_id     bigint      REFERENCES studios (id) ON DELETE SET NULL,
    rating        smallint    CHECK (rating BETWEEN 0 AND 100),
    organized     boolean     NOT NULL DEFAULT false,
    width         integer,
    height        integer,
    format        text,
    custom_fields jsonb       NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX images_created_idx ON images (created_at DESC, id DESC);
CREATE INDEX images_studio_idx  ON images (studio_id);

-- A gallery is backed by a folder, a zip archive, or nothing (user-curated).
CREATE TABLE galleries (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    title          text,
    details        text,
    release_date   date,
    studio_id      bigint      REFERENCES studios (id) ON DELETE SET NULL,
    rating         smallint    CHECK (rating BETWEEN 0 AND 100),
    organized      boolean     NOT NULL DEFAULT false,
    folder_path    text        UNIQUE,
    zip_file_id    bigint      UNIQUE REFERENCES files (id) ON DELETE CASCADE,
    cover_image_id bigint      REFERENCES images (id) ON DELETE SET NULL,
    image_count    integer     NOT NULL DEFAULT 0,  -- maintained by trigger on gallery_images
    custom_fields  jsonb       NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    CHECK (folder_path IS NULL OR zip_file_id IS NULL)
);
CREATE INDEX galleries_created_idx ON galleries (created_at DESC, id DESC);
CREATE INDEX galleries_release_idx ON galleries (release_date DESC NULLS LAST, id DESC);
CREATE INDEX galleries_studio_idx  ON galleries (studio_id);
CREATE INDEX galleries_title_trgm  ON galleries USING gin (title gin_trgm_ops);

CREATE TABLE gallery_images (
    gallery_id bigint  NOT NULL REFERENCES galleries (id) ON DELETE CASCADE,
    image_id   bigint  NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    position   integer NOT NULL DEFAULT 0,
    PRIMARY KEY (gallery_id, image_id)
);
CREATE INDEX gallery_images_order_idx ON gallery_images (gallery_id, position, image_id);
CREATE INDEX gallery_images_image_idx ON gallery_images (image_id);

CREATE FUNCTION gallery_images_count() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE galleries SET image_count = image_count + 1 WHERE id = NEW.gallery_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE galleries SET image_count = image_count - 1 WHERE id = OLD.gallery_id;
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER gallery_images_count AFTER INSERT OR DELETE ON gallery_images
    FOR EACH ROW EXECUTE FUNCTION gallery_images_count();

-- ---------------------------------------------------------------------------
-- Associations. Each has the natural PK plus a reverse index so lookups are
-- indexed from either side (e.g. "scenes with tag X" and "tags of scene Y").
-- ---------------------------------------------------------------------------

CREATE TABLE scene_performers (
    scene_id     bigint NOT NULL REFERENCES scenes (id) ON DELETE CASCADE,
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    PRIMARY KEY (scene_id, performer_id)
);
CREATE INDEX scene_performers_rev ON scene_performers (performer_id, scene_id);

CREATE TABLE scene_tags (
    scene_id bigint NOT NULL REFERENCES scenes (id) ON DELETE CASCADE,
    tag_id   bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (scene_id, tag_id)
);
CREATE INDEX scene_tags_rev ON scene_tags (tag_id, scene_id);

CREATE TABLE gallery_performers (
    gallery_id   bigint NOT NULL REFERENCES galleries (id) ON DELETE CASCADE,
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    PRIMARY KEY (gallery_id, performer_id)
);
CREATE INDEX gallery_performers_rev ON gallery_performers (performer_id, gallery_id);

CREATE TABLE gallery_tags (
    gallery_id bigint NOT NULL REFERENCES galleries (id) ON DELETE CASCADE,
    tag_id     bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (gallery_id, tag_id)
);
CREATE INDEX gallery_tags_rev ON gallery_tags (tag_id, gallery_id);

CREATE TABLE image_performers (
    image_id     bigint NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, performer_id)
);
CREATE INDEX image_performers_rev ON image_performers (performer_id, image_id);

CREATE TABLE image_tags (
    image_id bigint NOT NULL REFERENCES images (id) ON DELETE CASCADE,
    tag_id   bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, tag_id)
);
CREATE INDEX image_tags_rev ON image_tags (tag_id, image_id);

CREATE TABLE performer_tags (
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    tag_id       bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (performer_id, tag_id)
);
CREATE INDEX performer_tags_rev ON performer_tags (tag_id, performer_id);

-- ---------------------------------------------------------------------------
-- Access control. Rows here are *deny* rules for non-admin users:
--   hidden tag       -> the tag, its descendants, and anything tagged with them
--   hidden studio    -> the studio, its child studios, and their content
--   hidden performer -> the performer and any content featuring them
-- ---------------------------------------------------------------------------

CREATE TABLE user_hidden_tags (
    user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    tag_id  bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, tag_id)
);

CREATE TABLE user_hidden_studios (
    user_id   bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    studio_id bigint NOT NULL REFERENCES studios (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, studio_id)
);

CREATE TABLE user_hidden_performers (
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, performer_id)
);

-- ---------------------------------------------------------------------------
-- updated_at triggers
-- ---------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'library_paths', 'files', 'studios', 'performers',
                             'tags', 'scenes', 'images', 'galleries']
    LOOP
        EXECUTE format('CREATE TRIGGER %I_updated_at BEFORE UPDATE ON %I
                        FOR EACH ROW EXECUTE FUNCTION set_updated_at()', t, t);
    END LOOP;
END $$;
