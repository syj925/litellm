DO $request_policy$
BEGIN
    PERFORM pg_advisory_xact_lock(799405151);
    CREATE TABLE IF NOT EXISTS retail_request_policy_settings (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        prompt_count bigint NOT NULL DEFAULT 0 CHECK(prompt_count BETWEEN 0 AND 1000),
        policy_count bigint NOT NULL DEFAULT 0 CHECK(policy_count BETWEEN 0 AND 1000),
        target_count bigint NOT NULL DEFAULT 0 CHECK(target_count BETWEEN 0 AND 10000),
        binding_count bigint NOT NULL DEFAULT 0 CHECK(binding_count BETWEEN 0 AND 10000),
        receipt_count bigint NOT NULL DEFAULT 0 CHECK(receipt_count BETWEEN 0 AND 10000)
    );
    CREATE TABLE IF NOT EXISTS retail_request_policy_prompts (
        id uuid PRIMARY KEY,name text NOT NULL,content text NOT NULL,
        enabled boolean NOT NULL,deleted boolean NOT NULL DEFAULT false,
        version bigint NOT NULL DEFAULT 1 CHECK(version>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        CHECK(octet_length(content)<=8000)
    );
    CREATE TABLE IF NOT EXISTS retail_request_policy_policies (
        id uuid PRIMARY KEY,name text NOT NULL,definition jsonb NOT NULL,
        prompt_id uuid REFERENCES retail_request_policy_prompts(id),enabled boolean NOT NULL,
        deleted boolean NOT NULL DEFAULT false,version bigint NOT NULL DEFAULT 1 CHECK(version>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS retail_request_policy_targets (
        id uuid PRIMARY KEY,kind text NOT NULL CHECK(kind IN('user','key','model')),
        native_id text NOT NULL,anchor text NOT NULL CHECK(anchor ~ '^[a-f0-9]{64}$'),
        native_created_at timestamptz,owner_id text,owner_created_at timestamptz,label text NOT NULL,
        UNIQUE(kind,native_id,anchor)
    );
    CREATE INDEX IF NOT EXISTS retail_request_policy_target_identity ON retail_request_policy_targets(kind,native_id);
    CREATE TABLE IF NOT EXISTS retail_request_policy_bindings (
        id uuid PRIMARY KEY,target_id uuid NOT NULL REFERENCES retail_request_policy_targets(id),
        policy_id uuid NOT NULL REFERENCES retail_request_policy_policies(id),enabled boolean NOT NULL,
        deleted boolean NOT NULL DEFAULT false,version bigint NOT NULL DEFAULT 1 CHECK(version>0),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp(),updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE UNIQUE INDEX IF NOT EXISTS retail_request_policy_binding_current
        ON retail_request_policy_bindings(target_id) WHERE NOT deleted;
    CREATE TABLE IF NOT EXISTS retail_request_policy_receipts (
        actor_key text NOT NULL,operation_key text NOT NULL,digest text NOT NULL,
        response jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY(actor_key,operation_key)
    );
    CREATE OR REPLACE FUNCTION retail_request_policy_key_identity() RETURNS trigger LANGUAGE plpgsql AS $key$
    BEGIN
        IF TG_OP='INSERT' THEN
            IF EXISTS(SELECT 1 FROM retail_request_policy_targets t JOIN retail_request_policy_bindings b ON b.target_id=t.id
                WHERE t.kind='key' AND t.native_id=NEW.token AND b.enabled AND NOT b.deleted) THEN
                RAISE EXCEPTION 'A bound request-policy credential cannot be recreated';
            END IF;
            RETURN NEW;
        END IF;
        IF EXISTS(SELECT 1 FROM retail_request_policy_targets t JOIN retail_request_policy_bindings b ON b.target_id=t.id
            WHERE t.kind='key' AND t.native_id=OLD.token AND b.enabled AND NOT b.deleted) THEN
            IF NEW.user_id IS DISTINCT FROM OLD.user_id OR NEW.created_at IS DISTINCT FROM OLD.created_at
                OR EXISTS(SELECT 1 FROM retail_request_policy_targets t JOIN retail_request_policy_bindings b ON b.target_id=t.id
                    LEFT JOIN "LiteLLM_UserTable" u ON u.user_id=OLD.user_id
                    WHERE t.kind='key' AND t.native_id=OLD.token AND b.enabled AND NOT b.deleted
                    AND (t.native_created_at IS DISTINCT FROM (OLD.created_at AT TIME ZONE 'UTC')
                        OR t.owner_id IS DISTINCT FROM OLD.user_id
                        OR t.owner_created_at IS DISTINCT FROM (u.created_at AT TIME ZONE 'UTC'))) THEN
                RAISE EXCEPTION 'A bound request-policy identity cannot change';
            END IF;
        END IF;
        IF NEW.token IS DISTINCT FROM OLD.token THEN
            IF EXISTS(SELECT 1 FROM retail_request_policy_targets t JOIN retail_request_policy_bindings b ON b.target_id=t.id
                WHERE t.kind='key' AND t.native_id=NEW.token AND b.enabled AND NOT b.deleted) THEN
                RAISE EXCEPTION 'A bound request-policy credential cannot be reused';
            END IF;
            UPDATE retail_request_policy_targets SET native_id=NEW.token
                WHERE kind='key' AND native_id=OLD.token AND native_created_at=(OLD.created_at AT TIME ZONE 'UTC')
                AND owner_id IS NOT DISTINCT FROM OLD.user_id;
        END IF;
        RETURN NEW;
    END $key$;
    DROP TRIGGER IF EXISTS retail_request_policy_key_guard ON "LiteLLM_VerificationToken";
    CREATE TRIGGER retail_request_policy_key_guard BEFORE INSERT OR UPDATE OF token,user_id,created_at ON "LiteLLM_VerificationToken"
        FOR EACH ROW EXECUTE FUNCTION retail_request_policy_key_identity();
END
$request_policy$;
