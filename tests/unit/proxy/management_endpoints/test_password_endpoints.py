"""
Tests for POST /user/password/change (litellm/proxy/management_endpoints/password_endpoints.py).

HIBP is served by an AsyncHTTPHandler wrapping an httpx.MockTransport that is
injected straight into change_password; no test here touches the network.
"""

import hashlib
from typing import Final
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler
from litellm.proxy._types import UI_TEAM_ID, LitellmTableNames, ProxyErrorTypes, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.login_utils import PASSWORD_SESSION_METADATA
from litellm.proxy.management_endpoints.password_endpoints import change_password
from litellm.proxy.utils import hash_password, verify_password

CURRENT_PASSWORD = "OldP@ssw0rd-2026"
NEW_PASSWORD = "NewP@ssw0rd-2026"

_POLICY_NO_BREACH_CHECK = {"password_policy_check_breached_passwords": False}


def _make_user_row(password: str | None) -> MagicMock:
    user = MagicMock()
    user.user_id = "user-123"
    user.password = password
    return user


def _make_prisma(user: MagicMock | None) -> MagicMock:
    prisma = MagicMock()
    prisma.db.litellm_usertable.find_first = AsyncMock(return_value=user)
    prisma.db.litellm_usertable.update = AsyncMock(return_value=user)
    return prisma


def _caller(user_id: str | None = "user-123") -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id=user_id, team_id=UI_TEAM_ID, metadata=dict(PASSWORD_SESSION_METADATA))


def _sso_session_caller() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id="user-123", team_id=UI_TEAM_ID, metadata={})


def _virtual_key_caller() -> UserAPIKeyAuth:
    return UserAPIKeyAuth(user_id="user-123", team_id="team-abc", metadata=dict(PASSWORD_SESSION_METADATA))


def _hibp_suffix_for(password: str) -> str:
    return hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()[5:]


def _hibp_client_returning(body: str) -> AsyncHTTPHandler:
    return AsyncHTTPHandler(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))


def _hibp_client_never_called() -> AsyncHTTPHandler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HIBP call to {request.url}")

    return AsyncHTTPHandler(transport=httpx.MockTransport(handler))


def _hibp_client_recording(calls: list[httpx.Request]) -> AsyncHTTPHandler:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text="")

    return AsyncHTTPHandler(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_change_password_success_writes_new_scrypt_hash():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        response = await change_password(
            data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
            user_api_key_dict=_caller(),
            hibp_client=_hibp_client_never_called(),
        )

    assert response.user_id == "user-123"
    update_kwargs = prisma.db.litellm_usertable.update.call_args.kwargs
    assert update_kwargs["where"] == {"user_id": "user-123"}
    stored = update_kwargs["data"]["password"]
    assert stored != NEW_PASSWORD
    assert verify_password(NEW_PASSWORD, stored)
    # A successful change lifts any pending forced reset and re-arms the
    # login-time breach screen for the new password.
    assert update_kwargs["data"]["password_reset_required"] is False
    assert update_kwargs["data"]["last_breach_check_at"] is None


@pytest.mark.asyncio
async def test_change_password_rejects_wrong_current_password():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password="not-the-password", new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 400
    assert "Current password is incorrect" in exc_info.value.detail["error"]
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_rejects_unchanged_password():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=CURRENT_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 400
    assert "must be different from the current password" in exc_info.value.detail["error"]
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "caller",
    [
        pytest.param(_sso_session_caller(), id="sso_dashboard_session"),
        pytest.param(_virtual_key_caller(), id="virtual_key_with_forged_metadata"),
    ],
)
async def test_change_password_rejects_non_password_login_session(caller: UserAPIKeyAuth):
    """Only the session minted by a password login may change the password, so a
    stolen virtual key or an SSO session cannot use the endpoint as a
    current_password guessing oracle."""
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
                user_api_key_dict=caller,
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 403
    assert "logging in with a password" in exc_info.value.detail["error"]
    prisma.db.litellm_usertable.find_first.assert_not_called()
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_rejects_session_without_user():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(user=None)

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(user_id=None),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 400
    prisma.db.litellm_usertable.find_first.assert_not_called()
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_rejects_account_without_password():
    """SSO users and the env-credential admin have no DB password row to change."""
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(password=None))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 400
    assert "no password set" in exc_info.value.detail["error"]
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_enforces_min_length():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(ProxyException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password="Short1!"),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.code == "400"
    assert exc_info.value.type == ProxyErrorTypes.validation_error
    assert exc_info.value.param == "password"
    assert "at least 12 characters" in exc_info.value.message
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_rejects_breached_password():
    """With the default policy, the new password is screened against HIBP."""
    from litellm.proxy._types import ChangePasswordRequest

    breached_password = "Password123!"
    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", {}
        ),
    ):
        with pytest.raises(ProxyException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=breached_password),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_returning(f"{_hibp_suffix_for(breached_password)}:1"),
            )

    assert exc_info.value.code == "400"
    assert exc_info.value.type == ProxyErrorTypes.validation_error
    assert exc_info.value.param == "password"
    assert "data breaches" in exc_info.value.message
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_verifies_current_password_before_hibp_lookup():
    """A caller who fails current-password verification must not trigger any
    HIBP traffic: the injected client records each request it serves and the
    test asserts none were made."""
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))
    hibp_calls: Final[list[httpx.Request]] = []

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", {}
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password="not-the-password", new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_recording(hibp_calls),
            )

    assert exc_info.value.status_code == 400
    assert "Current password is incorrect" in exc_info.value.detail["error"]
    assert hibp_calls == []
    prisma.db.litellm_usertable.update.assert_not_called()


