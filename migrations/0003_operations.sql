-- Worker registry, retention indexes and safe operational metadata.
ALTER TABLE worker_heartbeats ADD COLUMN active_run_id uuid REFERENCES runs(id) ON DELETE SET NULL;
ALTER TABLE worker_heartbeats ADD COLUMN active_count integer NOT NULL DEFAULT 0 CHECK (active_count >= 0);
ALTER TABLE worker_heartbeats ADD COLUMN started_at timestamptz NOT NULL DEFAULT clock_timestamp();

ALTER TABLE jobs ADD COLUMN last_error text;

-- Effective policy and research limitations are committed state, not model
-- prose. Clients can therefore show why research happened and what failed.
ALTER TABLE runs ADD COLUMN policy jsonb;
ALTER TABLE runs ADD COLUMN research_limitations jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX runs_retention ON runs(updated_at) WHERE deleted_at IS NULL;
CREATE INDEX runs_tombstones ON runs(deleted_at) WHERE deleted_at IS NOT NULL;
CREATE INDEX run_events_retention ON run_events(created_at);
CREATE INDEX sources_retention ON sources(created_at);
CREATE INDEX evidence_retention ON evidence_excerpts(run_id);
CREATE INDEX claim_citations_revision ON claim_citations(revision_id);
CREATE INDEX sessions_owner ON sessions(owner_id, expires_at);
