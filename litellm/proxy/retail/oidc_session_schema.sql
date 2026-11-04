DO $oidc_schema$
BEGIN
    CREATE TABLE IF NOT EXISTS retail_oidc_settings (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        namespace text NOT NULL,
        generation bigint NOT NULL CHECK(generation > 0),
        digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{64}$'),
        issuer text NOT NULL,
        session_seconds integer NOT NULL CHECK(session_seconds BETWEEN 300 AND 3600),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS retail_oidc_bindings (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        namespace text NOT NULL,
        issuer text NOT NULL,
        subject text NOT NULL,
        user_id text NOT NULL,
        enabled boolean NOT NULL,
        revision uuid NOT NULL DEFAULT gen_random_uuid(),
        UNIQUE(namespace,issuer,subject)
    );
    CREATE TABLE IF NOT EXISTS retail_oidc_sessions (
        token_hash text PRIMARY KEY REFERENCES "LiteLLM_VerificationToken"(token) ON DELETE CASCADE,
        id uuid NOT NULL UNIQUE,
        namespace text NOT NULL,
        generation bigint NOT NULL,
        digest text NOT NULL,
        binding_id uuid NOT NULL REFERENCES retail_oidc_bindings(id),
        binding_revision uuid NOT NULL,
        user_id text NOT NULL REFERENCES "LiteLLM_UserTable"(user_id) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX IF NOT EXISTS retail_oidc_sessions_expiry ON retail_oidc_sessions(expires_at,token_hash);
    CREATE INDEX IF NOT EXISTS retail_oidc_sessions_user ON retail_oidc_sessions(user_id);
    CREATE OR REPLACE FUNCTION retail_oidc_protect_binding() RETURNS trigger LANGUAGE plpgsql AS $binding$
    BEGIN
        IF ROW(NEW.id,NEW.namespace,NEW.issuer,NEW.subject,NEW.user_id)
            IS DISTINCT FROM ROW(OLD.id,OLD.namespace,OLD.issuer,OLD.subject,OLD.user_id) THEN
            RAISE EXCEPTION 'OIDC binding identity cannot be reassigned';
        END IF;
        IF NEW.enabled IS DISTINCT FROM OLD.enabled AND NEW.revision=OLD.revision THEN
            NEW.revision := gen_random_uuid();
        END IF;
        RETURN NEW;
    END $binding$;
    DROP TRIGGER IF EXISTS retail_oidc_binding_guard ON retail_oidc_bindings;
    CREATE TRIGGER retail_oidc_binding_guard BEFORE UPDATE ON retail_oidc_bindings
        FOR EACH ROW EXECUTE FUNCTION retail_oidc_protect_binding();
    CREATE OR REPLACE FUNCTION retail_oidc_protect_proof() RETURNS trigger LANGUAGE plpgsql AS $proof$
    BEGIN
        RAISE EXCEPTION 'OIDC session proof cannot be changed';
    END $proof$;
    DROP TRIGGER IF EXISTS retail_oidc_session_guard ON retail_oidc_sessions;
    CREATE TRIGGER retail_oidc_session_guard BEFORE UPDATE ON retail_oidc_sessions
        FOR EACH ROW EXECUTE FUNCTION retail_oidc_protect_proof();
    CREATE OR REPLACE FUNCTION retail_oidc_protect_key() RETURNS trigger LANGUAGE plpgsql AS $key$
    BEGIN
        IF TG_OP='INSERT' THEN
            IF NEW.metadata->>'login_method'='retail_oidc'
                AND current_setting('retail.oidc_mint',true) IS DISTINCT FROM 'on' THEN
                RAISE EXCEPTION 'OIDC sessions require a verified native transaction';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.metadata->>'login_method'='retail_oidc' THEN
            IF ROW(NEW.token,NEW.user_id,NEW.team_id,NEW.metadata,NEW.allowed_routes,NEW.models,
                   NEW.permissions,NEW.config,NEW.aliases,NEW.organization_id,NEW.key_type)
                IS DISTINCT FROM ROW(OLD.token,OLD.user_id,OLD.team_id,OLD.metadata,OLD.allowed_routes,OLD.models,
                   OLD.permissions,OLD.config,OLD.aliases,OLD.organization_id,OLD.key_type) THEN
                RAISE EXCEPTION 'OIDC session identity and route scope cannot be changed';
            END IF;
            IF NEW.blocked IS TRUE OR NEW.expires IS DISTINCT FROM OLD.expires THEN
                DELETE FROM retail_oidc_sessions WHERE token_hash=OLD.token;
            END IF;
        ELSIF NEW.metadata->>'login_method'='retail_oidc' THEN
            RAISE EXCEPTION 'Ordinary keys cannot become OIDC sessions';
        END IF;
        RETURN NEW;
    END $key$;
    DROP TRIGGER IF EXISTS retail_oidc_key_guard ON "LiteLLM_VerificationToken";
    CREATE TRIGGER retail_oidc_key_guard BEFORE INSERT OR UPDATE OF
        token,user_id,team_id,metadata,allowed_routes,models,permissions,config,aliases,organization_id,key_type,
        blocked,expires ON "LiteLLM_VerificationToken"
        FOR EACH ROW EXECUTE FUNCTION retail_oidc_protect_key();
    CREATE OR REPLACE FUNCTION retail_oidc_invalidate_user() RETURNS trigger LANGUAGE plpgsql AS $user$
    BEGIN
        IF NEW.password IS DISTINCT FROM OLD.password OR NEW.password_reset_required IS TRUE
            OR NEW.user_role IS DISTINCT FROM OLD.user_role
            OR NEW.user_role IS NULL OR NEW.user_role NOT IN
                ('proxy_admin','proxy_admin_viewer','internal_user','internal_user_viewer','customer') THEN
            DELETE FROM retail_oidc_sessions WHERE user_id=OLD.user_id;
        END IF;
        RETURN NEW;
    END $user$;
    DROP TRIGGER IF EXISTS retail_oidc_user_guard ON "LiteLLM_UserTable";
    CREATE TRIGGER retail_oidc_user_guard BEFORE UPDATE OF password,password_reset_required,user_role
        ON "LiteLLM_UserTable"
        FOR EACH ROW EXECUTE FUNCTION retail_oidc_invalidate_user();
END
$oidc_schema$;
