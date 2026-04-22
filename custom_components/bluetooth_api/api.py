"""REST API view: exposes Bluetooth adapter config to the Android client."""

from __future__ import annotations

import asyncio
import logging
import pathlib

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import CONF_RFCOMM_CHANNEL, CONF_RFCOMM_ENABLED, DOMAIN

_LOGGER = logging.getLogger(__name__)


class BluetoothApiConfigView(HomeAssistantView):
    """GET /api/bluetooth_api/config – returns adapter address and active transport."""

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
        # Run blocking sysfs/subprocess I/O in a thread pool executor.
        adapter_address = await hass.async_add_executor_job(_read_adapter_address_sync)
        if adapter_address is None:
            adapter_address = await _read_adapter_address_hciconfig()

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


def _read_adapter_address_sync() -> str | None:
    """Read BT adapter MAC from sysfs (blocking – run in executor)."""
    try:
        for hci in sorted(pathlib.Path("/sys/class/bluetooth").glob("hci*")):
            addr_file = hci / "address"
            addr = addr_file.read_text().strip()
            if addr and addr != "00:00:00:00:00:00":
                return addr
    except OSError:
        pass
    return None


async def _read_adapter_address_hciconfig() -> str | None:
    """Fallback: parse hciconfig output (async subprocess – non-blocking)."""
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
