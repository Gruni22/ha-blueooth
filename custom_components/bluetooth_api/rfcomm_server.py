"""RFCOMM Bluetooth server that bridges the HA WebSocket API over Classic Bluetooth.

Uses the BlueZ ProfileManager1 D-Bus API (modern BlueZ ≥ 5.43, no --compat needed)
to register the SPP service record so Android can discover and connect via SDP.
Falls back to sdptool + raw socket if D-Bus profile registration fails.

Also provides RfcommTcpTunnel (channel 3): a raw TCP-over-RFCOMM bridge so that
the Android WebView can reach HA's HTTP/WebSocket server without WiFi.  Android
discovers the service via SDP UUID lookup (HA_TCP_TUNNEL_UUID) and all bytes
are relayed to localhost:8123.  No framing or protocol knowledge is needed.
"""

# NOTE: do NOT add "from __future__ import annotations" here.
# dbus_fast reads fn.__annotations__ directly; PEP-563 stringification breaks
# the D-Bus type codes ("o", "h", "a{sv}") by double-quoting them, and
# converts "-> None" return annotations to NoneType which parse_annotation rejects.

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
            bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Cannot connect to system D-Bus: %s", exc)
            return False

        server_ref = self

        class _SppProfileInterface(ServiceInterface):
            """Implements org.bluez.Profile1 – BlueZ calls NewConnection on RFCOMM connect."""

            def __init__(self) -> None:
                super().__init__("org.bluez.Profile1")

            @dbus_method()
            def Release(self) -> "":  # '' = D-Bus void; dbus_fast requires a string annotation
                _LOGGER.debug("SPP Profile: BlueZ released our profile")

            @dbus_method()
            def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}") -> "":
                _LOGGER.info("SPP: new RFCOMM connection from %s", device)
                try:
                    # dbus_fast extracts the actual FD from SCM_RIGHTS ancillary data.
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
            def RequestDisconnection(self, device: "o") -> "":
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
            # Send agent registration; default-agent is sent reactively in _agent_read_loop
            # once "Agent registered" is confirmed — avoids the race condition on reload
            # where the old session hasn't released the agent slot yet.
            self._agent_proc.stdin.write(b"agent DisplayYesNo\n")
            await self._agent_proc.stdin.drain()
            asyncio.ensure_future(self._agent_read_loop())
            _LOGGER.debug("Bluetooth pairing agent started (DisplayYesNo)")
        except OSError as exc:
            _LOGGER.debug("Could not start pairing agent: %s", exc)

    async def _agent_read_loop(self) -> None:
        """Read bluetoothctl output and auto-confirm pairing requests.

        IMPORTANT: use read() not readline(). The  `[agent] Confirm passkey (yes/no): `
        prompt has NO trailing newline, so readline() blocks until Android's pairing
        timeout fires (~30 s) — by which time auth already failed. read() returns
        as soon as any bytes arrive, so we always process output promptly.
        """
        proc = self._agent_proc
        if not proc or not proc.stdout or not proc.stdin:
            return
        stdout = proc.stdout
        stdin = proc.stdin
        import re
        _ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
        _confirmed_passkeys: set[str] = set()
        _buf = b""

        while self._agent_proc is not None:
            try:
                chunk = await asyncio.wait_for(stdout.read(4096), timeout=30.0)
            except TimeoutError:
                continue
            if not chunk:
                break
            _buf += chunk
            # Split on newlines but also check tail for no-newline prompt pattern.
            text = _ANSI_RE.sub("", _buf.decode(errors="replace"))
            # Process each logical chunk separated by newlines, preserving incomplete tail.
            parts = text.split("\n")
            _buf = parts[-1].encode()  # keep incomplete tail for next iteration
            # Also check if the tail itself contains an agent prompt (no newline).
            check_parts = parts[:-1] + ([parts[-1]] if parts[-1].strip() else [])

            for raw_part in check_parts:
                line = raw_part.strip()
                if not line:
                    continue
                # Skip high-frequency noise to avoid flooding the HA log buffer.
                if ("CHG] Device" in line or "NEW] Device" in line or "DEL] Device" in line
                        or "RSSI" in line or "ManufacturerData" in line
                        or "discovering" in line.lower()
                        # Controller UUID list changes — verbose at startup, not actionable.
                        or ("Controller" in line and "UUIDs:" in line)
                        # LE Audio / Bluetooth 5.2 service UUIDs (184x) — BLE audio noise.
                        or any(u in line.lower() for u in ("0000184d", "00001845", "00001843", "00001844", "0000184f"))
                        or (len(line) > 3 and all(c in "0123456789abcdef . " for c in line.lower()))):
                    continue

                _LOGGER.debug("bluetoothctl: %s", line)
                lower_line = line.lower()

                if "authorize service" in lower_line:
                    # Service-level authorization (e.g. OBEX, RFCOMM profile access).
                    # "Authorize service" is the notification line that appears BEFORE the
                    # no-newline "(yes/no):" agent prompt — respond early so the buffered
                    # "yes" is consumed before Android's connection timeout fires.
                    _LOGGER.debug("Bluetooth agent: authorizing service connection")
                    stdin.write(b"yes\n")
                    await stdin.drain()

                elif (
                    "confirm passkey" in lower_line
                    or "confirm value" in lower_line
                    or ("authorize" in lower_line and "yes/no" in lower_line)
                    # "Request confirmation" appears BEFORE the (no-newline) agent prompt;
                    # respond here so the pre-buffered "yes" is consumed immediately by
                    # bluetoothctl's stdin read — before Android's pairing timeout fires.
                    or "request confirmation" in lower_line
                ):
                    parts2 = line.split()
                    passkey = next(
                        (p for p in parts2 if p.isdigit() and len(p) in (4, 6)), "??????"
                    )
                    if passkey not in _confirmed_passkeys:
                        _confirmed_passkeys.add(passkey)
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

                elif "agent registered" in lower_line:
                    # BlueZ confirmed our agent registration (may arrive late after reload
                    # race condition where the old agent slot wasn't freed yet).
                    # Now set it as the default so it handles all pairing requests.
                    stdin.write(b"default-agent\n")
                    await stdin.drain()
                    _LOGGER.debug("Bluetooth agent registered — set as default-agent")

                elif "auth failed" in lower_line or "authentication failed" in lower_line:
                    # Clear confirmed set so next attempt can retry.
                    _confirmed_passkeys.clear()

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

    async def _ws_to_bt(
        self,
        ws: aiohttp.ClientWebSocketResponse,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Forward local HA WebSocket messages → BT client."""
        import json as _json
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                data_str = msg.data
                # Inject ha_url into auth_required so BT-only clients can onboard without WiFi.
                try:
                    data = _json.loads(data_str)
                    if data.get("type") == "auth_required":
                        try:
                            api = self._hass.config.api
                            if api is not None:
                                scheme = "https" if getattr(api, "use_ssl", False) else "http"
                                local_ip = getattr(api, "local_ip", "")
                                port = getattr(api, "port", 8123)
                                if local_ip and local_ip not in ("0.0.0.0", ""):
                                    data["ha_url"] = f"{scheme}://{local_ip}:{port}"
                                    data_str = _json.dumps(data)
                        except Exception:  # noqa: BLE001
                            pass
                except (_json.JSONDecodeError, AttributeError):
                    pass
                _LOGGER.debug("WS → RFCOMM TX (%d bytes): %.200s", len(data_str), data_str)
                await rfcomm_write_frame(writer, data_str.encode())
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                _LOGGER.debug("RFCOMM WS closed: type=%s", msg.type)
                break


# ---------------------------------------------------------------------------
# TCP tunnel (channel 3) – raw relay for WebView HTTP + WebSocket
# ---------------------------------------------------------------------------

# UUID for the TCP-over-RFCOMM tunnel service.
# We use OBEX Object Push (0x1105) — a well-known profile UUID that BlueZ
# automatically creates an SDP record for when registered via ProfileManager1.
# Custom UUIDs don't get auto-SDP records; providing ServiceRecord breaks the
# RFCOMM socket setup. OBEX Push is unused on HAOS so there's no conflict.
# Must match HA_TCP_TUNNEL_UUID in BluetoothTcpProxy.kt.
HA_TCP_TUNNEL_UUID = "00001105-0000-1000-8000-00805f9b34fb"

#: RFCOMM channel the TCP tunnel is registered on.
TCP_TUNNEL_CHANNEL: int = 3

#: D-Bus object path for the tunnel's BlueZ profile.
_TUNNEL_PROFILE_PATH = "/org/homeassistant/bluetooth_api/tcp_tunnel"

# BlueZ only auto-generates SDP records for well-known profile UUIDs (e.g. SPP 0x1101).
# For custom UUIDs we must supply the SDP record explicitly so Android can discover
# the RFCOMM channel via createInsecureRfcommSocketToServiceRecord(UUID).
# Attribute IDs: 0x0001=ServiceClassIDList, 0x0004=ProtocolDescriptorList (L2CAP+RFCOMM ch3),
#                0x0009=BluetoothProfileDescriptorList, 0x0100=ServiceName.
_TUNNEL_SDP_RECORD = (
    '<?xml version="1.0" encoding="UTF-8" ?>'
    "<record>"
    '<attribute id="0x0001">'
    "<sequence>"
    f'<uuid value="{HA_TCP_TUNNEL_UUID}"/>'
    "</sequence>"
    "</attribute>"
    '<attribute id="0x0004">'
    "<sequence>"
    "<sequence>"
    '<uuid value="0x0100"/>'
    "</sequence>"
    "<sequence>"
    '<uuid value="0x0003"/>'
    f'<uint8 value="{TCP_TUNNEL_CHANNEL}"/>'
    "</sequence>"
    "</sequence>"
    "</attribute>"
    '<attribute id="0x0009">'
    "<sequence>"
    "<sequence>"
    f'<uuid value="{HA_TCP_TUNNEL_UUID}"/>'
    '<uint16 value="0x0100"/>'
    "</sequence>"
    "</sequence>"
    "</attribute>"
    '<attribute id="0x0100">'
    '<text value="HA TCP Tunnel"/>'
    "</attribute>"
    "</record>"
)


class RfcommTcpTunnel:
    """Raw TCP-over-RFCOMM bridge: pipes RFCOMM connections to localhost:8123.

    Registers a BlueZ D-Bus profile so Android can discover the service via SDP.
    Each RFCOMM client gets its own TCP connection to the local HA server.  Bytes
    flow transparently, allowing the Android WebView to load the HA frontend and
    maintain its WebSocket connection via Bluetooth when WiFi is unavailable.
    """

    def __init__(self, hass: HomeAssistant, channel: int = TCP_TUNNEL_CHANNEL) -> None:
        self._hass = hass
        self._channel = channel
        self._dbus_bus = None
        self._running = False

    async def start(self) -> None:
        """Start the TCP tunnel on a raw RFCOMM socket.

        We use a raw socket instead of a BlueZ D-Bus profile because Android connects
        via createInsecureRfcommSocket(channel) (direct channel, no UUID), which is a raw
        RFCOMM connection.  BlueZ's ProfileManager1 only triggers NewConnection for
        UUID-negotiated connections — raw channel connects bypass the profile layer
        entirely and require a socket that is listening on the channel directly.
        """
        await self._start_raw_socket()

    async def _start_dbus_profile(self) -> bool:
        """Register SPP-variant profile via BlueZ ProfileManager1. Returns True on success."""
        try:
            from dbus_fast import BusType, Variant  # type: ignore[import]
            from dbus_fast.aio import MessageBus    # type: ignore[import]
            from dbus_fast.service import ServiceInterface, method as dbus_method  # type: ignore[import]
        except ImportError:
            return False

        try:
            bus = await MessageBus(bus_type=BusType.SYSTEM, negotiate_unix_fd=True).connect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("TCP tunnel: cannot connect to D-Bus: %s", exc)
            return False

        server_ref = self

        class _TunnelProfileInterface(ServiceInterface):
            def __init__(self) -> None:
                super().__init__("org.bluez.Profile1")

            @dbus_method()
            def Release(self) -> "":
                _LOGGER.debug("TCP tunnel profile released by BlueZ")

            @dbus_method()
            def NewConnection(self, device: "o", fd: "h", fd_properties: "a{sv}") -> "":
                _LOGGER.info("TCP tunnel: new RFCOMM connection from %s", device)
                try:
                    duped = os.dup(fd)
                    client_sock = socket.socket(
                        socket.AF_BLUETOOTH,
                        socket.SOCK_STREAM,
                        socket.BTPROTO_RFCOMM,  # type: ignore[attr-defined]
                        fileno=duped,
                    )
                    asyncio.ensure_future(server_ref._handle_client(client_sock))
                except Exception as exc2:  # noqa: BLE001
                    _LOGGER.error("TCP tunnel: failed to wrap fd: %s", exc2)

            @dbus_method()
            def RequestDisconnection(self, device: "o") -> "":
                pass

        profile = _TunnelProfileInterface()
        bus.export(_TUNNEL_PROFILE_PATH, profile)

        try:
            introspection = await bus.introspect("org.bluez", "/org/bluez")
            proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
            manager = proxy.get_interface("org.bluez.ProfileManager1")
            await manager.call_register_profile(
                _TUNNEL_PROFILE_PATH,
                HA_TCP_TUNNEL_UUID,
                {
                    "Name": Variant("s", "HA TCP Tunnel"),
                    "Channel": Variant("q", self._channel),
                    "AutoConnect": Variant("b", False),
                    "RequireAuthentication": Variant("b", False),
                    "RequireAuthorization": Variant("b", False),
                    # NOTE: We intentionally omit ServiceRecord here.
                    # Including ServiceRecord causes BlueZ to skip automatic RFCOMM socket
                    # setup, leaving the channel 3 listener inactive. Android falls back to
                    # direct channel 3 connect via reflection (BluetoothTcpProxy.openRfcommSocket).
                    # SDP-based discovery for the custom UUID can be added once the ServiceRecord
                    # interaction with BlueZ's RFCOMM socket setup is understood.
                },
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("TCP tunnel D-Bus profile registration failed: %s", exc)
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._dbus_bus = bus
        return True

    async def _start_raw_socket(self) -> None:
        """Fallback: bind raw RFCOMM socket (no SDP, Android must use direct channel)."""
        try:
            sock = socket.socket(
                socket.AF_BLUETOOTH,
                socket.SOCK_STREAM,
                socket.BTPROTO_RFCOMM,  # type: ignore[attr-defined]
            )
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("00:00:00:00:00:00", self._channel))
            sock.listen(10)
            sock.setblocking(False)
            self._server_sock = sock
            self._running = True
            _LOGGER.info(
                "HA Bluetooth TCP tunnel (raw socket) listening on RFCOMM channel %d",
                self._channel,
            )
        except OSError as exc:
            _LOGGER.error("Failed to start TCP tunnel on channel %d: %s", self._channel, exc)
            raise
        self._accept_task = asyncio.ensure_future(self._accept_loop())

    async def stop(self) -> None:
        """Unregister profile and close all resources."""
        self._running = False
        if hasattr(self, "_accept_task") and self._accept_task:
            self._accept_task.cancel()
        if hasattr(self, "_server_sock") and self._server_sock:
            self._server_sock.close()
            self._server_sock = None
        if self._dbus_bus:
            try:
                introspection = await self._dbus_bus.introspect("org.bluez", "/org/bluez")
                proxy = self._dbus_bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
                manager = proxy.get_interface("org.bluez.ProfileManager1")
                await manager.call_unregister_profile(_TUNNEL_PROFILE_PATH)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("TCP tunnel profile unregister: %s", exc)
            try:
                self._dbus_bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._dbus_bus = None
        _LOGGER.info("HA Bluetooth TCP tunnel stopped")

    async def _accept_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while self._running and self._server_sock:
            try:
                client_sock, addr = await loop.sock_accept(self._server_sock)
                _LOGGER.debug("TCP tunnel: RFCOMM connection from %s", addr)
                asyncio.ensure_future(self._handle_client(client_sock))
            except asyncio.CancelledError:
                break
            except OSError as exc:
                if self._running:
                    _LOGGER.error("TCP tunnel accept error: %s", exc)
                break

    async def _handle_client(self, rfcomm_sock: socket.socket) -> None:
        """Relay bytes between one RFCOMM client and a fresh TCP connection to HA."""
        ha_port: int = self._hass.config.api.port  # type: ignore[union-attr]
        try:
            rfcomm_reader, rfcomm_writer = await asyncio.open_connection(sock=rfcomm_sock)
            ha_reader, ha_writer = await asyncio.open_connection("127.0.0.1", ha_port)
            _LOGGER.debug("TCP tunnel: relaying RFCOMM ↔ localhost:%d", ha_port)
            try:
                await asyncio.gather(
                    self._pipe(rfcomm_reader, ha_writer, "RFCOMM→HA"),
                    self._pipe(ha_reader, rfcomm_writer, "HA→RFCOMM"),
                )
            finally:
                ha_writer.close()
                try:
                    await ha_writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("TCP tunnel: client error: %s", exc)
        finally:
            rfcomm_sock.close()

    @staticmethod
    async def _pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        label: str,
    ) -> None:
        """Copy all bytes from *reader* to *writer* until EOF or error."""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("TCP tunnel pipe %s ended: %s", label, exc)
        finally:
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
