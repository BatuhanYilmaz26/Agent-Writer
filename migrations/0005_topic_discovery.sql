CREATE TABLE topic_discovery (
    owner_id text PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    snapshot jsonb
);
