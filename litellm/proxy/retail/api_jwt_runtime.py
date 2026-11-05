import asyncio
import json
import secrets
import time
from collections.abc import Callable
from typing import Final

import httpx
from pydantic import SecretStr
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .api_jwt_config import ApiJwtConfig, ApiJwtFailure
from .api_jwt_provider import ApiJwtProvider, ApiJwtSnapshot, VerifiedApiJwt

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
_READ: Final = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return false end
return redis.call('GET', KEYS[2])
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


class ApiJwtRuntime:
    def __init__(
        self, config: ApiJwtConfig, redis: Redis, *, transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._redis = redis
        self._clock = clock
        self._provider = ApiJwtProvider(config, transport=transport, clock=clock)
        self._prefix = f"retail:api-jwt:{config.namespace}"
        self._identity = f"{self._prefix}:{config.digest()}"
        self._fence = json.dumps({"digest": config.digest(), "generation": config.generation}, sort_keys=True)
        self._snapshot: ApiJwtSnapshot | None = None
        self._available = False
        self._task: asyncio.Task[None] | None = None
        self._recovery: asyncio.Task[None] | None = None
        self._unknown_until = 0.0
        self._lock = asyncio.Lock()

    async def _eval(self, script: str, keys: tuple[str, ...], args: tuple[str, ...]) -> object | ApiJwtFailure:
        try:
            return await asyncio.wait_for(
                self._redis.execute_command("EVAL", script, len(keys), *keys, *args), timeout=1,
            )
        except (RedisError, OSError, TimeoutError, ValueError):
            return ApiJwtFailure("unavailable")

    async def _activate(self) -> bool:
        result: Final = await self._eval(_ACTIVATE, (f"{self._prefix}:active",), (self._fence,))
        if result != 1:
            self._available = False
            return False
        return True

    def _current(self) -> ApiJwtSnapshot | None:
        snapshot: Final = self._snapshot
        if snapshot is None or not 0 <= self._clock() - snapshot.fetched_at < 300:
            return None
        return snapshot

    def status(self) -> bool:
        return self._available and self._current() is not None

    async def _shared(self) -> ApiJwtSnapshot | None:
        raw: Final = await self._eval(
            _READ, (f"{self._prefix}:active", f"{self._identity}:snapshot"), (self._fence,),
        )
        if not isinstance(raw, (str, bytes)):
            return None
        try:
            snapshot: Final = ApiJwtSnapshot.model_validate_json(raw)
        except ValueError:
            return None
        if not 0 <= self._clock() - snapshot.fetched_at < 300:
            return None
        self._snapshot = snapshot
        self._available = True
        return snapshot

    async def _refresh(self, kid: str | None = None) -> None:
        async with self._lock:
            shared: Final = await self._shared()
            if shared is not None and (
                (kid is not None and any(key.kid == kid for key in shared.keys))
                or (kid is None and shared.fetched_at + 240 > self._clock())
            ):
                return
            owner: Final = secrets.token_urlsafe(32)
            lease: Final = await self._eval(
                _LEASE,
                (f"{self._prefix}:active", f"{self._identity}:lease", f"{self._identity}:cooldown"),
                (self._fence, owner),
            )
            if lease != 1:
                return
            try:
                snapshot: Final = await self._provider.fetch()
                if isinstance(snapshot, ApiJwtFailure):
                    return
                published: Final = await self._eval(
                    _PUBLISH, (f"{self._prefix}:active", f"{self._identity}:lease", f"{self._identity}:snapshot"),
                    (self._fence, owner, snapshot.model_dump_json()),
                )
                if published == 1:
                    self._snapshot = snapshot
                    self._available = True
            finally:
                await self._eval(_UNLOCK, (f"{self._identity}:lease",), (owner,))

    async def start(self) -> None:
        if self._task is not None:
            return
        if await self._activate():
            await self._refresh()
        self._task = asyncio.create_task(self._run(), name="retail-api-jwt-refresh")

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(5)
            if await self._activate():
                await self._refresh()

    async def _recover_unknown(self, kid: str) -> None:
        await self._refresh(kid)
        for _ in range(5):
            current: Final = self._current()
            if current is not None and any(key.kid == kid for key in current.keys):
                return
            await asyncio.sleep(0.05)
            await self._shared()

    async def verify(self, token: SecretStr) -> VerifiedApiJwt | ApiJwtFailure:
        kid: Final = self._provider.token_kid(token)
        if isinstance(kid, ApiJwtFailure):
            return kid
        current: Final = self._current()
        if not self._available or current is None:
            return ApiJwtFailure("unavailable")
        if any(key.kid == kid for key in current.keys):
            return self._provider.verify(current, token)
        if (self._recovery is None or self._recovery.done()) and self._clock() >= self._unknown_until:
            self._unknown_until = self._clock() + 1
            self._recovery = asyncio.create_task(self._recover_unknown(kid), name="retail-api-jwt-key-recovery")
        recovery: Final = self._recovery
        if recovery is not None:
            try:
                async with asyncio.timeout(0.5):
                    await asyncio.shield(recovery)
            except TimeoutError:
                return ApiJwtFailure("unavailable")
        updated: Final = self._current()
        if updated is None or not self._available:
            return ApiJwtFailure("unavailable")
        return self._provider.verify(updated, token)

    async def close(self) -> None:
        self._available = False
        tasks: Final = tuple(task for task in (self._task, self._recovery) if task is not None)
        self._task = None
        self._recovery = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._provider.close()
