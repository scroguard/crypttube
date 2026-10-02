-- Personal filters gain the opposite mode: "only show me content with these".
-- show_only = false: hide this tag/performer; show_only = true: it is one of the things to show.
ALTER TABLE user_filter_tags       ADD COLUMN show_only boolean NOT NULL DEFAULT false;
ALTER TABLE user_filter_performers ADD COLUMN show_only boolean NOT NULL DEFAULT false;
