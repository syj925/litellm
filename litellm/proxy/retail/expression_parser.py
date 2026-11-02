import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Final


class ExpressionPricingError(ValueError):
    pass


VARIABLES: Final = frozenset(
    (
        "p", "c", "len", "cr", "cc", "cc5m", "cc1h", "cc30m", "cc12h", "img", "img_cr", "img_o", "ai", "ai_cr",
        "ao", "reasoning",
    )
)
CACHE_TTL_VARIABLES: Final = {"cc5m": "300", "cc30m": "1800", "cc1h": "3600", "cc12h": "43200"}
REPORTED_ATTRIBUTE_PATHS: Final = frozenset(("service_tier", "inference_geo", "speed"))
ATTRIBUTE_PATHS: Final = REPORTED_ATTRIBUTE_PATHS.union(("data_residency", "vertex_location"))
PARAM_PATHS: Final = frozenset(
    (
        "service_tier",
        "inference_geo",
        "speed",
        "quality",
        "size",
        "resolution",
        "n",
        "duration",
        "reasoning_effort",
        "response_format.reasoning_effort",
        "response_format.type",
        "reasoning.effort",
        "prompt_cache_retention",
    )
)
TIME_PARTS: Final = frozenset(("hour", "minute", "weekday", "month", "day"))
_ARITIES: Final = {
    "fixed": (1, 1),
    "u": (1, 1),
    "cache_write": (1, 1),
    "reported": (1, 1),
    "param": (1, 2),
    "attr": (1, 2),
    "tier": (2, 2),
    "min": (1, 32),
    "max": (1, 32),
    "abs": (1, 1),
    "ceil": (1, 1),
    "floor": (1, 1),
    "hour": (0, 1),
    "minute": (0, 1),
    "weekday": (0, 1),
    "month": (0, 1),
    "day": (0, 1),
    "date": (0, 1),
    "time": (1, 2),
}
_BINDING: Final = {"||": 2, "&&": 3, "==": 4, "!=": 4, "<": 5, "<=": 5, ">": 5, ">=": 5, "+": 6, "-": 6, "*": 7, "/": 7}
_TOKEN: Final = re.compile(
    r"\s+|(?:\d+(?:\.\d*)?|\.\d+)|(?:[A-Za-z_][A-Za-z_0-9]*)|"
    r"""(?:"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')|(?:&&|\|\||==|!=|<=|>=|[+*/!?:(),<>-])"""
)
_ESCAPES: Final = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", "'": "'", '"': '"'}


@dataclass(frozen=True, slots=True)
class Node:
    kind: str
    value: str
    children: tuple["Node", ...] = ()


@dataclass(frozen=True, slots=True)
class Program:
    root: Node
    variables: frozenset[str]
    usage_keys: frozenset[str]
    param_paths: frozenset[str]
    attribute_paths: frozenset[str]
    cache_ttls: frozenset[str]
    reported_usage_keys: frozenset[str]


def _string(token: str) -> str:
    value: Final = token[1:-1]
    if len(value) > 256 or re.search(r"[\x00-\x1f]", value):
        raise ExpressionPricingError("String literals must contain at most 256 printable characters")
    if re.search(r"\\[^nrt\\'\"]", value):
        raise ExpressionPricingError("Unsupported string escape")
    return re.sub(r"\\(.)", lambda match: _ESCAPES[match[1]], value)


def _at(tokens: tuple[str, ...], position: int) -> str:
    return tokens[position] if position < len(tokens) else ""


def _expect(tokens: tuple[str, ...], position: int, token: str) -> int:
    if _at(tokens, position) != token:
        raise ExpressionPricingError(f"Expected '{token}'")
    return position + 1


def _arguments(tokens: tuple[str, ...], position: int, depth: int) -> tuple[tuple[Node, ...], int]:
    if _at(tokens, position) == ")":
        return (), position + 1
    first, next_position = _parse(tokens, position, 0, depth + 1)
    if _at(tokens, next_position) == ")":
        return (first,), next_position + 1
    comma_position: Final = _expect(tokens, next_position, ",")
    rest, end = _arguments(tokens, comma_position, depth + 1)
    return (first,) + rest, end


def _call(name: str, args: tuple[Node, ...]) -> Node:
    if name not in _ARITIES:
        raise ExpressionPricingError(f"Unknown function '{name}'")
    lower, upper = _ARITIES[name]
    if not lower <= len(args) <= upper:
        raise ExpressionPricingError(f"Invalid argument count for '{name}'")
    if name in ("u", "cache_write", "reported", "param", "attr", "tier", "time") and args[0].kind != "string":
        raise ExpressionPricingError(f"'{name}' requires a literal string as its first argument")
    if name == "param" and args[0].value not in PARAM_PATHS:
        raise ExpressionPricingError("Request parameter path is not allowlisted")
    if name == "attr" and args[0].value not in ATTRIBUTE_PATHS:
        raise ExpressionPricingError("Response attribute path is not allowlisted")
    if name == "u" and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.:-]{0,127}", args[0].value):
        raise ExpressionPricingError("Invalid usage fact name")
    if name == "reported" and not (
        args[0].value in VARIABLES
        or args[0].value == "tokens"
        or args[0].value.startswith("usage.")
        and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.:-]{0,127}", args[0].value[6:])
    ):
        raise ExpressionPricingError("reported() requires a token variable name or an allowlisted usage fact path")
    if name == "cache_write" and not (
        re.fullmatch(r"[1-9][0-9]{0,7}", args[0].value) and int(args[0].value) <= 31_536_000
    ):
        raise ExpressionPricingError("Cache write TTL must be canonical integer seconds between 1 and 31536000")
    if name == "tier" and not args[0].value.strip():
        raise ExpressionPricingError("Tier names must not be empty")
    if name == "time" and args[0].value not in TIME_PARTS:
        raise ExpressionPricingError("Unknown time component")
    timezone_args: Final = args if name in TIME_PARTS or name == "date" else args[1:] if name == "time" else ()
    if any(arg.kind != "string" for arg in timezone_args):
        raise ExpressionPricingError("Timezone arguments must be literal strings")
    return Node("call", name, args)


