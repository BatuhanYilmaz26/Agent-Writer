-- A call identity names one actual dispatch attempt, not a whole retry family.
-- Replayed authorization is observable but cannot authorize a second dispatch.
ALTER TABLE provider_calls ADD COLUMN provider_scope text NOT NULL DEFAULT 'legacy';
ALTER TABLE provider_calls ADD COLUMN reserved_tokens integer NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0);
ALTER TABLE provider_calls ADD COLUMN actual_tokens integer CHECK (actual_tokens >= 0);
ALTER TABLE provider_calls ADD COLUMN fencing_generation bigint NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX provider_calls_dispatch_identity ON provider_calls(run_id,call_id);
CREATE INDEX provider_calls_quota_window ON provider_calls(provider_scope,created_at);
CREATE INDEX provider_calls_user_window ON provider_calls(created_at,run_id);
CREATE INDEX runs_accounting_owner ON runs(owner_id,id);
CREATE INDEX reservations_accounting_window ON budget_reservations(created_at,run_id);

CREATE TABLE provider_circuits (
    provider_scope text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    blocked_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
