"""RFCOMM Bluetooth server that bridges the HA WebSocket API over Classic Bluetooth."""

from __future__ import annotations

import asyncio
import logging
import shutil
import socket
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.core import HomeAssistant

from .protocol import rfcomm_read_frame, rfcomm_write_frame

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Standard SPP channel; HA uses channel 1 unless overridden.
RFCOMM_CHANNEL: int = 1
RFCOMM_BACKLOG: int = 5

# Same UUID the Android app uses
HA_RFCOMM_UUID = "00001101-0000-1000-8000-00805F9B34FB"

_HAS_BLUETOOTHCTL = shutil.which("bluetoothctl") is not None


class RfcommServer:
    """Accepts RFCOMM connections and bridges them to the local HA WebSocket API."""

    def __init__(self, hass: HomeAssistant, channel: int = RFCOMM_CHANNEL) -> None:
        self._hass = hass
        self._channel = channel
        self._server_sock: socket.socket | None = None
        self._running = False
        self._accept_task: asyncio.Task | None = None

    async def _set_discoverable(self, enabled: bool) -> None:
        """Set the local Bluetooth adapter to discoverable/pairable via bluetoothctl."""
        if not _HAS_BLUETOOTHCTL:
            if enabled:
                _LOGGER.warning(
                    "bluetoothctl not found – cannot set adapter discoverable. "
                    "Pair your Android device with this host manually first."
                )
            return
        flag = "on" if enabled else "off"
        commands: list[list[str]] = []
        if enabled:
            commands.append(["bluetoothctl", "power", "on"])
        commands += [
            ["bluetoothctl", "discoverable", flag],
            ["bluetoothctl", "pairable", flag],
        ]
        for cmd in commands:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await proc.wait()
            except OSError as exc:
                _LOGGER.debug("bluetoothctl command %s failed: %s", cmd, exc)

    async def start(self) -> None:
        """Make the adapter discoverable, then bind the RFCOMM server socket."""
        await self._set_discoverable(True)
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM,  # type: ignore[attr-defined]
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("00:00:00:00:00:00", self._channel))
            sock.listen(RFCOMM_BACKLOG)
            sock.setblocking(False)
            self._server_sock = sock
            self._running = True
            _LOGGER.info("HA Bluetooth API (RFCOMM) listening on channel %d", self._channel)
        except OSError as exc:
            _LOGGER.error("Failed to start RFCOMM server: %s", exc)
            raise

        self._accept_task = asyncio.ensure_future(self._accept_loop())

    async def stop(self) -> None:
        """Stop accepting new connections and restore adapter discoverability."""
        self._running = False
        if self._accept_task:
            self._accept_task.cancel()
        if self._server_sock:
            self._server_sock.close()
            self._server_sock = None
        await self._set_discoverable(False)
        _LOGGER.info("HA Bluetooth API (RFCOMM) stopped")

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running and self._server_sock:
            try:
                client_sock, addr = await loop.sock_accept(self._server_sock)
                _LOGGER.debug("RFCOMM connection from %s", addr)
                asyncio.ensure_future(self._handle_client(client_sock))
            except asyncio.CancelledError:
                break
            except OSError as exc:
                if self._running:
                    _LOGGER.error("RFCOMM accept error: %s", exc)
                break

    async def _handle_client(self, client_sock: socket.socket) -> None:
        """Bridge one RFCOMM client to the local HA WebSocket API."""
        loop = asyncio.get_running_loop()
        reader, writer = await asyncio.open_connection(sock=client_sock)

        ws_url = f"ws://127.0.0.1:{self._hass.config.api.port}/api/websocket"  # type: ignore[union-attr]

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    await asyncio.gather(
                        self._bt_to_ws(reader, ws),
                        self._ws_to_bt(ws, writer),
                    )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("RFCOMM client session ended: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    async def _bt_to_ws(
        reader: asyncio.StreamReader,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Forward BT frames → local HA WebSocket."""
        try:
            while True:
                frame = await rfcomm_read_frame(reader)
                await ws.send_str(frame.decode())
        except asyncio.IncompleteReadError:
            pass  # BT client disconnected

    @staticmethod
    async def _ws_to_bt(
        ws: aiohttp.ClientWebSocketResponse,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Forward local HA WebSocket messages → BT client."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await rfcomm_write_frame(writer, msg.data.encode())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break
