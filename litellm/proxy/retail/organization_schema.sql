CREATE TABLE IF NOT EXISTS retail_organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT true,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS retail_organization_memberships (
    organization_id TEXT NOT NULL REFERENCES retail_organizations(id),
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'member')),
    enabled BOOLEAN NOT NULL DEFAULT true,
    approved_by TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (organization_id, user_id)
);
CREATE INDEX IF NOT EXISTS retail_organization_memberships_user
ON retail_organization_memberships(user_id, organization_id) WHERE enabled=true;
