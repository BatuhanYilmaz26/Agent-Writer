CREATE TABLE publication_packages (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    revision_id uuid NOT NULL REFERENCES article_revisions(id) ON DELETE CASCADE,
    content_hash text NOT NULL CHECK(content_hash ~ '^[a-f0-9]{64}$'),
    input_hash text NOT NULL CHECK(input_hash ~ '^[a-f0-9]{64}$'),
    data jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(revision_id,input_hash)
);
CREATE TRIGGER publication_package_immutable BEFORE UPDATE ON publication_packages
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
CREATE INDEX publication_packages_history ON publication_packages(run_id,created_at DESC,id DESC);
CREATE TABLE publications (
    id uuid PRIMARY KEY,
    package_id uuid NOT NULL REFERENCES publication_packages(id) ON DELETE CASCADE,
    platform text NOT NULL CHECK(platform IN ('medium','linkedin','x')),
    url text NOT NULL CHECK(length(url)<=2048),
    published_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(package_id,platform,url)
);
CREATE TRIGGER publication_record_immutable BEFORE UPDATE ON publications
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_update();
