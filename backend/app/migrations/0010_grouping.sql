-- "Unassigned" (no performer) sections of the grouped views: find those rows without
-- scanning the table.
CREATE INDEX scenes_no_performers ON scenes (id) WHERE cardinality(performer_ids) = 0;
CREATE INDEX images_no_performers ON images (id) WHERE cardinality(performer_ids) = 0;
