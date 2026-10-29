-- Authentication: roles, server-side sessions, per-user playback state.

ALTER TABLE users ADD COLUMN role text NOT NULL DEFAULT 'viewer'
    CHECK (role IN ('admin', 'editor', 'viewer'));
UPDATE users SET role = 'admin' WHERE is_admin;
ALTER TABLE users DROP COLUMN is_admin;
ALTER TABLE users ADD COLUMN last_login_at timestamptz;

-- Only a SHA-256 of the session token is stored: a leaked DB dump can't be replayed.
-- No IP addresses are recorded.
CREATE TABLE sessions (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_hash   bytea       NOT NULL UNIQUE,
    user_id      bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    user_agent   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL
);
CREATE INDEX sessions_user_idx    ON sessions (user_id);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

-- Resume position is per user; play_count / last_played_at on scenes stay global.
CREATE TABLE user_scene_state (
    user_id     bigint      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    scene_id    bigint      NOT NULL REFERENCES scenes (id) ON DELETE CASCADE,
    resume_time real        NOT NULL CHECK (resume_time >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, scene_id)
);
CREATE INDEX user_scene_state_scene_idx ON user_scene_state (scene_id);
ALTER TABLE scenes DROP COLUMN resume_time;