@pytest.mark.asyncio
async def test_change_password_success_emits_redacted_audit_log():
    """A successful change must land in the audit trail as field names only;
    the plaintext passwords must never reach the audit call."""
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))
    audit_mock = AsyncMock()

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
        patch(  # test-quality-ok: audit sink is a module-level import; no injection seam
            "litellm.proxy.management_endpoints.password_endpoints.create_object_audit_log", audit_mock
        ),
    ):
        await change_password(
            data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
            user_api_key_dict=_caller(),
            hibp_client=_hibp_client_never_called(),
        )

    audit_mock.assert_awaited_once()
    audit_kwargs = audit_mock.await_args.kwargs
    assert audit_kwargs["object_id"] == "user-123"
    assert audit_kwargs["action"] == "updated"
    assert audit_kwargs["table_name"] == LitellmTableNames.USER_TABLE_NAME
    assert audit_kwargs["after_value"] == '{"fields_changed": ["password"]}'
    assert CURRENT_PASSWORD not in str(audit_kwargs)
    assert NEW_PASSWORD not in str(audit_kwargs)


@pytest.mark.asyncio
async def test_change_password_failure_emits_no_audit_log():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))
    audit_mock = AsyncMock()

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
        patch(  # test-quality-ok: audit sink is a module-level import; no injection seam
            "litellm.proxy.management_endpoints.password_endpoints.create_object_audit_log", audit_mock
        ),
    ):
        with pytest.raises(HTTPException):
            await change_password(
                data=ChangePasswordRequest(current_password="not-the-password", new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_revokes_other_sessions_keeping_callers():
    """A successful change revokes the user's other UI sessions (the old
    password may be compromised) while keeping the session that just proved
    it holds the current password."""
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))
    revoke_mock = AsyncMock(return_value=0)
    caller = UserAPIKeyAuth(
        user_id="user-123",
        token="hashed-caller-token",
        team_id=UI_TEAM_ID,
        metadata=dict(PASSWORD_SESSION_METADATA),
    )

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
        patch(
            "litellm.proxy.management_endpoints.password_endpoints.revoke_ui_session_keys",
            revoke_mock,
        ),
    ):
        await change_password(
            data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
            user_api_key_dict=caller,
            hibp_client=_hibp_client_never_called(),
        )

    revoke_mock.assert_awaited_once()
    revoke_kwargs = revoke_mock.await_args.kwargs
    assert revoke_kwargs["user_id"] == "user-123"
    assert revoke_kwargs["keep_hashed_token"] == "hashed-caller-token"


