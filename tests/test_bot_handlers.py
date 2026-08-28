"""Tests for bot command handlers and pure formatters that were previously
only exercised indirectly — access gating, invite code flow, and source-name
parsing for the URL pipeline.

These cover the Telegram-facing contract without requiring a live bot token:
handlers are driven with mocked Update/Context objects.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from access_control import AccessStatus, RequestOutcome
from bot import (
    AudioBot,
    access_gate_text,
    access_request_keyboard,
    admin_access_keyboard,
    format_pending_access,
    format_welcome,
    main_menu,
    pending_access_keyboard,
    source_name_from_url,
)


# ── Source name parsing (URL pipeline) ──────────────────────────────────────


def test_source_name_strips_www_and_keeps_registrable_domain() -> None:
    assert source_name_from_url("https://www.splice.com/sounds") == "splice.com"
    assert source_name_from_url("https://cdn.example.com/audio/a.mp3") == "example.com"
    assert source_name_from_url("https://looperman.com/loops/1") == "looperman.com"


def test_source_name_handles_second_level_country_suffixes() -> None:
    assert source_name_from_url("https://sounds.bandcamp.com/track/x") == "bandcamp.com"
    assert source_name_from_url("https://www.freesound.org/s/1") == "freesound.org"


def test_source_name_falls_back_to_hostname() -> None:
    assert source_name_from_url("http://localhost:8000/audio") == "localhost"
    assert source_name_from_url("not a url") == "web"


# ── Access gate messaging ────────────────────────────────────────────────────


def test_access_gate_text_covers_every_status() -> None:
    assert "CHỜ DUYỆT" in access_gate_text(AccessStatus.PENDING)
    assert "BỊ CHẶN" in access_gate_text(AccessStatus.BLOCKED)
    assert "TỪ CHỐI" in access_gate_text(AccessStatus.REJECTED)
    assert "THU HỒI" in access_gate_text(AccessStatus.REVOKED)
    assert "ĐƯỢC DUYỆT" in access_gate_text(AccessStatus.APPROVED)
    assert "mã mời" in access_gate_text(None)


def test_access_request_keyboard_only_for_denied_states() -> None:
    assert access_request_keyboard(AccessStatus.APPROVED) is None
    assert access_request_keyboard(AccessStatus.PENDING) is None
    for status in (None, AccessStatus.REJECTED, AccessStatus.REVOKED):
        assert access_request_keyboard(status) is not None


def test_admin_access_keyboard_actions_match_status() -> None:
    buttons = admin_access_keyboard(7, AccessStatus.PENDING).inline_keyboard[0]
    labels = [b.text for b in buttons]
    assert labels == ["✅ Duyệt", "❌ Từ chối", "⛔ Chặn"]

    revoked_buttons = admin_access_keyboard(7, AccessStatus.APPROVED).inline_keyboard[0]
    assert [b.text for b in revoked_buttons] == ["🔒 Thu hồi", "⛔ Chặn"]


def test_pending_access_keyboard_and_formatting() -> None:
    user = SimpleNamespace(
        telegram_user_id=42,
        full_name="Minh Hieu",
        username="mhproducer",
    )
    keyboard = pending_access_keyboard([user])
    assert keyboard.inline_keyboard[0][0].text == "✅ Minh Hieu"

    text = format_pending_access([user])
    assert "Minh Hieu" in text
    assert "42" in text

    empty_text = format_pending_access([])
    assert "không có yêu cầu" in empty_text.lower()


# ── Menu building ────────────────────────────────────────────────────────────


def test_main_menu_shows_admin_only_actions() -> None:
    public = main_menu(is_admin=False)
    public_labels = [b.text for row in public.inline_keyboard for b in row]
    assert "🔑 Tạo mã mời" not in public_labels
    assert "👥 Xét duyệt người dùng" not in public_labels

    admin = main_menu(is_admin=True)
    admin_labels = [b.text for row in admin.inline_keyboard for b in row]
    assert "🔑 Tạo mã mời" in admin_labels
    assert "👥 Xét duyệt người dùng" in admin_labels


def test_welcome_is_download_first_flow() -> None:
    welcome = format_welcome()
    assert "MH - DOWNSAMPLE PRO" in welcome
    assert "liên kết" in welcome.lower()


# ── cmd_start access gate (critical path) ──────────────────────────────────


def test_cmd_start_blocks_unapproved_users() -> None:
    """cmd_start must reply with access-gate text for a user without APPROVED status."""
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    context = SimpleNamespace(args=[])
    bot = object.__new__(AudioBot)
    bot.access_control = SimpleNamespace(
        status_for=lambda uid: AccessStatus.PENDING,
    )

    import asyncio
    asyncio.run(bot.cmd_start(update, context))

    sent = reply_text.await_args.args[0]
    assert "CHỜ DUYỆT" in sent


def test_cmd_start_shows_menu_for_approved_user() -> None:
    """An approved user should see the welcome menu."""
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=999),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    context = SimpleNamespace(args=[])
    bot = object.__new__(AudioBot)
    bot.access_control = SimpleNamespace(status_for=lambda uid: AccessStatus.APPROVED)

    import asyncio
    asyncio.run(bot.cmd_start(update, context))

    sent = reply_text.await_args.args[0]
    assert "MH - DOWNSAMPLE PRO" in sent
    # Non-admin user sees public buttons only
    markup = reply_text.await_args.kwargs["reply_markup"]
    admin_labels = {"🔑 Tạo mã mời", "👥 Xét duyệt người dùng"}
    buttons = {b.text for row in markup.inline_keyboard for b in row}
    assert not admin_labels.intersection(buttons), "admin buttons must not appear for non-admin"
