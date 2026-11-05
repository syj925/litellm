DO $native_audit_schema$
BEGIN
    PERFORM pg_advisory_xact_lock(hashtextextended('retail:native-audit:schema',0));
    CREATE TABLE IF NOT EXISTS retail_native_audit_settings (
        singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
        source_id UUID NOT NULL UNIQUE,
        retained_count INTEGER NOT NULL DEFAULT 0 CHECK (retained_count BETWEEN 0 AND 10000)
    );
    CREATE TABLE IF NOT EXISTS retail_native_audit_outbox (
        id UUID PRIMARY KEY,
        source_id UUID NOT NULL,
        actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 191),
        action TEXT NOT NULL CHECK (action IN ('native_key.rotated','native_password.changed','native_session.revoked',
          'native_identity.configured','native_identity.signed_in','native_api_identity.configured')),
        resource_id UUID NOT NULL,
        affected_count BIGINT NOT NULL CHECK (affected_count>=0),
        occurred_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','processing','failed')),
        attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
        next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        lease_owner TEXT,
        lease_token UUID,
        lease_until TIMESTAMPTZ,
        expires_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()+interval '7 days',
        error_code TEXT CHECK (error_code IS NULL OR error_code IN
          ('bridge_unavailable','integrity_conflict','attempts_exhausted','retention_expired')),
        CHECK ((state='processing')=(lease_owner IS NOT NULL AND lease_token IS NOT NULL AND lease_until IS NOT NULL))
    );
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
        WHERE conrelid='retail_native_audit_outbox'::regclass
          AND conname='retail_native_audit_outbox_action_v3_check') THEN
        ALTER TABLE retail_native_audit_outbox DROP CONSTRAINT IF EXISTS retail_native_audit_outbox_action_check;
        ALTER TABLE retail_native_audit_outbox DROP CONSTRAINT IF EXISTS retail_native_audit_outbox_action_v2_check;
        ALTER TABLE retail_native_audit_outbox ADD CONSTRAINT retail_native_audit_outbox_action_v3_check
          CHECK (action IN ('native_key.rotated','native_password.changed','native_session.revoked',
            'native_identity.configured','native_identity.signed_in','native_api_identity.configured'));
    END IF;
    CREATE INDEX IF NOT EXISTS retail_native_audit_outbox_queue
      ON retail_native_audit_outbox(next_attempt_at,occurred_at,id) WHERE state IN ('pending','processing');
    CREATE INDEX IF NOT EXISTS retail_native_audit_outbox_lease
      ON retail_native_audit_outbox(lease_until) WHERE state='processing';
    CREATE OR REPLACE FUNCTION retail_native_audit_identity_guard() RETURNS trigger
    LANGUAGE plpgsql AS $guard$
    BEGIN
        IF (NEW.id,NEW.source_id,NEW.actor_id,NEW.action,NEW.resource_id,NEW.affected_count,NEW.occurred_at)
           IS DISTINCT FROM
           (OLD.id,OLD.source_id,OLD.actor_id,OLD.action,OLD.resource_id,OLD.affected_count,OLD.occurred_at) THEN
            RAISE EXCEPTION 'Native audit identity and metadata are immutable' USING ERRCODE='55000';
        END IF;
        RETURN NEW;
    END;
    $guard$;
    DROP TRIGGER IF EXISTS retail_native_audit_identity_guard ON retail_native_audit_outbox;
    CREATE TRIGGER retail_native_audit_identity_guard BEFORE UPDATE ON retail_native_audit_outbox
      FOR EACH ROW EXECUTE FUNCTION retail_native_audit_identity_guard();
END;
$native_audit_schema$;
