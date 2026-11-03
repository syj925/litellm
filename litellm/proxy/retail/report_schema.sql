CREATE TABLE IF NOT EXISTS retail_report_jobs (
    id UUID PRIMARY KEY,
    creator_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    snapshot_at TIMESTAMPTZ NOT NULL,
    kind TEXT CHECK (kind IS NULL OR kind IN ('credit','charge','release')),
    state TEXT NOT NULL DEFAULT 'queued'
      CHECK (state IN ('queued','processing','ready','failed','revoked','expired')),
    partial BOOLEAN NOT NULL,
    row_count INTEGER NOT NULL DEFAULT 0 CHECK (row_count>=0),
    summary JSONB NOT NULL DEFAULT '{}',
    unresolved JSONB NOT NULL DEFAULT '{}',
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
    CHECK (start_at<end_at)
);
CREATE INDEX IF NOT EXISTS retail_report_jobs_owner_created
  ON retail_report_jobs(creator_id,created_at DESC,id);
CREATE INDEX IF NOT EXISTS retail_report_jobs_queue
  ON retail_report_jobs(created_at,id) WHERE state IN ('queued','processing');
CREATE INDEX IF NOT EXISTS retail_report_jobs_expiry
  ON retail_report_jobs(expires_at) WHERE state<>'expired';
CREATE TABLE IF NOT EXISTS retail_report_rows (
    report_id UUID NOT NULL REFERENCES retail_report_jobs(id) ON DELETE CASCADE,
    sequence BIGINT NOT NULL,
    ledger_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('credit','charge','release')),
    amount_nanos BIGINT NOT NULL,
    balance_after_nanos BIGINT NOT NULL,
    request_id TEXT CHECK (request_id IS NULL OR octet_length(request_id)<=1024),
    model TEXT CHECK (model IS NULL OR octet_length(model)<=1024),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(report_id,sequence),
    UNIQUE(report_id,ledger_id)
);
