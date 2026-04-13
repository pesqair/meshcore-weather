"""MeshWX binary wire format: pack/unpack radar grids, warning polygons, refresh requests.

Wire format reference: Weather_Protocol.md
All multi-byte integers are big-endian unless noted.
All binary payloads are COBS-encoded before transmission to avoid null bytes
which the MeshCore firmware's companion protocol truncates at.
"""

import json
import struct
from pathlib import Path


# -- COBS (Consistent Overhead Byte Stuffing) --

def cobs_encode(data: bytes) -> bytes:
    """COBS-encode data to eliminate all 0x00 bytes.

    Overhead: at most 1 byte per 254 input bytes.
    """
    output = bytearray()
    block_start = len(output)
    output.append(0)  # placeholder for first code byte
    run_length = 1

    for byte in data:
        if byte == 0x00:
            output[block_start] = run_length
            block_start = len(output)
            output.append(0)  # placeholder for next code byte
            run_length = 1
        else:
            output.append(byte)
            run_length += 1
            if run_length == 0xFF:
                output[block_start] = run_length
                block_start = len(output)
                output.append(0)
                run_length = 1

    output[block_start] = run_length
    return bytes(output)


def cobs_decode(data: bytes) -> bytes:
    """Decode COBS-encoded data back to original bytes."""
    output = bytearray()
    i = 0
    while i < len(data):
        code = data[i]
        i += 1
        for _ in range(code - 1):
            if i >= len(data):
                break
            output.append(data[i])
            i += 1
        # Append a zero delimiter between blocks, but not after the last block
        if code < 0xFF and i < len(data):
            output.append(0x00)
    return bytes(output)

# -- Message type bytes --
MSG_REFRESH = 0x01       # v1: client → bot refresh request (DM)
MSG_DATA_REQUEST = 0x02  # v2: client → bot data request (DM)
MSG_NOT_AVAILABLE = 0x03 # v3: bot → client, "I can't serve that request" (broadcast)
MSG_RADAR = 0x10         # v1: radar grid (broadcast, fixed 16×16 flat)
MSG_RADAR_COMPRESSED = 0x11  # v3: compressed radar grid (32×32 or 64×64, sparse/RLE)
MSG_WARNING = 0x20       # v1: warning polygon (broadcast)
MSG_WARNING_ZONES = 0x21 # v2: zone-coded warning (client renders polygons)
MSG_OBSERVATION = 0x30   # v2: current conditions (wx reply)
MSG_FORECAST = 0x31      # v2: multi-period forecast
MSG_OUTLOOK = 0x32       # v2: HWO 1-7 day hazards
MSG_STORM_REPORTS = 0x33 # v2: LSR reports
MSG_RAIN_OBS = 0x34      # v2: rain city list
MSG_METAR = 0x35         # v2: raw METAR
MSG_TAF = 0x36           # v2: TAF forecast
MSG_WARNINGS_NEAR = 0x37 # v2: warnings near location summary
MSG_FIRE_WEATHER = 0x38  # v4: fire weather forecast (FWF)
MSG_DAILY_CLIMATE = 0x3A # v4: daily climate summary (RTP)
MSG_NOWCAST = 0x3C       # v4: short-term forecast (NOW)
MSG_QPF_GRID = 0x12      # v4: QPF precipitation grid (same encoding as 0x11)
MSG_TEXT_CHUNK = 0x40    # v2: compressed text fallback
MSG_BEACON = 0xF0        # v4: discovery beacon (bot → client response)
MSG_DISCOVER_PING = 0xF1 # v4: discovery ping (client → all bots)

# -- v4 frame format --
V4_VERSION = 0x04  # Protocol version byte (first byte of every v4 frame)


def v4_wrap(msg: bytes, seq: int, flags: int = 0, group_total: int = 0) -> bytes:
    """Wrap a v3-format message in a v4 frame header.

    v4 frame layout (6 bytes prepended):
      byte 0: 0x04 (protocol version)
      byte 1: msg_type (copied from msg[0])
      byte 2: msg_flags (FEC bits — 0 for single-message products)
      byte 3: group_total_units (0 if not FEC)
      bytes 4-5: sequence_number (uint16 BE, monotonic per bot)
      bytes 6+: original payload (msg[1:])

    The msg_type is extracted from the v3 message's first byte and
    placed in the v4 header. The original payload follows WITHOUT its
    type byte (since it's now in the header).
    """
    if not msg:
        return msg
    header = struct.pack(">BBBBH",
        V4_VERSION,
        msg[0],           # msg_type from the v3 message
        flags & 0xFF,
        group_total & 0xFF,
        seq & 0xFFFF,
    )
    return header + msg[1:]  # payload without the type byte


def v4_unwrap(data: bytes) -> tuple[bytes, int, int]:
    """Unwrap a v4 frame, returning (v3_message, seq_number, flags).

    Reconstructs the v3-format message by prepending the msg_type byte
    back onto the payload.
    """
    if len(data) < 6 or data[0] != V4_VERSION:
        raise ValueError("Not a v4 frame")
    msg_type = data[1]
    flags = data[2]
    # group_total = data[3]
    seq = struct.unpack_from(">H", data, 4)[0]
    v3_msg = bytes([msg_type]) + data[6:]
    return v3_msg, seq, flags


def is_v4_frame(data: bytes) -> bool:
    """Check if data starts with a v4 frame header."""
    return len(data) >= 6 and data[0] == V4_VERSION


class V4SequenceCounter:
    """Monotonic uint16 sequence counter for v4 frames.

    One counter per bot — increments with each message sent on the v4
    channel. Wraps from 65535 to 0.
    """

    def __init__(self) -> None:
        self._seq: int = 0

    def next(self) -> int:
        seq = self._seq
        self._seq = (self._seq + 1) & 0xFFFF
        return seq

    @property
    def current(self) -> int:
        return self._seq


# -- Warning type nibbles (high nibble of byte 1) --
WARN_TORNADO = 0x1
WARN_SEVERE_TSTORM = 0x2
WARN_FLASH_FLOOD = 0x3
WARN_FLOOD = 0x4
WARN_WINTER_STORM = 0x5
WARN_HIGH_WIND = 0x6
WARN_FIRE = 0x7
WARN_MARINE = 0x8
WARN_SPECIAL = 0x9
WARN_OTHER = 0xF

# -- Severity nibbles (low nibble of byte 1) --
SEV_ADVISORY = 0x1
SEV_WATCH = 0x2
SEV_WARNING = 0x3
SEV_EMERGENCY = 0x4

# -- NWS product type → warning type nibble --
PRODUCT_TYPE_MAP = {
    "TOR": WARN_TORNADO,
    "SVS": WARN_SEVERE_TSTORM,
    "SVR": WARN_SEVERE_TSTORM,
    "FFW": WARN_FLASH_FLOOD,
    "FLS": WARN_FLASH_FLOOD,
    "FLW": WARN_FLOOD,
    "WSW": WARN_WINTER_STORM,
    "NPW": WARN_HIGH_WIND,
    "FWS": WARN_FIRE,
    "RFW": WARN_FIRE,
    "MWS": WARN_MARINE,
    "SMW": WARN_MARINE,
    "SPS": WARN_SPECIAL,
    "DSW": WARN_OTHER,
    "EWW": WARN_OTHER,
    "SQW": WARN_OTHER,
    "SWO": WARN_OTHER,
}

# -- VTEC significance → severity nibble --
VTEC_SEVERITY_MAP = {
    "W": SEV_WARNING,
    "A": SEV_WATCH,
    "Y": SEV_ADVISORY,
    "S": SEV_ADVISORY,
}

# -- Region definitions (from protocol spec) --
REGIONS = {
    0x0: {"name": "Northeast", "n": 48.0, "s": 37.0, "w": -82.0, "e": -67.0, "scale": 55},
    0x1: {"name": "Southeast", "n": 37.0, "s": 24.0, "w": -92.0, "e": -75.0, "scale": 55},
    0x2: {"name": "Upper Midwest", "n": 50.0, "s": 40.0, "w": -98.0, "e": -82.0, "scale": 55},
    0x3: {"name": "Southern", "n": 37.0, "s": 25.0, "w": -105.0, "e": -88.0, "scale": 55},
    0x4: {"name": "Central", "n": 44.0, "s": 34.0, "w": -105.0, "e": -90.0, "scale": 55},
    0x5: {"name": "Mountain", "n": 49.0, "s": 31.0, "w": -117.0, "e": -102.0, "scale": 55},
    0x6: {"name": "Pacific", "n": 49.0, "s": 32.0, "w": -125.0, "e": -114.0, "scale": 40},
    0x7: {"name": "Alaska", "n": 72.0, "s": 51.0, "w": -180.0, "e": -130.0, "scale": 175},
    0x8: {"name": "Hawaii", "n": 23.0, "s": 18.0, "w": -161.0, "e": -154.0, "scale": 28},
    0x9: {"name": "Puerto Rico", "n": 19.5, "s": 17.0, "w": -68.0, "e": -65.0, "scale": 12},
}


def region_for_location(lat: float, lon: float) -> int | None:
    """Find which region contains a lat/lon point. Returns region_id or None."""
    for rid, r in REGIONS.items():
        if r["s"] <= lat <= r["n"] and r["w"] <= lon <= r["e"]:
            return rid
    return None


# -- Radar Grid (0x10) -- 133 bytes --

def pack_radar_grid(
    region_id: int,
    frame_seq: int,
    timestamp_utc_min: int,
    scale_km: int,
    grid: list[list[int]],
) -> bytes:
    """Pack a 16x16 radar grid into 133-byte wire format.

    grid: 16x16 array of 4-bit reflectivity values (0x0-0xE).
    """
    msg = bytearray(133)
    msg[0] = MSG_RADAR
    msg[1] = ((region_id & 0x0F) << 4) | (frame_seq & 0x0F)
    struct.pack_into(">H", msg, 2, timestamp_utc_min & 0xFFFF)
    msg[4] = scale_km & 0xFF
    idx = 5
    for row in range(16):
        for col in range(0, 16, 2):
            high = grid[row][col] & 0x0F
            low = grid[row][col + 1] & 0x0F
            msg[idx] = (high << 4) | low
            idx += 1
    return bytes(msg)


