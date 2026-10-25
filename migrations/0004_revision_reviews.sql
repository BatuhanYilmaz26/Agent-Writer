-- Review jobs reuse admission, leases, cancellation and billing through their
-- own run identity, without replacing the article or its generation history.
ALTER TABLE runs ADD COLUMN kind text NOT NULL DEFAULT 'generation'
    CHECK (kind IN ('generation','revision_review'));
ALTER TABLE runs ADD COLUMN review_revision_id uuid
    REFERENCES article_revisions(id) ON DELETE SET NULL;
ALTER TABLE runs ADD CONSTRAINT generation_has_no_review_subject
    CHECK (kind='revision_review' OR review_revision_id IS NULL);
CREATE INDEX runs_revision_reviews ON runs(review_revision_id,accepted_at DESC,id DESC)
    WHERE kind='revision_review';
CREATE UNIQUE INDEX one_active_revision_review ON runs(review_revision_id)
    WHERE kind='revision_review' AND deleted_at IS NULL
    AND state IN ('queued','running','retry_wait','cancel_requested');

CREATE TABLE revision_assessments (
    review_run_id uuid PRIMARY KEY REFERENCES runs(id) ON DELETE CASCADE,
    revision_id uuid NOT NULL REFERENCES article_revisions(id) ON DELETE CASCADE,
    content_hash text NOT NULL CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    review_status text NOT NULL CHECK (review_status IN ('approved','needs_review')),
    review jsonb NOT NULL,
    claims jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX revision_assessments_history ON revision_assessments(revision_id,created_at DESC,review_run_id DESC);
CREATE TRIGGER assessment_immutable BEFORE UPDATE ON revision_assessments
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
