import asyncio
from typing import Final

import pytest

from litellm.router_utils.managed_credential_errors import (
    ManagedCredentialRequestRejected,
    ManagedCredentialUnavailable,
    credential_rejected_before_dispatch,
    mark_credential_dispatch,
    reset_credential_dispatch,
)


@pytest.mark.parametrize("error", (ManagedCredentialUnavailable("public"), ManagedCredentialRequestRejected("public")))
def test_only_definitive_managed_rejections_before_any_dispatch_allow_release(error: Exception) -> None:
    reset_credential_dispatch()
    assert credential_rejected_before_dispatch(error)
    mark_credential_dispatch()
    assert not credential_rejected_before_dispatch(error)
    reset_credential_dispatch()
    assert credential_rejected_before_dispatch(error)


def test_an_unrelated_service_failure_is_not_proof_of_zero_provider_usage() -> None:
    reset_credential_dispatch()
    assert not credential_rejected_before_dispatch(RuntimeError("unavailable"))


@pytest.mark.asyncio
async def test_child_attempt_dispatch_survives_task_context_copy_and_new_requests_reset_it() -> None:
    reset_credential_dispatch()
    error: Final = ManagedCredentialUnavailable("public")
    assert credential_rejected_before_dispatch(error)

    async def attempt() -> None:
        mark_credential_dispatch()

    await asyncio.create_task(attempt())
    assert not credential_rejected_before_dispatch(error)
    reset_credential_dispatch()
    assert credential_rejected_before_dispatch(error)
