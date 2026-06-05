"""Outbound Telegram send is gated on the bot token alone
(``telegram_send_configured``), decoupled from the webhook/link gate
(``telegram_configured`` = token + username + webhook secret).

Regression for the production bug where the worker was wired with only
``FLOW_TELEGRAM_BOT_TOKEN`` (no username / webhook secret): under the old
``telegram_configured`` gate ``get_telegram_api()`` fell back to the
unconfigured stub, so every reminder dispatched over Telegram failed with
"telegram bot is not configured" and silently never arrived.
"""

from __future__ import annotations

import pytest

from flow_core import telegram_client
from flow_core.config import Settings, get_settings
from flow_core.telegram_client import (
    HttpxTelegramApi,
    _UnconfiguredTelegramApi,
    get_telegram_api,
)


def _settings(**overrides: str) -> Settings:
    """Base settings (jwt/secret from the test env) with the telegram
    fields overridden. ``model_copy`` keeps the concrete ``Settings`` type
    so the computed gate properties recompute from the new values."""
    base = {"telegram_bot_token": "", "telegram_bot_username": "", "telegram_webhook_secret": ""}
    return get_settings().model_copy(update={**base, **overrides})


def test_token_only_is_send_configured_but_not_webhook_configured() -> None:
    s = _settings(telegram_bot_token="123:abc")  # username + webhook secret empty
    assert s.telegram_send_configured is True
    assert s.telegram_configured is False


def test_full_triple_is_both() -> None:
    s = _settings(
        telegram_bot_token="123:abc",
        telegram_bot_username="flow_bot",
        telegram_webhook_secret="wh-secret",
    )
    assert s.telegram_send_configured is True
    assert s.telegram_configured is True


def test_no_token_is_not_send_configured() -> None:
    assert _settings(telegram_bot_token="").telegram_send_configured is False


def test_get_telegram_api_real_with_token_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker case: token present, webhook half absent -> the real API,
    not the fail-closed stub."""
    monkeypatch.setattr(telegram_client, "_override", None)
    monkeypatch.setattr(
        telegram_client, "get_settings", lambda: _settings(telegram_bot_token="123:abc")
    )
    assert isinstance(get_telegram_api(), HttpxTelegramApi)


def test_get_telegram_api_stub_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(telegram_client, "_override", None)
    monkeypatch.setattr(telegram_client, "get_settings", lambda: _settings(telegram_bot_token=""))
    assert isinstance(get_telegram_api(), _UnconfiguredTelegramApi)
