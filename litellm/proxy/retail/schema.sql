CREATE TABLE IF NOT EXISTS retail_pricing_plans (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    multiplier NUMERIC(18,9) NOT NULL CHECK (multiplier > 0 AND multiplier <= 1000),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO retail_pricing_plans (id, name, multiplier)
VALUES ('standard', 'Standard', 1) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS retail_wallets (
    user_id TEXT PRIMARY KEY,
    balance_nanos BIGINT NOT NULL DEFAULT 0,
    reserved_nanos BIGINT NOT NULL DEFAULT 0 CHECK (reserved_nanos >= 0),
    total_credited_nanos BIGINT NOT NULL DEFAULT 0 CHECK (total_credited_nanos >= 0),
    total_spent_nanos BIGINT NOT NULL DEFAULT 0 CHECK (total_spent_nanos >= 0),
    plan_id TEXT NOT NULL DEFAULT 'standard' REFERENCES retail_pricing_plans(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail_reservations (
    request_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    model TEXT NOT NULL,
    reserved_nanos BIGINT NOT NULL CHECK (reserved_nanos >= 0),
    settled_nanos BIGINT CHECK (settled_nanos >= 0),
    status TEXT NOT NULL DEFAULT 'reserved' CHECK (status IN ('reserved', 'settled', 'released')),
    price_snapshot JSONB NOT NULL DEFAULT '{}',
    details JSONB NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE retail_wallets ADD COLUMN IF NOT EXISTS discount_override NUMERIC(18,9)
CHECK (discount_override > 0 AND discount_override <= 1);

CREATE INDEX IF NOT EXISTS retail_reservations_user_status
ON retail_reservations(user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS retail_ledger (
    id UUID PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    kind TEXT NOT NULL CHECK (kind IN ('credit', 'charge', 'release')),
    amount_nanos BIGINT NOT NULL,
    balance_after_nanos BIGINT NOT NULL,
    operation_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL DEFAULT '{}',
    reference TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    actor_id TEXT NOT NULL DEFAULT '',
    request_id TEXT REFERENCES retail_reservations(request_id),
    model TEXT,
    details JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retail_ledger_user_created
ON retail_ledger(user_id, created_at DESC, id);

CREATE UNIQUE INDEX IF NOT EXISTS retail_ledger_credit_reference_unique
ON retail_ledger(reference) WHERE kind='credit';

CREATE TABLE IF NOT EXISTS retail_credit_idempotency (
    operation_key TEXT PRIMARY KEY,
    ledger_id UUID NOT NULL REFERENCES retail_ledger(id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail_model_prices (
    model TEXT PRIMARY KEY,
    input_cost_per_token NUMERIC(30,18) NOT NULL CHECK (input_cost_per_token >= 0),
    output_cost_per_token NUMERIC(30,18) NOT NULL CHECK (output_cost_per_token >= 0),
    cache_read_input_token_cost NUMERIC(30,18) NOT NULL CHECK (cache_read_input_token_cost >= 0),
    cache_creation_input_token_cost NUMERIC(30,18) NOT NULL CHECK (cache_creation_input_token_cost >= 0),
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail_signup_limits (
    source_hash TEXT PRIMARY KEY,
    window_started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempts INTEGER NOT NULL DEFAULT 0
);

ALTER TABLE retail_model_prices ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE retail_model_prices ADD COLUMN IF NOT EXISTS catalog_model TEXT;
ALTER TABLE retail_model_prices ADD COLUMN IF NOT EXISTS source_label TEXT NOT NULL DEFAULT 'Existing reference, review required';

CREATE TABLE IF NOT EXISTS retail_signup_identities (
    email_normalized TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES "LiteLLM_UserTable"(user_id)
);

CREATE TABLE IF NOT EXISTS retail_customer_model_discounts (
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, model)
);

CREATE TABLE IF NOT EXISTS retail_plan_model_discounts (
    plan_id TEXT NOT NULL REFERENCES retail_pricing_plans(id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, model)
);

CREATE TABLE IF NOT EXISTS retail_resource_pools (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('anthropic','bedrock','vertex','anthropic_subscribe','custom')),
    description TEXT NOT NULL DEFAULT '',
    enabled BOOLEAN NOT NULL DEFAULT false,
    visibility TEXT NOT NULL DEFAULT 'restricted' CHECK (visibility IN ('public','restricted')),
    priority INTEGER NOT NULL DEFAULT 100 CHECK (priority >= 0),
    default_discount NUMERIC(18,9) NOT NULL DEFAULT 1 CHECK (default_discount > 0 AND default_discount <= 1),
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail_pool_bindings (
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    deployment_id TEXT NOT NULL,
    model TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1 CHECK (weight > 0),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority >= 0),
    PRIMARY KEY (pool_id, deployment_id, model)
);

CREATE TABLE IF NOT EXISTS retail_pool_settings (
    singleton BOOLEAN PRIMARY KEY DEFAULT true CHECK (singleton),
    routing_enabled BOOLEAN NOT NULL DEFAULT false,
    version BIGINT NOT NULL DEFAULT 1
);
INSERT INTO retail_pool_settings(singleton) VALUES (true) ON CONFLICT(singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS retail_account_pool_access (
    user_id TEXT PRIMARY KEY REFERENCES retail_wallets(user_id),
    mode TEXT NOT NULL DEFAULT 'inherit' CHECK (mode IN ('inherit','custom')),
    grants JSONB NOT NULL DEFAULT '[]',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS retail_customer_pool_discounts (
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    PRIMARY KEY(user_id,pool_id)
);
CREATE TABLE IF NOT EXISTS retail_plan_pool_discounts (
    plan_id TEXT NOT NULL REFERENCES retail_pricing_plans(id),
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    PRIMARY KEY(plan_id,pool_id)
);
CREATE TABLE IF NOT EXISTS retail_customer_pool_model_discounts (
    user_id TEXT NOT NULL REFERENCES retail_wallets(user_id),
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    PRIMARY KEY(user_id,pool_id,model)
);
CREATE TABLE IF NOT EXISTS retail_plan_pool_model_discounts (
    plan_id TEXT NOT NULL REFERENCES retail_pricing_plans(id),
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 191),
    discount NUMERIC(18,9) NOT NULL CHECK (discount > 0 AND discount <= 1),
    PRIMARY KEY(plan_id,pool_id,model)
);

CREATE TABLE IF NOT EXISTS retail_channels (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 100),
    pool_id TEXT NOT NULL REFERENCES retail_resource_pools(id),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','provisioning','error')),
    version BIGINT NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS retail_channel_names ON retail_channels(lower(name));
ALTER TABLE retail_channels ADD COLUMN IF NOT EXISTS pending_deployment_ids JSONB NOT NULL DEFAULT '[]';
CREATE TABLE IF NOT EXISTS retail_channel_deployments (
    channel_id TEXT NOT NULL REFERENCES retail_channels(id) ON DELETE CASCADE,
    deployment_id TEXT PRIMARY KEY,
    model TEXT NOT NULL
);