def _prefix(tokens: tuple[str, ...], position: int, depth: int) -> tuple[Node, int]:
    token: Final = _at(tokens, position)
    if token in ("+", "-", "!"):
        operand, end = _parse(tokens, position + 1, 8, depth + 1)
        return Node("unary", token, (operand,)), end
    if token == "(":
        inner, parenthesis_end = _parse(tokens, position + 1, 0, depth + 1)
        return inner, _expect(tokens, parenthesis_end, ")")
    if token.startswith(("'", '"')):
        return Node("string", _string(token)), position + 1
    if token and (token[0].isdigit() or token[0] == "."):
        if len(token) > 128:
            raise ExpressionPricingError("Numeric literal is too long")
        return Node("number", token), position + 1
    if token and (token[0].isalpha() or token[0] == "_"):
        if _at(tokens, position + 1) == "(":
            args, arguments_end = _arguments(tokens, position + 2, depth + 1)
            return _call(token, args), arguments_end
        if token not in VARIABLES:
            raise ExpressionPricingError(f"Unknown variable '{token}'")
        return Node("variable", token), position + 1
    raise ExpressionPricingError("Expected a number, variable, function, or parenthesized expression")


def _tail(tokens: tuple[str, ...], left: Node, position: int, minimum: int, depth: int) -> tuple[Node, int]:
    if depth > 64:
        raise ExpressionPricingError("Expression nesting exceeds 64 levels")
    token: Final = _at(tokens, position)
    if token == "?" and minimum <= 1:
        yes, colon = _parse(tokens, position + 1, 0, depth + 1)
        no, conditional_end = _parse(tokens, _expect(tokens, colon, ":"), 1, depth + 1)
        return _tail(tokens, Node("conditional", "", (left, yes, no)), conditional_end, minimum, depth + 1)
    binding: Final = _BINDING.get(token, -1)
    if binding < minimum:
        return left, position
    right, end = _parse(tokens, position + 1, binding + 1, depth + 1)
    return _tail(tokens, Node("binary", token, (left, right)), end, minimum, depth + 1)


def _parse(tokens: tuple[str, ...], position: int, minimum: int, depth: int) -> tuple[Node, int]:
    if depth > 64:
        raise ExpressionPricingError("Expression nesting exceeds 64 levels")
    left, end = _prefix(tokens, position, depth)
    return _tail(tokens, left, end, minimum, depth)


def walk(node: Node) -> tuple[Node, ...]:
    return (node,) + sum((walk(branch) for branch in node.children), ())


@lru_cache(maxsize=256)
def compile_expression(expression: str) -> Program:
    if not expression.strip() or len(expression) > 16_384:
        raise ExpressionPricingError("Expression length must be between 1 and 16384 characters")
    matches: Final = tuple(_TOKEN.finditer(expression))
    if "".join(match[0] for match in matches) != expression:
        raise ExpressionPricingError("Unsupported expression syntax")
    tokens: Final = tuple(match[0] for match in matches if not match[0].isspace())
    if len(tokens) > 2048:
        raise ExpressionPricingError("Expression exceeds 2048 tokens")
    root, end = _parse(tokens, 0, 0, 0)
    if end != len(tokens):
        raise ExpressionPricingError("Unexpected token after expression")
    nodes: Final = walk(root)
    variables: Final = frozenset(node.value for node in nodes if node.kind == "variable")
    named_ttls: Final = frozenset(ttl for variable, ttl in CACHE_TTL_VARIABLES.items() if variable in variables)
    function_ttls: Final = frozenset(
        node.children[0].value for node in nodes if node.kind == "call" and node.value == "cache_write"
    )
    if named_ttls.intersection(function_ttls):
        raise ExpressionPricingError("A cache write TTL cannot use both its named variable and cache_write() alias")
    return Program(
        root=root,
        variables=variables,
        usage_keys=frozenset(node.children[0].value for node in nodes if node.kind == "call" and node.value == "u"),
        param_paths=frozenset(
            node.children[0].value for node in nodes if node.kind == "call" and node.value == "param"
        ),
        attribute_paths=frozenset(
            node.children[0].value for node in nodes if node.kind == "call" and node.value == "attr"
        ),
        cache_ttls=named_ttls.union(function_ttls),
        reported_usage_keys=frozenset(
            node.children[0].value[6:]
            for node in nodes
            if node.kind == "call" and node.value == "reported" and node.children[0].value.startswith("usage.")
        ),
    )
