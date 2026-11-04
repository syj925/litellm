CREATE TABLE IF NOT EXISTS retail_billing_organization_assignments (
    user_id TEXT PRIMARY KEY REFERENCES retail_wallets(user_id),
    organization_id TEXT REFERENCES retail_organizations(id),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS retail_request_organization_snapshots (
    request_id TEXT PRIMARY KEY REFERENCES retail_reservations(request_id),
    organization_id TEXT NOT NULL,
    assignment_version BIGINT NOT NULL CHECK (assignment_version > 0),
    organization_version BIGINT NOT NULL CHECK (organization_version > 0),
    membership_version BIGINT NOT NULL CHECK (membership_version > 0),
    captured_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS retail_request_organization_snapshots_cohort
ON retail_request_organization_snapshots(organization_id, captured_at, request_id);

CREATE OR REPLACE FUNCTION retail_request_organization_snapshot_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Request organization snapshots are immutable' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS retail_request_organization_snapshot_immutable ON retail_request_organization_snapshots;
CREATE TRIGGER retail_request_organization_snapshot_immutable
BEFORE UPDATE OR DELETE ON retail_request_organization_snapshots
FOR EACH ROW EXECUTE FUNCTION retail_request_organization_snapshot_immutable();