def unpack_radar_grid(data: bytes) -> dict:
    """Unpack a 133-byte radar grid message."""
    if len(data) < 133 or data[0] != MSG_RADAR:
        raise ValueError("Invalid radar grid message")
    region_id = (data[1] >> 4) & 0x0F
    frame_seq = data[1] & 0x0F
    timestamp = struct.unpack_from(">H", data, 2)[0]
    scale_km = data[4]
    grid = [[0] * 16 for _ in range(16)]
    idx = 5
    for row in range(16):
        for col in range(0, 16, 2):
            grid[row][col] = (data[idx] >> 4) & 0x0F
            grid[row][col + 1] = data[idx] & 0x0F
            idx += 1
    return {
        "type": MSG_RADAR,
        "region_id": region_id,
        "frame_seq": frame_seq,
        "timestamp_utc_min": timestamp,
        "scale_km": scale_km,
        "grid": grid,
    }


# -- Compressed Radar Grid (0x11) v3 — 32×32 or 64×64 with sparse/RLE --------
#
# Wire format:
#   byte 0     : 0x11 MSG_RADAR_COMPRESSED
#   byte 1     : region_id (hi nibble) | chunk_seq (lo nibble)
#                chunk_seq: 0 for single-message grids, 0-N for multi-msg
#   byte 2     : grid_size (32 or 64)
#   byte 3-4   : timestamp_utc_min (uint16 BE)
#   byte 5     : scale_km (uint8)
#   byte 6     : encoding (hi nibble) | total_chunks (lo nibble)
#                encoding: 0 = sparse, 1 = RLE
#   bytes 7+   : encoded grid data (format depends on encoding byte)
#
# Sparse encoding (encoding = 0):
#   Each non-zero cell is a 2-byte entry:
#     bits 15-4: position (row * grid_size + col), uint12 for 64×64
#     bits 3-0:  value (4-bit reflectivity 0x1-0xE)
#   Entries are sorted by position for efficient client rendering.
#   Total data = 2 × non_zero_count bytes.
#
# RLE encoding (encoding = 1):
#   Scans left→right, top→bottom. Each byte:
#     bits 7-4: run_length - 1 (0-15, so runs of 1-16 cells)
#     bits 3-0: value (4-bit reflectivity)
#   Total data = number_of_runs bytes.
#
# Multi-message: if encoded data > 129 bytes (136 - 7 header), the grid
# is split across multiple messages. chunk_seq counts up from 0,
# total_chunks tells the client how many to expect. Client reassembles
# before decoding.

RADAR_ENC_SPARSE = 0
RADAR_ENC_RLE = 1


def _encode_radar_sparse(grid: list[list[int]], grid_size: int) -> bytes:
    """Sparse-encode a grid: 2 bytes per non-zero cell."""
    entries = bytearray()
    for y in range(grid_size):
        for x in range(grid_size):
            val = grid[y][x] & 0x0F
            if val == 0:
                continue
            pos = y * grid_size + x
            # Pack: high byte = pos >> 4, low byte = (pos & 0xF) << 4 | val
            entries.append((pos >> 4) & 0xFF)
            entries.append(((pos & 0x0F) << 4) | val)
    return bytes(entries)


def _decode_radar_sparse(data: bytes, grid_size: int) -> list[list[int]]:
    """Decode sparse entries back to a grid."""
    grid = [[0] * grid_size for _ in range(grid_size)]
    i = 0
    while i + 1 < len(data):
        pos = (data[i] << 4) | (data[i + 1] >> 4)
        val = data[i + 1] & 0x0F
        y = pos // grid_size
        x = pos % grid_size
        if 0 <= y < grid_size and 0 <= x < grid_size:
            grid[y][x] = val
        i += 2
    return grid


def _encode_radar_rle(grid: list[list[int]], grid_size: int) -> bytes:
    """RLE-encode a grid: 1 byte per run (max run length 16)."""
    flat = []
    for row in grid:
        flat.extend(row)
    runs = bytearray()
    i = 0
    while i < len(flat):
        val = flat[i] & 0x0F
        run = 1
        while i + run < len(flat) and (flat[i + run] & 0x0F) == val and run < 16:
            run += 1
        runs.append(((run - 1) << 4) | val)
        i += run
    return bytes(runs)


def _decode_radar_rle(data: bytes, grid_size: int) -> list[list[int]]:
    """Decode RLE back to a grid."""
    flat = []
    for byte in data:
        run = ((byte >> 4) & 0x0F) + 1
        val = byte & 0x0F
        flat.extend([val] * run)
    # Pad or truncate to exact grid_size²
    total = grid_size * grid_size
    flat = flat[:total]
    flat.extend([0] * (total - len(flat)))
    return [flat[y * grid_size:(y + 1) * grid_size] for y in range(grid_size)]


def pack_radar_compressed(
    region_id: int,
    timestamp_utc_min: int,
    scale_km: int,
    grid: list[list[int]],
    grid_size: int = 32,
) -> list[bytes]:
    """Pack a compressed radar grid into one or more 0x11 messages.

    Picks whichever encoding (sparse vs RLE) is smaller. If the encoded
    data exceeds one frame, splits across multiple messages with
    chunk_seq / total_chunks.

    Returns a list of wire-ready messages (usually 1, occasionally 2-4
    during heavy weather).
    """
    # Encode both ways, pick the smaller
    sparse_data = _encode_radar_sparse(grid, grid_size)
    rle_data = _encode_radar_rle(grid, grid_size)
    if len(sparse_data) <= len(rle_data):
        encoding = RADAR_ENC_SPARSE
        encoded = sparse_data
    else:
        encoding = RADAR_ENC_RLE
        encoded = rle_data

    # Split into chunks that fit in one frame
    # Header = 7 bytes, max payload per frame = 136 - 7 = 129
    max_payload = 129
    chunks = []
    offset = 0
    while offset < len(encoded):
        chunks.append(encoded[offset:offset + max_payload])
        offset += max_payload
    if not chunks:
        chunks = [b""]  # empty grid is still one message

    total_chunks = len(chunks)
    messages = []
    for seq, chunk in enumerate(chunks):
        msg = bytearray()
        msg.append(MSG_RADAR_COMPRESSED)
        msg.append(((region_id & 0x0F) << 4) | (seq & 0x0F))
        msg.append(grid_size & 0xFF)
        msg.extend(struct.pack(">H", timestamp_utc_min & 0xFFFF))
        msg.append(scale_km & 0xFF)
        msg.append(((encoding & 0x0F) << 4) | (total_chunks & 0x0F))
        msg.extend(chunk)
        messages.append(bytes(msg))

    return messages


def unpack_radar_compressed(data: bytes) -> dict:
    """Unpack a single 0x11 compressed radar message.

    For multi-chunk grids, the caller must reassemble all chunks
    (matching region_id and timestamp) before calling this on the
    reassembled payload. For single-chunk messages (the common case),
    this returns the complete grid directly.
    """
    if len(data) < 7 or data[0] != MSG_RADAR_COMPRESSED:
        raise ValueError("Invalid compressed radar message")
    region_id = (data[1] >> 4) & 0x0F
    chunk_seq = data[1] & 0x0F
    grid_size = data[2]
    timestamp = struct.unpack_from(">H", data, 3)[0]
    scale_km = data[5]
    encoding = (data[6] >> 4) & 0x0F
    total_chunks = data[6] & 0x0F
    payload = data[7:]

    # For single-chunk messages, decode the grid immediately.
    # For multi-chunk, return the raw payload and metadata so the caller
    # can reassemble.
    grid = None
    if total_chunks <= 1:
        if encoding == RADAR_ENC_SPARSE:
            grid = _decode_radar_sparse(payload, grid_size)
        elif encoding == RADAR_ENC_RLE:
            grid = _decode_radar_rle(payload, grid_size)

    return {
        "type": MSG_RADAR_COMPRESSED,
        "region_id": region_id,
        "chunk_seq": chunk_seq,
        "total_chunks": total_chunks,
        "grid_size": grid_size,
        "timestamp_utc_min": timestamp,
        "scale_km": scale_km,
        "encoding": encoding,
        "grid": grid,
        "payload": payload,  # raw for multi-chunk reassembly
    }


def reassemble_radar_chunks(chunks: list[dict]) -> list[list[int]] | None:
    """Reassemble a multi-chunk 0x11 radar grid from individual messages.

    Each `chunk` is the dict returned by `unpack_radar_compressed`.
    All chunks must have the same region_id, grid_size, timestamp,
    and encoding. Returns the decoded grid or None on failure.
    """
    if not chunks:
        return None
    # Sort by chunk_seq
    sorted_chunks = sorted(chunks, key=lambda c: c["chunk_seq"])
    ref = sorted_chunks[0]
    grid_size = ref["grid_size"]
    encoding = ref["encoding"]
    total = ref["total_chunks"]
    if len(sorted_chunks) != total:
        return None  # missing chunks
    # Concatenate payloads
    full_payload = b"".join(c["payload"] for c in sorted_chunks)
    if encoding == RADAR_ENC_SPARSE:
        return _decode_radar_sparse(full_payload, grid_size)
    elif encoding == RADAR_ENC_RLE:
        return _decode_radar_rle(full_payload, grid_size)
    return None


# -- Headline truncation helper (shared by warning packers) ------------------


