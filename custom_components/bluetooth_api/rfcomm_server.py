"""RFCOMM Bluetooth server that bridges the HA WebSocket API over Classic Bluetooth.

Uses the BlueZ ProfileManager1 D-Bus API (modern BlueZ ≥ 5.43, no --compat needed)
to register the SPP service record so Android can discover and connect via SDP.
Falls back to sdptool + raw socket if D-Bus profile registration fails.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
from typing import TYPE_CHECKING

import aiohttp

from homeassistant.core import HomeAssistant

from .protocol import rfcomm_read_frame, rfcomm_write_frame

if TYPE_CHECKING:
    pass

_LOGGER = logging.getLogger(__name__)

# Standard SPP UUID – the same UUID the Android app uses for SDP lookup.
HA_RFCOMM_UUID = "00001101-0000-1000-8000-00805F9B34FB"

# RFCOMM channel 1 is the HA default.
RFCOMM_CHANNEL: int = 1
RFCOMM_BACKLOG: int = 5

# D-Bus object path we register for the BlueZ Profile.
_PROFILE_PATH = "/org/homeassistant/bluetooth_api/spp"

_HAS_BLUETOOTHCTL = shutil.which("bluetoothctl") is not None
_HAS_SDPTOOL = shutil.which("sdptool") is not None


def _async_create_notification(hass: HomeAssistant, message: str) -> None:
    """Create a persistent HA notification via the service bus."""
    try:
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "message": message,
                    "title": "Bluetooth Pairing",
                    "notification_id": "bluetooth_api_pairing",
                },
            )
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("Could not create pairing notification")


class RfcommServer:
    """Accepts RFCOMM connections and bridges them to the local HA WebSocket API.

    Connection model (tried in order):
    1. BlueZ ProfileManager1 D-Bus API – registers SPP profile + SDP record; BlueZ
       calls NewConnection with a ready file descriptor when a client connects.
       Works on modern BlueZ ≥ 5.43 without --compat flag.
    2. sdptool + raw socket – legacy fallback for BlueZ in --compat mode.
    """

    def __init__(self, hass: HomeAssistant, channel: int = RFCOMM_CHANNEL) -> None:
        self._hass = hass
        self._channel = channel
        self._running = False

        # Profile mode (D-Bus) state
        self._dbus_bus = None          # dbus_fast MessageBus
        self._profile_iface = None     # _SppProfileInterface
        self._profile_mode = False     # True when using D-Bus profile

        # Socket mode (legacy sdptool) state
        self._server_sock: socket.socket | None = None
        self._accept_task: asyncio.Task | None = None

        # Shared state
        self._agent_proc: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Make adapter discoverable, register SPP, start accepting connections."""
        await self._set_discoverable(True)
        await self._start_pairing_agent()

        # Try modern D-Bus profile registration first.
        if await self._start_dbus_profile():
            self._profile_mode = True
            self._running = True
            _LOGGER.info(
                "HA Bluetooth API (RFCOMM) ready via BlueZ D-Bus profile on channel %d",
                self._channel,
            )
            return

        # Fall back to legacy sdptool + raw socket.
        _LOGGER.info("Falling back to sdptool + raw RFCOMM socket")
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
            _LOGGER.info(
                "HA Bluetooth API (RFCOMM) listening on channel %d (socket mode)",
                self._channel,
            )
        except OSError as exc:
            _LOGGER.error("Failed to start RFCOMM server: %s", exc)
            raise

        self._accept_task = asyncio.ensure_future(self._accept_loop())

    async def stop(self) -> None:
        """Stop accepting connections and restore adapter state."""
        self._running = False
        if self._profile_mode:
            await self._stop_dbus_profile()
        else:
            if self._accept_task:
                self._accept_task.cancel()
            if self._server_sock:
                self._server_sock.close()
                self._server_sock = None
            await self._unregister_sdp()
        await self._stop_pairing_agent()
        await self._set_discoverable(False)
        _LOGGER.info("HA Bluetooth API (RFCOMM) stopped")

    # ------------------------------------------------------------------
    # D-Bus ProfileManager1 (modern BlueZ, no --compat needed)
    # ------------------------------------------------------------------

    async def _start_dbus_profile(self) -> bool:
        """Register SPP profile via BlueZ ProfileManager1. Returns True on success."""
        try:
            from dbus_fast import BusType, Variant  # type: ignore[import]
            from dbus_fast.aio import MessageBus    # type: ignore[import]
            from dbus_fast.service import ServiceInterface, method as dbus_method  # type: ignore[import]
        except ImportError:
            _LOGGER.debug("dbus_fast not available – skipping D-Bus profile registration")
            return False

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Cannot connect to system D-Bus: %s", exc)
            return False

        server_ref = self

        class _SppProfileInterface(ServiceInterface):
            """Implements org.bluez.Profile1 – BlueZ calls NewConnection on RFCOMM connect."""

            def __init__(self) -> None:
                super().__init__("org.bluez.Profile1")

            @dbus_method()
            def Release(self) -> None:
                _LOGGER.debug("SPP Profile: BlueZ released our profile")

            @dbus_method()
            def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}") -> None:  # type: ignore[override]
                _LOGGER.info("SPP: new RFCOMM connection from %s", device)
                try:
                    # fd is the file descriptor passed via SCM_RIGHTS (already dup'd by dbus_fast)
                    duped = os.dup(fd)
                    client_sock = socket.socket(
                        socket.AF_BLUETOOTH,
                        socket.SOCK_STREAM,
                        socket.BTPROTO_RFCOMM,  # type: ignore[attr-defined]
                        fileno=duped,
                    )
                    asyncio.ensure_future(server_ref._handle_client(client_sock))
                except Exception as exc2:  # noqa: BLE001
                    _LOGGER.error("SPP: failed to wrap connection fd: %s", exc2)

            @dbus_method()
            def RequestDisconnection(self, device: "o") -> None:  # type: ignore[override]
                _LOGGER.debug("SPP Profile: disconnect request from %s", device)

        profile = _SppProfileInterface()
        bus.export(_PROFILE_PATH, profile)

        try:
            introspection = await bus.introspect("org.bluez", "/org/bluez")
            proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
            manager = proxy.get_interface("org.bluez.ProfileManager1")
            await manager.call_register_profile(
                _PROFILE_PATH,
                HA_RFCOMM_UUID,
                {
                    "Name": Variant("s", "HA Serial Port"),
                    "Channel": Variant("q", self._channel),
                    "AutoConnect": Variant("b", False),
                    "RequireAuthentication": Variant("b", False),
                    "RequireAuthorization": Variant("b", False),
                },
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BlueZ profile registration failed: %s", exc)
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._dbus_bus = bus
        self._profile_iface = profile
        _LOGGER.debug("SPP D-Bus profile registered at %s", _PROFILE_PATH)
        return True

    async def _stop_dbus_profile(self) -> None:
        """Unregister the BlueZ profile and disconnect from D-Bus."""
        if not self._dbus_bus:
            return
        try:
            from dbus_fast import BusType  # type: ignore[import]  # noqa: F401
            introspection = await self._dbus_bus.introspect("org.bluez", "/org/bluez")
            proxy = self._dbus_bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
            manager = proxy.get_interface("org.bluez.ProfileManager1")
            await manager.call_unregister_profile(_PROFILE_PATH)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Profile unregister: %s", exc)
        try:
            self._dbus_bus.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._dbus_bus = None

    # ------------------------------------------------------------------
    # Legacy sdptool + socket
    # ------------------------------------------------------------------

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

    async def _register_sdp(self) -> None:
        """Register an SPP SDP record via sdptool (requires bluetoothd --compat)."""
        if not _HAS_SDPTOOL:
            _LOGGER.warning(
                "sdptool not found and D-Bus profile registration failed. "
                "Android cannot discover this server via SDP. "
                "Install sdptool or run bluetoothd with --compat flag."
            )
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "sdptool", "add", f"--channel={self._channel}", "SP",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                _LOGGER.warning(
                    "sdptool add failed (exit %d): %s — "
                    "run bluetoothd with --compat flag or switch to D-Bus profile mode",
                    proc.returncode,
                    stderr.decode(errors="replace").strip(),
                )
                return
        except OSError as exc:
            _LOGGER.debug("sdptool failed: %s", exc)
            return

        # Verify the record was actually added.
        try:
            proc = await asyncio.create_subprocess_exec(
                "sdptool", "browse", "local",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            if "Serial Port" in out.decode(errors="replace"):
                _LOGGER.info("SDP SPP record verified on channel %d", self._channel)
            else:
                _LOGGER.warning(
                    "sdptool add returned success but SPP record not found in 'sdptool browse local'. "
                    "bluetoothd may need --compat flag. Android SDP lookup will fail."
                )
        except OSError:
            _LOGGER.debug("sdptool browse local failed (ignoring)")

    async def _unregister_sdp(self) -> None:
        """Remove the SPP SDP record (best-effort)."""
        if not _HAS_SDPTOOL:
            return
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

    async def _accept_loop(self) -> None:
        """Accept loop for legacy socket mode."""
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

    # ------------------------------------------------------------------
    # Adapter control
    # ------------------------------------------------------------------

    async def _set_discoverable(self, enabled: bool) -> None:
        """Set the local Bluetooth adapter to discoverable/pairable."""
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
            await self._run_cmd(["bluetoothctl", "discoverable-timeout", "0"])
        await self._run_cmd(["bluetoothctl", "discoverable", flag])
        await self._run_cmd(["bluetoothctl", "pairable", flag])

    # ------------------------------------------------------------------
    # Pairing agent
    # ------------------------------------------------------------------

    async def _start_pairing_agent(self) -> None:
        """Start a bluetoothctl DisplayYesNo pairing agent."""
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
        """Read bluetoothctl output and auto-confirm pairing requests."""
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
            if "[CHG] Device" in line or "[NEW] Device" in line or "[DEL] Device" in line:
                continue

            _LOGGER.debug("bluetoothctl: %s", line)

            lower_line = line.lower()
            if (
                "confirm passkey" in lower_line
                or "confirm value" in lower_line
                or ("authorize" in lower_line and "yes/no" in lower_line)
            ):
                parts = line.split()
                passkey = next(
                    (p for p in parts if p.isdigit() and len(p) in (4, 6)), "??????"
                )
                _LOGGER.info(
                    "Bluetooth pairing – confirm passkey %s on your Android device",
                    passkey,
                )
                stdin.write(b"yes\n")
                await stdin.drain()
                _async_create_notification(
                    self._hass,
                    f"Bluetooth pairing in progress.\n\n"
                    f"Confirm passkey **{passkey}** on your Android device.",
                )

            elif "request pin" in lower_line or "request passkey" in lower_line:
                pin = b"000000"
                _LOGGER.info(
                    "Bluetooth legacy pairing – enter PIN %s on your Android device",
                    pin.decode(),
                )
                stdin.write(pin + b"\n")
                await stdin.drain()
                _async_create_notification(
                    self._hass,
                    f"Bluetooth legacy pairing.\n\nEnter PIN **{pin.decode()}** on your Android device.",
                )

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

    # ------------------------------------------------------------------
    # Client bridging (shared by both modes)
    # ------------------------------------------------------------------

    async def _handle_client(self, client_sock: socket.socket) -> None:
        """Bridge one RFCOMM client to the local HA WebSocket API."""
        reader, writer = await asyncio.open_connection(sock=client_sock)

        ws_url = f"ws://127.0.0.1:{self._hass.config.api.port}/api/websocket"  # type: ignore[union-attr]
        _LOGGER.info("RFCOMM: bridging client to %s", ws_url)

        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    _LOGGER.debug("RFCOMM: local WS established, starting bridge")
                    await asyncio.gather(
                        self._bt_to_ws(reader, ws),
                        self._ws_to_bt(ws, writer),
                    )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("RFCOMM client session ended: %s", exc)
        finally:
            _LOGGER.info("RFCOMM: client disconnected")
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
                _LOGGER.debug("RFCOMM RX (%d bytes) → WS: %.200s", len(frame), frame.decode(errors="replace"))
                await ws.send_str(frame.decode())
        except asyncio.IncompleteReadError:
            _LOGGER.debug("RFCOMM: client disconnected (IncompleteReadError)")

    @staticmethod
    async def _ws_to_bt(
        ws: aiohttp.ClientWebSocketResponse,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Forward local HA WebSocket messages → BT client."""
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                _LOGGER.debug("WS → RFCOMM TX (%d bytes): %.200s", len(msg.data), msg.data)
                await rfcomm_write_frame(writer, msg.data.encode())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                _LOGGER.debug("RFCOMM WS closed: type=%s", msg.type)
                break
