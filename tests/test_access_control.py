from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from access_control import AccessControlStore, AccessStatus, RequestOutcome
from bot import AudioBot

NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)


def _submit(
    store: AccessControlStore,
    user_id: int,
    code: str,
    *,
    username: str | None = None,
) -> RequestOutcome:
    return store.submit_request(
        telegram_user_id=user_id,
        username=username,
        full_name=f"User {user_id}",
        invite_code=code,
        now=NOW,
    )


def test_access_database_is_separate_from_audio_library_database(tmp_path) -> None:
    audio_db = tmp_path / "database.db"
    access_db = tmp_path / "access-control.db"
    store = AccessControlStore(access_db)

    with sqlite3.connect(store.db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }

    assert store.db_path != audio_db
    assert {"access_users", "invite_codes"}.issubset(tables)
    assert "files" not in tables


def test_invite_code_is_hashed_and_can_only_be_used_once(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="ABCD1234", now=NOW)

    with sqlite3.connect(store.db_path) as connection:
        stored_hash = connection.execute("SELECT code_hash FROM invite_codes").fetchone()[0]

    assert code == "ABCD-1234"
    assert "ABCD1234" not in stored_hash
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    assert _submit(store, 1002, code) is RequestOutcome.INVALID_INVITE
    assert store.status_for(1001) is AccessStatus.PENDING
    assert store.status_for(1002) is None


