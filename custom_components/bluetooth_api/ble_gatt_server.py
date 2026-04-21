"""BLE GATT server that bridges the HA WebSocket API over Bluetooth Low Energy."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import aiohttp
from bleak import BleakError  # type: ignore[import]

try:
    from bless import BlessServer, BlessGATTCharacteristic  # type: ignore[import]
    HAS_BLESS = True
except ImportError:
    HAS_BLESS = False

from homeassistant.core import HomeAssistant

from .protocol import (
    BLE_CHUNK_CONTINUES,
    BLE_CHUNK_FINAL,
    BLE_MAX_PAYLOAD,
    decode_ble_chunks,
)

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Custom Service / Characteristic UUIDs – must match the Android app constants.
HA_BLE_SERVICE_UUID = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1234"
HA_BLE_TX_UUID = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1235"  # HA → Android (notify)
HA_BLE_RX_UUID = "a10d4b1c-bf45-4c2a-9c32-4a8f7e3d1236"  # Android → HA (write)


class BleGattServer:
    """Exposes the HA WebSocket API over a BLE GATT service.

    Requires the ``bless`` library (``pip install bless``).
    Each Android client is served by forwarding frames to/from the local HA WebSocket.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._server: "BlessServer | None" = None
        self._chunk_buffer: list[bytes] = []
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Start the BLE GATT server."""
        if not HAS_BLESS:
            _LOGGER.warning(
                "BLE GATT server requires the 'bless' library. "
                "Install it with: pip install bless"
            )
            return

        self._server = BlessServer(name="Home Assistant")
        await self._server.add_new_service(HA_BLE_SERVICE_UUID)

        # TX characteristic – notify (HA → Android)
        await self._server.add_new_characteristic(
            HA_BLE_SERVICE_UUID,
            HA_BLE_TX_UUID,
            {"notify"},
            None,
            0,
        )
        # RX characteristic – write without response (Android → HA)
        await self._server.add_new_characteristic(
            HA_BLE_SERVICE_UUID,
            HA_BLE_RX_UUID,
            {"write-without-response"},
            None,
            0,
        )
        self._server.write_request_func = self._on_write
        await self._server.start()
        _LOGGER.info("HA Bluetooth API (BLE GATT) started, service UUID=%s", HA_BLE_SERVICE_UUID)

        # Open a local HA WebSocket session for bridging
        await self._open_local_ws()

    async def stop(self) -> None:
        """Stop the BLE GATT server."""
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()
        if self._server:
            await self._server.stop()
        _LOGGER.info("HA Bluetooth API (BLE GATT) stopped")

    async def _open_local_ws(self) -> None:
        """Connect to the local HA WebSocket API for bridging."""
        ws_url = f"ws://127.0.0.1:{self._hass.config.api.port}/api/websocket"  # type: ignore[union-attr]
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(ws_url)
        asyncio.ensure_future(self._ws_read_loop())

    async def _ws_read_loop(self) -> None:
        """Forward HA WebSocket messages → BLE TX notifications."""
        if not self._ws or not self._server:
            return
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._send_ble_frame(msg.data.encode())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _send_ble_frame(self, data: bytes) -> None:
        """Split *data* into BLE chunks and send as TX notifications."""
        if not self._server:
            return
        offset = 0
        while offset < len(data):
            end = min(offset + BLE_MAX_PAYLOAD, len(data))
            flag = BLE_CHUNK_FINAL if end == len(data) else BLE_CHUNK_CONTINUES
            chunk = bytes([flag]) + data[offset:end]
            try:
                await self._server.update_value(HA_BLE_SERVICE_UUID, HA_BLE_TX_UUID, chunk)
            except BleakError as exc:
                _LOGGER.debug("BLE send error: %s", exc)
                break
            offset = end

    def _on_write(
        self,
        characteristic: "BlessGATTCharacteristic",
        value: bytearray,
    ) -> None:
        """Receive a chunk from the Android client and forward complete frames to HA WS."""
        if characteristic.uuid.lower() != HA_BLE_RX_UUID:
            return
        data = bytes(value)
        if not data:
            return
        self._chunk_buffer.append(data)
        flag = data[0]
        if flag == BLE_CHUNK_FINAL:
            frame = decode_ble_chunks(self._chunk_buffer)
            self._chunk_buffer.clear()
            asyncio.ensure_future(self._forward_to_ws(frame))

    async def _forward_to_ws(self, frame: bytes) -> None:
        """Forward a complete BLE frame to the local HA WebSocket."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(frame.decode())
