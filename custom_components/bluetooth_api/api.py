"""REST API view: exposes Bluetooth adapter config to the Android client."""

from __future__ import annotations

import asyncio
import logging
import pathlib
from typing import TYPE_CHECKING

from aiohttp import web

from homeassistant.components.http import HomeAssistantView

from .const import CONF_BLE_ENABLED, CONF_RFCOMM_CHANNEL, CONF_RFCOMM_ENABLED, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class BluetoothApiConfigView(HomeAssistantView):
    """GET /api/bluetooth_api/config – returns adapter address and active transport.

    The Android app calls this (while still on WiFi) to auto-discover the BT
    address and transport type so the user never has to enter them manually.
    """

    url = "/api/bluetooth_api/config"
    name = "api:bluetooth_api:config"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        """Return Bluetooth adapter address and transport config."""
        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            return self.json_message("bluetooth_api is not configured", status_code=404)

        entry = entries[0]
        adapter_address = await _get_adapter_address()

        if entry.data.get(CONF_RFCOMM_ENABLED, True):
            transport = "rfcomm"
            channel: int | None = entry.data.get(CONF_RFCOMM_CHANNEL, 1)
        else:
            transport = "ble"
            channel = None

        return self.json(
            {
                "adapter_address": adapter_address,
                "transport": transport,
                "channel": channel,
            }
        )


async def _get_adapter_address() -> str | None:
    """Return the MAC address of the first available Bluetooth HCI adapter."""
    # Fast path: sysfs (no subprocess, works on Raspberry Pi OS / HA OS)
    try:
        for hci in sorted(pathlib.Path("/sys/class/bluetooth").glob("hci*")):
            addr_file = hci / "address"
            addr = addr_file.read_text().strip()
            if addr and addr != "00:00:00:00:00:00":
                return addr
    except OSError:
        pass

    # Fallback: hciconfig
    try:
        proc = await asyncio.create_subprocess_exec(
            "hciconfig",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        for line in stdout.decode(errors="replace").splitlines():
            if "BD Address:" in line:
                addr = line.split("BD Address:")[1].split()[0].strip()
                if addr:
                    return addr
    except OSError:
        pass

    _LOGGER.warning("Could not determine Bluetooth adapter address")
    return None
