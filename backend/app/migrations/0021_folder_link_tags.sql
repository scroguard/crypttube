-- Which folder link put which tag on what, so unlinking a folder can take back its own tags
-- and no others. A tag with no row here was set by hand, or before this table existed.

ALTER TABLE folder_links ADD COLUMN removing_at timestamptz;

CREATE TABLE folder_link_tags (
    link_id    bigint NOT NULL REFERENCES folder_links (id) ON DELETE CASCADE,
    kind       text   NOT NULL CHECK (kind IN ('scene', 'image', 'gallery')),
    content_id bigint NOT NULL,
    file_id    bigint NOT NULL REFERENCES files (id) ON DELETE CASCADE,
    PRIMARY KEY (link_id, kind, content_id)
);
CREATE INDEX folder_link_tags_content_idx ON folder_link_tags (kind, content_id);
CREATE INDEX folder_link_tags_file_idx ON folder_link_tags (file_id);

-- When a tag goes, however it goes, its record goes with it: if someone adds it back,
-- that's their doing and no link may take it away.
CREATE FUNCTION folder_link_performer_removed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM folder_link_tags t USING folder_links w
    WHERE t.link_id = w.id AND w.performer_id = OLD.performer_id
      AND t.kind = TG_ARGV[0] AND t.content_id = (to_jsonb(OLD) ->> TG_ARGV[1])::bigint;
    RETURN NULL;
END $$;
CREATE TRIGGER folder_link_tags_gone AFTER DELETE ON scene_performers
    FOR EACH ROW EXECUTE FUNCTION folder_link_performer_removed('scene', 'scene_id');
CREATE TRIGGER folder_link_tags_gone AFTER DELETE ON image_performers
    FOR EACH ROW EXECUTE FUNCTION folder_link_performer_removed('image', 'image_id');
CREATE TRIGGER folder_link_tags_gone AFTER DELETE ON gallery_performers
    FOR EACH ROW EXECUTE FUNCTION folder_link_performer_removed('gallery', 'gallery_id');

CREATE FUNCTION folder_link_content_deleted() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM folder_link_tags WHERE kind = TG_ARGV[0] AND content_id = OLD.id;
    RETURN NULL;
END $$;
CREATE TRIGGER folder_link_tags_content AFTER DELETE ON scenes
    FOR EACH ROW EXECUTE FUNCTION folder_link_content_deleted('scene');
CREATE TRIGGER folder_link_tags_content AFTER DELETE ON images
    FOR EACH ROW EXECUTE FUNCTION folder_link_content_deleted('image');
CREATE TRIGGER folder_link_tags_content AFTER DELETE ON galleries
    FOR EACH ROW EXECUTE FUNCTION folder_link_content_deleted('gallery');

CREATE FUNCTION folder_link_studio_changed() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    DELETE FROM folder_link_tags t USING folder_links w
    WHERE t.link_id = w.id AND w.studio_id = OLD.studio_id
      AND t.kind = TG_ARGV[0] AND t.content_id = OLD.id;
    RETURN NULL;
END $$;
CREATE TRIGGER folder_link_tags_studio AFTER UPDATE OF studio_id ON scenes FOR EACH ROW
    WHEN (OLD.studio_id IS NOT NULL AND OLD.studio_id IS DISTINCT FROM NEW.studio_id)
    EXECUTE FUNCTION folder_link_studio_changed('scene');
CREATE TRIGGER folder_link_tags_studio AFTER UPDATE OF studio_id ON images FOR EACH ROW
    WHEN (OLD.studio_id IS NOT NULL AND OLD.studio_id IS DISTINCT FROM NEW.studio_id)
    EXECUTE FUNCTION folder_link_studio_changed('image');
CREATE TRIGGER folder_link_tags_studio AFTER UPDATE OF studio_id ON galleries FOR EACH ROW
    WHEN (OLD.studio_id IS NOT NULL AND OLD.studio_id IS DISTINCT FROM NEW.studio_id)
    EXECUTE FUNCTION folder_link_studio_changed('gallery');
