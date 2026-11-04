import asyncio
import hashlib
import json
import secrets
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .oidc_config import OidcConfig, OidcFailure
from .oidc_provider import OidcSnapshot

_ACTIVATE: Final = """
local current = redis.call('GET', KEYS[1])
if current then
 local previous = cjson.decode(current)
 local proposed = cjson.decode(ARGV[1])
 if previous.generation > proposed.generation then return 0 end
 if previous.generation == proposed.generation and current ~= ARGV[1] then return 0 end
end
redis.call('SET', KEYS[1], ARGV[1])
return 1
"""
_CREATE: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
local rate = redis.call('INCR', KEYS[3])
if rate == 1 then redis.call('EXPIRE', KEYS[3], 60) end
if rate > 120 then return 0 end
return redis.call('SET', KEYS[2], ARGV[2], 'EX', 300, 'NX') and 1 or 0
"""
_CONSUME: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return false end
local raw = redis.call('GET', KEYS[2])
if not raw then return false end
local record = cjson.decode(raw)
if record.cookie_digest ~= ARGV[2] then return false end
redis.call('DEL', KEYS[2])
return raw
"""
_LEASE: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('EXISTS', KEYS[2]) == 1 or redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
redis.call('SET', KEYS[2], ARGV[2], 'PX', 7000)
redis.call('SET', KEYS[3], '1', 'EX', 30)
return 1
"""
_PUBLISH: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] or redis.call('GET', KEYS[2]) ~= ARGV[2] then return 0 end
redis.call('SET', KEYS[3], ARGV[3], 'EX', 300)
redis.call('DEL', KEYS[2])
return 1
"""
_UNLOCK: Final = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
_READ: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return false end
return redis.call('GET', KEYS[2])
"""


class OidcFlow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    cookie_digest: str = Field(min_length=64, max_length=64, repr=False)
    nonce_digest: str = Field(min_length=64, max_length=64, repr=False)
    verifier: SecretStr = Field(repr=False)
    return_to: str = Field(max_length=2048, repr=False)


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class OidcState:
    def __init__(self, config: OidcConfig, redis: Redis) -> None:
        self._config = config
        self._redis = redis
        self._prefix = f"retail:oidc:{config.namespace}"
        self._fence = json.dumps({"digest": config.digest(), "generation": config.generation}, sort_keys=True)
        self._identity = f"{self._prefix}:{config.digest()}"

    async def _eval(self, script: str, keys: tuple[str, ...], args: tuple[str, ...]) -> object | OidcFailure:
        try:
            return await asyncio.wait_for(
                self._redis.execute_command("EVAL", script, len(keys), *keys, *args), timeout=1,
            )
        except (RedisError, OSError, TimeoutError, ValueError):
            return OidcFailure("unavailable")

    async def activate(self) -> bool:
        result: Final = await self._eval(_ACTIVATE, (f"{self._prefix}:active",), (self._fence,))
        return result == 1

    async def active(self) -> bool:
        result: Final = await self._eval(
            _READ, (f"{self._prefix}:active", f"{self._prefix}:active"), (self._fence,),
        )
        return isinstance(result, (str, bytes))

    async def create(self, state: str, cookie: str, nonce: str, verifier: str, return_to: str) -> bool:
        flow: Final = OidcFlow(
            cookie_digest=digest_secret(cookie), nonce_digest=digest_secret(nonce),
            verifier=SecretStr(verifier), return_to=return_to,
        )
        body: Final = json.dumps({**flow.model_dump(exclude={"verifier"}), "verifier": verifier})
        result: Final = await self._eval(
            _CREATE, (f"{self._prefix}:active", f"{self._identity}:flow:{digest_secret(state)}", f"{self._prefix}:rate"),
            (self._fence, body),
        )
        return result == 1

    async def consume(self, state: str, cookie: str) -> OidcFlow | OidcFailure:
        result: Final = await self._eval(
            _CONSUME, (f"{self._prefix}:active", f"{self._identity}:flow:{digest_secret(state)}"),
            (self._fence, digest_secret(cookie)),
        )
        if isinstance(result, OidcFailure):
            return result
        if not isinstance(result, (str, bytes)):
            return OidcFailure("invalid")
        try:
            return OidcFlow.model_validate_json(result)
        except ValueError:
            return OidcFailure("invalid")

    async def snapshot(self) -> OidcSnapshot | OidcFailure:
        result: Final = await self._eval(
            _READ, (f"{self._prefix}:active", f"{self._identity}:snapshot"), (self._fence,),
        )
        if isinstance(result, OidcFailure):
            return result
        if not isinstance(result, (str, bytes)):
            return OidcFailure("unavailable")
        try:
            return OidcSnapshot.model_validate_json(result)
        except (ValueError, RecursionError):
            return OidcFailure("unavailable")

    async def lease(self) -> str | OidcFailure:
        owner: Final = secrets.token_urlsafe(32)
        result: Final = await self._eval(
            _LEASE, (f"{self._prefix}:active", f"{self._identity}:lease", f"{self._identity}:cooldown"),
            (self._fence, owner),
        )
        return owner if result == 1 else OidcFailure("unavailable")

    async def publish(self, owner: str, snapshot: OidcSnapshot) -> bool:
        result: Final = await self._eval(
            _PUBLISH, (f"{self._prefix}:active", f"{self._identity}:lease", f"{self._identity}:snapshot"),
            (self._fence, owner, snapshot.model_dump_json()),
        )
        return result == 1

    async def unlock(self, owner: str) -> None:
        await self._eval(_UNLOCK, (f"{self._identity}:lease",), (owner,))
