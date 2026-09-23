"""
bprotocol.py - Core binary framing, HPACK header codec, and protocol utilities
for Binary HTTP (B-HTTP/1.0).
"""

import struct
from typing import Dict, List, Optional, Tuple

# Header size in bytes: 3 (Length) + 1 (Type) + 1 (Flags) + 4 (Stream ID) = 9 bytes
HEADER_SIZE = 9

# Frame Types
FRAME_DATA = 0x00
FRAME_HEADERS = 0x01
FRAME_GOAWAY = 0x07

FRAME_TYPE_NAMES = {
    FRAME_DATA: "DATA",
    FRAME_HEADERS: "HEADERS",
    FRAME_GOAWAY: "GOAWAY",
}

# Flags
FLAG_NONE = 0x00
FLAG_END_STREAM = 0x01  # Bit 0

# HPACK Static Header Name Table (1-indexed, 10 entries)
STATIC_TABLE = {
    1: ":method",
    2: ":path",
    3: ":status",
    4: "content-type",
    5: "content-length",
    6: "user-agent",
    7: "host",
    8: "accept",
    9: "server",
    10: "date",
}

# Reverse lookup table for encoding
STATIC_TABLE_REV = {name.lower(): idx for idx, name in STATIC_TABLE.items()}


class ProtocolError(Exception):
    """Raised when encountering a protocol violation or malformed frame."""
    pass


def pack_frame(frame_type: int, flags: int, stream_id: int, payload: bytes = b"") -> bytes:
    """
    Packs a B-HTTP frame header and payload.
    Header format (9 bytes):
      - 24 bits: Payload Length
      - 8 bits: Frame Type
      - 8 bits: Flags
      - 1 bit Reserved (0) + 31 bits: Stream ID
    """
    length = len(payload)
    if length > 0xFFFFFF:
        raise ProtocolError(f"Payload length {length} exceeds 24-bit maximum (16777215 bytes)")

    # 3 bytes length + 1 byte type + 1 byte flags + 4 bytes stream_id
    header = (
        length.to_bytes(3, byteorder="big")
        + bytes([frame_type & 0xFF, flags & 0xFF])
        + struct.pack("!I", stream_id & 0x7FFFFFFF)
    )
    return header + payload


def unpack_frame_header(header_bytes: bytes) -> Tuple[int, int, int, int]:
    """
    Unpacks a 9-byte frame header.
    Returns: (length, frame_type, flags, stream_id)
    """
    if len(header_bytes) != HEADER_SIZE:
        raise ProtocolError(f"Expected {HEADER_SIZE} header bytes, got {len(header_bytes)}")

    length = int.from_bytes(header_bytes[0:3], byteorder="big")
    frame_type = header_bytes[3]
    flags = header_bytes[4]
    stream_id = struct.unpack("!I", header_bytes[5:9])[0] & 0x7FFFFFFF
    return length, frame_type, flags, stream_id


def encode_headers(headers: List[Tuple[str, str]]) -> bytes:
    """
    Encodes a list of (name, value) headers into a binary header block.
    Format:
      - 1 byte: number of headers (uint8)
      - For each header:
        - 1 byte Name identifier:
          - If high bit = 1 (0x80 | index): Static table index (1..10)
          - If high bit = 0 (0x00 | name_len): Literal name follows (name_len bytes ASCII)
        - 2 bytes Value length (uint16 big-endian)
        - val_len bytes Value (UTF-8)
    """
    if len(headers) > 255:
        raise ProtocolError(f"Maximum 255 headers allowed, got {len(headers)}")

    buf = bytearray([len(headers)])

    for name, val in headers:
        lower_name = name.lower()
        val_bytes = val.encode("utf-8")
        if len(val_bytes) > 0xFFFF:
            raise ProtocolError(f"Header value too long ({len(val_bytes)} bytes)")

        if lower_name in STATIC_TABLE_REV:
            idx = STATIC_TABLE_REV[lower_name]
            buf.append(0x80 | (idx & 0x7F))
        else:
            name_bytes = lower_name.encode("ascii")
            name_len = len(name_bytes)
            if name_len > 127:
                raise ProtocolError(f"Custom header name too long ({name_len} bytes)")
            buf.append(name_len & 0x7F)
            buf.extend(name_bytes)

        buf.extend(struct.pack("!H", len(val_bytes)))
        buf.extend(val_bytes)

    return bytes(buf)


