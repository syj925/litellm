CREATE TABLE IF NOT EXISTS retail_model_budget_recurrences (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    timezone TEXT NOT NULL CHECK (timezone IN ('UTC','Asia/Shanghai')),
    starts_at TIMESTAMPTZ NOT NULL,
    stops_at TIMESTAMPTZ CHECK (stops_at>=starts_at),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>0),
    reconcile_after TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_by TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS retail_model_budget_recurrences_scope
ON retail_model_budget_recurrences(user_id,model,starts_at,stops_at);
CREATE INDEX IF NOT EXISTS retail_model_budget_recurrences_due
ON retail_model_budget_recurrences(reconcile_after,id) WHERE stops_at IS NULL;

CREATE TABLE IF NOT EXISTS retail_model_budget_recurrence_terms (
    recurrence_id UUID NOT NULL REFERENCES retail_model_budget_recurrences(id),
    effective_from TIMESTAMPTZ NOT NULL,
    limit_nanos BIGINT NOT NULL CHECK (limit_nanos>0 AND limit_nanos<=9000000000000000000),
    PRIMARY KEY(recurrence_id,effective_from)
);
CREATE TABLE IF NOT EXISTS retail_model_budget_occurrences (
    recurrence_id UUID NOT NULL REFERENCES retail_model_budget_recurrences(id),
    starts_at TIMESTAMPTZ NOT NULL,
    ends_at TIMESTAMPTZ NOT NULL CHECK (ends_at>starts_at),
    policy_id UUID NOT NULL UNIQUE REFERENCES retail_model_budget_policies(id) DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(recurrence_id,starts_at)
);

CREATE OR REPLACE FUNCTION retail_monthly_recurrence_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Monthly budget plans cannot be deleted' USING ERRCODE='55000';
    END IF;
    PERFORM user_id FROM retail_wallets WHERE user_id=NEW.user_id FOR UPDATE;
    IF NEW.starts_at IS DISTINCT FROM (date_trunc('month',NEW.starts_at AT TIME ZONE NEW.timezone) AT TIME ZONE NEW.timezone)
      OR (NEW.stops_at IS NOT NULL AND NEW.stops_at IS DISTINCT FROM
        (date_trunc('month',NEW.stops_at AT TIME ZONE NEW.timezone) AT TIME ZONE NEW.timezone)) THEN
        RAISE EXCEPTION 'Monthly budget boundaries require complete calendar months' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND ((NEW.id,NEW.user_id,NEW.model,NEW.timezone,NEW.starts_at,NEW.created_at,NEW.created_by)
      IS DISTINCT FROM (OLD.id,OLD.user_id,OLD.model,OLD.timezone,OLD.starts_at,OLD.created_at,OLD.created_by)
      OR (OLD.stops_at IS NOT NULL AND NEW.stops_at IS DISTINCT FROM OLD.stops_at)) THEN
        RAISE EXCEPTION 'Monthly budget identity and explicit stop are immutable' USING ERRCODE='55000';
    END IF;
    IF EXISTS (SELECT 1 FROM retail_model_budget_recurrences r WHERE r.id<>NEW.id AND r.user_id=NEW.user_id
        AND r.model=NEW.model AND tstzrange(r.starts_at,r.stops_at,'[)') && tstzrange(NEW.starts_at,NEW.stops_at,'[)')) THEN
        RAISE EXCEPTION 'Monthly budget plans cannot overlap' USING ERRCODE='23P01';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_recurrence_guard ON retail_model_budget_recurrences;
CREATE TRIGGER retail_monthly_recurrence_guard BEFORE INSERT OR UPDATE OR DELETE ON retail_model_budget_recurrences
FOR EACH ROW EXECUTE FUNCTION retail_monthly_recurrence_guard();

