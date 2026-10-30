import re
from collections.abc import Iterator, Mapping
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

_TTL_TOKEN_FIELD: Final = re.compile(r"^(?:ephemeral_(\d+)([smhd])_input_tokens|ttl_(\d+)([smhd])_tokens)$")
_TTL_UNITS: Final = {"s": 1, "m": 60, "h": 3600, "d": 86400}
MAX_TTL_SECONDS: Final = 31_536_000
MAX_TTL_TIERS: Final = 16


def mapping_value(value: object) -> Mapping[str, object]:
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value if isinstance(value, Mapping) else {}


def token_count(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ValueError("A reported token count must be a non-negative integer")


def ttl_seconds_key(value: object) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise ValueError("Cache write durations must be integer seconds")
    seconds: Final = int(value)
    if str(seconds) != value or not 1 <= seconds <= MAX_TTL_SECONDS:
        raise ValueError("Cache write durations must be between 1 and 31536000 seconds")
    return value


def _validated_token_tiers(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping) or len(value) > MAX_TTL_TIERS:
        raise ValueError("Cache write usage must contain at most sixteen duration tiers")
    return {ttl_seconds_key(key): token_count(tokens) for key, tokens in value.items()}


class CacheUsage(BaseModel):
    model_config = ConfigDict(frozen=True)

    cache_read_tokens: int = Field(default=0, ge=0, strict=True)
    cache_write_tokens: int = Field(default=0, ge=0, strict=True)
    cache_write_ttl_tokens: dict[str, int] = Field(default_factory=dict)

    @field_validator("cache_write_ttl_tokens", mode="before")
    @classmethod
    def valid_tiers(cls, value: object) -> dict[str, int]:
        return _validated_token_tiers(value)

    @model_validator(mode="after")
    def consistent_total(self) -> "CacheUsage":
        if sum(self.cache_write_ttl_tokens.values()) > self.cache_write_tokens:
            raise ValueError("Cache write duration totals exceed the reported write total")
        return self

    @computed_field
    @property
    def cache_write_unclassified_tokens(self) -> int:
        return self.cache_write_tokens - sum(self.cache_write_ttl_tokens.values())


def _counts(containers: tuple[Mapping[str, object], ...], names: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(_container_counts(containers, names))


def _container_counts(containers: tuple[Mapping[str, object], ...], names: tuple[str, ...]) -> Iterator[int]:
    for container in containers:
        for name in names:
            value: Final = container.get(name)
            if value is not None:
                yield token_count(value)


def _named_ttl_tokens(value: Mapping[str, object]) -> tuple[tuple[str, int], ...]:
    return tuple(_ttl_entries(value))


def _ttl_entries(value: Mapping[str, object]) -> Iterator[tuple[str, int]]:
    for name, tokens in value.items():
        match: Final = _TTL_TOKEN_FIELD.fullmatch(name)
        if match is not None and tokens is not None:
            count: Final = match.group(1) or match.group(3)
            unit: Final = match.group(2) or match.group(4)
            yield ttl_seconds_key(str(int(count) * _TTL_UNITS[unit])), token_count(tokens)


def cache_usage(usage: Mapping[str, object]) -> CacheUsage:
    details: Final = mapping_value(usage.get("prompt_tokens_details") or usage.get("input_tokens_details"))
    containers: Final = (usage, details)
    reads: Final = _counts(containers, ("cache_read_input_tokens", "cached_tokens"))
    writes: Final = _counts(containers, ("cache_creation_input_tokens", "cache_write_tokens", "cache_creation_tokens"))
    split_sources: Final = (
        mapping_value(usage.get("cache_creation")),
        mapping_value(details.get("cache_creation_token_details")),
        mapping_value(details.get("cache_write_tokens_details")),
    )
    entries: Final = tuple(_all_ttl_entries(split_sources))
    explicit: Final = _validated_token_tiers(usage.get("cache_write_ttl_tokens", {}))
    durations: Final = frozenset(key for key, _ in entries) | explicit.keys()
    tiers: Final = {
        key: max((explicit.get(key, 0), *(tokens for duration, tokens in entries if duration == key)))
        for key in durations
    }
    return CacheUsage(
        cache_read_tokens=max(reads, default=0),
        cache_write_tokens=max(writes, default=sum(tiers.values())),
        cache_write_ttl_tokens={key: count for key, count in tiers.items() if count > 0},
    )


def _all_ttl_entries(sources: tuple[Mapping[str, object], ...]) -> Iterator[tuple[str, int]]:
    for source in sources:
        yield from _named_ttl_tokens(source)
