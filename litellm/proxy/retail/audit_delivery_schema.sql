CREATE TABLE IF NOT EXISTS retail_audit_delivery_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    enabled BOOLEAN NOT NULL DEFAULT false,
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>0),
    config_fingerprint TEXT CHECK (config_fingerprint IS NULL OR config_fingerprint ~ '^[0-9a-f]{64}$'),
    activated_at TIMESTAMPTZ,
    retained_count INTEGER NOT NULL DEFAULT 0 CHECK (retained_count BETWEEN 0 AND 10000),
    overflow_count BIGINT NOT NULL DEFAULT 0 CHECK (overflow_count>=0),
    last_overflow_at TIMESTAMPTZ,
    CHECK (NOT enabled OR config_fingerprint IS NOT NULL)
);
INSERT INTO retail_audit_delivery_settings(singleton) VALUES(true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS retail_audit_deliveries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id TEXT NOT NULL REFERENCES retail_operation_events(id),
    config_fingerprint TEXT NOT NULL CHECK (config_fingerprint ~ '^[0-9a-f]{64}$'),
    payload JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','processing','delivered','failed','discarded')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version>0),
    lease_owner TEXT,
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    error_code TEXT CHECK (error_code IS NULL OR error_code IN
      ('timeout','transport_error','http_retryable','http_rejected','redirect_rejected','lease_expired','expired','discarded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    completed_at TIMESTAMPTZ,
    UNIQUE(event_id,config_fingerprint),
    CHECK ((state='processing') = (lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS retail_audit_deliveries_queue
ON retail_audit_deliveries(next_attempt_at,created_at,id) WHERE state IN ('pending','processing');
CREATE INDEX IF NOT EXISTS retail_audit_deliveries_terminal
ON retail_audit_deliveries(completed_at,id) WHERE state IN ('delivered','failed','discarded');

CREATE OR REPLACE FUNCTION retail_audit_delivery_identity_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id,NEW.event_id,NEW.config_fingerprint,NEW.payload,NEW.created_at)
      IS DISTINCT FROM (OLD.id,OLD.event_id,OLD.config_fingerprint,OLD.payload,OLD.created_at) THEN
        RAISE EXCEPTION 'Delivery identity and envelope are immutable' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_audit_delivery_identity_guard ON retail_audit_deliveries;
CREATE TRIGGER retail_audit_delivery_identity_guard BEFORE UPDATE ON retail_audit_deliveries
FOR EACH ROW EXECUTE FUNCTION retail_audit_delivery_identity_guard();

CREATE OR REPLACE FUNCTION retail_enqueue_audit_delivery() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    settings retail_audit_delivery_settings%ROWTYPE;
BEGIN
    IF NEW.action NOT IN ('organization.created','organization.updated','organization.member_associated',
        'organization.member_updated','report.created','report.completed','report.failed','object_metadata.updated',
        'model_budget.created','model_budget.updated','model_budget.scheduled',
        'monthly_budget.created','monthly_budget.updated','monthly_budget.stopped','billing_organization.updated',
        'organization_report.created','organization_report.completed','organization_report.failed',
        'finance_grant.updated','delegated_report.created','delegated_report.completed','delegated_report.failed',
        'native_key.rotated','native_password.changed','native_session.revoked')
      OR NEW.resource_type NOT IN
        ('organization','organization_member','report','object_metadata','model_budget','monthly_budget','billing_organization',
         'organization_report','finance_grant','delegated_report','native_operation')
      OR NEW.id !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$' THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM retail_audit_delivery_settings WHERE singleton AND enabled) THEN
        RETURN NEW;
    END IF;
    SELECT * INTO settings FROM retail_audit_delivery_settings WHERE singleton FOR UPDATE;
    IF NOT settings.enabled THEN
        RETURN NEW;
    END IF;
    IF settings.retained_count>=10000 THEN
        UPDATE retail_audit_delivery_settings SET overflow_count=overflow_count+1,last_overflow_at=clock_timestamp()
        WHERE singleton;
        RETURN NEW;
    END IF;
    INSERT INTO retail_audit_deliveries(event_id,config_fingerprint,payload)
    VALUES(NEW.id,settings.config_fingerprint,jsonb_build_object(
        'event_id',NEW.id,'action',NEW.action,'resource_type',NEW.resource_type,'result',NEW.result,
        'version_before',NEW.version_before,'version_after',NEW.version_after,'occurred_at',NEW.created_at))
    ON CONFLICT(event_id,config_fingerprint) DO NOTHING;
    IF FOUND THEN
        UPDATE retail_audit_delivery_settings SET retained_count=retained_count+1 WHERE singleton;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS retail_enqueue_audit_delivery ON retail_operation_events;
CREATE TRIGGER retail_enqueue_audit_delivery AFTER INSERT ON retail_operation_events
FOR EACH ROW EXECUTE FUNCTION retail_enqueue_audit_delivery();
