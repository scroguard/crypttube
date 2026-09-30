-- Browsing: default titles, hierarchy helpers, and indexes that match the keyset
-- sort expressions used by the list API exactly (so every page is an index range scan).
--
-- Nullable sort columns are coalesced to a low sentinel (never ±infinity: those don't
-- round-trip through the JSON cursor), so NULLs sort as the lowest value.

UPDATE scenes s SET title = regexp_replace(regexp_replace(f.path, '^.*/', ''), '\.[^.]*$', '')
FROM files f WHERE f.id = s.file_id AND s.title IS NULL;

UPDATE images i SET title = regexp_replace(regexp_replace(f.path, '^.*/', ''), '\.[^.]*$', '')
FROM files f WHERE f.id = i.file_id AND i.title IS NULL;

-- ---------------------------------------------------------------------------
-- Hierarchy helpers (inclusive of the given ids)
-- ---------------------------------------------------------------------------

CREATE FUNCTION tag_descendants(ids bigint[]) RETURNS SETOF bigint
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH RECURSIVE d(id) AS (
        SELECT unnest(ids)
        UNION
        SELECT tr.child_id FROM tag_relations tr JOIN d ON tr.parent_id = d.id
    )
    SELECT id FROM d
$$;

CREATE FUNCTION studio_descendants(ids bigint[]) RETURNS SETOF bigint
LANGUAGE sql STABLE PARALLEL SAFE AS $$
    WITH RECURSIVE d(id) AS (
        SELECT unnest(ids)
        UNION
        SELECT s.id FROM studios s JOIN d ON s.parent_id = d.id
    )
    SELECT id FROM d
$$;

-- ---------------------------------------------------------------------------
-- Sort indexes
-- ---------------------------------------------------------------------------

DROP INDEX scenes_release_idx;
DROP INDEX scenes_duration_idx;
DROP INDEX scenes_height_idx;
CREATE INDEX scenes_sort_updated  ON scenes (updated_at, id);
CREATE INDEX scenes_sort_release  ON scenes (coalesce(release_date, '0001-01-01'::date), id);
CREATE INDEX scenes_sort_title    ON scenes ((coalesce(title, '') COLLATE natural_sort), id);
CREATE INDEX scenes_sort_duration ON scenes (coalesce(duration, -1), id);
CREATE INDEX scenes_sort_height   ON scenes (coalesce(height, 0), id);
CREATE INDEX scenes_sort_rating   ON scenes (coalesce(rating, -1), id);
CREATE INDEX scenes_sort_plays    ON scenes (play_count, id);
CREATE INDEX scenes_sort_played   ON scenes (coalesce(last_played_at, '0001-01-01 00:00+00'::timestamptz), id);

DROP INDEX galleries_release_idx;
CREATE INDEX galleries_sort_release ON galleries (coalesce(release_date, '0001-01-01'::date), id);
CREATE INDEX galleries_sort_title   ON galleries ((coalesce(title, '') COLLATE natural_sort), id);
CREATE INDEX galleries_sort_rating  ON galleries (coalesce(rating, -1), id);
CREATE INDEX galleries_sort_count   ON galleries (image_count, id);

CREATE INDEX images_sort_title  ON images ((coalesce(title, '') COLLATE natural_sort), id);
CREATE INDEX images_sort_rating ON images (coalesce(rating, -1), id);

CREATE INDEX performers_sort_name    ON performers ((name COLLATE natural_sort), id);
CREATE INDEX performers_sort_created ON performers (created_at, id);
CREATE INDEX performers_sort_rating  ON performers (coalesce(rating, -1), id);
CREATE INDEX performers_sort_birth   ON performers (coalesce(birthdate, '0001-01-01'::date), id);

CREATE INDEX studios_sort_name    ON studios ((name COLLATE natural_sort), id);
CREATE INDEX studios_sort_created ON studios (created_at, id);
CREATE INDEX tags_sort_name       ON tags ((name COLLATE natural_sort), id);
CREATE INDEX tags_sort_created    ON tags (created_at, id);