@pytest.mark.asyncio
async def test_change_password_failure_revokes_no_sessions():
    from litellm.proxy._types import ChangePasswordRequest

    prisma = _make_prisma(_make_user_row(hash_password(CURRENT_PASSWORD)))
    revoke_mock = AsyncMock(return_value=0)

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", prisma
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
        patch(
            "litellm.proxy.management_endpoints.password_endpoints.revoke_ui_session_keys",
            revoke_mock,
        ),
    ):
        with pytest.raises(HTTPException):
            await change_password(
                data=ChangePasswordRequest(current_password="not-the-password", new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    revoke_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_password_requires_db():
    from litellm.proxy._types import ChangePasswordRequest

    with (
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.prisma_client", None
        ),
        patch(  # test-quality-ok: change_password reads proxy_server module globals; no injection seam
            "litellm.proxy.proxy_server.general_settings", _POLICY_NO_BREACH_CHECK
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await change_password(
                data=ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD),
                user_api_key_dict=_caller(),
                hibp_client=_hibp_client_never_called(),
            )

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_present", (True, False))
async def test_audited_password_change_uses_fresh_session_and_archives_on_the_same_native_transaction(
    monkeypatch: pytest.MonkeyPatch, credential_present: bool,
) -> None:
    from litellm.proxy import proxy_server
    from litellm.proxy._types import ChangePasswordRequest

    stored: Final = hash_password(CURRENT_PASSWORD)
    prisma: Final = _make_prisma(_make_user_row(stored))
    prisma.writer_db.litellm_usertable.find_first = AsyncMock(return_value=_make_user_row(stored))
    transaction: Final = MagicMock()
    transaction.execute_raw = AsyncMock(return_value=1)
    transaction.litellm_deletedverificationtoken.create_many = AsyncMock(return_value=1)

    async def query(sql: str, *args: object) -> object:
        if 'FROM "LiteLLM_UserTable"' in sql:
            return [{"user_id": "user-123", "user_role": "internal_user", "password": stored}]
        if 'FROM "LiteLLM_VerificationToken" WHERE token=' in sql:
            return [{"user_id": "user-123", "team_id": UI_TEAM_ID, "active": True,
                     "metadata": dict(PASSWORD_SESSION_METADATA), "allowed_routes": []}] if credential_present else []
        if 'DELETE FROM "LiteLLM_VerificationToken"' in sql:
            return [{"token": "fixture-revoked-session", "user_id": "user-123", "team_id": UI_TEAM_ID}]
        if "SELECT source_id,retained_count" in sql:
            return [{"source_id": "cb600afc-3675-4fcd-bc06-fac693e46ef7", "retained_count": 0}]
        return []

    transaction.query_raw = AsyncMock(side_effect=query)
    prisma.tx.return_value.__aenter__ = AsyncMock(return_value=transaction)
    prisma.tx.return_value.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setenv("RETAIL_OPERATION_AUDIT_ENABLED", "true")
    monkeypatch.setenv("RETAIL_NATIVE_AUDIT_ENABLED", "true")
    monkeypatch.setattr(proxy_server, "prisma_client", prisma)
    monkeypatch.setattr(proxy_server, "general_settings", _POLICY_NO_BREACH_CHECK)
    caller: Final = _caller().model_copy(update={"token": "fixture-caller-session"})
    data: Final = ChangePasswordRequest(current_password=CURRENT_PASSWORD, new_password=NEW_PASSWORD)
    if not credential_present:
        with pytest.raises(HTTPException) as denied:
            await change_password(data=data, user_api_key_dict=caller, hibp_client=_hibp_client_never_called())
        assert denied.value.status_code == 403
        transaction.litellm_deletedverificationtoken.create_many.assert_not_awaited()
        assert not any('UPDATE "LiteLLM_UserTable"' in call.args[0] for call in transaction.execute_raw.await_args_list)
        return
    result: Final = await change_password(data=data, user_api_key_dict=caller, hibp_client=_hibp_client_never_called())
    assert result.user_id == "user-123"
    prisma.db.litellm_usertable.update.assert_not_awaited()
    records: Final = transaction.litellm_deletedverificationtoken.create_many.await_args.kwargs["data"]
    assert len(records) == 1 and records[0]["token"] == "fixture-revoked-session"
    assert records[0]["deleted_by"] == "user-123"
    assert records[0]["deleted_at"] is not None
    updates: Final = tuple(call.args for call in transaction.execute_raw.await_args_list if 'UPDATE "LiteLLM_UserTable"' in call.args[0])
    assert len(updates) == 1 and verify_password(NEW_PASSWORD, updates[0][2])
    outbox: Final = tuple(call.args for call in transaction.query_raw.await_args_list if "INSERT INTO retail_native_audit_outbox" in call.args[0])
    assert len(outbox) == 2
    assert CURRENT_PASSWORD not in str(outbox) and NEW_PASSWORD not in str(outbox) and stored not in str(outbox)
