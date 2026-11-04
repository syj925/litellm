CREATE TABLE IF NOT EXISTS retail_object_bindings (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('key','team','organization','model')),
    native_id TEXT NOT NULL,
    object_created_at TIMESTAMPTZ NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    bound_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(kind,native_id,object_created_at,scope_fingerprint)
);
CREATE TABLE IF NOT EXISTS retail_object_metadata (
    reference_id UUID PRIMARY KEY REFERENCES retail_object_bindings(id),
    tags TEXT[] NOT NULL CHECK (cardinality(tags) <= 10),
    version BIGINT NOT NULL CHECK (version > 0),
    source TEXT NOT NULL CHECK (source = 'management_api'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    created_by TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    updated_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS retail_object_metadata_receipts (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response JSONB NOT NULL,
    PRIMARY KEY(actor_id,idempotency_key)
);