def _fit_headline(headline: str, max_bytes: int) -> bytes:
    """Encode a headline to UTF-8 and truncate cleanly at a word boundary.

    Never cuts mid-word. If truncation is needed, ends with a trailing
    ellipsis (ASCII "...") so the client can tell the string is abbreviated.
    Returns an empty bytestring for an empty or empty-after-strip headline.
    """
    if not headline or max_bytes <= 0:
        return b""
    encoded = headline.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return encoded
    # Leave 3 bytes for the trailing "..."
    room = max_bytes - 3
    if room <= 0:
        # Not enough space for even "..." — just return as many bytes as fit.
        # Avoid slicing mid-codepoint by decoding with errors='ignore'.
        return encoded[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
    # Walk backward from `room` to the last space so we don't cut a word.
    # Decode as utf-8 to operate on characters, not bytes (avoids mid-codepoint).
    truncated = encoded[:room].decode("utf-8", errors="ignore")
    # Strip trailing whitespace, then cut at last space if present
    truncated = truncated.rstrip()
    last_space = truncated.rfind(" ")
    if last_space > room // 2:
        # Only cut at space if it doesn't waste too much room
        truncated = truncated[:last_space].rstrip()
    return (truncated + "...").encode("utf-8")


# -- Warning Polygon (0x20) v3 -- variable, max 136 bytes --
#
# Wire format (v3 — breaking change from v2):
#   byte 0     : MSG_WARNING (0x20)
#   byte 1     : warning_type (hi nibble) | severity (lo nibble)
#   bytes 2-5  : expires_unix_min (uint32 BE, minutes since Unix epoch)
#                NWS-authoritative absolute expiry. Client computes time
#                remaining from its own clock. Replaces v2's uint16
#                "expiry_minutes" relative counter.
#   byte 6     : vertex_count
#   bytes 7..  : first vertex (6B int24 lat, int24 lon * 10000)
#                then (vertex_count-1) * 4 bytes of int16 delta pairs
#   remainder  : headline, UTF-8, truncated at word boundary with "..."

def pack_warning_polygon(
    warning_type: int,
    severity: int,
    expires_unix_min: int,
    vertices: list[tuple[float, float]],
    headline: str,
) -> bytes:
    """Pack a warning polygon into wire format v3 (max 136 bytes).

    Args:
        warning_type: 4-bit type nibble (WARN_TORNADO, WARN_SEVERE_TSTORM, ...).
        severity: 4-bit severity nibble (SEV_ADVISORY/WATCH/WARNING/EMERGENCY).
        expires_unix_min: NWS expiry as uint32 Unix minutes (minutes since 1970).
        vertices: list of (lat, lon) in decimal degrees.
        headline: short text for the client to display.
    """
    msg = bytearray()
    msg.append(MSG_WARNING)
    msg.append(((warning_type & 0x0F) << 4) | (severity & 0x0F))
    msg.extend(struct.pack(">I", expires_unix_min & 0xFFFFFFFF))
    msg.append(len(vertices) & 0xFF)

    if vertices:
        # First vertex: 24-bit signed, degrees * 10000 (~11m precision)
        lat0, lon0 = vertices[0]
        lat_i = int(lat0 * 10000)
        lon_i = int(lon0 * 10000)
        msg.extend(lat_i.to_bytes(3, "big", signed=True))
        msg.extend(lon_i.to_bytes(3, "big", signed=True))

        # Remaining vertices: int16 delta pairs (0.001 degree units, ~110m).
        # Range ±32.767° from first vertex — enough for any real warning.
        for lat, lon in vertices[1:]:
            dlat = max(-32768, min(32767, int(round((lat - lat0) / 0.001))))
            dlon = max(-32768, min(32767, int(round((lon - lon0) / 0.001))))
            msg.extend(struct.pack(">hh", dlat, dlon))

    # Headline fills remaining space, truncated at a word boundary
    remaining = 136 - len(msg)
    if remaining > 0 and headline:
        msg.extend(_fit_headline(headline, remaining))
    return bytes(msg)


def unpack_warning_polygon(data: bytes) -> dict:
    """Unpack a v3 warning polygon message."""
    if len(data) < 7 or data[0] != MSG_WARNING:
        raise ValueError("Invalid warning polygon message")
    warning_type = (data[1] >> 4) & 0x0F
    severity = data[1] & 0x0F
    expires_unix_min = struct.unpack_from(">I", data, 2)[0]
    vertex_count = data[6]

    vertices = []
    offset = 7
    if vertex_count > 0 and offset + 6 <= len(data):
        lat0 = int.from_bytes(data[offset : offset + 3], "big", signed=True) / 10000
        lon0 = int.from_bytes(data[offset + 3 : offset + 6], "big", signed=True) / 10000
        vertices.append((lat0, lon0))
        offset += 6
        for _ in range(vertex_count - 1):
            if offset + 4 > len(data):
                break
            dlat, dlon = struct.unpack_from(">hh", data, offset)
            vertices.append((lat0 + dlat * 0.001, lon0 + dlon * 0.001))
            offset += 4

    headline = data[offset:].decode("utf-8", errors="replace").rstrip("\x00")
    return {
        "type": MSG_WARNING,
        "warning_type": warning_type,
        "severity": severity,
        "expires_unix_min": expires_unix_min,
        "vertices": vertices,
        "headline": headline,
    }


# -- Refresh Request (0x01) -- 4 bytes --

def pack_refresh_request(
    region_id: int, request_type: int, client_newest: int
) -> bytes:
    """Pack a 4-byte refresh request.

    request_type: 0x1=radar, 0x2=warnings, 0x3=both.
    client_newest: minutes since midnight UTC of newest cached data.
    """
    msg = bytearray(4)
    msg[0] = MSG_REFRESH
    msg[1] = ((region_id & 0x0F) << 4) | (request_type & 0x0F)
    struct.pack_into(">H", msg, 2, client_newest & 0xFFFF)
    return bytes(msg)


def unpack_refresh_request(data: bytes) -> dict:
    """Unpack a 4-byte refresh request."""
    if len(data) < 4 or data[0] != MSG_REFRESH:
        raise ValueError("Invalid refresh request")
    return {
        "type": MSG_REFRESH,
        "region_id": (data[1] >> 4) & 0x0F,
        "request_type": data[1] & 0x0F,
        "client_newest": struct.unpack_from(">H", data, 2)[0],
    }


# -- v2: Data types, sky codes, location encoding --

# Data type codes for MSG_DATA_REQUEST (0x02)
DATA_WX = 0x0
DATA_FORECAST = 0x1
DATA_OUTLOOK = 0x2
DATA_STORM_REPORTS = 0x3
DATA_RAIN_OBS = 0x4
DATA_METAR = 0x5
DATA_TAF = 0x6
DATA_WARNINGS_NEAR = 0x7

# Sky condition codes (low nibble in observation messages)
SKY_CLEAR = 0x0
SKY_FEW = 0x1
SKY_SCATTERED = 0x2
SKY_BROKEN = 0x3
SKY_OVERCAST = 0x4
SKY_FOG = 0x5
SKY_SMOKE = 0x6
SKY_HAZE = 0x7
SKY_RAIN = 0x8
SKY_SNOW = 0x9
SKY_THUNDERSTORM = 0xA
SKY_DRIZZLE = 0xB
SKY_MIST = 0xC
SKY_SQUALL = 0xD
SKY_SAND = 0xE
SKY_OTHER = 0xF

# 16-point compass wind direction
COMPASS_POINTS = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
                  "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]


def wind_dir_to_nibble(degrees: int) -> int:
    """Map 0-360° to a 4-bit compass point (0=N, 4=E, 8=S, 12=W)."""
    if degrees < 0 or degrees > 360:
        return 0
    return int(round(degrees / 22.5)) % 16


def nibble_to_wind_dir(nibble: int) -> str:
    """Return the compass abbreviation for a 4-bit wind direction."""
    return COMPASS_POINTS[nibble & 0x0F]


# -- State index (bundled with both server and client) --

_STATE_INDEX_PATH = Path(__file__).parent.parent / "geodata" / "state_index.json"
_STATE_LIST: list[str] | None = None
_STATE_TO_IDX: dict[str, int] | None = None


def _load_state_index() -> None:
    global _STATE_LIST, _STATE_TO_IDX
    if _STATE_LIST is not None:
        return
    with open(_STATE_INDEX_PATH) as f:
        data = json.load(f)
    _STATE_LIST = data["states"]
    _STATE_TO_IDX = {s: i for i, s in enumerate(_STATE_LIST)}


def state_to_idx(state: str) -> int:
    """Map 2-letter state code to 1-byte index. Returns 0xFF if unknown."""
    _load_state_index()
    return _STATE_TO_IDX.get(state.upper(), 0xFF)


def idx_to_state(idx: int) -> str:
    """Map 1-byte index back to 2-letter state code."""
    _load_state_index()
    if 0 <= idx < len(_STATE_LIST):
        return _STATE_LIST[idx]
    return "??"


# -- Location reference encoding --

LOC_ZONE = 0x01      # 3 bytes: state_idx + zone_num (uint16)
LOC_STATION = 0x02   # 4 bytes: ICAO ASCII
LOC_PLACE = 0x03     # 3 bytes: uint24 place_id
LOC_LATLON = 0x04    # 6 bytes: 2x int24 * 10000
LOC_WFO = 0x05       # 3 bytes: WFO ASCII
LOC_PFM_POINT = 0x06 # 3 bytes: uint24 index into client's pfm_points.json


def pack_location(loc_type: int, loc_id) -> bytes:
    """Pack a location reference as type tag + payload.

    loc_type values and expected loc_id formats:
      LOC_ZONE:      str "TXZ192" → 3 bytes (state_idx byte + uint16 zone_num BE)
      LOC_STATION:   str "KAUS" → 4 bytes ASCII
      LOC_PLACE:     int place index → 3 bytes uint24 BE
      LOC_LATLON:    (lat, lon) tuple → 6 bytes (2x int24 * 10000 BE)
      LOC_WFO:       str "EWX" → 3 bytes ASCII
      LOC_PFM_POINT: int pfm_point index → 3 bytes uint24 BE
    """
    if loc_type == LOC_ZONE:
        if not isinstance(loc_id, str) or len(loc_id) != 6 or loc_id[2] != "Z":
            raise ValueError(f"Zone code must be like 'TXZ192', got: {loc_id!r}")
        state = loc_id[:2]
        zone_num = int(loc_id[3:])
        idx = state_to_idx(state)
        return bytes([LOC_ZONE, idx]) + struct.pack(">H", zone_num)

    if loc_type == LOC_STATION:
        if not isinstance(loc_id, str) or len(loc_id) != 4:
            raise ValueError(f"Station ICAO must be 4 chars, got: {loc_id!r}")
        return bytes([LOC_STATION]) + loc_id.upper().encode("ascii")

    if loc_type == LOC_PLACE:
        if not isinstance(loc_id, int) or loc_id < 0 or loc_id >= (1 << 24):
            raise ValueError(f"Place ID must be uint24, got: {loc_id}")
        return bytes([LOC_PLACE]) + loc_id.to_bytes(3, "big")

    if loc_type == LOC_LATLON:
        lat, lon = loc_id
        lat_i = int(round(lat * 10000))
        lon_i = int(round(lon * 10000))
        return bytes([LOC_LATLON]) + lat_i.to_bytes(3, "big", signed=True) + lon_i.to_bytes(3, "big", signed=True)

    if loc_type == LOC_WFO:
        if not isinstance(loc_id, str) or len(loc_id) != 3:
            raise ValueError(f"WFO must be 3 chars, got: {loc_id!r}")
        return bytes([LOC_WFO]) + loc_id.upper().encode("ascii")

    if loc_type == LOC_PFM_POINT:
        if not isinstance(loc_id, int) or loc_id < 0 or loc_id >= (1 << 24):
            raise ValueError(f"PFM point ID must be uint24, got: {loc_id}")
        return bytes([LOC_PFM_POINT]) + loc_id.to_bytes(3, "big")

    raise ValueError(f"Unknown loc_type: {loc_type}")


def unpack_location(data: bytes, offset: int = 0) -> tuple[dict, int]:
    """Unpack a location reference starting at offset.

    Returns (location_dict, new_offset).
    location_dict has 'type' and type-specific fields.
    """
    if offset >= len(data):
        raise ValueError("Location data empty")
    loc_type = data[offset]

    if loc_type == LOC_ZONE:
        if offset + 4 > len(data):
            raise ValueError("Zone location truncated")
        state_idx = data[offset + 1]
        zone_num = struct.unpack_from(">H", data, offset + 2)[0]
        state = idx_to_state(state_idx)
        return (
            {"type": LOC_ZONE, "zone": f"{state}Z{zone_num:03d}"},
            offset + 4,
        )

    if loc_type == LOC_STATION:
        if offset + 5 > len(data):
            raise ValueError("Station location truncated")
        icao = data[offset + 1 : offset + 5].decode("ascii", errors="replace")
        return {"type": LOC_STATION, "station": icao}, offset + 5

    if loc_type == LOC_PLACE:
        if offset + 4 > len(data):
            raise ValueError("Place location truncated")
        place_id = int.from_bytes(data[offset + 1 : offset + 4], "big")
        return {"type": LOC_PLACE, "place_id": place_id}, offset + 4

    if loc_type == LOC_LATLON:
        if offset + 7 > len(data):
            raise ValueError("Lat/lon location truncated")
        lat = int.from_bytes(data[offset + 1 : offset + 4], "big", signed=True) / 10000
        lon = int.from_bytes(data[offset + 4 : offset + 7], "big", signed=True) / 10000
        return {"type": LOC_LATLON, "lat": lat, "lon": lon}, offset + 7

    if loc_type == LOC_WFO:
        if offset + 4 > len(data):
            raise ValueError("WFO location truncated")
        wfo = data[offset + 1 : offset + 4].decode("ascii", errors="replace")
        return {"type": LOC_WFO, "wfo": wfo}, offset + 4

    if loc_type == LOC_PFM_POINT:
        if offset + 4 > len(data):
            raise ValueError("PFM point location truncated")
        pfm_point_id = int.from_bytes(data[offset + 1 : offset + 4], "big")
        return {"type": LOC_PFM_POINT, "pfm_point_id": pfm_point_id}, offset + 4

    raise ValueError(f"Unknown location type: 0x{loc_type:02x}")


# -- Data Request (0x02) --

def pack_data_request(
    data_type: int,
    loc_type: int,
    loc_id,
    client_newest: int = 0,
    flags: int = 0,
) -> bytes:
    """Pack a data request to be sent as a DM to the bot."""
    header = bytes([
        MSG_DATA_REQUEST,
        ((data_type & 0x0F) << 4) | (flags & 0x0F),
    ]) + struct.pack("<H", client_newest & 0xFFFF)
    return header + pack_location(loc_type, loc_id)


def unpack_data_request(data: bytes) -> dict:
    """Unpack a data request."""
    if len(data) < 5 or data[0] != MSG_DATA_REQUEST:
        raise ValueError("Invalid data request")
    data_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    client_newest = struct.unpack_from("<H", data, 2)[0]
    location, _ = unpack_location(data, 4)
    return {
        "type": MSG_DATA_REQUEST,
        "data_type": data_type,
        "flags": flags,
        "client_newest": client_newest,
        "location": location,
    }


# -- Not Available (0x03) — bot tells client "I can't serve that request" --
#
# Wire format:
#   byte 0     : 0x03 MSG_NOT_AVAILABLE
#   byte 1     : data_type (hi nibble) | reason_code (lo nibble)
#   bytes 2..N : location reference (same encoding as the request, echoed
#                so the client can correlate the response with its
#                outstanding request)
#
# Total: 6-9 bytes depending on location type.
#
# Purpose: without this, a client that sent a request and hit any of the
# silent-drop paths in respond_to_data_request (unresolvable location,
# no data in cache, builder exception, unsupported data_type) would spin
# indefinitely. This message lets the bot say "request received and
# understood, but I don't have data for you right now" so the client can
# show an appropriate UI and stop retrying.
#
# Cached and double-transmitted just like any other v2 response.

# Reason codes — low nibble of byte 1
REASON_NO_DATA = 0x0               # Bot parsed the request and looked up the
                                   # source product, but nothing is in the cache
                                   # for this specific location/product right now
REASON_LOCATION_UNRESOLVABLE = 0x1  # Location tag didn't resolve (unknown zone,
                                    # out-of-range pfm index, etc.)
REASON_PRODUCT_UNSUPPORTED = 0x2    # data_type is defined in the protocol but
                                    # the bot has no builder wired up for it
REASON_BOT_ERROR = 0x3              # Builder raised an exception
REASON_UNKNOWN = 0xF                # Fallback / catch-all


def pack_not_available(
    data_type: int,
    reason: int,
    loc_type: int,
    loc_id,
) -> bytes:
    """Pack a 0x03 Not Available response.

    Echoes back the data_type and location so the client can correlate
    the response with its outstanding request. The reason code tells
    the client why the request couldn't be served, which drives UI
    behavior (show "no data", show "try again later", etc.).
    """
    header = bytes([
        MSG_NOT_AVAILABLE,
        ((data_type & 0x0F) << 4) | (reason & 0x0F),
    ])
    return header + pack_location(loc_type, loc_id)


def unpack_not_available(data: bytes) -> dict:
    """Unpack a 0x03 Not Available response."""
    if len(data) < 3 or data[0] != MSG_NOT_AVAILABLE:
        raise ValueError("Invalid not-available message")
    data_type = (data[1] >> 4) & 0x0F
    reason = data[1] & 0x0F
    location, _ = unpack_location(data, 2)
    return {
        "type": MSG_NOT_AVAILABLE,
        "data_type": data_type,
        "reason": reason,
        "location": location,
    }


# -- Observation (0x30) -- current conditions --

def pack_observation(
    loc_type: int,
    loc_id,
    timestamp_utc_min: int,
    temp_f: int,
    dewpoint_f: int,
    wind_dir_deg: int,
    sky_code: int,
    wind_speed_mph: int,
    wind_gust_mph: int = 0,
    visibility_mi: int = 10,
    pressure_inhg: float = 29.92,
    feels_like_delta: int = 0,
) -> bytes:
    """Pack a current-conditions observation message."""

    def _clamp_i8(v: int) -> int:
        return max(-128, min(127, int(v)))

    def _clamp_u8(v: int) -> int:
        return max(0, min(255, int(v)))

    loc_bytes = pack_location(loc_type, loc_id)
    pressure_byte = max(0, min(255, int(round((pressure_inhg - 29.00) * 100))))
    wind_dir_nib = wind_dir_to_nibble(wind_dir_deg) if wind_dir_deg is not None else 0
    packed_dir_sky = ((wind_dir_nib & 0x0F) << 4) | (sky_code & 0x0F)

    msg = bytearray()
    msg.append(MSG_OBSERVATION)
    msg.extend(loc_bytes)
    msg.extend(struct.pack("<H", timestamp_utc_min & 0xFFFF))
    msg.append(_clamp_i8(temp_f) & 0xFF)
    msg.append(_clamp_i8(dewpoint_f) & 0xFF)
    msg.append(packed_dir_sky)
    msg.append(_clamp_u8(wind_speed_mph))
    msg.append(_clamp_u8(wind_gust_mph))
    msg.append(_clamp_u8(visibility_mi))
    msg.append(pressure_byte)
    msg.append(_clamp_i8(feels_like_delta) & 0xFF)
    return bytes(msg)


def unpack_observation(data: bytes) -> dict:
    """Unpack an observation message."""
    if len(data) < 2 or data[0] != MSG_OBSERVATION:
        raise ValueError("Invalid observation message")
    location, offset = unpack_location(data, 1)
    if offset + 10 > len(data):
        raise ValueError("Observation truncated")
    timestamp = struct.unpack_from("<H", data, offset)[0]
    temp = struct.unpack_from("b", data, offset + 2)[0]
    dewpoint = struct.unpack_from("b", data, offset + 3)[0]
    dir_sky = data[offset + 4]
    wind_dir_nib = (dir_sky >> 4) & 0x0F
    sky_code = dir_sky & 0x0F
    wind_speed = data[offset + 5]
    wind_gust = data[offset + 6]
    visibility = data[offset + 7]
    pressure = 29.00 + data[offset + 8] / 100
    feels_like_delta = struct.unpack_from("b", data, offset + 9)[0]
    return {
        "type": MSG_OBSERVATION,
        "location": location,
        "timestamp_utc_min": timestamp,
        "temp_f": temp,
        "dewpoint_f": dewpoint,
        "wind_dir": nibble_to_wind_dir(wind_dir_nib),
        "wind_dir_nibble": wind_dir_nib,
        "sky_code": sky_code,
        "wind_speed_mph": wind_speed,
        "wind_gust_mph": wind_gust,
        "visibility_mi": visibility,
        "pressure_inhg": round(pressure, 2),
        "feels_like_delta": feels_like_delta,
        "feels_like_f": temp + feels_like_delta,
    }


# -- Forecast (0x31) -- multi-period forecast --

def pack_forecast(
    loc_type: int,
    loc_id,
    issued_hours_ago: int,
    periods: list[dict],
) -> bytes:
    """Pack a multi-period forecast message.

    periods: list of dicts with keys:
      period_id, high_f, low_f, sky_code, precip_pct,
      wind_dir_nibble, wind_speed_5mph, condition_flags
    """
    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_FORECAST)
    msg.extend(loc_bytes)
    msg.append(max(0, min(255, int(issued_hours_ago))))
    msg.append(min(len(periods), 255))
    for p in periods:
        high = p.get("high_f", 127)
        low = p.get("low_f", 127)
        msg.append(max(0, min(255, p.get("period_id", 0))))
        msg.append(max(-128, min(127, int(high))) & 0xFF)
        msg.append(max(-128, min(127, int(low))) & 0xFF)
        msg.append(p.get("sky_code", 0) & 0x0F)
        msg.append(max(0, min(100, int(p.get("precip_pct", 0)))))
        wind_byte = ((p.get("wind_dir_nibble", 0) & 0x0F) << 4) | (
            p.get("wind_speed_5mph", 0) & 0x0F
        )
        msg.append(wind_byte)
        msg.append(p.get("condition_flags", 0) & 0xFF)
    return bytes(msg)


