CREATE TABLE IF NOT EXISTS retail_finance_organization_generations (
    organization_id TEXT PRIMARY KEY REFERENCES retail_organizations(id),
    generation BIGINT NOT NULL DEFAULT 0 CHECK (generation>=0)
);
CREATE OR REPLACE FUNCTION retail_finance_organization_generation_changed() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.enabled IS DISTINCT FROM OLD.enabled THEN
        INSERT INTO retail_finance_organization_generations(organization_id,generation) VALUES(NEW.id,1)
        ON CONFLICT(organization_id) DO UPDATE
          SET generation=retail_finance_organization_generations.generation+1;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_finance_organization_generation_changed ON retail_organizations;
CREATE TRIGGER retail_finance_organization_generation_changed
AFTER UPDATE OF enabled ON retail_organizations FOR EACH ROW
EXECUTE FUNCTION retail_finance_organization_generation_changed();
CREATE TABLE IF NOT EXISTS retail_organization_finance_grants (
    organization_id TEXT NOT NULL REFERENCES retail_organizations(id),
    user_id TEXT NOT NULL,
    grant_id UUID NOT NULL UNIQUE,
    version BIGINT NOT NULL CHECK (version>0),
    revision UUID NOT NULL,
    can_view BOOLEAN NOT NULL,
    can_export BOOLEAN NOT NULL,
    granted_member_version BIGINT NOT NULL CHECK (granted_member_version>=0),
    granted_org_generation BIGINT NOT NULL CHECK (granted_org_generation>=0),
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(organization_id,user_id),
    CHECK (NOT can_export OR can_view)
);
CREATE INDEX IF NOT EXISTS retail_organization_finance_grants_user
ON retail_organization_finance_grants(user_id,organization_id) WHERE can_view;
