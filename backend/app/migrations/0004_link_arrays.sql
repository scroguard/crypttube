-- Denormalized tag/performer id arrays on scenes, galleries and images.
--
-- ACL checks and tag/performer filters become per-row array operators
-- (`tag_ids && $hidden`) instead of anti/semi-joins against link tables. That lets the
-- planner walk the sort index and stop after one page, rather than hashing every
-- hidden link row up front (measured: 50-150 ms -> single-digit ms at 200k scenes).
--
-- The link tables stay the source of truth; statement-level triggers recompute the
-- arrays of exactly the parents a statement touched, so bulk edits cost one UPDATE.

ALTER TABLE scenes    ADD COLUMN tag_ids bigint[] NOT NULL DEFAULT '{}',
                      ADD COLUMN performer_ids bigint[] NOT NULL DEFAULT '{}';
ALTER TABLE galleries ADD COLUMN tag_ids bigint[] NOT NULL DEFAULT '{}',
                      ADD COLUMN performer_ids bigint[] NOT NULL DEFAULT '{}';
ALTER TABLE images    ADD COLUMN tag_ids bigint[] NOT NULL DEFAULT '{}',
                      ADD COLUMN performer_ids bigint[] NOT NULL DEFAULT '{}';

CREATE FUNCTION sync_link_array() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent text := TG_ARGV[0];   -- e.g. scenes
    fk     text := TG_ARGV[1];   -- e.g. scene_id
    col    text := TG_ARGV[2];   -- e.g. tag_id
    arr    text := TG_ARGV[3];   -- e.g. tag_ids
BEGIN
    EXECUTE format(
        'UPDATE %1$I p SET %4$I = coalesce(
             (SELECT array_agg(l.%3$I ORDER BY l.%3$I) FROM %5$I l WHERE l.%2$I = p.id), ''{}'')
         WHERE p.id IN (SELECT %2$I FROM changed)',
        parent, fk, col, arr, TG_TABLE_NAME);
    RETURN NULL;
END $$;

CREATE FUNCTION forbid_link_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows cannot be updated; delete and insert instead', TG_TABLE_NAME
        USING ERRCODE = 'feature_not_supported';
END $$;

DO $$
DECLARE
    spec text[];
BEGIN
    FOREACH spec SLICE 1 IN ARRAY ARRAY[
        ['scene_tags',         'scenes',    'scene_id',   'tag_id',       'tag_ids'],
        ['scene_performers',   'scenes',    'scene_id',   'performer_id', 'performer_ids'],
        ['gallery_tags',       'galleries', 'gallery_id', 'tag_id',       'tag_ids'],
        ['gallery_performers', 'galleries', 'gallery_id', 'performer_id', 'performer_ids'],
        ['image_tags',         'images',    'image_id',   'tag_id',       'tag_ids'],
        ['image_performers',   'images',    'image_id',   'performer_id', 'performer_ids']
    ]
    LOOP
        EXECUTE format(
            'CREATE TRIGGER %1$s_sync_ins AFTER INSERT ON %1$I REFERENCING NEW TABLE AS changed
             FOR EACH STATEMENT EXECUTE FUNCTION sync_link_array(%2$L, %3$L, %4$L, %5$L)',
            spec[1], spec[2], spec[3], spec[4], spec[5]);
        EXECUTE format(
            'CREATE TRIGGER %1$s_sync_del AFTER DELETE ON %1$I REFERENCING OLD TABLE AS changed
             FOR EACH STATEMENT EXECUTE FUNCTION sync_link_array(%2$L, %3$L, %4$L, %5$L)',
            spec[1], spec[2], spec[3], spec[4], spec[5]);
        -- Link rows are pure keys; changing one in place would bypass the triggers above.
        EXECUTE format(
            'CREATE TRIGGER %1$s_no_update BEFORE UPDATE ON %1$I
             FOR EACH ROW EXECUTE FUNCTION forbid_link_update()', spec[1]);
        -- Backfill.
        EXECUTE format(
            'UPDATE %1$I p SET %4$I = l.ids FROM (
                 SELECT %2$I AS pid, array_agg(%3$I ORDER BY %3$I) AS ids FROM %5$I GROUP BY 1) l
             WHERE l.pid = p.id',
            spec[2], spec[3], spec[4], spec[5], spec[1]);
    END LOOP;
END $$ ;

CREATE INDEX scenes_tag_ids_gin       ON scenes    USING gin (tag_ids);
CREATE INDEX scenes_performer_ids_gin ON scenes    USING gin (performer_ids);
CREATE INDEX galleries_tag_ids_gin    ON galleries USING gin (tag_ids);
CREATE INDEX galleries_perf_ids_gin   ON galleries USING gin (performer_ids);
CREATE INDEX images_tag_ids_gin       ON images    USING gin (tag_ids);
CREATE INDEX images_perf_ids_gin      ON images    USING gin (performer_ids);