def unpack_forecast(data: bytes) -> dict:
    """Unpack a forecast message."""
    if len(data) < 2 or data[0] != MSG_FORECAST:
        raise ValueError("Invalid forecast message")
    location, offset = unpack_location(data, 1)
    if offset + 2 > len(data):
        raise ValueError("Forecast header truncated")
    issued_hours_ago = data[offset]
    count = data[offset + 1]
    offset += 2
    periods = []
    for _ in range(count):
        if offset + 7 > len(data):
            break
        pid = data[offset]
        high = struct.unpack_from("b", data, offset + 1)[0]
        low = struct.unpack_from("b", data, offset + 2)[0]
        sky = data[offset + 3]
        precip = data[offset + 4]
        wind_byte = data[offset + 5]
        flags = data[offset + 6]
        periods.append({
            "period_id": pid,
            "high_f": None if high == 127 else high,
            "low_f": None if low == 127 else low,
            "sky_code": sky,
            "precip_pct": precip,
            "wind_dir_nibble": (wind_byte >> 4) & 0x0F,
            "wind_dir": nibble_to_wind_dir((wind_byte >> 4) & 0x0F),
            "wind_speed_5mph": wind_byte & 0x0F,
            "wind_speed_mph": (wind_byte & 0x0F) * 5,
            "condition_flags": flags,
        })
        offset += 7
    return {
        "type": MSG_FORECAST,
        "location": location,
        "issued_hours_ago": issued_hours_ago,
        "periods": periods,
    }


