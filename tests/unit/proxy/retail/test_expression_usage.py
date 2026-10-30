from datetime import datetime, timezone
from decimal import Decimal
from typing import Final

import pytest

from litellm.proxy.retail.expression_pricing import (
    ExpressionPolicy,
    ExpressionPricingError,
    evaluate_expression,
    freeze_expression_context,
    validate_expression_policy,
)
from litellm.proxy.retail.expression_usage import expression_usage_facts, request_usage_evidence

_START: Final = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _policy(expression: str, **fields: object) -> ExpressionPolicy:
    return validate_expression_policy({"expression": expression, **fields})


def test_openai_modality_partitions_are_exclusive_only_when_explicitly_priced():
    response: Final = {
        "usage": {
            "prompt_tokens": 1000,
            "completion_tokens": 200,
            "prompt_tokens_details": {
                "cached_tokens": 100,
                "image_tokens": 300,
                "audio_tokens": 50,
                "cached_tokens_details": {"image_tokens": 60, "audio_tokens": 10},
            },
            "completion_tokens_details": {"reasoning_tokens": 80, "image_tokens": 20, "audio_tokens": 10},
        }
    }
    policy: Final = _policy("p+img+ai+cr+img_cr+c+reasoning+img_o+ao")
    facts: Final = expression_usage_facts(response, policy, {})
    assert {key: facts[key] for key in ("p", "img", "ai", "cr", "img_cr", "c", "reasoning", "img_o", "ao")} == {
        "p": 620,
        "img": 240,
        "ai": 40,
        "cr": 40,
        "img_cr": 60,
        "c": 90,
        "reasoning": 80,
        "img_o": 20,
        "ao": 10,
    }
    assert facts["len"] == 1000
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "0.0012"
    aggregate: Final = expression_usage_facts(response, _policy("p+c+cr"), {})
    assert (aggregate["p"], aggregate["c"], aggregate["cr"]) == (900, 200, 100)


def test_claude_full_context_includes_cache_and_cc_excludes_only_referenced_ttl_buckets():
    response: Final = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 30,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 100,
            "cache_creation": {"ephemeral_5m_input_tokens": 70, "ephemeral_1h_input_tokens": 30},
        }
    }
    facts: Final = expression_usage_facts(response, _policy("p+c+cr+cc+cc5m"), {})
    assert (facts["p"], facts["c"], facts["cr"], facts["cc"], facts["cc5m"], facts["len"]) == (
        100,
        30,
        200,
        30,
        70,
        400,
    )
    both: Final = expression_usage_facts(response, _policy("cc+cc5m+cc1h"), {})
    assert (both["cc"], both["cc5m"], both["cc1h"], both["cc12h"]) == (0, 70, 30, 0)
    assert facts["reported_tokens"]["cache_write_tokens"] == 100


def test_gemini_cached_modalities_and_reasoning_preserve_total_context():
    response: Final = {
        "usageMetadata": {
            "promptTokenCount": 1000,
            "cachedContentTokenCount": 200,
            "candidatesTokenCount": 100,
            "thoughtsTokenCount": 50,
            "promptTokensDetails": [
                {"modality": "IMAGE", "tokenCount": 300},
                {"modality": "AUDIO", "tokenCount": 100},
                {"modality": "TEXT", "tokenCount": 600},
            ],
            "cacheTokensDetails": [
                {"modality": "IMAGE", "tokenCount": 100},
                {"modality": "AUDIO", "tokenCount": 20},
                {"modality": "TEXT", "tokenCount": 80},
            ],
            "candidatesTokensDetails": [
                {"modality": "IMAGE", "tokenCount": 30},
                {"modality": "AUDIO", "tokenCount": 10},
                {"modality": "TEXT", "tokenCount": 60},
            ],
        }
    }
    policy: Final = _policy("p+img+ai+cr+img_cr+c+reasoning+img_o+ao")
    facts: Final = expression_usage_facts(response, policy, {})
    assert {key: facts[key] for key in ("p", "img", "ai", "cr", "img_cr", "c", "reasoning", "img_o", "ao")} == {
        "p": 520,
        "img": 200,
        "ai": 80,
        "cr": 100,
        "img_cr": 100,
        "c": 60,
        "reasoning": 50,
        "img_o": 30,
        "ao": 10,
    }
    assert facts["len"] == 1000
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "0.00115"


