-- Personal content filters: tags and performers a user has chosen not to see. They work
-- like the admin's deny rules (user_hidden_*), but belong to the user, apply to every
-- role, and can be switched off without losing the selection.
CREATE TABLE user_filter_tags (
    user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    tag_id  bigint NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, tag_id)
);
CREATE INDEX user_filter_tags_tag_idx ON user_filter_tags (tag_id);

CREATE TABLE user_filter_performers (
    user_id      bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    performer_id bigint NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, performer_id)
);
CREATE INDEX user_filter_performers_performer_idx ON user_filter_performers (performer_id);

ALTER TABLE user_settings ADD COLUMN filters_enabled boolean NOT NULL DEFAULT true;
