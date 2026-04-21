"""Fixtures for Bluetooth API tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.components.bluetooth_api.const import (
    CONF_BLE_ENABLED,
    CONF_RFCOMM_CHANNEL,
    CONF_RFCOMM_ENABLED,
    DOMAIN,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """Return a mock config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_RFCOMM_ENABLED: True,
            CONF_RFCOMM_CHANNEL: 1,
            CONF_BLE_ENABLED: False,
        },
    )


@pytest.fixture
def mock_rfcomm_server():
    """Mock the RfcommServer to avoid actual Bluetooth socket creation."""
    with patch(
        "homeassistant.components.bluetooth_api.RfcommServer",
        autospec=True,
    ) as mock_cls:
        instance = mock_cls.return_value
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        yield instance
