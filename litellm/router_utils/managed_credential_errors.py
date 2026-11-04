from contextvars import ContextVar
from typing import Final

from litellm.exceptions import BadRequestError, ServiceUnavailableError


class ManagedCredentialUnavailable(ServiceUnavailableError):
    def __init__(self, model: str) -> None:
        super().__init__(message="This model is temporarily unavailable", model=model, llm_provider="openai")


class ManagedCredentialRequestRejected(BadRequestError):
    def __init__(self, model: str, *, unsupported: bool = False) -> None:
        super().__init__(
            message=("This model does not support this request type" if unsupported
                     else "This model does not allow routing overrides"),
            model=model, llm_provider="openai",
        )


class _DispatchEvidence:
    __slots__ = ("started",)

    def __init__(self) -> None:
        self.started = False


_dispatch: Final[ContextVar[_DispatchEvidence | None]] = ContextVar("retail_credential_dispatch", default=None)


def reset_credential_dispatch() -> None:
    _dispatch.set(_DispatchEvidence())


def mark_credential_dispatch() -> None:
    evidence: Final = _dispatch.get()
    if evidence is not None:
        evidence.started = True


def credential_rejected_before_dispatch(error: Exception) -> bool:
    evidence: Final = _dispatch.get()
    return (
        evidence is not None and not evidence.started
        and is_credential_rejection(error)
    )


def is_credential_rejection(error: Exception) -> bool:
    return isinstance(error, (ManagedCredentialUnavailable, ManagedCredentialRequestRejected))
