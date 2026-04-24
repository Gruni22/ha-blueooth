"""RFCOMM Bluetooth server that bridges the HA WebSocket API over Classic Bluetooth.

Uses the BlueZ ProfileManager1 D-Bus API (modern BlueZ ≥ 5.43, no --compat needed)
to register the SPP service record so Android can discover and connect via SDP.
Falls back to sdptool + raw socket if D-Bus profile registration fails.

Each incoming RFCOMM connection is auto-detected:
- Raw HTTP request (WebView via BluetoothTcpProxy) → byte-for-byte TCP relay to localhost:8123
- 4-byte length-prefix framing (BluetoothWebSocketCoreImpl) → HA WebSocket relay
Both connection types share the same SPP channel 1 (UUID 0x1101).
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

# 3-byte prefixes that identify HTTP request methods.
# Used to distinguish raw HTTP connections (WebView via BluetoothTcpProxy) from
# the 4-byte length-prefix framing used by BluetoothWebSocketCoreImpl.
# HTTP verbs start with capital ASCII letters (0x44–0x54); the framing protocol's
# length prefix always starts with 0x00 for any payload < 16 MB.
_HTTP_METHOD_PREFIXES: frozenset[bytes] = frozenset({
    b"GET", b"POS", b"PUT", b"DEL", b"HEA", b"OPT", b"PAT", b"CON", b"TRA",
})


def _is_http_method(data: bytes) -> bool:
    """Return True if *data*'s first 3 bytes match an HTTP method verb."""
    return data[:3] in _HTTP_METHOD_PREFIXES


# D-Bus path for our Bluetooth Agent1.
_AGENT_PATH = "/org/homeassistant/bluetooth_api/agent"

