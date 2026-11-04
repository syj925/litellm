CREATE TABLE IF NOT EXISTS retail_delegated_report_jobs (
    id UUID PRIMARY KEY,
    creator_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    organization_name TEXT NOT NULL,
    grant_id UUID NOT NULL,
    grant_version BIGINT NOT NULL CHECK (grant_version>0),
    grant_revision UUID NOT NULL,
    grant_member_version BIGINT NOT NULL CHECK (grant_member_version>0),
    grant_org_generation BIGINT NOT NULL CHECK (grant_org_generation>=0),
    operation_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL DEFAULT 'queued'
      CHECK (state IN ('queued','processing','ready','failed','revoked','expired')),
    partial BOOLEAN NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count BETWEEN 0 AND 10000),
    summary JSONB NOT NULL DEFAULT '{}',
    artifact BYTEA,
    artifact_bytes INTEGER NOT NULL DEFAULT 0 CHECK (artifact_bytes>=0),
    artifact_sha256 TEXT,
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts>=0),
    lease_owner TEXT,
    lease_until TIMESTAMPTZ,
    error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    UNIQUE(creator_id,operation_key),
    CHECK (start_at<end_at AND end_at-start_at<=interval '31 days')
);
CREATE INDEX IF NOT EXISTS retail_delegated_report_jobs_owner_created
  ON retail_delegated_report_jobs(creator_id,organization_id,created_at DESC,id);
CREATE INDEX IF NOT EXISTS retail_delegated_report_jobs_queue
  ON retail_delegated_report_jobs(created_at,id) WHERE state IN ('queued','processing');
CREATE INDEX IF NOT EXISTS retail_delegated_report_jobs_expiry
  ON retail_delegated_report_jobs(expires_at) WHERE state<>'expired';
CREATE TABLE IF NOT EXISTS retail_delegated_report_rows (
    report_id UUID NOT NULL REFERENCES retail_delegated_report_jobs(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL,
    model TEXT NOT NULL CHECK (octet_length(model)<=1024),
    captured_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('settled','released','pending','unknown','conflict')),
    settled_nanos BIGINT CHECK (settled_nanos>=0),
    inconsistent BOOLEAN NOT NULL CONSTRAINT retail_delegated_report_snapshot_consistent CHECK (NOT inconsistent),
    PRIMARY KEY(report_id,sequence),
    CHECK ((state='settled')=(settled_nanos IS NOT NULL))
);
