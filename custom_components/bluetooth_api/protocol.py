"""Shared framing and auth logic for the Bluetooth API transport."""

from __future__ import annotations

import asyncio
import json
import struct
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# RFCOMM: 4-byte big-endian length prefix followed by UTF-8 JSON payload.
# BLE:    1-byte flag (0x00=more, 0x01=last) + payload chunk.

_HEADER = struct.Struct("!I")  # big-endian unsigned int (4 bytes)
BLE_CHUNK_CONTINUES: int = 0x00
BLE_CHUNK_FINAL: int = 0x01
BLE_MAX_PAYLOAD = 508  # 512 ATT MTU − 3 ATT header − 1 flag byte


async def rfcomm_read_frame(reader: asyncio.StreamReader) -> bytes:
    """Read one length-prefixed frame from an RFCOMM stream."""
    header = await reader.readexactly(_HEADER.size)
    (length,) = _HEADER.unpack(header)
    if length == 0 or length > 16 * 1024 * 1024:
        raise ValueError(f"Invalid RFCOMM frame length: {length}")
    return await reader.readexactly(length)


async def rfcomm_write_frame(writer: asyncio.StreamWriter, data: bytes) -> None:
    """Write a length-prefixed frame to an RFCOMM stream."""
    writer.write(_HEADER.pack(len(data)))
    writer.write(data)
    await writer.drain()


def encode_ble_chunks(data: bytes) -> list[bytes]:
    """Split *data* into BLE chunks with the continuation flag byte prepended."""
    chunks: list[bytes] = []
    offset = 0
    while offset < len(data):
        end = min(offset + BLE_MAX_PAYLOAD, len(data))
        flag = BLE_CHUNK_FINAL if end == len(data) else BLE_CHUNK_CONTINUES
        chunks.append(bytes([flag]) + data[offset:end])
        offset = end
    return chunks


def decode_ble_chunks(chunks: list[bytes]) -> bytes:
    """Reassemble BLE chunks into a complete frame payload."""
    return b"".join(chunk[1:] for chunk in chunks)


async def send_json(writer: asyncio.StreamWriter, msg: dict) -> None:
    """Serialise *msg* and write it as an RFCOMM frame."""
    await rfcomm_write_frame(writer, json.dumps(msg).encode())


async def read_json(reader: asyncio.StreamReader) -> dict:
    """Read one RFCOMM frame and deserialise it as JSON."""
    raw = await rfcomm_read_frame(reader)
    return json.loads(raw.decode())