def test_missing_tokens_and_units_fail_only_when_selected_branch_reads_them():
    policy: Final = _policy("weekday()==0 ? fixed(0.5) : p+u('duration')", usage_bounds={"duration": "60"})
    facts: Final = expression_usage_facts({}, policy, {})
    assert "p" not in facts
    assert facts["usage"] == {}
    context: Final = freeze_expression_context(policy, {}, _START)
    assert evaluate_expression(policy, facts, context)["amount"] == "0.5"
    weekday: Final = freeze_expression_context(policy, {}, datetime(2026, 11, 2, tzinfo=timezone.utc))
    with pytest.raises(ExpressionPricingError, match="Missing usage variable 'p'"):
        evaluate_expression(policy, facts, weekday)


def test_missing_modality_split_omits_dependent_base_variable_and_preserves_lazy_branch():
    policy: Final = _policy("1 ? fixed(1) : p+img")
    response: Final = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    evidence: Final = {"input_image": True}
    facts: Final = expression_usage_facts(response, policy, evidence)
    assert "img" not in facts
    assert "p" not in facts
    assert facts["len"] == 100
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "1"
    actual: Final = _policy("p+img")
    with pytest.raises(ExpressionPricingError, match="Missing usage variable"):
        evaluate_expression(actual, facts, freeze_expression_context(actual, {}, _START))


def test_known_text_request_missing_modality_details_has_zero_modality_tokens():
    policy: Final = _policy("p+img+ai+c+ao+img_o+reasoning")
    facts: Final = expression_usage_facts({"usage": {"prompt_tokens": 100, "completion_tokens": 20}}, policy, {})
    assert (facts["p"], facts["c"], facts["img"], facts["ai"], facts["reasoning"]) == (100, 20, 0, 0, 0)


def test_unknown_reasoning_split_omits_c_only_when_reasoning_is_explicitly_priced():
    response: Final = {"usage": {"prompt_tokens": 100, "completion_tokens": 20}}
    evidence: Final = request_usage_evidence({"reasoning_effort": "high"}, "completion")
    facts: Final = expression_usage_facts(response, _policy("c+reasoning"), evidence)
    assert "c" not in facts
    assert "reasoning" not in facts
    aggregate: Final = expression_usage_facts(response, _policy("c"), evidence)
    assert aggregate["c"] == 20


def test_unclassified_cache_writes_use_default_coefficient_and_reported_ttl_subsets_stay_zero():
    response: Final = {"usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 30}}
    policy: Final = _policy("cc*2+cc5m*4+cc1h*8")
    split: Final = expression_usage_facts(response, policy, {})
    assert (split["cc"], split["cc5m"], split["cc1h"]) == (30, 0, 0)
    assert split["reported_tokens"]["cache_write_unclassified_tokens"] == 30
    assert evaluate_expression(policy, split, freeze_expression_context(policy, {}, _START))["amount"] == "0.00006"
    assert expression_usage_facts(response, _policy("cc"), {})["cc"] == 30


def test_image_billing_uses_actual_returned_images_instead_of_requested_n():
    policy: Final = _policy("fixed(u('images')*0.04)", usage_bounds={"images": "3"})
    request: Final = {"n": 3, "prompt": "private"}
    evidence: Final = request_usage_evidence(request, "image_generation")
    response: Final = {"data": [{"b64_json": "a"}, {"url": "https://invalid.example/image"}]}
    facts: Final = expression_usage_facts(response, policy, evidence)
    assert facts["usage"] == {"images": "2"}
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, request, _START))["amount"] == "0.08"


