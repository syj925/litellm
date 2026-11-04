import asyncio
from typing import Final

import pytest
from pydantic import SecretStr

from litellm.proxy.retail.secret_cache import ManagedSecretCache
from litellm.proxy.retail.secret_config import ManagedSecretsConfig, SecretBinding
from litellm.proxy.retail.secret_provider import SecretFailure, SecretResult, SecretValue


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Provider:
    def __init__(self) -> None:
        self.result: SecretResult = SecretValue(SecretStr("fixture-first"), 1)
        self.calls = 0
        self.closed = False
        self.entered = asyncio.Event()
        self.proceed = asyncio.Event()
        self.proceed.set()

    async def read(self, binding: SecretBinding) -> SecretResult:
        self.calls += 1
        self.entered.set()
        await self.proceed.wait()
        return self.result

    async def close(self) -> None:
        self.closed = True


def cache(provider: Provider, clock: Clock) -> ManagedSecretCache:
    config: Final = ManagedSecretsConfig.model_validate(
        {"endpoint": "https://vault.test", "refs": [{"ref": "main", "path": "models/main"}]}
    )
    return ManagedSecretCache(config, provider, clock=clock)


@pytest.mark.asyncio
async def test_reads_are_memory_only_and_rotation_is_published_after_refresh() -> None:
    provider: Final = Provider()
    clock: Final = Clock()
    subject: Final = cache(provider, clock)
    await subject.refresh()
    assert tuple(subject.read("main") for _ in range(100)) == (provider.result,) * 100
    assert provider.calls == 1
    provider.result = SecretValue(SecretStr("fixture-rotated"), 2)
    await subject.refresh()
    assert subject.read("main") == provider.result
    assert provider.calls == 2
    await subject.close()


@pytest.mark.asyncio
async def test_transient_outage_does_not_extend_original_expiry() -> None:
    provider: Final = Provider()
    clock: Final = Clock()
    subject: Final = cache(provider, clock)
    await subject.refresh()
    provider.result = SecretFailure("unavailable")
    clock.now = 59
    await subject.refresh()
    assert subject.read("main") is not None
    clock.now = 60
    assert subject.read("main") is None
    await subject.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ("revoked", "invalid"))
async def test_revocation_or_invalid_provider_data_invalidates_unexpired_secret(code: str) -> None:
    provider: Final = Provider()
    subject: Final = cache(provider, Clock())
    await subject.refresh()
    provider.result = SecretFailure("revoked" if code == "revoked" else "invalid")
    await subject.refresh()
    assert subject.read("main") is None
    await subject.close()


@pytest.mark.asyncio
async def test_refresh_is_singleflight_for_concurrent_callers() -> None:
    provider: Final = Provider()
    provider.proceed.clear()
    subject: Final = cache(provider, Clock())
    tasks: Final = tuple(asyncio.create_task(subject.refresh()) for _ in range(30))
    await provider.entered.wait()
    provider.proceed.set()
    await asyncio.gather(*tasks)
    assert provider.calls == 1
    assert subject.read("main") == provider.result
    await subject.close()


@pytest.mark.asyncio
async def test_local_revocation_wins_against_inflight_refresh_and_cannot_be_undone() -> None:
    provider: Final = Provider()
    provider.proceed.clear()
    subject: Final = cache(provider, Clock())
    task: Final = asyncio.create_task(subject.refresh())
    await provider.entered.wait()
    subject.revoke("main")
    provider.proceed.set()
    await task
    await subject.refresh()
    assert subject.read("main") is None
    await subject.close()


@pytest.mark.asyncio
async def test_provider_version_rollback_stays_closed_until_current_version_returns() -> None:
    provider: Final = Provider()
    provider.result = SecretValue(SecretStr("fixture-current"), 5)
    subject: Final = cache(provider, Clock())
    await subject.refresh()
    provider.result = SecretValue(SecretStr("fixture-old"), 4)
    await subject.refresh()
    await subject.refresh()
    assert subject.read("main") is None
    provider.result = SecretValue(SecretStr("fixture-next"), 6)
    await subject.refresh()
    assert subject.read("main") == provider.result
    await subject.close()


@pytest.mark.asyncio
async def test_failed_prewarm_keeps_empty_cache_and_background_worker_can_recover() -> None:
    provider: Final = Provider()
    provider.result = SecretFailure("revoked")
    subject: Final = cache(provider, Clock())
    await subject.start()
    await provider.entered.wait()
    await subject.refresh()
    assert subject.read("main") is None
    assert not provider.closed
    provider.result = SecretValue(SecretStr("fixture-recovered"), 2)
    await subject.refresh()
    assert subject.read("main") == provider.result
    await subject.close()


@pytest.mark.asyncio
async def test_closed_cache_releases_secret_and_never_restarts_fetches() -> None:
    provider: Final = Provider()
    subject: Final = cache(provider, Clock())
    await subject.refresh()
    await subject.close()
    await subject.refresh()
    assert subject.read("main") is None
    assert provider.calls == 1
    assert provider.closed


class ConcurrentProvider:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def read(self, binding: SecretBinding) -> SecretResult:
        self.active += 1
        self.peak = max(self.active, self.peak)
        if self.active == 2:
            self.entered.set()
        await self.release.wait()
        self.active -= 1
        return SecretValue(SecretStr(f"fixture-{binding.ref}"), 1)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_refresh_limits_provider_concurrency_and_keeps_all_allowlisted_refs() -> None:
    settings: Final = ManagedSecretsConfig.model_validate({
        "endpoint": "https://vault.test", "concurrency": 2,
        "refs": [{"ref": f"ref-{index}", "path": f"keys/key-{index}"} for index in range(8)],
    })
    provider: Final = ConcurrentProvider()
    subject: Final = ManagedSecretCache(settings, provider, clock=Clock())
    task: Final = asyncio.create_task(subject.refresh())
    await provider.entered.wait()
    assert provider.peak == settings.concurrency
    provider.release.set()
    await task
    assert provider.peak == settings.concurrency
    assert tuple(subject.read(binding.ref) for binding in settings.refs) == tuple(
        SecretValue(SecretStr(f"fixture-{binding.ref}"), 1) for binding in settings.refs
    )
    await subject.close()


class MixedProvider:
    def __init__(self) -> None:
        self.fast_done = asyncio.Event()
        self.slow_release = asyncio.Event()

    async def read(self, binding: SecretBinding) -> SecretResult:
        if binding.ref == "slow":
            await self.slow_release.wait()
        else:
            self.fast_done.set()
        return SecretValue(SecretStr(f"fixture-{binding.ref}"), 1)

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_slow_ref_does_not_delay_publishing_a_successful_ref() -> None:
    settings: Final = ManagedSecretsConfig.model_validate({
        "endpoint": "https://vault.test", "refs": [{"ref": "fast", "path": "keys/fast"},
                                                    {"ref": "slow", "path": "keys/slow"}],
    })
    provider: Final = MixedProvider()
    subject: Final = ManagedSecretCache(settings, provider, clock=Clock())
    task: Final = asyncio.create_task(subject.refresh())
    await provider.fast_done.wait()
    assert subject.read("fast") == SecretValue(SecretStr("fixture-fast"), 1)
    assert subject.read("slow") is None
    provider.slow_release.set()
    await task
    assert subject.read("slow") == SecretValue(SecretStr("fixture-slow"), 1)
    await subject.close()
