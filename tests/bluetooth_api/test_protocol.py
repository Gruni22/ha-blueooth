"""Tests for the BT framing protocol."""

from __future__ import annotations

import asyncio
import struct

import pytest

from homeassistant.components.bluetooth_api.protocol import (
    BLE_CHUNK_CONTINUES,
    BLE_CHUNK_FINAL,
    decode_ble_chunks,
    encode_ble_chunks,
    rfcomm_read_frame,
    rfcomm_write_frame,
)


# ─── RFCOMM framing tests ────────────────────────────────────────────────────


async def _make_stream(data: bytes):
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


async def test_rfcomm_roundtrip() -> None:
    """Writing then reading a frame returns the original payload."""
    payload = b'{"type": "auth_required", "ha_version": "2025.1.0"}'
    buf = asyncio.StreamReader()

    # Capture writes via a mock writer
    written: list[bytes] = []

    class _FakeTransport:
        def write(self, data: bytes) -> None:
            written.append(data)

        def get_extra_info(self, _key):
            return None

    transport = _FakeTransport()
    protocol = asyncio.StreamReaderProtocol(asyncio.StreamReader())
    writer = asyncio.StreamWriter(transport, protocol, asyncio.StreamReader(), asyncio.get_event_loop())

    # Manually write the frame
    writer.write(struct.pack("!I", len(payload)))
    writer.write(payload)

    raw = b"".join(written)
    reader = await _make_stream(raw)
    result = await rfcomm_read_frame(reader)
    assert result == payload


async def test_rfcomm_reject_zero_length() -> None:
    """A zero-length frame raises ValueError."""
    reader = await _make_stream(struct.pack("!I", 0))
    with pytest.raises(ValueError, match="Invalid RFCOMM frame length"):
        await rfcomm_read_frame(reader)


# ─── BLE chunk tests ─────────────────────────────────────────────────────────


def test_ble_small_payload_is_single_chunk() -> None:
    """Payloads ≤ BLE_MAX_PAYLOAD are encoded as a single final chunk."""
    data = b"hello world"
    chunks = encode_ble_chunks(data)
    assert len(chunks) == 1
    assert chunks[0][0] == BLE_CHUNK_FINAL
    assert chunks[0][1:] == data


def test_ble_large_payload_splits_correctly() -> None:
    """Large payloads are split into continuation + final chunks."""
    from homeassistant.components.bluetooth_api.protocol import BLE_MAX_PAYLOAD

    data = bytes(range(256)) * 3  # 768 bytes
    chunks = encode_ble_chunks(data)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert chunk[0] == BLE_CHUNK_CONTINUES
    assert chunks[-1][0] == BLE_CHUNK_FINAL


def test_ble_encode_decode_roundtrip() -> None:
    """encode_ble_chunks followed by decode_ble_chunks returns original data."""
    data = b'{"type": "call_service", "id": 1, "domain": "light", "service": "turn_on"}'
    chunks = encode_ble_chunks(data)
    result = decode_ble_chunks(chunks)
    assert result == data
