DO $api_jwt_schema$
BEGIN
    PERFORM pg_advisory_xact_lock(799405121);
    CREATE TABLE IF NOT EXISTS retail_api_jwt_settings (
        singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
        namespace text NOT NULL,
        generation bigint NOT NULL CHECK(generation>0),
        digest text NOT NULL CHECK(digest ~ '^[a-f0-9]{64}$'),
        updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS retail_api_jwt_revisions (
        revision uuid PRIMARY KEY,
        created_at timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE TABLE IF NOT EXISTS retail_api_jwt_bindings (
        namespace text NOT NULL,
        issuer text NOT NULL,
        subject_hash text NOT NULL CHECK(subject_hash ~ '^[a-f0-9]{64}$'),
        user_id text NOT NULL,
        token_hash text NOT NULL CHECK(token_hash ~ '^[a-f0-9]{64}$'),
        revision uuid NOT NULL REFERENCES retail_api_jwt_revisions(revision),
        enabled boolean NOT NULL,
        PRIMARY KEY(namespace,issuer,subject_hash)
    );
    CREATE OR REPLACE FUNCTION retail_api_jwt_protect_revision() RETURNS trigger LANGUAGE plpgsql AS $revision$
    BEGIN
        RAISE EXCEPTION 'API identity revision history is immutable';
    END $revision$;
    DROP TRIGGER IF EXISTS retail_api_jwt_revision_guard ON retail_api_jwt_revisions;
    CREATE TRIGGER retail_api_jwt_revision_guard BEFORE UPDATE OR DELETE ON retail_api_jwt_revisions
        FOR EACH ROW EXECUTE FUNCTION retail_api_jwt_protect_revision();
END
$api_jwt_schema$;