@pytest.mark.parametrize(
    "response",
    [
        {"data": [{"url": "a"}], "usage": {"image_count": 2}},
        {"usage": {"image_count": 2, "images": 3}},
    ],
)
def test_conflicting_reported_image_counts_fail_closed(response: object):
    policy: Final = _policy("fixed(u('images')*0.04)", usage_bounds={"images": "4"})
    with pytest.raises(ValueError, match="image count"):
        expression_usage_facts(response, policy, {"output_image": True})


def test_returned_images_above_usage_bound_are_not_silently_capped():
    policy: Final = _policy("fixed(u('images')*0.04)", usage_bounds={"images": "1"})
    facts: Final = expression_usage_facts({"data": [{"url": "a"}, {"url": "b"}]}, policy, {"output_image": True})
    with pytest.raises(ExpressionPricingError, match="exceeds its configured bound"):
        evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))


@pytest.mark.parametrize("value", ["1e-10000000", "NaN", "Infinity", -1, True, "not a number"])
def test_reported_usage_quantities_reject_nonfinite_unsafe_precision_or_negative_numbers(value: object):
    policy: Final = _policy("fixed(u('duration'))", usage_bounds={"duration": "60"})
    with pytest.raises(ValueError):
        expression_usage_facts({"usage": {"duration": value}}, policy, {})


def test_extreme_zero_exponent_is_canonicalized_and_nested_numeric_usage_is_supported():
    policy: Final = _policy("fixed(u('timing.duration'))", usage_bounds={"timing.duration": "60"})
    facts: Final = expression_usage_facts({"usage": {"timing": {"duration": "0e-10000000"}}}, policy, {})
    assert facts["usage"] == {"timing.duration": "0"}
    assert Decimal(evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"]) == 0


def test_evidence_uses_modality_structure_without_matching_words_in_plain_text():
    plain: Final = {"messages": [{"role": "user", "content": "Explain input_image, image_url, file_id and audio"}]}
    assert request_usage_evidence(plain, "completion") == {
        "input_image": False,
        "input_audio": False,
        "output_image": False,
        "image_response": False,
        "output_audio": False,
        "reasoning": False,
        "web_search": False,
        "google_maps": False,
    }
    multimodal: Final = {
        "input": [
            {"content": [{"type": "input_image", "image_url": "private"}, {"type": "input_audio", "data": "private"}]}
        ],
        "modalities": ["audio"],
        "reasoning": {"effort": "high"},
    }
    evidence: Final = request_usage_evidence(multimodal, "completion")
    assert evidence == {
        "input_image": True,
        "input_audio": True,
        "output_image": False,
        "image_response": False,
        "output_audio": True,
        "reasoning": True,
        "web_search": False,
        "google_maps": False,
    }
    assert "private" not in str(evidence)


def test_invalid_cached_modality_subset_is_rejected():
    response: Final = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {
                "cached_tokens": 10,
                "image_tokens": 5,
                "cached_tokens_details": {"image_tokens": 8},
            },
        }
    }
    with pytest.raises(ValueError, match="Cached modality"):
        expression_usage_facts(response, _policy("p+img+img_cr"), {})


def test_cached_image_subset_can_be_priced_without_an_uncached_image_total():
    response: Final = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 20, "cached_tokens_details": {"image_tokens": 8}},
        }
    }
    facts: Final = expression_usage_facts(response, _policy("p+cr+img_cr"), {})
    assert (facts["p"], facts["cr"], facts["img_cr"]) == (80, 12, 8)
    assert "img" not in facts


