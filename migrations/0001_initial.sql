CREATE TABLE users (
    id text PRIMARY KEY CHECK (length(id) BETWEEN 1 AND 255),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE sessions (
    token_hash text PRIMARY KEY,
    owner_id text NOT NULL REFERENCES users(id),
    csrf_hash text NOT NULL,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX sessions_expiry ON sessions(expires_at);

CREATE TABLE runs (
    id uuid PRIMARY KEY,
    owner_id text NOT NULL REFERENCES users(id),
    request jsonb NOT NULL,
    request_hash text NOT NULL,
    state text NOT NULL DEFAULT 'queued' CHECK (state IN (
        'queued','running','retry_wait','cancel_requested',
        'succeeded','needs_review','failed','cancelled')),
    graph_version text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    prompt_version text NOT NULL DEFAULT '1',
    accepted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    deadline_at timestamptz NOT NULL,
    deleted_at timestamptz,
    current_revision uuid,
    error_code text,
    error_message text,
    next_event_sequence bigint NOT NULL DEFAULT 0 CHECK (next_event_sequence >= 0),
    model_call_count integer NOT NULL DEFAULT 0 CHECK (model_call_count >= 0)
);
CREATE INDEX runs_history ON runs(owner_id,accepted_at DESC,id DESC) WHERE deleted_at IS NULL;
CREATE INDEX runs_active ON runs(state) WHERE state IN ('queued','running','retry_wait','cancel_requested');

CREATE TABLE idempotency_keys (
    owner_id text NOT NULL REFERENCES users(id),
    key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
    request_hash text NOT NULL,
    run_id uuid NOT NULL REFERENCES runs(id),
    expires_at timestamptz NOT NULL,
    PRIMARY KEY(owner_id,key)
);
CREATE INDEX idempotency_expiry ON idempotency_keys(expires_at);

CREATE TABLE jobs (
    run_id uuid PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    due_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    attempt integer NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 10),
    lease_owner text,
    lease_expires_at timestamptz,
    generation bigint NOT NULL DEFAULT 0 CHECK (generation >= 0)
);
CREATE INDEX jobs_due ON jobs(due_at);
CREATE INDEX jobs_lease_expiry ON jobs(lease_expires_at) WHERE lease_owner IS NOT NULL;

CREATE TABLE worker_heartbeats (
    id text PRIMARY KEY,
    graph_version text NOT NULL,
    draining boolean NOT NULL DEFAULT false,
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX worker_freshness ON worker_heartbeats(last_seen);

CREATE TABLE run_events (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence bigint NOT NULL CHECK (sequence > 0),
    event_key text NOT NULL,
    schema_version integer NOT NULL DEFAULT 1,
    event_type text NOT NULL,
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,sequence),
    UNIQUE(run_id,event_key)
);

CREATE TABLE sources (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    source_data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,source_id)
);
CREATE TABLE evidence_excerpts (
    run_id uuid NOT NULL,
    id text NOT NULL,
    source_id text NOT NULL,
    excerpt_data jsonb NOT NULL,
    PRIMARY KEY(run_id,id),
    FOREIGN KEY(run_id,source_id) REFERENCES sources(run_id,source_id) ON DELETE CASCADE
);

CREATE TABLE plans (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    revision integer NOT NULL DEFAULT 0 CHECK (revision >= 0),
    plan jsonb NOT NULL,
    PRIMARY KEY(run_id,revision)
);
CREATE TABLE section_drafts (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    task_id text NOT NULL,
    revision integer NOT NULL CHECK (revision >= 0),
    content jsonb NOT NULL,
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,task_id,revision)
);
CREATE TABLE article_revisions (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    parent_revision uuid REFERENCES article_revisions(id),
    markdown text NOT NULL CHECK (length(markdown) BETWEEN 1 AND 200000),
    content_hash text NOT NULL,
    review_status text NOT NULL CHECK (review_status IN ('approved','needs_review','unassessed','failed')),
    issues jsonb NOT NULL DEFAULT '[]'::jsonb,
    citation_map jsonb NOT NULL DEFAULT '{}'::jsonb,
    author_type text NOT NULL CHECK (author_type IN ('model','human')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX article_history ON article_revisions(run_id,created_at,id);
ALTER TABLE runs ADD CONSTRAINT current_revision_fk FOREIGN KEY(current_revision) REFERENCES article_revisions(id);

CREATE TABLE claim_citations (
    revision_id uuid NOT NULL REFERENCES article_revisions(id) ON DELETE CASCADE,
    claim_id text NOT NULL,
    assessment jsonb NOT NULL,
    PRIMARY KEY(revision_id,claim_id)
);
CREATE TABLE provider_calls (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    call_id text NOT NULL,
    attempt integer NOT NULL CHECK (attempt > 0),
    status text NOT NULL CHECK (status IN ('reserved','succeeded','failed','unknown')),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,call_id,attempt)
);
CREATE TABLE budget_reservations (
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    call_id text NOT NULL,
    amount_usd numeric(18,8) NOT NULL CHECK (amount_usd >= 0),
    actual_usd numeric(18,8) CHECK (actual_usd >= 0),
    status text NOT NULL CHECK (status IN ('reserved','reconciled','unknown')),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(run_id,call_id)
);

CREATE FUNCTION reject_immutable_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable records cannot be updated';
END;
$$;
CREATE TRIGGER article_immutable BEFORE UPDATE ON article_revisions FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
CREATE TRIGGER source_immutable BEFORE UPDATE ON sources FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
CREATE TRIGGER excerpt_immutable BEFORE UPDATE ON evidence_excerpts FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
CREATE TRIGGER event_immutable BEFORE UPDATE ON run_events FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
