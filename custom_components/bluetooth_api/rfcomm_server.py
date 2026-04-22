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
_HAS_SDPTOOL = shutil.which("sdptool") is not None


class RfcommServer:
    """Accepts RFCOMM connections and bridges them to the local HA WebSocket API."""

    def __init__(self, hass: HomeAssistant, channel: int = RFCOMM_CHANNEL) -> None:
        self._hass = hass
        self._channel = channel
        self._server_sock: socket.socket | None = None
        self._running = False
        self._accept_task: asyncio.Task | None = None
        self._agent_proc: asyncio.subprocess.Process | None = None

    async def _run_cmd(self, cmd: list[str]) -> None:
        """Run a subprocess command, ignoring errors."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        except OSError as exc:
            _LOGGER.debug("Command %s failed: %s", cmd, exc)

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
        if enabled:
            await self._run_cmd(["bluetoothctl", "power", "on"])
            # Disable timeout so the adapter stays discoverable indefinitely.
            await self._run_cmd(["bluetoothctl", "discoverable-timeout", "0"])
        await self._run_cmd(["bluetoothctl", "discoverable", flag])
        await self._run_cmd(["bluetoothctl", "pairable", flag])

    async def _register_sdp(self) -> None:
        """Register an SPP SDP record so Android can discover the RFCOMM service."""
        if not _HAS_SDPTOOL:
            _LOGGER.debug("sdptool not found – skipping SDP registration")
            return
        await self._run_cmd(["sdptool", "add", f"--channel={self._channel}", "SP"])
        _LOGGER.debug("Registered SDP SPP record on channel %d", self._channel)

    async def _unregister_sdp(self) -> None:
        """Remove the SPP SDP record (best-effort)."""
        if not _HAS_SDPTOOL:
            return
        # sdptool del <handle> – retrieve handle first
        try:
            proc = await asyncio.create_subprocess_exec(
                "sdptool", "browse", "local",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
            handle: str | None = None
            lines = stdout.decode(errors="replace").splitlines()
            for i, line in enumerate(lines):
                if "Serial Port" in line:
                    for prev in lines[max(0, i - 5):i]:
                        if "Service RecHandle:" in prev:
                            handle = prev.split(":")[-1].strip()
                            break
                    break
            if handle:
                await self._run_cmd(["sdptool", "del", handle])
        except OSError:
            pass

    async def _start_pairing_agent(self) -> None:
        """Start a bluetoothctl DisplayYesNo agent.

        Uses Numeric Comparison: Android shows a 6-digit passkey that the user
        confirms on Android; the Pi auto-confirms and logs the passkey + creates
        a HA persistent notification so the user can verify it.
        """
        if not _HAS_BLUETOOTHCTL:
            return
        try:
            self._agent_proc = await asyncio.create_subprocess_exec(
                "bluetoothctl",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            assert self._agent_proc.stdin  # noqa: S101
            self._agent_proc.stdin.write(b"agent DisplayYesNo\n")
            self._agent_proc.stdin.write(b"default-agent\n")
            await self._agent_proc.stdin.drain()
            asyncio.ensure_future(self._agent_read_loop())
            _LOGGER.debug("Bluetooth pairing agent started (DisplayYesNo)")
        except OSError as exc:
            _LOGGER.debug("Could not start pairing agent: %s", exc)

    async def _agent_read_loop(self) -> None:
        """Read bluetoothctl output and handle pairing requests automatically."""
        proc = self._agent_proc
        if not proc or not proc.stdout or not proc.stdin:
            return
        stdout = proc.stdout
        stdin = proc.stdin
        async for raw in stdout:
            if self._agent_proc is None:
                break
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            # Ignore high-frequency BLE scan noise (RSSI updates, device changes).
            if "[CHG] Device" in line or "[NEW] Device" in line or "[DEL] Device" in line:
                continue

            if "Confirm passkey" in line:
                # Format: "[agent] Confirm passkey 123456 (yes/no):"
                parts = line.split()
                passkey = next(
                    (p for p in parts if p.isdigit() and len(p) == 6), "??????"
                )
                _LOGGER.info(
                    "Bluetooth pairing – confirm passkey %s on your Android device",
                    passkey,
                )
                self._hass.components.persistent_notification.async_create(
                    f"Bluetooth pairing in progress.\n\n"
                    f"Confirm passkey **{passkey}** on your Android device.",
                    title="Bluetooth Pairing",
                    notification_id="bluetooth_api_pairing",
                )
                stdin.write(b"yes\n")
                await stdin.drain()

            elif "Request PIN" in line or "Request Passkey" in line:
                # Legacy pairing fallback – use a fixed PIN logged for the user.
                pin = b"000000"
                _LOGGER.info(
                    "Bluetooth legacy pairing – enter PIN %s on your Android device",
                    pin.decode(),
                )
                self._hass.components.persistent_notification.async_create(
                    f"Bluetooth legacy pairing.\n\nEnter PIN **{pin.decode()}** on your Android device.",
                    title="Bluetooth Pairing",
                    notification_id="bluetooth_api_pairing",
                )
                stdin.write(pin + b"\n")
                await stdin.drain()

    async def _stop_pairing_agent(self) -> None:
        """Terminate the bluetoothctl pairing agent subprocess."""
        if self._agent_proc and self._agent_proc.returncode is None:
            try:
                assert self._agent_proc.stdin  # noqa: S101
                self._agent_proc.stdin.write(b"quit\n")
                await self._agent_proc.stdin.drain()
            except OSError:
                pass
            self._agent_proc.terminate()
            self._agent_proc = None

    async def start(self) -> None:
        """Make the adapter discoverable, register SDP, then bind the RFCOMM socket."""
        await self._set_discoverable(True)
        await self._start_pairing_agent()
        await self._register_sdp()
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
        await self._unregister_sdp()
        await self._stop_pairing_agent()
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
