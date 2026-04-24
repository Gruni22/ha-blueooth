"""The Bluetooth API integration for Home Assistant.

Exposes the HA WebSocket API over Classic Bluetooth (RFCOMM) and/or BLE GATT,
allowing mobile clients to connect without a network connection.

Architecture
────────────
Android App ←─ BT transport ─→ bluetooth_api ←─ local WebSocket ─→ HA Core

The integration acts as a transparent bridge: it accepts raw Bluetooth frames,
reassembles them into complete JSON messages, and forwards them to HA's own
WebSocket API running on localhost.  Responses travel the same path in reverse.

The Android client authenticates using a Long-Lived Access Token exactly as it
would over a normal WebSocket connection.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import BluetoothApiConfigView
from .const import CONF_BLE_ENABLED, CONF_RFCOMM_CHANNEL, CONF_RFCOMM_ENABLED, DOMAIN
from .rfcomm_server import RfcommServer, RfcommTcpTunnel

_LOGGER = logging.getLogger(__name__)

type BluetoothApiConfigEntry = ConfigEntry  # noqa: PYI042


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Bluetooth API from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    rfcomm_enabled: bool = entry.data.get(CONF_RFCOMM_ENABLED, True)
    rfcomm_channel: int = entry.data.get(CONF_RFCOMM_CHANNEL, 1)
    ble_enabled: bool = entry.data.get(CONF_BLE_ENABLED, False)

    servers = []

    if rfcomm_enabled:
        rfcomm = RfcommServer(hass, channel=rfcomm_channel)
        try:
            await rfcomm.start()
            servers.append(rfcomm)
            _LOGGER.info("Bluetooth API RFCOMM server started on channel %d", rfcomm_channel)
        except OSError as exc:
            _LOGGER.error(
                "Failed to start RFCOMM server on channel %d: %s – "
                "check that no other process is using this channel and that "
                "Bluetooth is available on this host",
                rfcomm_channel,
                exc,
            )

        # Always start the TCP tunnel alongside the WS relay so the Android
        # WebView can reach HA's full HTTP/WebSocket API via Bluetooth.
        tcp_tunnel = RfcommTcpTunnel(hass)
        try:
            await tcp_tunnel.start()
            servers.append(tcp_tunnel)
        except OSError as exc:
            _LOGGER.warning(
                "Failed to start RFCOMM TCP tunnel (channel 3): %s – "
                "WebView will not work via Bluetooth without WiFi",
                exc,
            )

    if ble_enabled:
        try:
            from .ble_gatt_server import BleGattServer  # noqa: PLC0415

            ble = BleGattServer(hass)
            await ble.start()
            servers.append(ble)
            _LOGGER.info("Bluetooth API BLE GATT server started")
        except ImportError:
            _LOGGER.warning(
                "BLE GATT server requires the 'bless' library. "
                "Disable BLE in integration settings or install bless."
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.error("Failed to start BLE GATT server: %s", exc)

    hass.data[DOMAIN][entry.entry_id] = servers

    # Register REST endpoint once (guard survives reloads).
    if not hass.data[DOMAIN].get("view_registered"):
        hass.http.register_view(BluetoothApiConfigView())
        hass.data[DOMAIN]["view_registered"] = True

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop all BT servers and unload the config entry."""
    servers = hass.data.get(DOMAIN, {}).pop(entry.entry_id, [])
    for server in servers:
        try:
            await server.stop()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Error stopping BT server during unload: %s", exc)
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the config entry is updated."""
    await hass.config_entries.async_reload(entry.entry_id)
