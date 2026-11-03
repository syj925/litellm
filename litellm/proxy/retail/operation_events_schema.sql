CREATE TABLE IF NOT EXISTS retail_operation_events (
    id TEXT PRIMARY KEY,
    organization_id TEXT,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result = 'succeeded'),
    version_before BIGINT,
    version_after BIGINT,
    changes JSONB NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    response JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (actor_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS retail_operation_events_org_time
ON retail_operation_events(organization_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS retail_operation_events_actor_time
ON retail_operation_events(actor_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS retail_operation_events_time
ON retail_operation_events(created_at DESC, id DESC);

CREATE OR REPLACE FUNCTION retail_operation_events_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Operation events are append-only' USING ERRCODE = '55000';
END;
$$;
DROP TRIGGER IF EXISTS retail_operation_events_immutable ON retail_operation_events;
CREATE TRIGGER retail_operation_events_immutable
BEFORE UPDATE OR DELETE ON retail_operation_events
FOR EACH ROW EXECUTE FUNCTION retail_operation_events_immutable();
