CREATE TABLE IF NOT EXISTS retail_model_budget_policies (
    id UUID PRIMARY KEY,
    predecessor_id UUID REFERENCES retail_model_budget_policies(id),
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    effective_from TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ends_at TIMESTAMPTZ NOT NULL,
    limit_nanos BIGINT NOT NULL CHECK (limit_nanos > 0 AND limit_nanos <= 9000000000000000000),
    spent_nanos NUMERIC(38,0) NOT NULL DEFAULT 0 CHECK (spent_nanos >= 0),
    held_nanos NUMERIC(38,0) NOT NULL DEFAULT 0 CHECK (held_nanos >= 0),
    enabled BOOLEAN NOT NULL DEFAULT true,
    config_version BIGINT NOT NULL DEFAULT 1 CHECK (config_version > 0),
    accounting_version BIGINT NOT NULL DEFAULT 0 CHECK (accounting_version >= 0),
    source TEXT NOT NULL DEFAULT 'management_api' CHECK (source = 'management_api'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_by TEXT NOT NULL,
    accounted_at TIMESTAMPTZ,
    CHECK (ends_at > effective_from AND ends_at <= effective_from + interval '366 days')
);
ALTER TABLE retail_model_budget_policies
ADD COLUMN IF NOT EXISTS predecessor_id UUID REFERENCES retail_model_budget_policies(id);
CREATE UNIQUE INDEX IF NOT EXISTS retail_model_budget_policies_successor
ON retail_model_budget_policies(predecessor_id) WHERE predecessor_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS retail_model_budget_policies_scope
ON retail_model_budget_policies(user_id,model,ends_at,effective_from);

CREATE TABLE IF NOT EXISTS retail_model_budget_allocations (
    request_id TEXT PRIMARY KEY REFERENCES retail_reservations(request_id),
    policy_id UUID NOT NULL REFERENCES retail_model_budget_policies(id),
    reserved_nanos BIGINT NOT NULL CHECK (reserved_nanos >= 0),
    settled_nanos BIGINT CHECK (settled_nanos >= 0),
    status TEXT NOT NULL CHECK (status IN ('held','settled','released')),
    config_version BIGINT NOT NULL CHECK (config_version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS retail_model_budget_allocations_policy
ON retail_model_budget_allocations(policy_id,status);

CREATE OR REPLACE FUNCTION retail_model_budget_policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    admitted_at TIMESTAMPTZ;
    predecessor_end TIMESTAMPTZ;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'Budget windows cannot be deleted' USING ERRCODE = '55000';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.user_id,NEW.model,NEW.predecessor_id,NEW.effective_from,NEW.ends_at,NEW.source,NEW.created_at,NEW.created_by)
          IS DISTINCT FROM
          (OLD.user_id,OLD.model,OLD.predecessor_id,OLD.effective_from,OLD.ends_at,OLD.source,OLD.created_at,OLD.created_by) THEN
            RAISE EXCEPTION 'Budget window identity is immutable' USING ERRCODE = '55000';
        END IF;
        IF NEW.spent_nanos < OLD.spent_nanos THEN
            RAISE EXCEPTION 'Budget consumption cannot be reset' USING ERRCODE = '55000';
        END IF;
        RETURN NEW;
    END IF;
    PERFORM user_id FROM retail_wallets WHERE user_id=NEW.user_id FOR UPDATE;
    admitted_at := clock_timestamp();
    IF NEW.predecessor_id IS NULL THEN
        NEW.effective_from := admitted_at;
    ELSE
        SELECT ends_at INTO predecessor_end FROM retail_model_budget_policies
        WHERE id=NEW.predecessor_id AND user_id=NEW.user_id AND model=NEW.model
          AND effective_from<=admitted_at AND ends_at>admitted_at;
        IF predecessor_end IS NULL THEN
            RAISE EXCEPTION 'A successor requires the current budget window' USING ERRCODE = '23514';
        END IF;
        NEW.effective_from := predecessor_end;
    END IF;
    NEW.created_at := admitted_at;
    NEW.updated_at := admitted_at;
    IF EXISTS (
        SELECT 1 FROM retail_model_budget_policies
        WHERE user_id=NEW.user_id AND model=NEW.model
          AND effective_from<NEW.ends_at AND ends_at>NEW.effective_from
    ) THEN
        RAISE EXCEPTION 'Budget windows cannot overlap' USING ERRCODE = '23P01';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_model_budget_policy_guard ON retail_model_budget_policies;
CREATE TRIGGER retail_model_budget_policy_guard
BEFORE INSERT OR UPDATE OR DELETE ON retail_model_budget_policies
FOR EACH ROW EXECUTE FUNCTION retail_model_budget_policy_guard();
