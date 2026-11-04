CREATE TABLE IF NOT EXISTS retail_organization_report_jobs (
    id UUID PRIMARY KEY,
    creator_id TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    organization_name TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS retail_organization_report_jobs_owner_created
  ON retail_organization_report_jobs(creator_id,organization_id,created_at DESC,id);
CREATE INDEX IF NOT EXISTS retail_organization_report_jobs_queue
  ON retail_organization_report_jobs(created_at,id) WHERE state IN ('queued','processing');
CREATE INDEX IF NOT EXISTS retail_organization_report_jobs_expiry
  ON retail_organization_report_jobs(expires_at) WHERE state<>'expired';
CREATE TABLE IF NOT EXISTS retail_organization_report_rows (
    report_id UUID NOT NULL REFERENCES retail_organization_report_jobs(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL,
    request_id TEXT NOT NULL CHECK (octet_length(request_id)<=1024),
    user_id TEXT NOT NULL CHECK (octet_length(user_id)<=1024),
    model TEXT NOT NULL CHECK (octet_length(model)<=1024),
    captured_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('settled','released','pending','unknown','conflict')),
    settled_nanos BIGINT CHECK (settled_nanos>=0),
    assignment_version BIGINT NOT NULL CHECK (assignment_version>0),
    organization_version BIGINT NOT NULL CHECK (organization_version>0),
    membership_version BIGINT NOT NULL CHECK (membership_version>0),
    inconsistent BOOLEAN NOT NULL CONSTRAINT retail_org_report_snapshot_consistent CHECK (NOT inconsistent),
    PRIMARY KEY(report_id,sequence),
    UNIQUE(report_id,request_id),
    CHECK ((state='settled')=(settled_nanos IS NOT NULL))
);
