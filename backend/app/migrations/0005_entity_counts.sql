-- Cached content counts per tag/performer/studio, per viewer.
--
-- Counting live costs one scene visit per tagged scene per row shown (tens of
-- thousands per page on a big library), so counts are recomputed in one pass by the
-- worker's `refresh_counts` job after scans and edits. viewer = 0 holds unrestricted
-- counts; each user with deny rules gets their own rows, so counts never leak the
-- existence of hidden content.
CREATE TABLE entity_counts (
    viewer    bigint   NOT NULL,
    kind      text     NOT NULL CHECK (kind IN ('tag', 'performer', 'studio')),
    entity_id bigint   NOT NULL,
    scenes    integer  NOT NULL DEFAULT 0,
    galleries integer  NOT NULL DEFAULT 0,
    images    integer  NOT NULL DEFAULT 0,
    PRIMARY KEY (viewer, kind, entity_id)
);

-- Per-element statistics for the id arrays, so the planner can tell a tag on 0.5% of
-- scenes from one on 40% (the default target only tracks the most common elements).
ALTER TABLE scenes    ALTER COLUMN tag_ids SET STATISTICS 1000, ALTER COLUMN performer_ids SET STATISTICS 1000;
ALTER TABLE galleries ALTER COLUMN tag_ids SET STATISTICS 1000, ALTER COLUMN performer_ids SET STATISTICS 1000;
ALTER TABLE images    ALTER COLUMN tag_ids SET STATISTICS 1000, ALTER COLUMN performer_ids SET STATISTICS 1000;
