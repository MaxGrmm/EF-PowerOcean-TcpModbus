"""Tests for integration setup helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import ef_powerocean_tcpmodbus as integration
from ef_powerocean_tcpmodbus.const import DOMAIN


def test_creates_translated_modbus_warning(monkeypatch) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(language="en"))
    entry = SimpleNamespace(entry_id="test-entry")
    coordinator = SimpleNamespace(is_modbus_disabled=True)
    translations = {
        f"component.{DOMAIN}.config.step.warning.title": "Warning title",
        f"component.{DOMAIN}.config.step.warning.description": "Warning body",
    }
    get_translations = AsyncMock(return_value=translations)
    create = Mock()
    dismiss = Mock()
    monkeypatch.setattr(integration, "async_get_translations", get_translations)
    monkeypatch.setattr(integration.persistent_notification, "async_create", create)
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", dismiss)

    asyncio.run(integration._async_show_modbus_warning(hass, entry, coordinator))

    create.assert_called_once_with(
        hass,
        "Warning body",
        title="Warning title",
        notification_id=f"{DOMAIN}_test-entry_modbus_warning",
    )
    dismiss.assert_not_called()


def test_dismisses_modbus_warning_when_condition_clears(monkeypatch) -> None:
    hass = SimpleNamespace(config=SimpleNamespace(language="en"))
    entry = SimpleNamespace(entry_id="test-entry")
    coordinator = SimpleNamespace(is_modbus_disabled=False)
    get_translations = AsyncMock()
    create = Mock()
    dismiss = Mock()
    monkeypatch.setattr(integration, "async_get_translations", get_translations)
    monkeypatch.setattr(integration.persistent_notification, "async_create", create)
    monkeypatch.setattr(integration.persistent_notification, "async_dismiss", dismiss)

    asyncio.run(integration._async_show_modbus_warning(hass, entry, coordinator))

    dismiss.assert_called_once_with(
        hass,
        f"{DOMAIN}_test-entry_modbus_warning",
    )
    create.assert_not_called()
    get_translations.assert_not_awaited()
