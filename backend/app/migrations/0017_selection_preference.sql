-- Per-user preference: keep the selection when moving to another page of a paged list
-- (default: each page starts with nothing selected).
ALTER TABLE user_settings ADD COLUMN keep_selection_across_pages boolean NOT NULL DEFAULT false;