# -- Zone-coded Warning (0x21) --
# Clients with preloaded NWS zone polygons render the warning area from the
# zone codes alone — no vertex data needed. Eliminates polygon crossing
# artifacts entirely since geometry is canonical.

def pack_warning_zones(
    warning_type: int,
    severity: int,
    expires_unix_min: int,
    zones: list[str],
    headline: str,
) -> bytes:
    """Pack a zone-coded warning (v3 wire format).

    Wire format:
      byte 0     : MSG_WARNING_ZONES (0x21)
      byte 1     : warning_type (hi nibble) | severity (lo nibble)
      bytes 2-5  : expires_unix_min (uint32 BE)
      byte 6     : zone_count (max 30)
      bytes 7..  : zone_count * 3 bytes (state_idx + uint16 zone_num)
      remainder  : headline, UTF-8, word-boundary truncated

    zones: list of NWS zone codes like ["TXZ192", "TXZ193"]. Max 30 per message.
    Client uses its preloaded zone polygon data to render the affected area.
    """
    msg = bytearray()
    msg.append(MSG_WARNING_ZONES)
    msg.append(((warning_type & 0x0F) << 4) | (severity & 0x0F))
    msg.extend(struct.pack(">I", expires_unix_min & 0xFFFFFFFF))
    msg.append(min(len(zones), 30))

    # Pack each zone as 3 bytes: state_idx (1) + zone_num (uint16 BE)
    for zone_code in zones[:30]:
        if len(zone_code) != 6 or zone_code[2] != "Z":
            continue
        try:
            state_idx = state_to_idx(zone_code[:2])
            zone_num = int(zone_code[3:])
        except (ValueError, KeyError):
            continue
        msg.append(state_idx & 0xFF)
        msg.extend(struct.pack(">H", zone_num & 0xFFFF))

    # Headline fills remaining space, word-boundary truncated
    remaining = 136 - len(msg)
    if remaining > 0 and headline:
        msg.extend(_fit_headline(headline, remaining))
    return bytes(msg)


def unpack_warning_zones(data: bytes) -> dict:
    """Unpack a v3 zone-coded warning message."""
    if len(data) < 7 or data[0] != MSG_WARNING_ZONES:
        raise ValueError("Invalid zone-coded warning")
    warning_type = (data[1] >> 4) & 0x0F
    severity = data[1] & 0x0F
    expires_unix_min = struct.unpack_from(">I", data, 2)[0]
    zone_count = data[6]

    zones = []
    offset = 7
    for _ in range(zone_count):
        if offset + 3 > len(data):
            break
        state_idx = data[offset]
        zone_num = struct.unpack_from(">H", data, offset + 1)[0]
        state = idx_to_state(state_idx)
        zones.append(f"{state}Z{zone_num:03d}")
        offset += 3

    headline = data[offset:].decode("utf-8", errors="replace").rstrip("\x00")
    return {
        "type": MSG_WARNING_ZONES,
        "warning_type": warning_type,
        "severity": severity,
        "expires_unix_min": expires_unix_min,
        "zones": zones,
        "headline": headline,
    }


# -- Hazard Outlook (0x32) -- 1-7 day HWO --

# Hazard type codes
HAZARD_THUNDERSTORM = 0x0
HAZARD_SEVERE_THUNDER = 0x1
HAZARD_TORNADO = 0x2
HAZARD_FLOOD = 0x3
HAZARD_FLASH_FLOOD = 0x4
HAZARD_EXCESSIVE_HEAT = 0x5
HAZARD_WINTER_STORM = 0x6
HAZARD_BLIZZARD = 0x7
HAZARD_ICE = 0x8
HAZARD_HIGH_WIND = 0x9
HAZARD_FIRE_WEATHER = 0xA
HAZARD_DENSE_FOG = 0xB
HAZARD_RIP_CURRENT = 0xC
HAZARD_HURRICANE = 0xD
HAZARD_MARINE = 0xE
HAZARD_OTHER = 0xF

# Risk levels
RISK_NONE = 0
RISK_SLIGHT = 1
RISK_LIMITED = 2
RISK_ENHANCED = 3
RISK_MODERATE = 4
RISK_HIGH = 5
RISK_EXTREME = 6


def pack_outlook(
    loc_type: int,
    loc_id,
    issued_utc_min: int,
    days: list[dict],
) -> bytes:
    """Pack a 0x32 hazard outlook message.

    days: list of {"day_offset": 1-7, "hazards": [(hazard_type, risk_level), ...]}
    """
    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_OUTLOOK)
    msg.extend(loc_bytes)
    msg.extend(struct.pack("<H", issued_utc_min & 0xFFFF))
    msg.append(min(len(days), 7))
    for day in days[:7]:
        hazards = day.get("hazards", [])
        msg.append(day.get("day_offset", 1) & 0xFF)
        msg.append(min(len(hazards), 15))
        for h_type, risk in hazards[:15]:
            msg.append(h_type & 0xFF)
            msg.append(risk & 0xFF)
    return bytes(msg)


def unpack_outlook(data: bytes) -> dict:
    """Unpack a hazard outlook message."""
    if len(data) < 2 or data[0] != MSG_OUTLOOK:
        raise ValueError("Invalid outlook message")
    location, offset = unpack_location(data, 1)
    if offset + 3 > len(data):
        raise ValueError("Outlook header truncated")
    issued = struct.unpack_from("<H", data, offset)[0]
    day_count = data[offset + 2]
    offset += 3
    days = []
    for _ in range(day_count):
        if offset + 2 > len(data):
            break
        day_offset = data[offset]
        hazard_count = data[offset + 1]
        offset += 2
        hazards = []
        for _ in range(hazard_count):
            if offset + 2 > len(data):
                break
            h_type = data[offset]
            risk = data[offset + 1]
            hazards.append({"hazard_type": h_type, "risk_level": risk})
            offset += 2
        days.append({"day_offset": day_offset, "hazards": hazards})
    return {
        "type": MSG_OUTLOOK,
        "location": location,
        "issued_utc_min": issued,
        "days": days,
    }


# -- Storm Reports (0x33) -- LSR list --

# Event type codes
EVENT_TORNADO = 0x0
EVENT_FUNNEL = 0x1
EVENT_HAIL = 0x2
EVENT_WIND_DAMAGE = 0x3
EVENT_NON_TSTM_WIND = 0x4
EVENT_TSTM_WIND = 0x5
EVENT_FLOOD = 0x6
EVENT_FLASH_FLOOD = 0x7
EVENT_HEAVY_RAIN = 0x8
EVENT_SNOW = 0x9
EVENT_ICE = 0xA
EVENT_LIGHTNING = 0xB
EVENT_DEBRIS_FLOW = 0xC
EVENT_OTHER = 0xF


