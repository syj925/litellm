DO $scim_schema$
BEGIN
    PERFORM pg_advisory_xact_lock(799405131);
    CREATE TABLE IF NOT EXISTS retail_scim_settings (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        namespace text NOT NULL,
        generation bigint NOT NULL CHECK(generation>0),
        digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{64}$'),
        max_users bigint NOT NULL CHECK(max_users BETWEEN 1 AND 10000),
        retained_count bigint NOT NULL DEFAULT 0 CHECK(retained_count BETWEEN 0 AND 10000),
        credential_count bigint NOT NULL DEFAULT 0 CHECK(credential_count BETWEEN 0 AND 1000000)
    );
    CREATE TABLE IF NOT EXISTS retail_scim_users (
        id uuid PRIMARY KEY,
        user_id text NOT NULL UNIQUE CHECK(user_id LIKE 'retail-scim-user:%'),
        namespace text NOT NULL,
        external_id text NOT NULL,
        user_name text NOT NULL,
        user_name_normalized text NOT NULL,
        display_name text,
        active boolean NOT NULL,
        deleted boolean NOT NULL DEFAULT false,
        version bigint NOT NULL DEFAULT 1 CHECK(version>0),
        credential_revision uuid NOT NULL,
        credential_count bigint NOT NULL DEFAULT 0 CHECK(credential_count BETWEEN 0 AND 10000),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK(user_name_normalized=lower(user_name)),
        CHECK(NOT deleted OR NOT active)
    );
    CREATE UNIQUE INDEX IF NOT EXISTS retail_scim_external_live
        ON retail_scim_users(namespace,external_id) WHERE NOT deleted;
    CREATE UNIQUE INDEX IF NOT EXISTS retail_scim_username_live
        ON retail_scim_users(namespace,user_name_normalized) WHERE NOT deleted;
    CREATE INDEX IF NOT EXISTS retail_scim_users_page ON retail_scim_users(namespace,created_at,id) WHERE NOT deleted;
    CREATE TABLE IF NOT EXISTS retail_scim_credentials (
        token_hash text PRIMARY KEY CHECK(token_hash ~ '^[a-f0-9]{64}$'),
        user_id text NOT NULL,
        credential_revision uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX IF NOT EXISTS retail_scim_credentials_owner ON retail_scim_credentials(user_id);
    CREATE OR REPLACE FUNCTION retail_scim_protect_credential() RETURNS trigger LANGUAGE plpgsql AS $proof$
    BEGIN
        IF TG_OP<>'INSERT' OR current_setting('retail.scim_issue',true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION 'Managed credential proofs are server-owned and immutable';
        END IF;
        RETURN NEW;
    END $proof$;
    DROP TRIGGER IF EXISTS retail_scim_credential_guard ON retail_scim_credentials;
    CREATE TRIGGER retail_scim_credential_guard BEFORE INSERT OR UPDATE OR DELETE ON retail_scim_credentials
        FOR EACH ROW EXECUTE FUNCTION retail_scim_protect_credential();
    CREATE OR REPLACE FUNCTION retail_scim_protect_user() RETURNS trigger LANGUAGE plpgsql AS $user$
    BEGIN
        IF TG_OP='INSERT' THEN
            IF NEW.user_id LIKE 'retail-scim-user:%' AND (
                current_setting('retail.scim_provision',true) IS DISTINCT FROM 'on'
                OR NEW.user_role IS DISTINCT FROM 'customer' OR NEW.password IS NOT NULL
                OR NEW.user_email IS NOT NULL OR NEW.sso_user_id IS NOT NULL
                OR NEW.team_id IS NOT NULL OR NEW.organization_id IS NOT NULL OR NEW.object_permission_id IS NOT NULL
                OR cardinality(NEW.teams)>0 OR cardinality(NEW.models)>0) THEN
                RAISE EXCEPTION 'Managed users require native directory provisioning';
            END IF;
            RETURN NEW;
        END IF;
        IF TG_OP='DELETE' THEN
            IF OLD.user_id LIKE 'retail-scim-user:%' THEN
                RAISE EXCEPTION 'Managed users retain native account history';
            END IF;
            RETURN OLD;
        END IF;
        IF NEW.user_id IS DISTINCT FROM OLD.user_id AND
            (OLD.user_id LIKE 'retail-scim-user:%' OR NEW.user_id LIKE 'retail-scim-user:%') THEN
            RAISE EXCEPTION 'Managed user identity cannot be reassigned';
        END IF;
        IF OLD.user_id LIKE 'retail-scim-user:%' AND NEW.user_role IS DISTINCT FROM 'customer' THEN
            RAISE EXCEPTION 'Managed native users remain customers';
        END IF;
        RETURN NEW;
    END $user$;
    DROP TRIGGER IF EXISTS retail_scim_native_user_guard ON "LiteLLM_UserTable";
    CREATE TRIGGER retail_scim_native_user_guard BEFORE INSERT OR UPDATE OR DELETE ON "LiteLLM_UserTable"
        FOR EACH ROW EXECUTE FUNCTION retail_scim_protect_user();
    CREATE OR REPLACE FUNCTION retail_scim_issue_credential() RETURNS trigger LANGUAGE plpgsql AS $key$
    DECLARE
        resource retail_scim_users%ROWTYPE;
        proof retail_scim_credentials%ROWTYPE;
        settings retail_scim_settings%ROWTYPE;
        issue_setting text;
    BEGIN
        IF TG_OP='UPDATE' AND NEW.user_id IS DISTINCT FROM OLD.user_id AND
            (OLD.user_id LIKE 'retail-scim-user:%' OR NEW.user_id LIKE 'retail-scim-user:%') THEN
            RAISE EXCEPTION 'Managed credential ownership cannot be reassigned';
        END IF;
        IF NEW.user_id IS NULL OR NEW.user_id NOT LIKE 'retail-scim-user:%' THEN
            IF EXISTS(SELECT 1 FROM retail_scim_credentials WHERE token_hash=NEW.token) THEN
                RAISE EXCEPTION 'A protected credential hash cannot be reused';
            END IF;
            RETURN NEW;
        END IF;
        SELECT * INTO resource FROM retail_scim_users WHERE user_id=NEW.user_id FOR UPDATE;
        IF NOT FOUND OR NOT resource.active OR resource.deleted OR NOT EXISTS(
            SELECT 1 FROM "LiteLLM_UserTable" WHERE user_id=NEW.user_id AND user_role='customer'
            AND password_reset_required IS DISTINCT FROM true) THEN
            RAISE EXCEPTION 'Managed credential issuance is unavailable';
        END IF;
        IF TG_OP='UPDATE' THEN
            SELECT * INTO proof FROM retail_scim_credentials WHERE token_hash=OLD.token;
            IF NOT FOUND OR proof.user_id<>NEW.user_id OR proof.credential_revision<>resource.credential_revision
                OR OLD.blocked IS TRUE OR (OLD.expires IS NOT NULL AND
                (OLD.expires AT TIME ZONE 'UTC')<=clock_timestamp()) THEN
                RAISE EXCEPTION 'A revoked managed credential cannot be rotated';
            END IF;
            IF NEW.token=OLD.token THEN RETURN NEW; END IF;
        END IF;
        IF EXISTS(SELECT 1 FROM retail_scim_credentials WHERE token_hash=NEW.token) THEN
            RAISE EXCEPTION 'A protected credential hash cannot be reused';
        END IF;
        SELECT * INTO settings FROM retail_scim_settings WHERE singleton FOR UPDATE;
        IF NOT FOUND OR settings.credential_count>=1000000 OR resource.credential_count>=10000 THEN
            RAISE EXCEPTION 'Managed credential history capacity reached';
        END IF;
        issue_setting:=coalesce(current_setting('retail.scim_issue',true),'');
        PERFORM set_config('retail.scim_issue','on',true);
        INSERT INTO retail_scim_credentials(token_hash,user_id,credential_revision)
            VALUES(NEW.token,NEW.user_id,resource.credential_revision);
        PERFORM set_config('retail.scim_issue',issue_setting,true);
        UPDATE retail_scim_users SET credential_count=credential_count+1 WHERE id=resource.id;
        UPDATE retail_scim_settings SET credential_count=credential_count+1 WHERE singleton;
        RETURN NEW;
    END $key$;
    DROP TRIGGER IF EXISTS retail_scim_native_key_guard ON "LiteLLM_VerificationToken";
    CREATE TRIGGER retail_scim_native_key_guard BEFORE INSERT OR UPDATE OF token,user_id ON "LiteLLM_VerificationToken"
        FOR EACH ROW EXECUTE FUNCTION retail_scim_issue_credential();
END
$scim_schema$;
