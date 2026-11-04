import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

from .secret_config import ManagedSecretsConfig, SecretBinding
from .secret_provider import SecretFailure, SecretProvider, SecretResult, SecretValue


@dataclass(frozen=True, slots=True)
class _Entry:
    secret: SecretValue
    expires_at: float


class ManagedSecretCache:
    def __init__(self, config: ManagedSecretsConfig, provider: SecretProvider, *, clock: Callable[[], float]) -> None:
        self._config = config
        self._provider = provider
        self._clock = clock
        self._entries: Mapping[str, _Entry] = MappingProxyType({})
        self._versions: Mapping[str, int] = MappingProxyType({})
        self._revoked: frozenset[str] = frozenset()
        self._refresh_task: asyncio.Task[None] | None = None
        self._runner: asyncio.Task[None] | None = None
        self._closed = False

    def read(self, ref: str) -> SecretValue | None:
        entry: Final = self._entries.get(ref)
        if self._closed or ref in self._revoked or entry is None or self._clock() >= entry.expires_at:
            return None
        return entry.secret

    def revoke(self, ref: str) -> None:
        self._revoked = self._revoked | frozenset((ref,))
        self._entries = MappingProxyType({key: entry for key, entry in self._entries.items() if key != ref})

    async def refresh(self) -> None:
        if self._closed:
            return
        task: Final = self._refresh_task
        if task is not None and not task.done():
            await asyncio.shield(task)
            return
        self._refresh_task = asyncio.create_task(self._refresh(), name="retail-managed-secret-refresh")
        await asyncio.shield(self._refresh_task)

    async def _refresh(self) -> None:
        semaphore: Final = asyncio.Semaphore(self._config.concurrency)

        async def fetch(binding: SecretBinding) -> None:
            async with semaphore:
                result: Final = await self._provider.read(binding)
                self._publish(binding.ref, result, self._clock())

        await asyncio.gather(*(fetch(binding) for binding in self._config.refs))

    def _publish(self, ref: str, result: SecretResult, fetched_at: float) -> None:
        if self._closed:
            return
        previous: Final = self._entries
        if ref in self._revoked:
            return
        if isinstance(result, SecretFailure) and result.code == "unavailable":
            return
        if isinstance(result, SecretValue) and result.version >= self._versions.get(ref, 0):
            self._entries = MappingProxyType({**previous, ref: _Entry(result, fetched_at + self._config.ttl_seconds)})
            self._versions = MappingProxyType({**self._versions, ref: result.version})
            return
        self._entries = MappingProxyType({key: entry for key, entry in previous.items() if key != ref})

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Managed credential cache is closed")
        if self._runner is not None:
            return
        self._runner = asyncio.create_task(self._run(), name="retail-managed-secret-worker")

    async def _run(self) -> None:
        while not self._closed:
            await self.refresh()
            await asyncio.sleep(self._config.refresh_seconds)

    async def close(self) -> None:
        self._closed = True
        self._entries = MappingProxyType({})
        tasks: Final = tuple(task for task in (self._runner, self._refresh_task) if task is not None)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self._provider.close()