def pack_storm_reports(
    loc_type: int,
    loc_id,
    reports: list[dict],
) -> bytes:
    """Pack a 0x33 storm reports message.

    reports: list of dicts with:
      event_type: int event code
      magnitude: uint8 (hail size * 4 for inches, mph for wind)
      minutes_ago: uint16
      place_id: uint24 (index into preloaded places.json)
    """
    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_STORM_REPORTS)
    msg.extend(loc_bytes)
    # Max reports that fit: (136 - header) / 7 bytes each
    max_reports = (136 - 2 - len(loc_bytes)) // 7
    report_count = min(len(reports), max_reports, 255)
    msg.append(report_count)
    for r in reports[:report_count]:
        msg.append(r.get("event_type", EVENT_OTHER) & 0xFF)
        msg.append(max(0, min(255, int(r.get("magnitude", 0)))))
        msg.extend(struct.pack("<H", max(0, min(0xFFFF, r.get("minutes_ago", 0)))))
        place_id = max(0, min(0xFFFFFF, r.get("place_id", 0)))
        msg.extend(place_id.to_bytes(3, "big"))
    return bytes(msg)


def unpack_storm_reports(data: bytes) -> dict:
    """Unpack a storm reports message."""
    if len(data) < 2 or data[0] != MSG_STORM_REPORTS:
        raise ValueError("Invalid storm reports message")
    location, offset = unpack_location(data, 1)
    if offset + 1 > len(data):
        raise ValueError("Storm reports header truncated")
    report_count = data[offset]
    offset += 1
    reports = []
    for _ in range(report_count):
        if offset + 7 > len(data):
            break
        reports.append({
            "event_type": data[offset],
            "magnitude": data[offset + 1],
            "minutes_ago": struct.unpack_from("<H", data, offset + 2)[0],
            "place_id": int.from_bytes(data[offset + 4 : offset + 7], "big"),
        })
        offset += 7
    return {
        "type": MSG_STORM_REPORTS,
        "location": location,
        "reports": reports,
    }


# -- Rain Observations (0x34) --

# Rain type codes (reuse sky codes where they overlap)
RAIN_LIGHT = 0x0
RAIN_MODERATE = 0x1
RAIN_HEAVY = 0x2
RAIN_SHOWER = 0x3
RAIN_TSTORM = 0x4
RAIN_DRIZZLE = 0x5
RAIN_SNOW = 0x6
RAIN_FREEZING = 0x7
RAIN_MIX = 0x8


def pack_rain_obs(
    loc_type: int,
    loc_id,
    timestamp_utc_min: int,
    cities: list[dict],
) -> bytes:
    """Pack a 0x34 rain observations message.

    cities: list of {"place_id": int, "rain_type": int, "temp_f": int}
    """
    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_RAIN_OBS)
    msg.extend(loc_bytes)
    msg.extend(struct.pack("<H", timestamp_utc_min & 0xFFFF))

    max_cities = (136 - 4 - len(loc_bytes)) // 5
    city_count = min(len(cities), max_cities, 255)
    msg.append(city_count)
    for c in cities[:city_count]:
        place_id = max(0, min(0xFFFFFF, c.get("place_id", 0)))
        msg.extend(place_id.to_bytes(3, "big"))
        msg.append(c.get("rain_type", RAIN_LIGHT) & 0xFF)
        msg.append(max(-128, min(127, int(c.get("temp_f", 60)))) & 0xFF)
    return bytes(msg)


def unpack_rain_obs(data: bytes) -> dict:
    """Unpack a rain observations message."""
    if len(data) < 2 or data[0] != MSG_RAIN_OBS:
        raise ValueError("Invalid rain obs message")
    location, offset = unpack_location(data, 1)
    if offset + 3 > len(data):
        raise ValueError("Rain obs header truncated")
    timestamp = struct.unpack_from("<H", data, offset)[0]
    city_count = data[offset + 2]
    offset += 3
    cities = []
    for _ in range(city_count):
        if offset + 5 > len(data):
            break
        cities.append({
            "place_id": int.from_bytes(data[offset : offset + 3], "big"),
            "rain_type": data[offset + 3],
            "temp_f": struct.unpack_from("b", data, offset + 4)[0],
        })
        offset += 5
    return {
        "type": MSG_RAIN_OBS,
        "location": location,
        "timestamp_utc_min": timestamp,
        "cities": cities,
    }


# -- Warnings Near location (0x37) v3 --
# Summary reply: list of warnings currently active at the given location.
# Each entry is (type, severity, absolute expiry, zone) so the client can
# look up the full details from its cache of previously-received 0x21/0x20.
#
# Per-entry layout (8 bytes):
#   1 byte  : warning_type (hi nibble) | severity (lo nibble)
#   4 bytes : expires_unix_min (uint32 BE)  — NWS-authoritative absolute expiry
#   3 bytes : zone reference (state_idx + uint16 zone_num)

def pack_warnings_near(
    loc_type: int,
    loc_id,
    warnings: list[dict],
) -> bytes:
    """Pack a 0x37 'warnings near location' summary (v3).

    warnings: list of {"warning_type", "severity", "expires_unix_min", "zone"}
    where zone is an optional 6-char NWS zone code for quick lookup.
    """
    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_WARNINGS_NEAR)
    msg.extend(loc_bytes)
    max_entries = (136 - 2 - len(loc_bytes)) // 8
    msg.append(min(len(warnings), max_entries))
    for w in warnings[:max_entries]:
        msg.append(((w.get("warning_type", WARN_OTHER) & 0x0F) << 4) | (w.get("severity", 0) & 0x0F))
        msg.extend(struct.pack(">I", w.get("expires_unix_min", 0) & 0xFFFFFFFF))
        zone = w.get("zone", "")
        if len(zone) == 6 and zone[2] == "Z":
            try:
                state_idx = state_to_idx(zone[:2])
                zone_num = int(zone[3:])
                msg.append(state_idx & 0xFF)
                msg.extend(struct.pack(">H", zone_num & 0xFFFF))
            except ValueError:
                msg.extend(b"\x00\x00\x00")
        else:
            msg.extend(b"\x00\x00\x00")
    return bytes(msg)


def unpack_warnings_near(data: bytes) -> dict:
    """Unpack a v3 warnings-near summary."""
    if len(data) < 2 or data[0] != MSG_WARNINGS_NEAR:
        raise ValueError("Invalid warnings-near message")
    location, offset = unpack_location(data, 1)
    if offset + 1 > len(data):
        raise ValueError("Warnings-near header truncated")
    count = data[offset]
    offset += 1
    warnings = []
    for _ in range(count):
        if offset + 8 > len(data):
            break
        type_sev = data[offset]
        expires_unix_min = struct.unpack_from(">I", data, offset + 1)[0]
        state_idx = data[offset + 5]
        zone_num = struct.unpack_from(">H", data, offset + 6)[0]
        state = idx_to_state(state_idx)
        zone = f"{state}Z{zone_num:03d}" if state != "??" else ""
        warnings.append({
            "warning_type": (type_sev >> 4) & 0x0F,
            "severity": type_sev & 0x0F,
            "expires_unix_min": expires_unix_min,
            "zone": zone,
        })
        offset += 8
    return {
        "type": MSG_WARNINGS_NEAR,
        "location": location,
        "warnings": warnings,
    }


# -- TAF (0x36) — Terminal Aerodrome Forecast snapshot --
#
# Wire format (v3, single-snapshot — multi-period TAFs are future work):
#
#   byte 0     : 0x36  MSG_TAF
#   byte 1..5  : LOC_STATION reference (5 bytes: 0x02 + 4-byte ICAO ASCII)
#   byte 6     : issued_hours_ago (uint8) — hours since the TAF was issued
#   byte 7     : valid_from_hour (uint8, 0-23 UTC)
#   byte 8     : valid_to_hour (uint8, 0-23 UTC; may wrap past midnight)
#   byte 9     : wind_dir_nibble (high) | wind_speed_5kt_nibble (low)
#                wind speed unit is 5 kt (so a value of 3 = 15 kt, max 75 kt)
#   byte 10    : wind_gust_kt (uint8, 0 = no gust)
#   byte 11    : visibility_qsm (uint8, 1/4 statute mile units;
#                40 = 10 sm, 64+ = 16 sm = "10+" / unlimited)
#   byte 12    : ceiling_100ft (uint8, 0 = no ceiling, 1 = 100 ft, 250 = 25000+ ft)
#   byte 13    : sky_code (low nibble) — same table as 0x30
#   byte 14    : weather flags (uint8 bitfield):
#                  bit 0 (0x01): rain
#                  bit 1 (0x02): snow
#                  bit 2 (0x04): thunderstorm
#                  bit 3 (0x08): freezing precipitation
#                  bit 4 (0x10): mist / fog
#                  bit 5 (0x20): showers
#                  bit 6 (0x40): heavy
#                  bit 7 (0x80): light
#
# Total: 15 bytes. Carries the BASE forecast group of a TAF for one
# station. Multi-period TAFs (FROM/BECMG/TEMPO change groups) are not yet
# expressed on the wire — when we add them this becomes a list of these
# 9-byte snapshots after a count byte.

TAF_WX_RAIN = 0x01
TAF_WX_SNOW = 0x02
TAF_WX_TSTM = 0x04
TAF_WX_FREEZING = 0x08
TAF_WX_FOG = 0x10
TAF_WX_SHOWERS = 0x20
TAF_WX_HEAVY = 0x40
TAF_WX_LIGHT = 0x80


def pack_taf(
    station_icao: str,
    issued_hours_ago: int,
    valid_from_hour: int,
    valid_to_hour: int,
    wind_dir_nibble: int,
    wind_speed_5kt: int,
    wind_gust_kt: int,
    visibility_qsm: int,
    ceiling_100ft: int,
    sky_code: int,
    weather_flags: int,
) -> bytes:
    """Pack a 0x36 TAF snapshot message."""
    msg = bytearray()
    msg.append(MSG_TAF)
    msg.extend(pack_location(LOC_STATION, station_icao))
    msg.append(max(0, min(255, int(issued_hours_ago))))
    msg.append(max(0, min(23, int(valid_from_hour))))
    msg.append(max(0, min(23, int(valid_to_hour))))
    msg.append(((wind_dir_nibble & 0x0F) << 4) | (wind_speed_5kt & 0x0F))
    msg.append(max(0, min(255, int(wind_gust_kt))))
    msg.append(max(0, min(255, int(visibility_qsm))))
    msg.append(max(0, min(255, int(ceiling_100ft))))
    msg.append(sky_code & 0x0F)
    msg.append(weather_flags & 0xFF)
    return bytes(msg)


