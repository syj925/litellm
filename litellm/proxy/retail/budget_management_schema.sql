CREATE TABLE IF NOT EXISTS retail_model_budget_management_receipts (
    actor_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    response JSONB NOT NULL,
    PRIMARY KEY(actor_id,idempotency_key)
);
