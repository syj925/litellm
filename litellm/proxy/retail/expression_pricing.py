import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, DecimalException, localcontext
from types import MappingProxyType
from typing import Final, Literal, TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from typing_extensions import Self

from .expression_parser import TIME_PARTS, ExpressionPricingError, Node, compile_expression, walk

MILLION: Final = Decimal(1_000_000)
MAX_USD: Final = Decimal(1_000_000)
_MAX_NUMBER: Final = Decimal("1e36")
_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)
_ATTRIBUTE_VALUES: Final = {
    "inference_geo": frozenset(("global", "us")),
    "speed": frozenset(("standard", "fast")),
    "data_residency": frozenset(("global", "eu", "us")),
    "service_tier": frozenset((
        "default", "auto", "standard", "priority", "fast", "flex", "batches", "batch", "ultrafast", "scale",
    )),
}


def _decimal(value: object, label: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str, int, float)):
        raise ExpressionPricingError(f"{label} must be numeric")
    if isinstance(value, str) and len(value) > 256:
        raise ExpressionPricingError(f"{label} is too long")
    if isinstance(value, int) and value.bit_length() > 128:
        raise ExpressionPricingError(f"{label} is too large")
    try:
        number: Final = Decimal(str(value))
    except DecimalException as exc:
        raise ExpressionPricingError(f"{label} must be numeric") from exc
    if not number.is_finite() or number.copy_abs() > _MAX_NUMBER:
        raise ExpressionPricingError(f"{label} must be finite and at most 1e36 in magnitude")
    if number != 0 and number.adjusted() < -36:
        raise ExpressionPricingError(f"{label} requires more than 36 fractional places")
    if len(number.as_tuple().digits) > 256:
        raise ExpressionPricingError(f"{label} exceeds 256 significant digits")
    if nonnegative and number < 0:
        raise ExpressionPricingError(f"{label} must not be negative")
    return _ZERO if number == 0 else number


def _timezone(name: str) -> ZoneInfo:
    if not name or len(name) > 128:
        raise ExpressionPricingError("Invalid timezone")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ExpressionPricingError(f"Unknown timezone '{name}'") from exc


class ExpressionPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal["expression-v1"] = "expression-v1"
    expression: str = Field(min_length=1, max_length=16_384)
    timezone: str = "UTC"
    usage_bounds: Mapping[str, str] = Field(default_factory=dict, validate_default=True)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        _timezone(value)
        return value

    @field_validator("usage_bounds")
    @classmethod
    def validate_bounds(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if len(value) > 128:
            raise ExpressionPricingError("At most 128 usage bounds are supported")
        return MappingProxyType(
            {
                key: format(_decimal(bound, f"Usage bound '{key}'", nonnegative=True), "f")
                for key, bound in value.items()
            }
        )

    @field_serializer("usage_bounds")
    def serialize_bounds(self, value: Mapping[str, str]) -> Mapping[str, str]:
        return dict(value)

    @model_validator(mode="after")
    def validate_program(self) -> Self:
        program: Final = compile_expression(self.expression)
        missing: Final = program.usage_keys.difference(self.usage_bounds)
        if missing:
            raise ExpressionPricingError(f"Missing usage bounds for: {', '.join(sorted(missing))}")
        for node in walk(program.root):
            if node.kind == "number":
                _decimal(node.value, "Numeric literal")
            if node.kind == "call" and (node.value in TIME_PARTS or node.value == "date") and node.children:
                _timezone(node.children[0].value)
            if node.kind == "call" and node.value == "time" and len(node.children) == 2:
                _timezone(node.children[1].value)
        return self


def validate_expression_policy(raw: object) -> ExpressionPolicy:
    try:
        return ExpressionPolicy.model_validate(raw)
    except ValidationError as exc:
        raise ExpressionPricingError(str(exc)) from exc


def expression_variables(policy: ExpressionPolicy) -> frozenset[str]:
    return compile_expression(policy.expression).variables


def expression_usage_keys(policy: ExpressionPolicy) -> frozenset[str]:
    return compile_expression(policy.expression).usage_keys


def expression_attribute_keys(policy: ExpressionPolicy) -> frozenset[str]:
    return compile_expression(policy.expression).attribute_paths


def expression_cache_ttls(policy: ExpressionPolicy) -> frozenset[str]:
    return compile_expression(policy.expression).cache_ttls


def expression_reported_usage_keys(policy: ExpressionPolicy) -> frozenset[str]:
    return compile_expression(policy.expression).reported_usage_keys


def _path_value(data: Mapping[str, object], path: str) -> object:
    head, separator, rest = path.partition(".")
    value: Final = data.get(head)
    if not separator:
        return value
    if isinstance(value, Mapping):
        return _path_value(value, rest)
    return None


def _safe_param(value: object, path: str) -> str | int | float | bool:
    if isinstance(value, str):
        if len(value) > 256:
            raise ExpressionPricingError(f"Request parameter '{path}' is too long")
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        _decimal(value, f"Request parameter '{path}'")
        return value
    raise ExpressionPricingError(f"Request parameter '{path}' must be a scalar")


def _freeze_param(requestdata: Mapping[str, object], path: str) -> tuple[str, str | int | float | bool] | None:
    value: Final = _path_value(requestdata, path)
    default: Final = "default" if path == "service_tier" else 1 if path == "n" else None
    selected: Final = value if value is not None else default
    return None if selected is None else (path, _safe_param(selected, path))


def freeze_expression_context(
    policy: ExpressionPolicy, requestdata: Mapping[str, object], started_at: datetime
) -> Mapping[str, JsonValue]:
    if started_at.tzinfo is None or started_at.utcoffset() is None:
        raise ExpressionPricingError("Request start time must include a timezone")
    pairs: Final = tuple(
        _freeze_param(requestdata, path) for path in sorted(compile_expression(policy.expression).param_paths)
    )
    params: Final = {pair[0]: pair[1] for pair in pairs if pair is not None}
    return {"started_at": started_at.isoformat(), "timezone": policy.timezone, "params": params}


@dataclass(frozen=True, slots=True)
class _Environment:
    facts: Mapping[str, object]
    params: Mapping[str, object]
    started_at: datetime
    timezone: str
    usage_bounds: Mapping[str, str]


def _environment(policy: ExpressionPolicy, facts: Mapping[str, object], context: Mapping[str, object]) -> _Environment:
    started: Final = context.get("started_at")
    params: Final = context.get("params", {})
    if not isinstance(started, str) or not isinstance(params, Mapping):
        raise ExpressionPricingError("Invalid frozen expression context")
    try:
        parsed: Final = datetime.fromisoformat(started)
    except ValueError as exc:
        raise ExpressionPricingError("Invalid request start time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExpressionPricingError("Request start time must include a timezone")
    if context.get("timezone") != policy.timezone:
        raise ExpressionPricingError("Frozen context timezone does not match the policy")
    return _Environment(
        facts=facts, params=params, started_at=parsed, timezone=policy.timezone, usage_bounds=policy.usage_bounds
    )


_Value: TypeAlias = Decimal | str


@dataclass(frozen=True, slots=True)
class _Result:
    value: _Value
    tiers: tuple[str, ...] = ()
    variables: tuple[tuple[str, str], ...] = ()
    usage: tuple[tuple[str, str], ...] = ()
    cache_write_ttl_tokens: tuple[tuple[str, str], ...] = ()


def _number(value: _Value) -> Decimal:
    if not isinstance(value, Decimal):
        raise ExpressionPricingError("Arithmetic and logical operands must be numeric")
    return value


def _merge(value: _Value, results: tuple[_Result, ...], tier: str | None = None) -> _Result:
    return _Result(
        value=value,
        tiers=sum((result.tiers for result in results), ()) + ((tier,) if tier is not None else ()),
        variables=sum((result.variables for result in results), ()),
        usage=sum((result.usage for result in results), ()),
        cache_write_ttl_tokens=sum((result.cache_write_ttl_tokens for result in results), ()),
    )


def _math(operator: str, left: Decimal, right: Decimal) -> Decimal:
    if operator == "+":
        return _decimal(left + right, "Arithmetic result")
    if operator == "-":
        return _decimal(left - right, "Arithmetic result")
    if operator == "*":
        return _decimal(left * right, "Arithmetic result")
    if operator == "/" and right != 0:
        return _decimal(left / right, "Arithmetic result")
    raise ExpressionPricingError("Division by zero")


def _compare(operator: str, left: _Value, right: _Value) -> Decimal:
    if operator == "==":
        return Decimal(left == right)
    if operator == "!=":
        return Decimal(left != right)
    if isinstance(left, str) and isinstance(right, str):
        return _ordered_compare(operator, (left > right) - (left < right))
    if isinstance(left, Decimal) and isinstance(right, Decimal):
        return _ordered_compare(operator, (left > right) - (left < right))
    raise ExpressionPricingError("Ordered comparisons require operands of the same type")


def _ordered_compare(operator: str, order: int) -> Decimal:
    if operator == "<":
        return Decimal(order < 0)
    if operator == "<=":
        return Decimal(order <= 0)
    if operator == ">":
        return Decimal(order > 0)
    return Decimal(order >= 0)


def _time_value(node: Node, environment: _Environment) -> _Value:
    part: Final = node.children[0].value if node.value == "time" else node.value
    timezone_nodes: Final = node.children[1:] if node.value == "time" else node.children
    timezone: Final = timezone_nodes[0].value if timezone_nodes else environment.timezone
    started: Final = environment.started_at.astimezone(_timezone(timezone))
    if part == "date":
        return started.date().isoformat()
    if part == "weekday":
        return Decimal((started.weekday() + 1) % 7)
    return Decimal(getattr(started, part))


def _param(node: Node, environment: _Environment) -> _Result:
    path: Final = node.children[0].value
    value: Final = environment.params.get(path)
    if value is None:
        if len(node.children) == 2:
            return _evaluate(node.children[1], environment)
        raise ExpressionPricingError(f"Missing request parameter '{path}'")
    safe: Final = _safe_param(value, path)
    if isinstance(safe, bool):
        return _Result(Decimal(safe))
    if isinstance(safe, str):
        return _Result(safe)
    return _Result(_decimal(safe, f"Request parameter '{path}'"))


def response_attribute_value(path: str, value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or not value.isprintable():
        raise ExpressionPricingError(f"Response attribute '{path}' must be a nonempty printable string")
    normalized: Final = value.lower()
    if path == "vertex_location":
        if re.fullmatch(r"[a-z][a-z0-9-]{0,62}", normalized) is None:
            raise ExpressionPricingError("Response attribute 'vertex_location' must be a valid location name")
        return normalized
    if normalized not in _ATTRIBUTE_VALUES.get(path, frozenset()):
        raise ExpressionPricingError(f"Unknown pricing response attribute '{path}' value")
    return normalized


def _attribute(node: Node, environment: _Environment) -> _Result:
    path: Final = node.children[0].value
    attributes: Final = environment.facts.get("attributes")
    value: Final = attributes.get(path) if isinstance(attributes, Mapping) else None
    if value is None:
        if len(node.children) == 2:
            return _evaluate(node.children[1], environment)
        raise ExpressionPricingError(f"Missing response attribute '{path}'")
    safe: Final = response_attribute_value(path, value)
    return _Result(safe, variables=((f"attr.{path}", safe),))


def _reported(node: Node, environment: _Environment) -> _Result:
    path: Final = node.children[0].value
    usage: Final = environment.facts.get("usage")
    source: Final = usage if path.startswith("usage.") and isinstance(usage, Mapping) else environment.facts
    key: Final = path.removeprefix("usage.")
    if path.startswith("usage.") and not isinstance(usage, Mapping) or key not in source:
        return _Result(_ZERO)
    _decimal(source[key], f"Reported fact '{path}'", nonnegative=True)
    return _Result(_ONE)


def _call(node: Node, environment: _Environment) -> _Result:
    if node.value == "param":
        return _param(node, environment)
    if node.value == "attr":
        return _attribute(node, environment)
    if node.value == "reported":
        return _reported(node, environment)
    if node.value in TIME_PARTS or node.value in ("time", "date"):
        return _Result(_time_value(node, environment))
    if node.value == "cache_write":
        ttl: Final = node.children[0].value
        counts: Final = environment.facts.get("cache_write_ttl_tokens")
        if not isinstance(counts, Mapping) or ttl not in counts:
            raise ExpressionPricingError(f"Missing cache write TTL usage '{ttl}'")
        tokens: Final = _decimal(counts[ttl], f"Cache write TTL '{ttl}'", nonnegative=True)
        return _Result(tokens, cache_write_ttl_tokens=((ttl, format(tokens, "f")),))
    if node.value == "u":
        key: Final = node.children[0].value
        usage: Final = environment.facts.get("usage")
        if not isinstance(usage, Mapping) or key not in usage:
            raise ExpressionPricingError(f"Missing usage fact '{key}'")
        number: Final = _decimal(usage[key], f"Usage fact '{key}'", nonnegative=True)
        if number > _decimal(environment.usage_bounds[key], f"Usage bound '{key}'"):
            raise ExpressionPricingError(f"Usage fact '{key}' exceeds its configured bound")
        return _Result(number, usage=((key, format(number, "f")),))
    if node.value == "tier":
        result: Final = _evaluate(node.children[1], environment)
        return _merge(result.value, (result,), tier=node.children[0].value)
    results: Final = tuple(_evaluate(child, environment) for child in node.children)
    numbers: Final = tuple(_number(result.value) for result in results)
    return _merge(_function_number(node.value, numbers), results)


def _function_number(name: str, numbers: tuple[Decimal, ...]) -> Decimal:
    if name == "fixed":
        return _math("*", numbers[0], MILLION)
    if name == "min":
        return min(numbers)
    if name == "max":
        return max(numbers)
    if name == "abs":
        return abs(numbers[0])
    if name == "ceil":
        return numbers[0].to_integral_value(rounding=ROUND_CEILING)
    return numbers[0].to_integral_value(rounding=ROUND_FLOOR)


def _evaluate(node: Node, environment: _Environment) -> _Result:
    if node.kind == "number":
        return _Result(_decimal(node.value, "Numeric literal"))
    if node.kind == "string":
        return _Result(node.value)
    if node.kind == "variable":
        if node.value not in environment.facts:
            raise ExpressionPricingError(f"Missing usage variable '{node.value}'")
        number: Final = _decimal(environment.facts[node.value], f"Usage variable '{node.value}'", nonnegative=True)
        return _Result(number, variables=((node.value, format(number, "f")),))
    if node.kind == "call":
        return _call(node, environment)
    left: Final = _evaluate(node.children[0], environment)
    if node.kind == "unary":
        value: Final = _number(left.value)
        unary: Final = value if node.value == "+" else -value if node.value == "-" else Decimal(value == 0)
        return _merge(unary, (left,))
    if node.kind == "conditional":
        chosen: Final = node.children[1] if _number(left.value) != 0 else node.children[2]
        branch: Final = _evaluate(chosen, environment)
        return _merge(branch.value, (left, branch))
    if node.value == "&&" and _number(left.value) == 0:
        return _merge(_ZERO, (left,))
    if node.value == "||" and _number(left.value) != 0:
        return _merge(_ONE, (left,))
    right: Final = _evaluate(node.children[1], environment)
    if node.value in ("&&", "||"):
        return _merge(Decimal(_number(right.value) != 0), (left, right))
    if node.value in ("+", "-", "*", "/"):
        return _merge(_math(node.value, _number(left.value), _number(right.value)), (left, right))
    return _merge(_compare(node.value, left.value, right.value), (left, right))


def _usd(value: Decimal) -> Decimal:
    amount: Final = value / MILLION
    if amount < 0 or amount > MAX_USD or not amount.is_finite():
        raise ExpressionPricingError("Expression amount must be between 0 and 1000000 USD")
    return amount


def evaluate_expression(
    policy: ExpressionPolicy, facts: Mapping[str, object], context: Mapping[str, object]
) -> Mapping[str, JsonValue]:
    environment: Final = _environment(policy, facts, context)
    try:
        with localcontext(Context(prec=60)):
            result: Final = _evaluate(compile_expression(policy.expression).root, environment)
            amount: Final = _usd(_number(result.value))
    except DecimalException as exc:
        raise ExpressionPricingError("Invalid decimal arithmetic") from exc
    return {
        "version": "expression-v1",
        "amount": format(amount, "f"),
        "tiers": list(dict.fromkeys(result.tiers)),
        "variables": dict(result.variables),
        "usage": dict(result.usage),
        "cache_write_ttl_tokens": dict(result.cache_write_ttl_tokens),
    }


@dataclass(frozen=True, slots=True)
class _Interval:
    lower: Decimal
    upper: Decimal


_BoundValue: TypeAlias = _Interval | str


def _interval(value: _BoundValue) -> _Interval:
    if not isinstance(value, _Interval):
        raise ExpressionPricingError("Arithmetic and logical operands must be numeric")
    return value


def _directed(operator: str, left: Decimal, right: Decimal, rounding: str) -> Decimal:
    with localcontext(Context(prec=60, rounding=rounding)):
        return _math(operator, left, right)


def _interval_math(operator: str, left: _Interval, right: _Interval) -> _Interval:
    if operator == "+":
        return _Interval(
            _directed("+", left.lower, right.lower, ROUND_FLOOR), _directed("+", left.upper, right.upper, ROUND_CEILING)
        )
    if operator == "-":
        return _Interval(
            _directed("-", left.lower, right.upper, ROUND_FLOOR), _directed("-", left.upper, right.lower, ROUND_CEILING)
        )
    if operator == "/" and right.lower <= 0 <= right.upper:
        raise ExpressionPricingError("Reservation denominator may be zero; no finite upper bound")
    pairs: Final = (
        (left.lower, right.lower),
        (left.lower, right.upper),
        (left.upper, right.lower),
        (left.upper, right.upper),
    )
    return _Interval(
        min(_directed(operator, first, second, ROUND_FLOOR) for first, second in pairs),
        max(_directed(operator, first, second, ROUND_CEILING) for first, second in pairs),
    )


def _bound_param(node: Node, environment: _Environment, input_upper: Decimal, output_upper: Decimal) -> _BoundValue:
    path: Final = node.children[0].value
    value: Final = environment.params.get(path)
    if value is None:
        if len(node.children) == 2:
            return _bound(node.children[1], environment, input_upper, output_upper)
        raise ExpressionPricingError(f"Missing request parameter '{path}'")
    safe: Final = _safe_param(value, path)
    if isinstance(safe, str):
        return safe
    number: Final = Decimal(safe) if isinstance(safe, bool) else _decimal(safe, f"Request parameter '{path}'")
    return _Interval(number, number)


def _bound_function(name: str, values: tuple[_Interval, ...]) -> _Interval:
    if name == "fixed":
        return _interval_math("*", values[0], _Interval(MILLION, MILLION))
    if name == "min":
        return _Interval(min(value.lower for value in values), min(value.upper for value in values))
    if name == "max":
        return _Interval(max(value.lower for value in values), max(value.upper for value in values))
    first: Final = values[0]
    if name == "abs":
        lower: Final = _ZERO if first.lower <= 0 <= first.upper else min(abs(first.lower), abs(first.upper))
        return _Interval(lower, max(abs(first.lower), abs(first.upper)))
    rounding: Final = ROUND_CEILING if name == "ceil" else ROUND_FLOOR
    return _Interval(first.lower.to_integral_value(rounding=rounding), first.upper.to_integral_value(rounding=rounding))


def _bound_call(node: Node, environment: _Environment, input_upper: Decimal, output_upper: Decimal) -> _BoundValue:
    if node.value == "param":
        return _bound_param(node, environment, input_upper, output_upper)
    if node.value == "attr":
        return ""
    if node.value == "reported":
        return _Interval(_ZERO, _ONE)
    if node.value in TIME_PARTS or node.value in ("time", "date"):
        value: Final = _time_value(node, environment)
        return value if isinstance(value, str) else _Interval(value, value)
    if node.value == "cache_write":
        return _Interval(_ZERO, input_upper)
    if node.value == "u":
        key: Final = node.children[0].value
        upper: Final = _decimal(environment.usage_bounds[key], f"Usage bound '{key}'", nonnegative=True)
        return _Interval(_ZERO, upper)
    if node.value == "tier":
        return _bound(node.children[1], environment, input_upper, output_upper)
    values: Final = tuple(_interval(_bound(child, environment, input_upper, output_upper)) for child in node.children)
    return _bound_function(node.value, values)


def _bound(node: Node, environment: _Environment, input_upper: Decimal, output_upper: Decimal) -> _BoundValue:
    if node.kind == "number":
        value: Final = _decimal(node.value, "Numeric literal")
        return _Interval(value, value)
    if node.kind == "string":
        return node.value
    if node.kind == "variable":
        upper: Final = output_upper if node.value in ("c", "img_o", "ao", "reasoning") else input_upper
        return _Interval(_ZERO, upper)
    if node.kind == "call":
        return _bound_call(node, environment, input_upper, output_upper)
    left: Final = _bound(node.children[0], environment, input_upper, output_upper)
    if node.kind == "unary":
        operand: Final = _interval(left)
        if node.value == "+":
            return operand
        if node.value == "-":
            return _Interval(-operand.upper, -operand.lower)
        return _Interval(_ZERO, _ONE)
    if node.kind == "conditional":
        _interval(left)
        yes: Final = _bound(node.children[1], environment, input_upper, output_upper)
        no: Final = _bound(node.children[2], environment, input_upper, output_upper)
        if isinstance(yes, str) and isinstance(no, str) and yes == no:
            return yes
        first: Final = _interval(yes)
        second: Final = _interval(no)
        return _Interval(min(first.lower, second.lower), max(first.upper, second.upper))
    right: Final = _bound(node.children[1], environment, input_upper, output_upper)
    if node.value in ("+", "-", "*", "/"):
        return _interval_math(node.value, _interval(left), _interval(right))
    if node.value in ("&&", "||"):
        _interval(left)
        _interval(right)
        return _Interval(_ZERO, _ONE)
    return _Interval(_ZERO, _ONE)


def reserve_expression(
    policy: ExpressionPolicy, context: Mapping[str, object], input_upper: int, output_upper: int
) -> Decimal:
    if isinstance(input_upper, bool) or not isinstance(input_upper, int):
        raise ExpressionPricingError("Input upper bound must be a nonnegative integer")
    if isinstance(output_upper, bool) or not isinstance(output_upper, int):
        raise ExpressionPricingError("Output upper bound must be a nonnegative integer")
    input_limit: Final = _decimal(input_upper, "Input upper bound", nonnegative=True)
    output_limit: Final = _decimal(output_upper, "Output upper bound", nonnegative=True)
    environment: Final = _environment(policy, {}, context)
    try:
        with localcontext(Context(prec=60, rounding=ROUND_CEILING)):
            bound: Final = _interval(
                _bound(compile_expression(policy.expression).root, environment, input_limit, output_limit)
            )
            return _usd(bound.upper)
    except DecimalException as exc:
        raise ExpressionPricingError("Invalid reservation arithmetic") from exc