CREATE OR REPLACE FUNCTION retail_monthly_term_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    plan retail_model_budget_recurrences%ROWTYPE;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Monthly budget terms cannot be deleted' USING ERRCODE='55000';
    END IF;
    SELECT * INTO STRICT plan FROM retail_model_budget_recurrences WHERE id=NEW.recurrence_id;
    IF NEW.effective_from<plan.starts_at OR NEW.effective_from IS DISTINCT FROM
      (date_trunc('month',NEW.effective_from AT TIME ZONE plan.timezone) AT TIME ZONE plan.timezone) THEN
        RAISE EXCEPTION 'Monthly terms require a complete future month' USING ERRCODE='23514';
    END IF;
    IF TG_OP='UPDATE' AND ((NEW.recurrence_id,NEW.effective_from) IS DISTINCT FROM
      (OLD.recurrence_id,OLD.effective_from) OR OLD.effective_from<=clock_timestamp()) THEN
        RAISE EXCEPTION 'Started monthly terms are immutable' USING ERRCODE='55000';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_term_guard ON retail_model_budget_recurrence_terms;
CREATE TRIGGER retail_monthly_term_guard BEFORE INSERT OR UPDATE OR DELETE ON retail_model_budget_recurrence_terms
FOR EACH ROW EXECUTE FUNCTION retail_monthly_term_guard();

CREATE OR REPLACE FUNCTION retail_monthly_occurrence_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    zone TEXT;
BEGIN
    IF TG_OP<>'INSERT' THEN
        RAISE EXCEPTION 'Monthly occurrence identity is immutable' USING ERRCODE='55000';
    END IF;
    SELECT timezone INTO STRICT zone FROM retail_model_budget_recurrences WHERE id=NEW.recurrence_id;
    IF NEW.starts_at IS DISTINCT FROM (date_trunc('month',NEW.starts_at AT TIME ZONE zone) AT TIME ZONE zone)
      OR NEW.ends_at IS DISTINCT FROM ((NEW.starts_at AT TIME ZONE zone)+interval '1 month') AT TIME ZONE zone THEN
        RAISE EXCEPTION 'Monthly occurrences require complete calendar months' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_occurrence_guard ON retail_model_budget_occurrences;
CREATE TRIGGER retail_monthly_occurrence_guard BEFORE INSERT OR UPDATE OR DELETE ON retail_model_budget_occurrences
FOR EACH ROW EXECUTE FUNCTION retail_monthly_occurrence_guard();