def unpack_taf(data: bytes) -> dict:
    """Unpack a 0x36 TAF snapshot message."""
    if len(data) < 15 or data[0] != MSG_TAF:
        raise ValueError("Invalid TAF message")
    location, offset = unpack_location(data, 1)  # offset should be 6
    issued_hours_ago = data[offset]
    valid_from_hour = data[offset + 1]
    valid_to_hour = data[offset + 2]
    dir_spd = data[offset + 3]
    wind_dir_nib = (dir_spd >> 4) & 0x0F
    wind_spd_5kt = dir_spd & 0x0F
    wind_gust_kt = data[offset + 4]
    visibility_qsm = data[offset + 5]
    ceiling_100ft = data[offset + 6]
    sky_code = data[offset + 7] & 0x0F
    weather_flags = data[offset + 8]
    return {
        "type": MSG_TAF,
        "location": location,
        "issued_hours_ago": issued_hours_ago,
        "valid_from_hour": valid_from_hour,
        "valid_to_hour": valid_to_hour,
        "wind_dir": nibble_to_wind_dir(wind_dir_nib),
        "wind_dir_nibble": wind_dir_nib,
        "wind_speed_kt": wind_spd_5kt * 5,
        "wind_gust_kt": wind_gust_kt,
        "visibility_sm": round(visibility_qsm / 4.0, 2),
        "ceiling_ft": ceiling_100ft * 100,
        "sky_code": sky_code,
        "weather_flags": weather_flags,
    }


# -- Discovery Beacon (0xF0) — bot announces itself on #meshwx-discover ------

# Beacon flag bits
BEACON_ACCEPTING_REQUESTS = 0x01
BEACON_HAS_RADAR = 0x02
BEACON_HAS_WARNINGS = 0x04
BEACON_HAS_FORECASTS = 0x08
BEACON_HAS_FIRE_WEATHER = 0x10
BEACON_HAS_NOWCAST = 0x20
BEACON_HAS_QPF = 0x40


def pack_beacon(
    bot_id: int,
    beacon_flags: int,
    lat: float,
    lon: float,
    radius_km: int,
    active_warnings: int,
    channel_name: str,
) -> bytes:
    """Pack a discovery beacon message.

    bot_id: uint24 unique per deployment (e.g. hash of pubkey)
    beacon_flags: capability bits (see BEACON_* constants)
    lat/lon: coverage center
    radius_km: coverage radius
    active_warnings: count of active warnings
    channel_name: deployment channel name (e.g. "aus-meshwx-v4")
    """
    msg = bytearray()
    msg.append(MSG_BEACON)
    msg.append(V4_VERSION)  # protocol version
    # bot_id uint24 BE
    msg.append((bot_id >> 16) & 0xFF)
    msg.append((bot_id >> 8) & 0xFF)
    msg.append(bot_id & 0xFF)
    msg.append(beacon_flags & 0xFF)
    # coverage center: int16 lat*100, int16 lon*100
    lat_i = max(-32768, min(32767, int(round(lat * 100))))
    lon_i = max(-32768, min(32767, int(round(lon * 100))))
    msg.extend(struct.pack(">hh", lat_i, lon_i))
    msg.append(min(255, max(0, radius_km)))
    msg.append(min(255, max(0, active_warnings)))
    # channel name
    name_bytes = channel_name.encode("utf-8")[:32]
    msg.append(len(name_bytes))
    msg.extend(name_bytes)
    return bytes(msg)


def unpack_beacon(data: bytes) -> dict:
    """Unpack a discovery beacon message."""
    if len(data) < 12 or data[0] != MSG_BEACON:
        raise ValueError("Invalid beacon message")
    protocol_version = data[1]
    bot_id = (data[2] << 16) | (data[3] << 8) | data[4]
    flags = data[5]
    lat = struct.unpack_from(">h", data, 6)[0] / 100.0
    lon = struct.unpack_from(">h", data, 8)[0] / 100.0
    radius_km = data[10]
    active_warnings = data[11]
    name_len = data[12] if len(data) > 12 else 0
    channel_name = data[13:13 + name_len].decode("utf-8", errors="replace") if name_len else ""

    return {
        "type": MSG_BEACON,
        "protocol_version": protocol_version,
        "bot_id": bot_id,
        "beacon_flags": flags,
        "accepting_requests": bool(flags & BEACON_ACCEPTING_REQUESTS),
        "has_radar": bool(flags & BEACON_HAS_RADAR),
        "has_warnings": bool(flags & BEACON_HAS_WARNINGS),
        "has_forecasts": bool(flags & BEACON_HAS_FORECASTS),
        "has_fire_weather": bool(flags & BEACON_HAS_FIRE_WEATHER),
        "has_nowcast": bool(flags & BEACON_HAS_NOWCAST),
        "has_qpf": bool(flags & BEACON_HAS_QPF),
        "lat": lat,
        "lon": lon,
        "radius_km": radius_km,
        "active_warnings": active_warnings,
        "channel_name": channel_name,
    }


# -- Fire Weather Forecast (0x38) — FWF per-zone fire weather ----------------


def pack_fire_weather(
    loc_type: int,
    loc_id,
    issued_hours_ago: int,
    periods: list[dict],
) -> bytes:
    """Pack a fire weather forecast message.

    periods: list of dicts with keys:
      period_id, max_temp_f, min_rh_pct, transport_wind_dir_nibble,
      transport_wind_speed_5mph, mixing_height_500ft, haines_index,
      lightning_risk, cloud_cover, weather_byte
    """
    def _clamp_i8(v: int) -> int:
        return max(-128, min(127, int(v)))

    def _clamp_u8(v: int) -> int:
        return max(0, min(255, int(v)))

    loc_bytes = pack_location(loc_type, loc_id)
    msg = bytearray()
    msg.append(MSG_FIRE_WEATHER)
    msg.extend(loc_bytes)
    msg.append(_clamp_u8(issued_hours_ago))
    msg.append(len(periods) & 0xFF)

    for p in periods[:7]:
        msg.append(p.get("period_id", 0) & 0xFF)
        msg.append(_clamp_i8(p.get("max_temp_f", 127)) & 0xFF)
        msg.append(_clamp_u8(p.get("min_rh_pct", 0)))
        wind_byte = ((p.get("transport_wind_dir_nibble", 0) & 0x0F) << 4) | \
                    (p.get("transport_wind_speed_5mph", 0) & 0x0F)
        msg.append(wind_byte)
        msg.append(_clamp_u8(p.get("mixing_height_500ft", 0)))
        haines_lightning = ((p.get("lightning_risk", 0) & 0x0F) << 4) | \
                          (p.get("haines_index", 2) & 0x0F)
        msg.append(haines_lightning)
        msg.append(_clamp_u8(p.get("cloud_cover", 0)))
        msg.append(_clamp_u8(p.get("weather_byte", 0)))

    return bytes(msg)


def unpack_fire_weather(data: bytes) -> dict:
    """Unpack a fire weather forecast message."""
    if len(data) < 2 or data[0] != MSG_FIRE_WEATHER:
        raise ValueError("Invalid fire weather message")
    location, offset = unpack_location(data, 1)
    if offset + 2 > len(data):
        raise ValueError("Fire weather truncated")
    issued_hours_ago = data[offset]
    period_count = data[offset + 1]
    offset += 2

    periods = []
    for _ in range(period_count):
        if offset + 8 > len(data):
            break
        wind_byte = data[offset + 3]
        haines_lightning = data[offset + 5]
        periods.append({
            "period_id": data[offset],
            "max_temp_f": struct.unpack_from("b", data, offset + 1)[0],
            "min_rh_pct": data[offset + 2],
            "transport_wind_dir_nibble": (wind_byte >> 4) & 0x0F,
            "transport_wind_speed_5mph": wind_byte & 0x0F,
            "mixing_height_500ft": data[offset + 4],
            "lightning_risk": (haines_lightning >> 4) & 0x0F,
            "haines_index": haines_lightning & 0x0F,
            "cloud_cover": data[offset + 6],
            "weather_byte": data[offset + 7],
        })
        offset += 8

    return {
        "type": MSG_FIRE_WEATHER,
        "location": location,
        "issued_hours_ago": issued_hours_ago,
        "periods": periods,
    }


# -- Daily Climate (0x3A) — RTP regional temp/precip summary ----------------


def pack_daily_climate(
    report_day_offset: int,
    cities: list[dict],
) -> bytes:
    """Pack a daily climate summary message.

    cities: list of dicts with keys:
      place_id (uint24), max_temp_f, min_temp_f,
      precip_hundredths (0xFF=trace, 0xFE=missing),
      snow_tenths (0xFF=trace, 0xFE=missing)
    """
    def _clamp_i8(v: int) -> int:
        return max(-128, min(127, int(v)))

    def _clamp_u8(v: int) -> int:
        return max(0, min(255, int(v)))

    msg = bytearray()
    msg.append(MSG_DAILY_CLIMATE)
    msg.append(min(18, len(cities)) & 0xFF)
    msg.append(_clamp_u8(report_day_offset))

    for c in cities[:18]:
        pid = c.get("place_id", 0)
        msg.append((pid >> 16) & 0xFF)
        msg.append((pid >> 8) & 0xFF)
        msg.append(pid & 0xFF)
        msg.append(_clamp_i8(c.get("max_temp_f", 127)) & 0xFF)
        msg.append(_clamp_i8(c.get("min_temp_f", 127)) & 0xFF)
        msg.append(_clamp_u8(c.get("precip_hundredths", 0xFE)))
        msg.append(_clamp_u8(c.get("snow_tenths", 0xFE)))

    return bytes(msg)