def test_expired_invite_cannot_create_request(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(
        created_by=999,
        code="EXPR1234",
        ttl=timedelta(hours=1),
        now=NOW,
    )

    outcome = store.submit_request(
        telegram_user_id=1001,
        username=None,
        full_name="Expired User",
        invite_code=code,
        now=NOW + timedelta(hours=2),
    )

    assert outcome is RequestOutcome.INVALID_INVITE
    assert store.status_for(1001) is None


def test_admin_can_approve_revoke_and_block_without_touching_audio_data(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="STAT1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED

    assert store.set_status(1001, AccessStatus.APPROVED, decided_by=999, now=NOW)
    assert store.status_for(1001) is AccessStatus.APPROVED
    assert store.set_status(1001, AccessStatus.REVOKED, decided_by=999, now=NOW)
    assert store.status_for(1001) is AccessStatus.REVOKED
    assert store.set_status(1001, AccessStatus.BLOCKED, decided_by=999, now=NOW)
    assert store.status_for(1001) is AccessStatus.BLOCKED

    second_code = store.create_invite(created_by=999, code="NEXT1234", now=NOW)
    assert _submit(store, 1001, second_code) is RequestOutcome.BLOCKED
    assert _submit(store, 1002, second_code) is RequestOutcome.CREATED


def test_rejected_user_can_submit_a_fresh_invite_for_review(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    first_code = store.create_invite(created_by=999, code="FIRST123", now=NOW)
    second_code = store.create_invite(created_by=999, code="SECOND12", now=NOW)
    assert _submit(store, 1001, first_code) is RequestOutcome.CREATED
    assert store.set_status(1001, AccessStatus.REJECTED, decided_by=999, now=NOW)

    assert _submit(store, 1001, second_code) is RequestOutcome.CREATED
    assert store.status_for(1001) is AccessStatus.PENDING


def test_pending_user_does_not_consume_another_invite(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    first_code = store.create_invite(created_by=999, code="PEND1234", now=NOW)
    spare_code = store.create_invite(created_by=999, code="SPAR1234", now=NOW)
    assert _submit(store, 1001, first_code) is RequestOutcome.CREATED

    assert _submit(store, 1001, spare_code) is RequestOutcome.ALREADY_PENDING
    assert _submit(store, 1002, spare_code) is RequestOutcome.CREATED


@pytest.mark.asyncio
async def test_request_notifies_admin_with_approve_reject_and_block_buttons(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="REQS1234", now=NOW)
    reply_text = AsyncMock()
    send_message = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001, username="producer", full_name="Test Producer"),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    context = SimpleNamespace(args=[code], bot=SimpleNamespace(send_message=send_message))
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.cmd_request_access(update, context)

    assert store.status_for(1001) is AccessStatus.PENDING
    send_message.assert_awaited_once()
    assert send_message.await_args.kwargs["chat_id"] == 999
    assert "<code>1001</code>" in send_message.await_args.kwargs["text"]
    keyboard = send_message.await_args.kwargs["reply_markup"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["✅ Duyệt", "❌ Từ chối", "⛔ Chặn"]


@pytest.mark.asyncio
async def test_admin_command_creates_a_usable_one_time_invite(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.cmd_create_invite(update, SimpleNamespace(args=["12"]))

    response = reply_text.await_args.args[0]
    code = response.split("<code>", 1)[1].split("</code>", 1)[0]
    assert "Hết hạn sau 12 giờ" in response
    assert (
        store.submit_request(
            telegram_user_id=1001,
            username="invited",
            full_name="Invited User",
            invite_code=code,
        )
        is RequestOutcome.CREATED
    )


@pytest.mark.asyncio
async def test_unapproved_user_cannot_enter_frozen_url_pipeline(tmp_path) -> None:
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = AccessControlStore(tmp_path / "access.db")
    instance.handle_url = AsyncMock()
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_authorized_url(update, None)

    instance.handle_url.assert_not_awaited()
    assert "BOT CHỈ DÀNH CHO NGƯỜI ĐÃ ĐƯỢC DUYỆT" in reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_revoked_user_loses_access_on_the_next_message(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="LIVE1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    assert store.set_status(1001, AccessStatus.APPROVED, decided_by=999, now=NOW)
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store
    instance.handle_url = AsyncMock()
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_authorized_url(update, None)
        assert store.set_status(1001, AccessStatus.REVOKED, decided_by=999, now=NOW)
        await instance.handle_authorized_url(update, None)

    instance.handle_url.assert_awaited_once_with(update, None)
    assert "QUYỀN SỬ DỤNG ĐÃ BỊ THU HỒI" in reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_non_admin_cannot_use_forged_admin_callback(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="SAFE1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    answer = AsyncMock()
    edit_message_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001),
        callback_query=SimpleNamespace(
            data="access:admin:approve:1001:pending",
            answer=answer,
            edit_message_text=edit_message_text,
        ),
    )
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_access_callback(update, SimpleNamespace())

    assert store.status_for(1001) is AccessStatus.PENDING
    answer.assert_awaited_once_with("Bạn không có quyền quản trị.", show_alert=True)
    edit_message_text.assert_not_awaited()


@pytest.mark.parametrize(
    ("action", "expected_status"),
    [
        ("approve", AccessStatus.APPROVED),
        ("reject", AccessStatus.REJECTED),
        ("block", AccessStatus.BLOCKED),
    ],
)
@pytest.mark.asyncio
async def test_admin_pending_buttons_apply_each_decision(
    tmp_path: Path,
    action: str,
    expected_status: AccessStatus,
) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="ACTN1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    query = SimpleNamespace(
        data=f"access:admin:{action}:1001:pending",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), callback_query=query)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_access_callback(update, context)

    assert store.status_for(1001) is expected_status


@pytest.mark.asyncio
async def test_stale_admin_button_cannot_overwrite_newer_block(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="STAL1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    assert store.set_status(1001, AccessStatus.BLOCKED, decided_by=999, now=NOW)
    query = SimpleNamespace(
        data="access:admin:approve:1001:pending",
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )
    update = SimpleNamespace(effective_user=SimpleNamespace(id=999), callback_query=query)
    context = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock()))
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_access_callback(update, context)

    assert store.status_for(1001) is AccessStatus.BLOCKED
    assert "TRẠNG THÁI ĐÃ THAY ĐỔI" in query.edit_message_text.await_args.args[0]
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_admin_status_command_ignores_another_users_id(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    own_code = store.create_invite(created_by=999, code="OWNR1234", now=NOW)
    other_code = store.create_invite(created_by=999, code="OTHR1234", now=NOW)
    assert _submit(store, 1001, own_code) is RequestOutcome.CREATED
    assert _submit(store, 2002, other_code) is RequestOutcome.CREATED
    assert store.set_status(2002, AccessStatus.APPROVED, decided_by=999, now=NOW)
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.cmd_access_status(update, SimpleNamespace(args=["2002"]))

    response = reply_text.await_args.args[0]
    assert "Đang chờ duyệt" in response
    assert "2002" not in response
    assert "Đã được duyệt" not in response


@pytest.mark.asyncio
async def test_admin_callback_revokes_approved_user(tmp_path) -> None:
    store = AccessControlStore(tmp_path / "access.db")
    code = store.create_invite(created_by=999, code="RVOK1234", now=NOW)
    assert _submit(store, 1001, code) is RequestOutcome.CREATED
    assert store.set_status(1001, AccessStatus.APPROVED, decided_by=999, now=NOW)
    answer = AsyncMock()
    edit_message_text = AsyncMock()
    send_message = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        callback_query=SimpleNamespace(
            data="access:admin:revoke:1001:approved",
            answer=answer,
            edit_message_text=edit_message_text,
        ),
    )
    context = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    instance = AudioBot.__new__(AudioBot)
    instance.access_control = store

    with patch("bot.ADMIN_USER_ID", 999):
        await instance.handle_access_callback(update, context)

    assert store.status_for(1001) is AccessStatus.REVOKED
    assert "Đã bị thu hồi" in edit_message_text.await_args.args[0]
    send_message.assert_awaited_once()
    assert send_message.await_args.kwargs["chat_id"] == 1001
