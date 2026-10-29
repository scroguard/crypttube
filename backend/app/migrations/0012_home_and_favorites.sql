-- Favourites become personal, and each user gets a configurable home page.

CREATE TABLE user_favorite_performers (
    user_id      bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    performer_id bigint      NOT NULL REFERENCES performers (id) ON DELETE CASCADE,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, performer_id)
);
CREATE INDEX user_favorite_performers_performer_idx ON user_favorite_performers (performer_id);

-- The old flag was shared by everyone: carry it over to every existing user.
INSERT INTO user_favorite_performers (user_id, performer_id)
SELECT u.id, p.id FROM users u CROSS JOIN performers p WHERE p.favorite;
ALTER TABLE performers DROP COLUMN favorite;

-- Per-user settings. `home` is the ordered list of home page sections (NULL = default).
CREATE TABLE user_settings (
    user_id    bigint      PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    home       jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- "Continue watching": a user's most recently updated resume positions.
CREATE INDEX user_scene_state_recent_idx ON user_scene_state (user_id, updated_at DESC);
