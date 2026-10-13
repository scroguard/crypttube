-- Live Photos: a still image and a short video with the same name in the same folder.
-- The scanner pairs them; the video then plays inside the image viewer and is left out
-- of scene lists and counts.
ALTER TABLE images ADD COLUMN live_scene_id bigint REFERENCES scenes (id) ON DELETE SET NULL;
CREATE INDEX images_live_scene_idx ON images (live_scene_id) WHERE live_scene_id IS NOT NULL;
ALTER TABLE scenes ADD COLUMN is_live_photo boolean NOT NULL DEFAULT false;