def test_custom_cache_ttl_and_named_ttl_partition_default_writes_with_context_and_service_tiers():
    response: Final = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 200,
            "cache_creation_input_tokens": 100,
            "cache_write_ttl_tokens": {"750": 40, "3600": 30},
        }
    }
    policy: Final = _policy(
        "len>350 ? tier('long',(p*4+c*12+cr*0.4+cc*3+cache_write('750')*9+cc1h*12)"
        "*(param('service_tier')=='flex'?0.5:1))"
        ":tier('short',p*2+c*8+cr*0.2+cc*2+cache_write('750')*6+cc1h*8)"
    )
    facts: Final = expression_usage_facts(response, policy, {})
    context: Final = freeze_expression_context(policy, {"service_tier": "flex"}, _START)
    assert (facts["len"], facts["cc"], facts["cc1h"]) == (400, 30, 30)
    assert facts["cache_write_ttl_tokens"] == {"750": 40, "3600": 30}
    result: Final = evaluate_expression(policy, facts, context)
    assert result["amount"] == "0.000765"
    assert result["tiers"] == ["long"]
    assert result["cache_write_ttl_tokens"] == {"750": "40"}


def test_absent_custom_ttl_bucket_is_zero_and_unclassified_writes_use_default_price_once():
    response: Final = {"usage": {"input_tokens": 100, "output_tokens": 20, "cache_creation_input_tokens": 30}}
    policy: Final = _policy("cc*2+cache_write('750')*6")
    facts: Final = expression_usage_facts(response, policy, {})
    assert facts["cc"] == 30
    assert facts["cache_write_ttl_tokens"] == {"750": 0}
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "0.00006"


def test_repeated_custom_ttl_references_remove_the_bucket_from_cc_once():
    response: Final = {
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 30,
            "cache_write_ttl_tokens": {"750": 10},
        }
    }
    policy: Final = _policy("cc*2+(cache_write('750')+cache_write('750'))*3")
    facts: Final = expression_usage_facts(response, policy, {})
    assert facts["cc"] == 20
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "0.0001"


@pytest.mark.parametrize(
    "response, expected",
    [
        ({"data": [{"url": "a"}, {"url": "b"}]}, "0.08"),
        (
            {
                "data": [{"url": "a"}, {"url": "b"}],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 100,
                    "completion_tokens_details": {"image_tokens": 100},
                },
            },
            "0.0004",
        ),
    ],
)
def test_native_image_alternative_uses_token_rate_only_when_image_tokens_are_reported(response: object, expected: str):
    policy: Final = _policy("reported('img_o') ? img_o*4 : fixed(u('images')*0.04)", usage_bounds={"images": "4"})
    facts: Final = expression_usage_facts(response, policy, {"output_image": True})
    result: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert result["amount"] == expected


def test_presence_only_usage_key_is_normalized_without_needing_a_u_function_or_bounds():
    policy: Final = _policy("reported('usage.duration') ? fixed(2) : fixed(1)")
    present: Final = expression_usage_facts({"usage": {"duration": 12}}, policy, {})
    missing: Final = expression_usage_facts({}, policy, {})
    context: Final = freeze_expression_context(policy, {}, _START)
    assert present["usage"] == {"duration": "12"}
    assert evaluate_expression(policy, present, context)["amount"] == "2"
    assert evaluate_expression(policy, missing, context)["amount"] == "1"


@pytest.mark.parametrize(
    "usage, expected, marker",
    [
        ({}, "0.08", False),
        ({"prompt_tokens": 0, "completion_tokens": 0}, "0.08", False),
        ({"input_tokens": 0, "output_tokens": 100}, "0.0004", True),
    ],
)
def test_native_image_token_marker_and_output_only_image_tokens_choose_exclusive_billing_path(
    usage: dict[str, object],
    expected: str,
    marker: bool,
):
    policy: Final = _policy(
        "reported('tokens') ? p*2+c*8+img_o*4 : fixed(u('images')*0.04)", usage_bounds={"images": "4"}
    )
    evidence: Final = request_usage_evidence({}, "image_generation")
    response: Final = {"usage": usage, "data": [{"url": "a"}, {"url": "b"}]}
    facts: Final = expression_usage_facts(response, policy, evidence)
    assert ("tokens" in facts) is marker
    result: Final = evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))
    assert result["amount"] == expected
    if marker:
        assert (facts["img_o"], facts["c"], facts["tokens"]) == (100, 0, 100)