def unpack_daily_climate(data: bytes) -> dict:
    """Unpack a daily climate summary message."""
    if len(data) < 3 or data[0] != MSG_DAILY_CLIMATE:
        raise ValueError("Invalid daily climate message")
    city_count = data[1]
    report_day_offset = data[2]
    offset = 3

    cities = []
    for _ in range(city_count):
        if offset + 7 > len(data):
            break
        place_id = (data[offset] << 16) | (data[offset + 1] << 8) | data[offset + 2]
        cities.append({
            "place_id": place_id,
            "max_temp_f": struct.unpack_from("b", data, offset + 3)[0],
            "min_temp_f": struct.unpack_from("b", data, offset + 4)[0],
            "precip_hundredths": data[offset + 5],
            "snow_tenths": data[offset + 6],
        })
        offset += 7

    return {
        "type": MSG_DAILY_CLIMATE,
        "city_count": city_count,
        "report_day_offset": report_day_offset,
        "cities": cities,
    }


# -- Nowcast (0x3C) — short-term forecast (NOW) -----------------------------


def pack_nowcast(
    loc_type: int,
    loc_id,
    valid_hours: int,
    urgency_flags: int,
    text: str,
) -> bytes:
    """Pack a short-term forecast nowcast message.

    urgency_flags bits: 0=thunder, 1=flooding, 2=winter, 3=fire, 4=wind

    If the text exceeds single-frame capacity, it is truncated. The caller
    should send overflow via pack_text_chunks() with TEXT_SUBJECT_NOWCAST.
    """
    loc_bytes = pack_location(loc_type, loc_id)
    header_size = 1 + len(loc_bytes) + 2  # msg_type + loc + valid_hours + urgency
    max_text = 136 - header_size

    encoded_text = text.encode("utf-8", errors="replace")
    if len(encoded_text) > max_text:
        encoded_text = encoded_text[:max_text]
        # Walk back to avoid splitting a UTF-8 codepoint
        encoded_text = encoded_text.decode("utf-8", errors="ignore").encode("utf-8")

    msg = bytearray()
    msg.append(MSG_NOWCAST)
    msg.extend(loc_bytes)
    msg.append(max(0, min(255, valid_hours)))
    msg.append(urgency_flags & 0xFF)
    msg.extend(encoded_text)
    return bytes(msg)


def unpack_nowcast(data: bytes) -> dict:
    """Unpack a nowcast message."""
    if len(data) < 2 or data[0] != MSG_NOWCAST:
        raise ValueError("Invalid nowcast message")
    location, offset = unpack_location(data, 1)
    if offset + 2 > len(data):
        raise ValueError("Nowcast truncated")
    valid_hours = data[offset]
    urgency_flags = data[offset + 1]
    text_payload = data[offset + 2:].decode("utf-8", errors="replace")

    return {
        "type": MSG_NOWCAST,
        "location": location,
        "valid_hours": valid_hours,
        "urgency_flags": urgency_flags,
        "has_thunder": bool(urgency_flags & 0x01),
        "has_flooding": bool(urgency_flags & 0x02),
        "has_winter": bool(urgency_flags & 0x04),
        "has_fire": bool(urgency_flags & 0x08),
        "has_wind": bool(urgency_flags & 0x10),
        "text": text_payload,
    }


# -- QPF Grid (0x12) — quantitative precipitation forecast grid ----------------
#
# Wire format identical to MSG_RADAR_COMPRESSED (0x11), but:
#   byte 0: 0x12 instead of 0x11
#   byte 4: valid_period (hi nibble: start offset in 6h units from 00Z,
#                         lo nibble: duration in 6h units)
#           instead of: timestamp_utc_min nibble
#   4-bit cell values: precipitation amount levels (not reflectivity)
#
# QPF levels:
#   0x0 = none,         0x1 = trace-0.10",  0x2 = 0.10-0.25"
#   0x3 = 0.25-0.50",   0x4 = 0.50-0.75",   0x5 = 0.75-1.00"
#   0x6 = 1.00-1.50",   0x7 = 1.50-2.00",   0x8 = 2.00-2.50"
#   0x9 = 2.50-3.00",   0xA = 3.00-4.00",   0xB = 4.00-5.00"
#   0xC = 5.00-7.00",   0xD = 7.00-10.00",  0xE = 10.00"+
#
# Uses the same sparse/RLE encoding and chunking as radar. The only
# code difference is the message type byte and the semantic meaning
# of cell values. See pack_radar_compressed() for the encoding logic.


# -- Text Chunk (0x40) — arbitrary NWS text products, chunked ----------------
#
# Wire format:
#   byte 0     : 0x40 MSG_TEXT_CHUNK
#   byte 1     : chunk_seq (hi nibble, 0-15) | total_chunks (lo nibble, 1-15)
#   byte 2     : subject_type (uint8, see TEXT_SUBJECT_* constants)
#   bytes 3..N : location reference (type-tagged — LOC_ZONE, LOC_WFO, etc.)
#   bytes N+1..: text payload (UTF-8)
#
# For multi-chunk texts, the client reassembles by matching
# (subject_type, location) and concatenating payloads in chunk_seq
# order (0, 1, 2, ...). Each chunk is a standalone 0x40 message.
#
# Future: dictionary compression using weather_dict.json with 0xFE
# escape sequences. The client detects dictionary-compressed text by
# the presence of 0xFE bytes in the payload (0xFE is never valid
# UTF-8, so raw text never contains it).
#
# Max text per chunk: 136 - 3 - location_bytes ≈ 127-129 bytes
# Max total text: 15 chunks × ~127 bytes ≈ 1900 bytes (before compression)

# Subject type constants — what kind of NWS text this is
TEXT_SUBJECT_AFD = 0x00            # Area Forecast Discussion
TEXT_SUBJECT_SPACE_WEATHER = 0x01  # SWPC space weather (solar, geomag, aurora)
TEXT_SUBJECT_TROPICAL = 0x02       # Tropical cyclone advisory/discussion
TEXT_SUBJECT_RIVER = 0x03          # River statement / flood potential
TEXT_SUBJECT_FIRE = 0x04           # Fire weather forecast / spot forecast
TEXT_SUBJECT_MARINE = 0x05         # Coastal/offshore/marine forecast
TEXT_SUBJECT_GENERAL = 0x06        # General text (PNS, admin, other)
TEXT_SUBJECT_CLIMATE = 0x07        # Climate data / records
TEXT_SUBJECT_NOWCAST = 0x08        # Short-term forecast (NOW) overflow


def pack_text_chunks(
    subject_type: int,
    loc_type: int,
    loc_id,
    text: str,
) -> list[bytes]:
    """Pack a text product into one or more 0x40 messages.

    Splits the text across multiple frames so each fits within 136 bytes.
    Returns a list of wire-ready messages. The client reassembles by
    concatenating payloads in chunk_seq order.
    """
    loc_bytes = pack_location(loc_type, loc_id)
    header_size = 3 + len(loc_bytes)  # 0x40 + seq/total + subject + location
    max_text_per_chunk = 136 - header_size

    # Encode the full text as UTF-8, then split into chunk-sized pieces.
    # We split on byte boundaries but avoid cutting a multi-byte UTF-8
    # codepoint by walking back to the last valid boundary.
    encoded = text.encode("utf-8", errors="replace")
    chunks: list[bytes] = []
    offset = 0
    while offset < len(encoded):
        end = min(offset + max_text_per_chunk, len(encoded))
        chunk = encoded[offset:end]
        # If we split mid-codepoint, walk back to last complete char
        if end < len(encoded):
            # Decode then re-encode to find the safe boundary
            safe = chunk.decode("utf-8", errors="ignore").encode("utf-8")
            chunk = safe
        chunks.append(chunk)
        offset += len(chunk)
    if not chunks:
        chunks = [b""]

    # Cap at 15 chunks (4-bit field)
    if len(chunks) > 15:
        chunks = chunks[:15]

    total_chunks = len(chunks)
    messages: list[bytes] = []
    for seq, chunk in enumerate(chunks):
        msg = bytearray()
        msg.append(MSG_TEXT_CHUNK)
        msg.append(((seq & 0x0F) << 4) | (total_chunks & 0x0F))
        msg.append(subject_type & 0xFF)
        msg.extend(loc_bytes)
        msg.extend(chunk)
        messages.append(bytes(msg))

    return messages


def unpack_text_chunk(data: bytes) -> dict:
    """Unpack a single 0x40 text chunk message."""
    if len(data) < 4 or data[0] != MSG_TEXT_CHUNK:
        raise ValueError("Invalid text chunk message")
    chunk_seq = (data[1] >> 4) & 0x0F
    total_chunks = data[1] & 0x0F
    subject_type = data[2]
    location, offset = unpack_location(data, 3)
    text_payload = data[offset:].decode("utf-8", errors="replace")

    subject_names = {
        TEXT_SUBJECT_AFD: "afd",
        TEXT_SUBJECT_SPACE_WEATHER: "space_weather",
        TEXT_SUBJECT_TROPICAL: "tropical",
        TEXT_SUBJECT_RIVER: "river",
        TEXT_SUBJECT_FIRE: "fire",
        TEXT_SUBJECT_MARINE: "marine",
        TEXT_SUBJECT_GENERAL: "general",
        TEXT_SUBJECT_CLIMATE: "climate",
    }
    return {
        "type": MSG_TEXT_CHUNK,
        "chunk_seq": chunk_seq,
        "total_chunks": total_chunks,
        "subject_type": subject_type,
        "subject_name": subject_names.get(subject_type, "unknown"),
        "location": location,
        "text": text_payload,
    }


def reassemble_text_chunks(chunks: list[dict]) -> str:
    """Reassemble a multi-chunk text from individual 0x40 messages.

    Each `chunk` is the dict from `unpack_text_chunk`. Returns the
    concatenated text string, or empty string on failure.
    """
    if not chunks:
        return ""
    sorted_chunks = sorted(chunks, key=lambda c: c["chunk_seq"])
    return "".join(c["text"] for c in sorted_chunks)
