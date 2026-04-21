"""Tests for bluetooth_api __init__.py."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant.components.bluetooth_api.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from tests.common import MockConfigEntry


async def test_setup_entry_rfcomm_only(
    hass: HomeAssistant,
    mock_rfcomm_server,  # noqa: ANN001
) -> None:
    """Setup with RFCOMM enabled starts the RFCOMM server."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"rfcomm_enabled": True, "rfcomm_channel": 1, "ble_enabled": False},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.async_setup(entry.entry_id)
    assert result is True
    mock_rfcomm_server.start.assert_awaited_once()


async def test_setup_entry_rfcomm_start_failure(hass: HomeAssistant) -> None:
    """Setup fails gracefully when RFCOMM socket cannot be created."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"rfcomm_enabled": True, "rfcomm_channel": 1, "ble_enabled": False},
    )
    entry.add_to_hass(hass)

    with patch(
        "homeassistant.components.bluetooth_api.RfcommServer.start",
        side_effect=OSError("RFCOMM not available"),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)

    assert result is False


async def test_unload_entry_stops_servers(
    hass: HomeAssistant,
    mock_rfcomm_server,  # noqa: ANN001
) -> None:
    """Unloading the entry stops all BT servers."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"rfcomm_enabled": True, "rfcomm_channel": 1, "ble_enabled": False},
    )
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)

    await hass.config_entries.async_unload(entry.entry_id)
    mock_rfcomm_server.stop.assert_awaited_once()


async def test_setup_entry_ble_missing_bless(hass: HomeAssistant) -> None:
    """Setup with BLE enabled logs a warning when bless is not installed."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"rfcomm_enabled": False, "rfcomm_channel": 1, "ble_enabled": True},
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.bluetooth_api.ble_gatt_server.HAS_BLESS",
            False,
        ),
    ):
        result = await hass.config_entries.async_setup(entry.entry_id)

    # Should still succeed (BLE is optional, just logs a warning)
    assert result is True