CREATE OR REPLACE FUNCTION retail_model_budget_policy_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    admitted_at TIMESTAMPTZ;
    predecessor_end TIMESTAMPTZ;
    monthly RECORD;
    term_nanos BIGINT;
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'Budget windows cannot be deleted' USING ERRCODE='55000';
    END IF;
    IF TG_OP='UPDATE' THEN
        IF (NEW.user_id,NEW.model,NEW.predecessor_id,NEW.effective_from,NEW.ends_at,NEW.source,NEW.created_at,NEW.created_by)
          IS DISTINCT FROM (OLD.user_id,OLD.model,OLD.predecessor_id,OLD.effective_from,OLD.ends_at,OLD.source,OLD.created_at,OLD.created_by) THEN
            RAISE EXCEPTION 'Budget window identity is immutable' USING ERRCODE='55000';
        END IF;
        IF NEW.spent_nanos<OLD.spent_nanos THEN
            RAISE EXCEPTION 'Budget consumption cannot be reset' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END IF;
    PERFORM user_id FROM retail_wallets WHERE user_id=NEW.user_id FOR UPDATE;
    admitted_at:=clock_timestamp();
    SELECT o.starts_at AS occurrence_start,o.ends_at AS occurrence_end,r.* INTO monthly
      FROM retail_model_budget_occurrences o JOIN retail_model_budget_recurrences r ON r.id=o.recurrence_id
      WHERE o.policy_id=NEW.id;
    IF monthly.id IS NOT NULL THEN
        SELECT limit_nanos INTO term_nanos FROM retail_model_budget_recurrence_terms
          WHERE recurrence_id=monthly.id AND effective_from<=monthly.occurrence_start ORDER BY effective_from DESC LIMIT 1;
        IF NEW.user_id<>monthly.user_id OR NEW.model<>monthly.model OR NEW.predecessor_id IS NOT NULL
          OR NEW.effective_from IS DISTINCT FROM monthly.occurrence_start OR NEW.ends_at IS DISTINCT FROM monthly.occurrence_end
          OR NEW.limit_nanos IS DISTINCT FROM term_nanos OR NOT NEW.enabled OR NEW.ends_at<=admitted_at
          OR NEW.effective_from<monthly.starts_at OR (monthly.stops_at IS NOT NULL AND NEW.effective_from>=monthly.stops_at) THEN
            RAISE EXCEPTION 'Invalid monthly budget occurrence' USING ERRCODE='23514';
        END IF;
    ELSE
        IF NEW.predecessor_id IS NULL THEN
            NEW.effective_from:=admitted_at;
        ELSE
            SELECT ends_at INTO predecessor_end FROM retail_model_budget_policies p WHERE p.id=NEW.predecessor_id
              AND p.user_id=NEW.user_id AND p.model=NEW.model AND p.effective_from<=admitted_at AND p.ends_at>admitted_at
              AND NOT EXISTS(SELECT 1 FROM retail_model_budget_occurrences o WHERE o.policy_id=p.id);
            IF predecessor_end IS NULL THEN
                RAISE EXCEPTION 'A successor requires the current manual budget window' USING ERRCODE='23514';
            END IF;
            NEW.effective_from:=predecessor_end;
        END IF;
        IF EXISTS(SELECT 1 FROM retail_model_budget_recurrences r WHERE r.user_id=NEW.user_id AND r.model=NEW.model
          AND (r.stops_at IS NULL OR r.stops_at>r.starts_at)
          AND r.starts_at<NEW.ends_at AND (r.stops_at IS NULL OR r.stops_at>NEW.effective_from)) THEN
            RAISE EXCEPTION 'A monthly plan already controls this window' USING ERRCODE='23P01';
        END IF;
    END IF;
    NEW.created_at:=admitted_at;
    NEW.updated_at:=admitted_at;
    IF EXISTS(SELECT 1 FROM retail_model_budget_policies p WHERE p.user_id=NEW.user_id AND p.model=NEW.model
      AND p.effective_from<NEW.ends_at AND p.ends_at>NEW.effective_from
      AND NOT EXISTS(SELECT 1 FROM retail_model_budget_occurrences o JOIN retail_model_budget_recurrences r
        ON r.id=o.recurrence_id WHERE o.policy_id=p.id AND r.stops_at IS NOT NULL AND p.effective_from>=r.stops_at)) THEN
        RAISE EXCEPTION 'Budget windows cannot overlap' USING ERRCODE='23P01';
    END IF;
    RETURN NEW;
END;
$$;

CREATE OR REPLACE FUNCTION retail_monthly_policy_configuration_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    plan retail_model_budget_recurrences%ROWTYPE;
    term_nanos BIGINT;
BEGIN
    SELECT r.* INTO plan FROM retail_model_budget_occurrences o
      JOIN retail_model_budget_recurrences r ON r.id=o.recurrence_id WHERE o.policy_id=OLD.id;
    IF plan.id IS NULL THEN RETURN NEW; END IF;
    IF (NEW.limit_nanos,NEW.enabled,NEW.config_version,NEW.updated_at,NEW.updated_by) IS NOT DISTINCT FROM
      (OLD.limit_nanos,OLD.enabled,OLD.config_version,OLD.updated_at,OLD.updated_by) THEN RETURN NEW; END IF;
    IF OLD.effective_from<=clock_timestamp() THEN
        RAISE EXCEPTION 'Started monthly windows are frozen' USING ERRCODE='55000';
    END IF;
    SELECT limit_nanos INTO term_nanos FROM retail_model_budget_recurrence_terms
      WHERE recurrence_id=plan.id AND effective_from<=OLD.effective_from ORDER BY effective_from DESC LIMIT 1;
    IF NEW.limit_nanos IS DISTINCT FROM term_nanos OR NEW.enabled IS DISTINCT FROM
      (plan.stops_at IS NULL OR OLD.effective_from<plan.stops_at) THEN
        RAISE EXCEPTION 'Monthly windows must follow their explicit plan terms' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_policy_configuration_guard ON retail_model_budget_policies;