# Audio/media profile UUIDs that Android auto-connects for sound streaming.
# We block these so the Pi does not appear as an audio device.
_AUDIO_UUIDS: frozenset[str] = frozenset({
    "0000110a-0000-1000-8000-00805f9b34fb",  # A2DP Source
    "0000110b-0000-1000-8000-00805f9b34fb",  # A2DP Sink
    "0000110c-0000-1000-8000-00805f9b34fb",  # AVRCP Target
    "0000110d-0000-1000-8000-00805f9b34fb",  # A/V Remote Control (AVRCP)
    "0000110e-0000-1000-8000-00805f9b34fb",  # AVRCP Controller
    "00001111-0000-1000-8000-00805f9b34fb",  # Audio Video Rendering
    "00001108-0000-1000-8000-00805f9b34fb",  # HSP Headset
    "00001112-0000-1000-8000-00805f9b34fb",  # HSP Audio Gateway
    "0000111e-0000-1000-8000-00805f9b34fb",  # HFP Hands-Free
    "0000111f-0000-1000-8000-00805f9b34fb",  # HFP Audio Gateway
    # LE Audio (Bluetooth 5.2+)
    "0000184d-0000-1000-8000-00805f9b34fb",  # Published Audio Capabilities
    "00001843-0000-1000-8000-00805f9b34fb",  # Basic Audio Announcement
    "00001844-0000-1000-8000-00805f9b34fb",  # Broadcast Audio Announcement
    "00001845-0000-1000-8000-00805f9b34fb",  # Common Audio Service
    "0000184e-0000-1000-8000-00805f9b34fb",  # Hearing Access Service
    "0000184f-0000-1000-8000-00805f9b34fb",  # Telephone Bearer Profile
})


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

        # D-Bus Agent1 (primary — blocks audio UUIDs, auto-confirms pairing)
        self._agent_bus = None         # dbus_fast MessageBus for Agent1
        # Fallback pairing agent via bluetoothctl subprocess
        self._agent_proc: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Make adapter discoverable, register SPP, start accepting connections."""
        await self._set_discoverable(True)
        # Prefer the D-Bus Agent1 (precise UUID-based blocking of audio profiles).
        # Fall back to the bluetoothctl subprocess agent if dbus_fast is unavailable.
        if not await self._start_dbus_agent():
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
        await self._stop_dbus_agent()
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
    # D-Bus Agent1 (primary pairing + audio-blocking agent)
    # ------------------------------------------------------------------

    async def _start_dbus_agent(self) -> bool:
        """Register a BlueZ Agent1 via D-Bus.

        This agent:
        - Blocks audio/media profiles (A2DP, AVRCP, HFP, HSP, LE Audio) so Android
          does not stream phone audio to the Pi.
        - Auto-confirms pairing requests (passkey shown in HA notification).

        Returns True if the agent was registered successfully.
        """
        try:
            from dbus_fast import BusType, DBusError as _DBusError  # type: ignore[import]
            from dbus_fast.aio import MessageBus as _MessageBus  # type: ignore[import]
            from dbus_fast.service import ServiceInterface, method as dbus_method  # type: ignore[import]
        except ImportError:
            _LOGGER.debug("dbus_fast not available – using bluetoothctl agent fallback")
            return False

        try:
            bus = await _MessageBus(bus_type=BusType.SYSTEM).connect()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("D-Bus agent: cannot connect to system bus: %s", exc)
            return False

        hass_ref = self._hass

        # Annotations below use dbus_fast D-Bus type codes as strings ("o"=object path,
        # "s"=string, "u"=uint32, "q"=uint16, ""=void). Pylance flags them as unknown
        # type names — that is expected; do NOT replace them with Python types.
        class _Agent1(ServiceInterface):  # type: ignore[misc]
            """BlueZ Agent1 implementation."""

            def __init__(self) -> None:
                super().__init__("org.bluez.Agent1")

            @dbus_method()
            def Release(self) -> "":  # type: ignore[return,misc]
                _LOGGER.debug("BT agent: released by BlueZ")

            @dbus_method()
            def Cancel(self) -> "":  # type: ignore[return,misc]
                _LOGGER.debug("BT agent: operation cancelled")

            @dbus_method()
            def AuthorizeService(self, device: "o", uuid: "s") -> "":  # type: ignore[misc]
                uuid_lower = uuid.lower()
                if uuid_lower in _AUDIO_UUIDS:
                    _LOGGER.info(
                        "BT agent: blocking audio service %s from %s "
                        "(prevents phone audio streaming to Pi)",
                        uuid, device,
                    )
                    raise _DBusError(
                        "org.bluez.Error.Rejected",
                        f"Audio profile {uuid} is disabled on this server",
                    )
                _LOGGER.debug("BT agent: authorizing service %s from %s", uuid, device)

            @dbus_method()
            def RequestAuthorization(self, device: "o") -> "":  # type: ignore[misc]
                _LOGGER.debug("BT agent: authorizing device %s", device)

            @dbus_method()
            def RequestConfirmation(self, device: "o", passkey: "u") -> "":  # type: ignore[misc]
                passkey_str = str(passkey).zfill(6)
                _LOGGER.info(
                    "BT pairing: confirm passkey %s for %s on your Android device",
                    passkey_str, device,
                )
                _async_create_notification(
                    hass_ref,
                    f"Bluetooth pairing in progress.\n\n"
                    f"Confirm passkey **{passkey_str}** on your Android device.",
                )

            @dbus_method()
            def RequestPinCode(self, device: "o") -> "s":  # type: ignore[misc]
                _LOGGER.info("BT agent: PIN code requested for %s", device)
                return "000000"

            @dbus_method()
            def RequestPasskey(self, device: "o") -> "u":  # type: ignore[misc]
                _LOGGER.info("BT agent: passkey requested for %s", device)
                return 0

            @dbus_method()
            def DisplayPinCode(self, device: "o", pincode: "s") -> "":  # type: ignore[misc]
                _LOGGER.info("BT agent: PIN code for %s is %s", device, pincode)

            @dbus_method()
            def DisplayPasskey(self, device: "o", passkey: "u", entered: "q") -> "":  # type: ignore[misc]
                _LOGGER.info("BT agent: passkey for %s: %06d", device, passkey)

        agent = _Agent1()
        bus.export(_AGENT_PATH, agent)

        try:
            introspection = await bus.introspect("org.bluez", "/org/bluez")
            proxy = bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
            agent_mgr = proxy.get_interface("org.bluez.AgentManager1")
            # Unregister any stale agent from a previous integration load before registering.
            try:
                await agent_mgr.call_unregister_agent(_AGENT_PATH)
                _LOGGER.debug("BT agent: unregistered stale previous agent")
            except Exception:  # noqa: BLE001
                pass  # Not registered yet — expected on first start.
            await agent_mgr.call_register_agent(_AGENT_PATH, "DisplayYesNo")
            await agent_mgr.call_request_default_agent(_AGENT_PATH)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BT agent registration failed: %s – using bluetoothctl fallback", exc)
            try:
                bus.disconnect()
            except Exception:  # noqa: BLE001
                pass
            return False

        self._agent_bus = bus
        _LOGGER.info(
            "BT D-Bus Agent1 registered (audio profiles blocked, pairing auto-confirmed)"
        )
        # Disconnect audio profiles that connected before our agent started, and
        # mark all paired devices as trusted so raw RFCOMM connections are accepted.
        asyncio.ensure_future(self._disconnect_audio_profiles())
        asyncio.ensure_future(self._trust_paired_devices())
        return True

    async def _disconnect_audio_profiles(self) -> None:
        """Walk all connected BT devices and disconnect audio profiles.

        Called once after the agent registers to clean up any audio connections that
        were established before our agent started (e.g. during HA restart window).
        """
        if not self._agent_bus:
            return
        try:
            root_intro = await self._agent_bus.introspect("org.bluez", "/")
            root_proxy = self._agent_bus.get_proxy_object("org.bluez", "/", root_intro)
            om = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BT: could not enumerate BT objects: %s", exc)
            return

        for path, interfaces in objects.items():
            if "org.bluez.Device1" not in interfaces:
                continue
            props = interfaces["org.bluez.Device1"]
            # dbus_fast may return Variant or raw value depending on version.
            def _unwrap(v: object) -> object:
                return v.value if hasattr(v, "value") else v

            if not _unwrap(props.get("Connected", False)):
                continue

            try:
                dev_intro = await self._agent_bus.introspect("org.bluez", path)
                dev_proxy = self._agent_bus.get_proxy_object("org.bluez", path, dev_intro)
                dev_iface = dev_proxy.get_interface("org.bluez.Device1")
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("BT: cannot get device interface for %s: %s", path, exc)
                continue

            for uuid in _AUDIO_UUIDS:
                try:
                    await dev_iface.call_disconnect_profile(uuid)
                    _LOGGER.info("BT: disconnected audio profile %s from %s", uuid, path)
                except Exception:  # noqa: BLE001
                    pass  # Profile not connected — expected for most UUIDs.

    async def _trust_paired_devices(self) -> None:
        """Mark all paired BT devices as Trusted so raw RFCOMM connections work.

        BlueZ may reject raw RFCOMM connections (no UUID, no profile) from devices
        that are Paired but not Trusted.  Setting Trusted=True removes this barrier.
        """
        if not self._agent_bus:
            return
        try:
            from dbus_fast import Variant as _Variant  # type: ignore[import]
            root_intro = await self._agent_bus.introspect("org.bluez", "/")
            root_proxy = self._agent_bus.get_proxy_object("org.bluez", "/", root_intro)
            om = root_proxy.get_interface("org.freedesktop.DBus.ObjectManager")
            objects = await om.call_get_managed_objects()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BT trust: could not enumerate devices: %s", exc)
            return

        def _unwrap(v: object) -> object:
            return v.value if hasattr(v, "value") else v

        for path, interfaces in objects.items():
            if "org.bluez.Device1" not in interfaces:
                continue
            props = interfaces["org.bluez.Device1"]
            if not _unwrap(props.get("Paired", False)):
                continue
            if _unwrap(props.get("Trusted", False)):
                continue  # already trusted
            try:
                dev_intro = await self._agent_bus.introspect("org.bluez", path)
                dev_proxy = self._agent_bus.get_proxy_object("org.bluez", path, dev_intro)
                props_iface = dev_proxy.get_interface("org.freedesktop.DBus.Properties")
                await props_iface.call_set("org.bluez.Device1", "Trusted", _Variant("b", True))
                _LOGGER.info("BT trust: marked paired device %s as Trusted", path)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("BT trust: could not trust %s: %s", path, exc)

    async def _stop_dbus_agent(self) -> None:
        """Unregister the D-Bus Agent1."""
        if not self._agent_bus:
            return
        try:
            introspection = await self._agent_bus.introspect("org.bluez", "/org/bluez")
            proxy = self._agent_bus.get_proxy_object("org.bluez", "/org/bluez", introspection)
            agent_mgr = proxy.get_interface("org.bluez.AgentManager1")
            await agent_mgr.call_unregister_agent(_AGENT_PATH)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("BT agent unregister: %s", exc)
        try:
            self._agent_bus.disconnect()
        except Exception:  # noqa: BLE001
            pass
        self._agent_bus = None

    # ------------------------------------------------------------------
    # Pairing agent (bluetoothctl subprocess fallback)
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
                    # "[agent] Authorize service <UUID> (yes/no):" — no trailing newline.
                    # Extract the UUID and deny audio profiles; allow everything else.
                    import re as _re
                    _uuid_m = _re.search(
                        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                        lower_line,
                    )
                    _svc_uuid = _uuid_m.group(1) if _uuid_m else None
                    if _svc_uuid and _svc_uuid in _AUDIO_UUIDS:
                        _LOGGER.info(
                            "Bluetooth agent: blocking audio service %s "
                            "(prevents phone audio streaming to Pi)",
                            _svc_uuid,
                        )
                        stdin.write(b"no\n")
                    else:
                        _LOGGER.debug(
                            "Bluetooth agent: authorizing service %s",
                            _svc_uuid or "(unknown)",
                        )
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
        """Route one RFCOMM client to HTTP relay or WebSocket bridge.

        Reads the first 4 bytes to distinguish:
        - Raw HTTP (WebView via BluetoothTcpProxy): relayed byte-for-byte to localhost:8123
        - Framing protocol (BluetoothWebSocketCoreImpl): bridged to HA WebSocket API
        """
        reader, writer = await asyncio.open_connection(sock=client_sock)
        try:
            first = await asyncio.wait_for(reader.readexactly(4), timeout=10.0)
        except (asyncio.IncompleteReadError, TimeoutError):
            writer.close()
            return

        if _is_http_method(first):
            await self._relay_http(first, reader, writer)
        else:
            await self._relay_ws(first, reader, writer)

    async def _relay_http(
        self,
        first: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Raw TCP relay: RFCOMM ↔ localhost:8123 (for WebView HTTP + WebSocket upgrade)."""
        ha_port: int = self._hass.config.api.port  # type: ignore[union-attr]
        _LOGGER.info("RFCOMM: HTTP relay → localhost:%d", ha_port)
        try:
            ha_reader, ha_writer = await asyncio.open_connection("127.0.0.1", ha_port)
            ha_writer.write(first)
            await ha_writer.drain()
            try:
                await asyncio.gather(
                    self._pipe(reader, ha_writer, "RFCOMM→HA"),
                    self._pipe(ha_reader, writer, "HA→RFCOMM"),
                )
            finally:
                ha_writer.close()
                try:
                    await ha_writer.wait_closed()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("RFCOMM HTTP relay error: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _relay_ws(
        self,
        first: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Bridge framing-protocol client to the local HA WebSocket API."""
        ws_url = f"ws://127.0.0.1:{self._hass.config.api.port}/api/websocket"  # type: ignore[union-attr]
        _LOGGER.info("RFCOMM: WS relay → %s", ws_url)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(ws_url) as ws:
                    _LOGGER.debug("RFCOMM: local WS established, starting bridge")
                    await asyncio.gather(
                        self._bt_to_ws_with_prefix(first, reader, ws),
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
    async def _bt_to_ws_with_prefix(
        first: bytes,
        reader: asyncio.StreamReader,
        ws: aiohttp.ClientWebSocketResponse,
    ) -> None:
        """Forward BT frames → WS; *first* is the already-consumed 4-byte length prefix."""
        import struct as _struct
        try:
            (length,) = _struct.unpack("!I", first)
            if 0 < length <= 16 * 1024 * 1024:
                payload = await reader.readexactly(length)
                _LOGGER.debug("RFCOMM RX (%d bytes) → WS: %.200s", len(payload), payload.decode(errors="replace"))
                await ws.send_str(payload.decode(errors="replace"))
            while True:
                frame = await rfcomm_read_frame(reader)
                _LOGGER.debug("RFCOMM RX (%d bytes) → WS: %.200s", len(frame), frame.decode(errors="replace"))
                await ws.send_str(frame.decode(errors="replace"))
        except asyncio.IncompleteReadError:
            _LOGGER.debug("RFCOMM: client disconnected (IncompleteReadError)")

    @staticmethod
    async def _pipe(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        label: str,
    ) -> None:
        """Copy bytes from *reader* to *writer* until EOF or error."""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("RFCOMM pipe %s ended: %s", label, exc)

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

