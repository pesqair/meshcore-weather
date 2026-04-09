"""MeshWX binary wire format: pack/unpack radar grids, warning polygons, refresh requests.

Wire format reference: Weather_Protocol.md
All multi-byte integers are big-endian unless noted.
"""

import struct

# -- Message type bytes --
MSG_RADAR = 0x10
MSG_WARNING = 0x20
MSG_REFRESH = 0x01

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


# -- Warning Polygon (0x20) -- variable, max 136 bytes --

def pack_warning_polygon(
    warning_type: int,
    severity: int,
    expiry_minutes: int,
    vertices: list[tuple[float, float]],
    headline: str,
) -> bytes:
    """Pack a warning polygon into wire format (max 136 bytes).

    vertices: list of (lat, lon) in decimal degrees.
    """
    msg = bytearray()
    msg.append(MSG_WARNING)
    msg.append(((warning_type & 0x0F) << 4) | (severity & 0x0F))
    msg.extend(struct.pack(">H", min(expiry_minutes, 0xFFFF)))
    msg.append(len(vertices) & 0xFF)

    if not vertices:
        remaining = 136 - len(msg)
        if remaining > 0:
            msg.extend(headline.encode("utf-8")[:remaining])
        return bytes(msg)

    # First vertex: 24-bit signed, degrees * 10000 (~11m precision)
    lat0, lon0 = vertices[0]
    lat_i = int(lat0 * 10000)
    lon_i = int(lon0 * 10000)
    msg.extend(lat_i.to_bytes(3, "big", signed=True))
    msg.extend(lon_i.to_bytes(3, "big", signed=True))

    # Remaining vertices: int8 delta pairs (0.01 degree units)
    for lat, lon in vertices[1:]:
        dlat = max(-128, min(127, int((lat - lat0) / 0.01)))
        dlon = max(-128, min(127, int((lon - lon0) / 0.01)))
        msg.extend(struct.pack("bb", dlat, dlon))

    # Headline fills remaining space
    remaining = 136 - len(msg)
    if remaining > 0:
        msg.extend(headline.encode("utf-8")[:remaining])
    return bytes(msg)


def unpack_warning_polygon(data: bytes) -> dict:
    """Unpack a warning polygon message."""
    if len(data) < 5 or data[0] != MSG_WARNING:
        raise ValueError("Invalid warning polygon message")
    warning_type = (data[1] >> 4) & 0x0F
    severity = data[1] & 0x0F
    expiry = struct.unpack_from(">H", data, 2)[0]
    vertex_count = data[4]

    vertices = []
    offset = 5
    if vertex_count > 0 and offset + 6 <= len(data):
        lat0 = int.from_bytes(data[offset : offset + 3], "big", signed=True) / 10000
        lon0 = int.from_bytes(data[offset + 3 : offset + 6], "big", signed=True) / 10000
        vertices.append((lat0, lon0))
        offset += 6
        for _ in range(vertex_count - 1):
            if offset + 2 > len(data):
                break
            dlat, dlon = struct.unpack_from("bb", data, offset)
            vertices.append((lat0 + dlat * 0.01, lon0 + dlon * 0.01))
            offset += 2

    headline = data[offset:].decode("utf-8", errors="replace").rstrip("\x00")
    return {
        "type": MSG_WARNING,
        "warning_type": warning_type,
        "severity": severity,
        "expiry_minutes": expiry,
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