CREATE TRIGGER retail_monthly_policy_configuration_guard
BEFORE UPDATE OF limit_nanos,enabled,config_version,updated_at,updated_by ON retail_model_budget_policies
FOR EACH ROW EXECUTE FUNCTION retail_monthly_policy_configuration_guard();

CREATE OR REPLACE FUNCTION retail_monthly_policy_insert_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    monthly RECORD;
    term_nanos BIGINT;
BEGIN
    SELECT o.starts_at AS occurrence_start,o.ends_at AS occurrence_end,r.* INTO monthly
      FROM retail_model_budget_occurrences o JOIN retail_model_budget_recurrences r ON r.id=o.recurrence_id
      WHERE o.policy_id=NEW.id;
    IF monthly.id IS NOT NULL THEN
        SELECT limit_nanos INTO term_nanos FROM retail_model_budget_recurrence_terms
          WHERE recurrence_id=monthly.id AND effective_from<=monthly.occurrence_start ORDER BY effective_from DESC LIMIT 1;
        IF (NEW.user_id,NEW.model,NEW.effective_from,NEW.ends_at,NEW.limit_nanos,NEW.enabled)
          IS DISTINCT FROM (monthly.user_id,monthly.model,monthly.occurrence_start,monthly.occurrence_end,term_nanos,true) THEN
            RAISE EXCEPTION 'A monthly policy must retain its full occurrence identity' USING ERRCODE='23514';
        END IF;
    ELSIF EXISTS(SELECT 1 FROM retail_model_budget_recurrences r WHERE r.user_id=NEW.user_id AND r.model=NEW.model
      AND (r.stops_at IS NULL OR r.stops_at>r.starts_at) AND r.starts_at<NEW.ends_at
      AND (r.stops_at IS NULL OR r.stops_at>NEW.effective_from)) THEN
        RAISE EXCEPTION 'A monthly plan already controls this window' USING ERRCODE='23P01';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_policy_insert_guard ON retail_model_budget_policies;
CREATE TRIGGER retail_monthly_policy_insert_guard AFTER INSERT ON retail_model_budget_policies
FOR EACH ROW EXECUTE FUNCTION retail_monthly_policy_insert_guard();

CREATE OR REPLACE FUNCTION retail_monthly_admission_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    admitted_at TIMESTAMPTZ:=clock_timestamp();
BEGIN
    IF EXISTS(SELECT 1 FROM retail_model_budget_recurrences r WHERE r.user_id=NEW.user_id AND r.model=NEW.model
      AND r.starts_at<=admitted_at AND (r.stops_at IS NULL OR r.stops_at>admitted_at)
      AND NOT EXISTS(SELECT 1 FROM retail_model_budget_occurrences o JOIN retail_model_budget_policies p
        ON p.id=o.policy_id WHERE o.recurrence_id=r.id AND o.starts_at=
          (date_trunc('month',admitted_at AT TIME ZONE r.timezone) AT TIME ZONE r.timezone)
        AND p.user_id=NEW.user_id AND p.model=NEW.model AND p.enabled
        AND p.effective_from<=admitted_at AND p.ends_at>admitted_at)) THEN
        RAISE EXCEPTION 'A required monthly budget occurrence is unavailable' USING ERRCODE='23514';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_monthly_admission_guard ON retail_reservations;
CREATE TRIGGER retail_monthly_admission_guard BEFORE INSERT ON retail_reservations
FOR EACH ROW EXECUTE FUNCTION retail_monthly_admission_guard();
