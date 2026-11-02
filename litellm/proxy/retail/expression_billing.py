import hashlib
from collections.abc import Mapping, Sequence
from decimal import Context, Decimal, localcontext
from typing import Final

from pydantic import JsonValue

from litellm.llms.openai.data_residency import infer_openai_data_residency

from .expression_pricing import evaluate_expression, response_attribute_value, validate_expression_policy
from .expression_usage import expression_usage_facts
from .models import cost_to_nanos, nanos_to_amount
from .pricing import as_mapping


def catalog_routing_attributes(
    deployments: Sequence[Mapping[str, object]],
    keys: frozenset[str] = frozenset(("data_residency", "vertex_location")),
) -> dict[str, str]:
    choices: Final = tuple(_deployment_attributes(item, keys) for item in deployments)
    return {
        key: values[0] for key in ("data_residency", "vertex_location")
        if choices and len(values := tuple(dict.fromkeys(item.get(key) for item in choices))) == 1
        and values[0] is not None
    }


def _deployment_attributes(deployment: Mapping[str, object], keys: frozenset[str]) -> dict[str, str]:
    params: Final = as_mapping(deployment.get("litellm_params"))
    info: Final = as_mapping(deployment.get("model_info"))
    model: Final = str(params.get("model", ""))
    provider: Final = params.get("custom_llm_provider") or model.partition("/")[0]
    base: Final = params.get("api_base")
    inferred: Final = infer_openai_data_residency(
        str(provider), base if isinstance(base, str) else None,
    ) or "global"
    region: Final = info["pricing_data_residency"] if info.get("pricing_data_residency") is not None else inferred
    location: Final = params.get("vertex_location")
    return {
        **({"data_residency": response_attribute_value("data_residency", region)}
           if provider == "openai" and "data_residency" in keys else {}),
        **({"vertex_location": response_attribute_value("vertex_location", location)}
           if location is not None and str(provider).startswith("vertex_ai") and "vertex_location" in keys else {}),
    }


def expression_token_limits(card: Mapping[str, object]) -> dict[str, int]:
    from litellm.litellm_core_utils.get_model_cost_map import GetModelCostMap

    policy: Final = validate_expression_policy(card.get("expression_policy"))
    name: Final = str(card.get("catalog_model") or card.get("model") or "")
    metadata: Final = GetModelCostMap.loaded_model_cost_map().get(name, {})
    defaults: Final = {
        "input_tokens": metadata.get("max_input_tokens") or metadata.get("max_tokens") or 1_000_000,
        "output_tokens": metadata.get("max_output_tokens") or 1_000_000,
    }
    selected: Final = {key: Decimal(str(policy.usage_bounds.get(key, value))) for key, value in defaults.items()}
    if any(
        not value.is_finite() or value != int(value) or not 1 <= value <= 1_000_000_000 for value in selected.values()
    ):
        raise ValueError("Expression token bounds must be positive whole numbers no greater than 1000000000")
    return {key: int(value) for key, value in selected.items()}


def expression_settlement(snapshot: Mapping[str, object], response: object) -> tuple[str, dict[str, JsonValue]]:
    policy: Final = validate_expression_policy(snapshot.get("expression_policy"))
    context: Final = as_mapping(snapshot.get("expression_context"))
    facts: Final = expression_usage_facts(
        response,
        policy,
        as_mapping(snapshot.get("expression_usage_evidence")),
        allow_input_only=snapshot.get("billing_mode") == "embedding",
    )
    reported: Final = as_mapping(facts.get("reported_tokens"))
    limits: Final = as_mapping(snapshot.get("expression_limits"))
    full_input: Final = sum(
        int(reported.get(key, 0)) for key in ("input_tokens", "cache_read_tokens", "cache_write_tokens")
    )
    if ("input_tokens" in limits and full_input > int(limits["input_tokens"])) or (
        "output_tokens" in limits and int(reported.get("output_tokens", 0)) > int(limits["output_tokens"])
    ):
        raise ValueError("Reported token usage exceeds the frozen expression token bound")
    evaluated_facts: Final = {
        **facts,
        "attributes": {
            **as_mapping(facts.get("attributes")),
            **as_mapping(snapshot.get("expression_routing_attributes")),
        },
    }
    result: Final = evaluate_expression(policy, evaluated_facts, context)
    discount: Final = Decimal(str(snapshot.get("discount", snapshot.get("multiplier"))))
    with localcontext(Context(prec=80)):
        unrounded: Final = Decimal(str(result["amount"])) * discount
        amount: Final = nanos_to_amount(cost_to_nanos(unrounded))
        adjustment: Final = format(Decimal(amount) - unrounded, "f")
    trace: Final[dict[str, JsonValue]] = {
        "version": "expression-v1",
        "expression": policy.expression,
        "expression_hash": hashlib.sha256(policy.expression.encode("utf-8")).hexdigest(),
        "timezone": policy.timezone,
        "started_at": str(context["started_at"]),
        "tiers": result["tiers"],
        "variables": {
            **as_mapping(result["variables"]),
            **{f"cache_write_{key}": value for key, value in as_mapping(result.get("cache_write_ttl_tokens")).items()},
        },
        "usage": result.get("usage", {}),
        "undiscounted_amount": result["amount"],
        "discount": str(discount),
        "amount": amount,
        "rounding_adjustment": adjustment,
    }
    return amount, {
        "usage": facts.get("reported_tokens", {}),
        "expression_pricing": trace,
        "cost_breakdown": {"expression": format(unrounded, "f"), "rounding_adjustment": adjustment},
        "billing_basis": "reported_expression_usage",
    }