def decode_headers(payload: bytes) -> List[Tuple[str, str]]:
    """
    Decodes a binary header block into a list of (name, value) tuples.
    """
    if len(payload) < 1:
        raise ProtocolError("Header payload empty; missing header count")

    num_headers = payload[0]
    offset = 1
    headers: List[Tuple[str, str]] = []

    for _ in range(num_headers):
        if offset >= len(payload):
            raise ProtocolError("Unexpected end of payload while parsing header name identifier")

        id_byte = payload[offset]
        offset += 1

        if id_byte & 0x80:
            # Static table index
            idx = id_byte & 0x7F
            if idx not in STATIC_TABLE:
                raise ProtocolError(f"Invalid static table index: {idx}")
            name = STATIC_TABLE[idx]
        else:
            # Literal name
            name_len = id_byte & 0x7F
            if name_len == 0:
                raise ProtocolError("Literal header name length cannot be zero")
            if offset + name_len > len(payload):
                raise ProtocolError("Unexpected end of payload reading literal header name")
            name = payload[offset:offset + name_len].decode("ascii", errors="replace")
            offset += name_len

        # Read 2-byte value length
        if offset + 2 > len(payload):
            raise ProtocolError("Unexpected end of payload reading header value length")
        val_len = struct.unpack("!H", payload[offset:offset + 2])[0]
        offset += 2

        # Read value bytes
        if offset + val_len > len(payload):
            raise ProtocolError("Unexpected end of payload reading header value")
        val = payload[offset:offset + val_len].decode("utf-8", errors="replace")
        offset += val_len

        headers.append((name, val))

    if offset != len(payload):
        raise ProtocolError(f"Extra trailing bytes in header block: {len(payload) - offset} bytes")

    return headers


def recv_exact(sock, n: int) -> Optional[bytes]:
    """
    Reads exactly n bytes from the socket.
    Returns None if connection closed before any bytes were read.
    Raises ProtocolError if connection closed mid-stream.
    """
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if len(buf) == 0:
                return None
            raise ProtocolError(f"Connection closed unexpectedly (read {len(buf)}/{n} bytes)")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(sock) -> Optional[Tuple[int, int, int, bytes, bytes]]:
    """
    Reads a single frame from the socket.
    Returns: (frame_type, flags, stream_id, payload, raw_frame_bytes) or None on clean EOF.
    """
    header_bytes = recv_exact(sock, HEADER_SIZE)
    if header_bytes is None:
        return None

    length, frame_type, flags, stream_id = unpack_frame_header(header_bytes)
    payload = recv_exact(sock, length) if length > 0 else b""
    if payload is None and length > 0:
        raise ProtocolError(f"Failed to read {length} bytes of frame payload")

    raw_frame = header_bytes + payload
    return frame_type, flags, stream_id, payload, raw_frame


def format_hexdump(data: bytes, prefix: str = "") -> str:
    """
    Formats binary data in canonical hexdump (hexdump -C) format.
    """
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_bytes_1 = " ".join(f"{b:02x}" for b in chunk[:8])
        hex_bytes_2 = " ".join(f"{b:02x}" for b in chunk[8:])
        hex_str = f"{hex_bytes_1:<23}  {hex_bytes_2:<23}".rstrip()

        ascii_str = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append(f"{prefix}{i:08x}  {hex_str:<48}  |{ascii_str}|")
    return "\n".join(lines)


def describe_frame(frame_type: int, flags: int, stream_id: int, length: int) -> str:
    """
    Returns a human-readable description of a frame's header.
    """
    type_name = FRAME_TYPE_NAMES.get(frame_type, f"UNKNOWN (0x{frame_type:02x})")
    flag_descs = []
    if flags & FLAG_END_STREAM:
        flag_descs.append("END_STREAM (0x01)")
    if not flag_descs:
        flag_descs.append(f"0x{flags:02x}")
    flags_str = ", ".join(flag_descs)
    return f"Type: {type_name} (0x{frame_type:02x}), Length: {length}, Flags: [{flags_str}], Stream ID: {stream_id}"