def test_chat_response_does_not_infer_image_tokens_from_aggregate_output():
    policy: Final = _policy("reported('tokens') ? img_o*4 : fixed(1)")
    evidence: Final = request_usage_evidence({"modalities": ["image"]}, "completion")
    facts: Final = expression_usage_facts({"usage": {"prompt_tokens": 0, "completion_tokens": 100}}, policy, evidence)
    assert facts["tokens"] == 100
    assert "img_o" not in facts
    with pytest.raises(ExpressionPricingError, match="Missing usage variable 'img_o'"):
        evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))


def test_search_quantity_is_known_zero_without_search_tools_and_no_reported_count():
    policy: Final = _policy("fixed(u('web_search_requests')*0.04)", usage_bounds={"web_search_requests": "64"})
    evidence: Final = request_usage_evidence(
        {"messages": [{"content": "Explain googleSearch and web_search"}]}, "completion"
    )
    facts: Final = expression_usage_facts({}, policy, evidence)
    assert evidence["web_search"] is False
    assert facts["usage"] == {"web_search_requests": "0"}
    assert evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))["amount"] == "0"


@pytest.mark.parametrize(
    "tool",
    [
        {"type": "web_search"},
        {"type": "web_search_preview"},
        {"type": "web_search_20250305"},
        {"googleSearch": {}},
        {"google_search": {}},
    ],
)
def test_requested_search_without_reported_count_remains_missing(tool: dict[str, object]):
    policy: Final = _policy("fixed(u('web_search_requests')*0.04)", usage_bounds={"web_search_requests": "64"})
    evidence: Final = request_usage_evidence({"tools": [tool]}, "completion")
    facts: Final = expression_usage_facts({}, policy, evidence)
    assert evidence["web_search"] is True
    assert facts["usage"] == {}
    with pytest.raises(ExpressionPricingError, match="Missing usage fact 'web_search_requests'"):
        evaluate_expression(policy, facts, freeze_expression_context(policy, {}, _START))


@pytest.mark.parametrize("counts, expected", [((2, 2, 2), "2"), ((1, 3, 2), "3"), ((0, 0, 0), "0")])
def test_search_usage_aliases_use_maximum_instead_of_adding_mirrored_counts(
    counts: tuple[int, int, int], expected: str
):
    direct, server, details = counts
    policy: Final = _policy("fixed(u('web_search_requests')*0.04)", usage_bounds={"web_search_requests": "64"})
    response: Final = {
        "usage": {
            "web_search_requests": direct,
            "server_tool_use": {"web_search_requests": server},
            "input_tokens_details": {"web_search_requests": details},
        }
    }
    evidence: Final = request_usage_evidence({"tools": [{"type": "web_search_preview"}]}, "completion")
    facts: Final = expression_usage_facts(response, policy, evidence)
    assert facts["usage"] == {"web_search_requests": expected}


@pytest.mark.parametrize("tool", [{"googleMaps": {}}, {"google_maps": {}}, {"type": "google_maps"}])
def test_maps_grounding_distinguishes_unrequested_zero_from_requested_unknown(tool: dict[str, object]):
    policy: Final = _policy(
        "fixed(u('google_maps_grounding_requests')*0.04)", usage_bounds={"google_maps_grounding_requests": "64"}
    )
    assert expression_usage_facts({}, policy, {})["usage"] == {"google_maps_grounding_requests": "0"}
    evidence: Final = request_usage_evidence({"tools": [tool]}, "completion")
    assert evidence["google_maps"] is True
    assert expression_usage_facts({}, policy, evidence)["usage"] == {}
    response: Final = {"usage": {"prompt_tokens_details": {"google_maps_grounding_requests": 2}}}
    assert expression_usage_facts(response, policy, evidence)["usage"] == {"google_maps_grounding_requests": "2"}
