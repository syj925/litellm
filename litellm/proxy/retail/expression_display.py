from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Context, Decimal, DecimalException, localcontext
from functools import lru_cache
from itertools import chain
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator

from .expression_parser import TIME_PARTS, Node, compile_expression, walk
from .expression_pricing import validate_expression_policy

_MAX_ROWS: Final = 64
_MILLION: Final = Decimal(1_000_000)
_SERVICE_NAMES: Final = {
    "default": "Standard",
    "standard": "Standard (reported)",
    "auto": "Automatic",
    "priority": "Priority",
    "fast": "Fast",
    "flex": "Flex",
    "batches": "Batch",
    "batch": "Batch",
    "ultrafast": "Ultrafast",
}
_FIELD_NAMES: Final = {
    "service_tier": "Service",
    "inference_geo": "Inference region",
    "speed": "Speed",
    "data_residency": "Processing region",
    "vertex_location": "Endpoint location",
    "quality": "Quality",
    "size": "Image size",
    "resolution": "Resolution",
    "n": "Requested images",
    "duration": "Duration",
    "reasoning_effort": "Reasoning effort",
    "prompt_cache_retention": "Cache retention",
}
_TOKEN_NAMES: Final = {
    "p": "Input tokens",
    "c": "Output tokens",
    "len": "Input context",
    "cr": "Cached input tokens",
    "cc": "Cache write tokens",
    "img": "Image input tokens",
    "img_o": "Image output tokens",
    "ai": "Audio input tokens",
    "ao": "Audio output tokens",
    "reasoning": "Reasoning tokens",
}
_OPERATORS: Final = {"==": "is", "!=": "is not", ">": ">", ">=": "≥", "<": "<", "<=": "≤"}
_INVERSE: Final = {"==": "!=", "!=": "==", ">": "<=", ">=": "<", "<": ">=", "<=": ">"}
_DAYS: Final = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


class TokenRateRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    conditions: tuple[str, ...] = ()
    rates: Mapping[str, str]
    baseline: bool = False

    @field_validator("rates")
    @classmethod
    def freeze_rates(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        return MappingProxyType(dict(value))

    @field_serializer("rates")
    def serialize_rates(self, value: Mapping[str, str]) -> dict[str, str]:
        return dict(value)


class FeeRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    conditions: tuple[str, ...] = ()
    label: str
    amount: str
    usage_key: str | None = None
    unit: str


class ModifierRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    conditions: tuple[str, ...]
    multiplier: str
    applies_to: str = "Token charges"


class ExpressionPriceDisplay(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["complete", "unsupported"]
    token_rates: tuple[TokenRateRow, ...] = ()
    fees: tuple[FeeRow, ...] = ()
    modifiers: tuple[ModifierRow, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Constraint:
    text: str
    dimension: str = "other"
    positive: bool = True
    baseline: bool = False
    services: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Parts:
    token_rates: tuple[TokenRateRow, ...] = ()
    fees: tuple[FeeRow, ...] = ()
    modifiers: tuple[ModifierRow, ...] = ()


def _unwrap(node: Node) -> Node:
    return _unwrap(node.children[1]) if node.kind == "call" and node.value == "tier" else node


def _scalar(raw: Node) -> Decimal | None:
    node: Final = _unwrap(raw)
    if node.kind == "number":
        return Decimal(node.value)
    if node.kind == "unary" and node.value in {"+", "-"}:
        operand: Final = _scalar(node.children[0])
        return None if operand is None else operand if node.value == "+" else -operand
    if node.kind != "binary" or node.value not in {"+", "-", "*", "/"}:
        return None
    left: Final = _scalar(node.children[0])
    right: Final = _scalar(node.children[1])
    if left is None or right is None or node.value == "/" and right == 0:
        return None
    return (
        left + right
        if node.value == "+"
        else left - right
        if node.value == "-"
        else (left * right if node.value == "*" else left / right)
    )


def _linear(raw: Node) -> dict[str, Decimal] | None:
    node: Final = _unwrap(raw)
    if node.kind == "variable":
        return {node.value: Decimal(1)}
    if node.kind == "call" and node.value == "cache_write":
        return {f"cache_write_{node.children[0].value}": Decimal(1)}
    if node.kind == "number" and Decimal(node.value) == 0:
        return {}
    if node.kind != "binary":
        return None
    left: Final = _linear(node.children[0])
    right: Final = _linear(node.children[1])
    if node.value in {"+", "-"} and left is not None and right is not None:
        sign: Final = Decimal(1) if node.value == "+" else Decimal(-1)
        return {
            key: left.get(key, Decimal(0)) + sign * right.get(key, Decimal(0)) for key in left.keys() | right.keys()
        }
    left_scalar: Final = _scalar(node.children[0])
    right_scalar: Final = _scalar(node.children[1])
    if node.value == "*" and left_scalar is not None and right is not None:
        return {key: value * left_scalar for key, value in right.items()}
    if (
        node.value in {"*", "/"}
        and right_scalar is not None
        and left is not None
        and (node.value == "*" or right_scalar != 0)
    ):
        factor: Final = right_scalar if node.value == "*" else Decimal(1) / right_scalar
        return {key: value * factor for key, value in left.items()}
    return None


def _path(node: Node) -> str | None:
    return node.children[0].value if node.kind == "call" and node.value in {"attr", "param"} else None


def _operand(node: Node) -> str | None:
    if node.kind == "number":
        return format(Decimal(node.value), ",f")
    if node.kind == "string":
        return _SERVICE_NAMES.get(node.value, node.value.upper() if node.value in {"us", "eu"} else node.value)
    if node.kind == "variable":
        return _TOKEN_NAMES.get(node.value)
    path: Final = _path(node)
    if path is not None:
        return _FIELD_NAMES.get(path, path.replace("_", " ").replace(".", " ").capitalize())
    if node.kind == "call" and node.value in TIME_PARTS | {"date", "time"}:
        part: Final = node.children[0].value if node.value == "time" else node.value
        timezone: Final = node.children[1:] if node.value == "time" else node.children
        label: Final = {"weekday": "Day of week", "date": "Request date"}.get(part, part.capitalize())
        return f"{label} ({timezone[0].value})" if timezone else label
    return None


def _service_values(node: Node) -> tuple[str, ...] | None:
    if node.kind == "binary" and node.value == "||":
        left: Final = _service_values(node.children[0])
        right: Final = _service_values(node.children[1])
        return (*left, *right) if left is not None and right is not None else None
    if node.kind == "binary" and node.value == "==" and _path(node.children[0]) == "service_tier":
        return (node.children[1].value,) if node.children[1].kind == "string" else None
    return None


def _condition(node: Node, positive: bool = True) -> _Constraint | None:
    services: Final = _service_values(node)
    if services is not None:
        label: Final = " / ".join(_SERVICE_NAMES.get(value, value.title()) for value in services)
        return _Constraint(
            f"Service: {label}" if positive else "Standard",
            "service",
            positive,
            ("default" in services) == positive,
            services,
        )
    if node.kind == "call" and node.value == "reported" and node.children[0].value == "tokens":
        return _Constraint("Token usage reported" if positive else "No token usage reported")
    if node.kind == "unary" and node.value == "!":
        return _condition(node.children[0], not positive)
    if node.kind != "binary":
        return None
    if node.value in {"&&", "||"}:
        left: Final = _condition(node.children[0])
        right: Final = _condition(node.children[1])
        if left is None or right is None:
            return None
        joined: Final = f"({left.text}) {'and' if node.value == '&&' else 'or'} ({right.text})"
        return _Constraint(joined if positive else f"Outside ({joined})")
    if node.value not in _OPERATORS:
        return None
    first, second = node.children
    left_operand: Final = _operand(first)
    right_operand: Final = _operand(second)
    if left_operand is None or right_operand is None:
        return None
    operator: Final = node.value if positive else _INVERSE[node.value]
    if first.kind == "variable" and first.value == "len" and second.kind == "number":
        return _Constraint(
            f"Input context {_OPERATORS[operator]} {right_operand} tokens", "context", positive, operator in {"<", "<="}
        )
    day: Final = (
        _DAYS[int(Decimal(second.value))]
        if first.kind == "call"
        and first.value == "weekday"
        and second.kind == "number"
        and Decimal(second.value) == int(Decimal(second.value))
        and 0 <= int(Decimal(second.value)) <= 6
        else None
    )
    return _Constraint(f"{left_operand} {_OPERATORS[operator]} {day or right_operand}")


def _conditions(constraints: tuple[_Constraint, ...], *, token: bool = False) -> tuple[str, ...]:
    service_constraints: Final = tuple(item for item in constraints if item.dimension == "service")
    positive: Final = tuple(frozenset(item.services) for item in service_constraints if item.positive)
    excluded: Final = frozenset(chain.from_iterable(item.services for item in service_constraints if not item.positive))
    selected: Final = frozenset.intersection(*positive).difference(excluded) if positive else frozenset()
    names: Final = " / ".join(_SERVICE_NAMES.get(value, value.title()) for value in sorted(selected))
    excluded_names: Final = ", ".join(_SERVICE_NAMES.get(value, value.title()) for value in sorted(excluded))
    other: Final = tuple(item.text for item in constraints if item.dimension != "service")
    service: Final = (
        (f"Service: {names}",)
        if positive
        else (f"Other services (excluding {excluded_names})",)
        if "default" in excluded
        else ("Standard",)
        if token or service_constraints
        else ()
    )
    return tuple(dict.fromkeys((*service, *other)))


def _baseline(constraints: tuple[_Constraint, ...]) -> bool:
    return all(item.baseline for item in constraints)


def _reachable(constraints: tuple[_Constraint, ...]) -> bool:
    positive: Final = tuple(
        frozenset(item.services) for item in constraints if item.dimension == "service" and item.positive
    )
    excluded: Final = frozenset(
        chain.from_iterable(item.services for item in constraints if item.dimension == "service" and not item.positive)
    )
    return not positive or bool(frozenset.intersection(*positive).difference(excluded))


def _limited(parts: _Parts) -> _Parts | None:
    return parts if len(parts.token_rates) + len(parts.fees) + len(parts.modifiers) <= _MAX_ROWS else None


def _join(left: _Parts | None, right: _Parts | None) -> _Parts | None:
    if left is None or right is None:
        return None
    if (left.modifiers and right.fees or right.modifiers and left.fees) and any(
        row.applies_to != "Token charges" for row in (*left.modifiers, *right.modifiers)
    ):
        return None
    return _limited(
        _Parts(left.token_rates + right.token_rates, left.fees + right.fees, left.modifiers + right.modifiers)
    )


def _units(raw: Node) -> tuple[str, Decimal, bool] | None:
    node: Final = _unwrap(raw)
    if node.kind == "call" and node.value == "u":
        return node.children[0].value, Decimal(1), False
    if node.kind == "call" and node.value == "min" and len(node.children) == 2:
        first, second = node.children
        quantity: Final = _units(second)
        if _scalar(first) == 1 and quantity is not None and quantity[1] == 1 and not quantity[2]:
            return quantity[0], Decimal(1), True
        return None
    if node.kind != "binary" or node.value not in {"*", "/"}:
        return None
    left: Final = _units(node.children[0])
    right: Final = _units(node.children[1])
    first_scalar: Final = _scalar(node.children[0])
    second_scalar: Final = _scalar(node.children[1])
    if (
        left is not None
        and second_scalar is not None
        and (second_scalar > 0 or node.value == "*" and second_scalar == 0)
    ):
        factor: Final = second_scalar if node.value == "*" else Decimal(1) / second_scalar
        return left[0], left[1] * factor, left[2]
    if node.value == "*" and right is not None and first_scalar is not None and first_scalar >= 0:
        return right[0], right[1] * first_scalar, right[2]
    return None


def _fee(node: Node, constraints: tuple[_Constraint, ...]) -> _Parts | None:
    scalar: Final = _scalar(node)
    if scalar is not None and scalar >= 0:
        return _Parts(
            fees=(
                FeeRow(
                    conditions=_conditions(constraints), label="Request fee", amount=format(scalar, "f"), unit="request"
                ),
            )
        )
    quantity: Final = _units(node)
    if quantity is None:
        return None
    key, rate, capped = quantity
    if capped and key not in {"web_search_requests", "google_maps_grounding_requests"}:
        return None
    label, unit = {
        "web_search_requests": ("Web search", "grounded_request" if capped else "search"),
        "google_maps_grounding_requests": ("Maps grounding", "grounded_request" if capped else "search"),
        "images": ("Generated image", "image"),
        "image_count": ("Generated image", "image"),
        "audio_seconds": ("Audio", "second"),
        "video_seconds": ("Video", "second"),
    }.get(key, (key.replace("_", " ").capitalize(), "reported unit"))
    return _Parts(
        fees=(
            FeeRow(
                conditions=_conditions(constraints),
                label=label,
                amount=format(rate, "f"),
                usage_key=key,
                unit=unit,
            ),
        )
    )


def _factor(raw: Node, constraints: tuple[_Constraint, ...], applies_to: str) -> tuple[ModifierRow, ...] | None:
    node: Final = _unwrap(raw)
    if node.kind != "conditional":
        return None
    condition, yes, no = node.children
    positive: Final = _condition(condition)
    negative: Final = _condition(condition, False)
    high: Final = _scalar(yes)
    low: Final = _scalar(no)
    if positive is None or negative is None or high is None or low is None or min(high, low) < 0:
        return None
    return tuple(
        ModifierRow(conditions=_conditions((*constraints, rule)), multiplier=format(amount, "f"), applies_to=applies_to)
        for rule, amount in ((positive, high), (negative, low))
        if amount != 1
    )


def _scale(parts: _Parts, amount: Decimal) -> _Parts | None:
    if amount < 0:
        return None
    return _Parts(
        token_rates=tuple(
            row.model_copy(
                update={
                    "rates": MappingProxyType(
                        {key: format(Decimal(value) * amount, "f") for key, value in row.rates.items()}
                    )
                }
            )
            for row in parts.token_rates
        ),
        fees=tuple(row.model_copy(update={"amount": format(Decimal(row.amount) * amount, "f")}) for row in parts.fees),
        modifiers=parts.modifiers,
    )


def _product(node: Node, constraints: tuple[_Constraint, ...]) -> _Parts | None:
    left, right = node.children
    if left.kind == "call" and left.value == "fixed" and _scalar(left.children[0]) is not None:
        return _fee(Node("binary", "*", (left.children[0], right)), constraints)
    if right.kind == "call" and right.value == "fixed" and _scalar(right.children[0]) is not None:
        return _fee(Node("binary", "*", (left, right.children[0])), constraints)
    first: Final = _scalar(left)
    second: Final = _scalar(right)
    if first is not None:
        remaining: Final = _display(right, constraints)
        return _scale(remaining, first) if remaining is not None else None
    if second is not None:
        remaining_right: Final = _display(left, constraints)
        return _scale(remaining_right, second) if remaining_right is not None else None
    base: Final = _display(left, constraints)
    scope: Final = (
        "All charges"
        if base is not None and base.token_rates and base.fees
        else ("Token charges" if base is not None and base.token_rates else "Request fees")
    )
    modifiers: Final = _factor(right, constraints, scope)
    if base is not None and modifiers is not None:
        return _limited(_Parts(base.token_rates, base.fees, base.modifiers + modifiers))
    return None


def _display(raw: Node, constraints: tuple[_Constraint, ...] = ()) -> _Parts | None:
    if not _reachable(constraints):
        return _Parts()
    node: Final = _unwrap(raw)
    linear: Final = _linear(node)
    if linear:
        if any(rate < 0 for rate in linear.values()):
            return None
        return _Parts(
            token_rates=(
                TokenRateRow(
                    conditions=_conditions(constraints, token=True),
                    rates={key: format(rate, "f") for key, rate in linear.items()},
                    baseline=_baseline(constraints),
                ),
            )
        )
    scalar: Final = _scalar(node)
    if scalar is not None:
        return _fee(Node("number", format(scalar / _MILLION, "f"), ()), constraints)
    if node.kind == "call" and node.value == "fixed":
        return _fee(node.children[0], constraints)
    if node.kind == "conditional":
        condition, yes, no = node.children
        constant: Final = _scalar(condition)
        if constant is not None:
            return _display(yes if constant else no, constraints)
        positive: Final = _condition(condition)
        negative: Final = _condition(condition, False)
        if positive is None or negative is None:
            return None
        first, second = (
            ((no, negative), (yes, positive))
            if negative.baseline and not positive.baseline
            else ((yes, positive), (no, negative))
        )
        return _join(_display(first[0], (*constraints, first[1])), _display(second[0], (*constraints, second[1])))
    if node.kind == "binary" and node.value == "+":
        left: Final = _display(node.children[0], constraints)
        right: Final = _display(node.children[1], constraints)
        if left is not None and right is not None and left.token_rates and right.token_rates:
            return None
        return _join(left, right)
    if node.kind == "binary" and node.value == "*":
        return _product(node, constraints)
    return None


@lru_cache(maxsize=256)
def _project(expression: str, timezone: str) -> ExpressionPriceDisplay:
    program: Final = compile_expression(expression)
    with localcontext(Context(prec=60)):
        parts: Final = _display(program.root)
    if parts is None:
        return ExpressionPriceDisplay(
            status="unsupported",
            notes=(
                "This custom rule needs a usage estimate. A fixed token price would not describe its charges accurately.",
            ),
        )
    nodes: Final = walk(program.root)
    has_time: Final = any(node.kind == "call" and node.value in TIME_PARTS | {"time", "date"} for node in nodes)
    has_served_tier: Final = any(
        node.kind == "call" and node.value == "attr" and _path(node) == "service_tier" for node in nodes
    )
    has_batch: Final = any(
        bool(frozenset(_service_values(node) or ()).intersection({"batch", "batches"})) for node in nodes
    )
    return ExpressionPriceDisplay(
        status="complete",
        token_rates=tuple(sorted(parts.token_rates, key=lambda row: not row.baseline)),
        fees=parts.fees,
        modifiers=parts.modifiers,
        notes=(
            *(
                ("Batch prices are reference only; asynchronous Batch requests are not enabled for prepaid wallets.",)
                if has_batch
                else ()
            ),
            *(("Rates use the actual served tier when it is reported.",) if has_served_tier else ()),
            *(
                (
                    f"Time conditions use the request start time in {timezone}, unless a condition names another timezone.",
                )
                if has_time
                else ()
            ),
        ),
    )


def build_expression_display(raw_policy: object) -> ExpressionPriceDisplay | None:
    if raw_policy is None:
        return None
    try:
        policy: Final = validate_expression_policy(raw_policy)
        return _project(policy.expression, policy.timezone)
    except (ValueError, DecimalException):
        return ExpressionPriceDisplay(status="unsupported", notes=("The pricing rule could not be summarized safely.",))
