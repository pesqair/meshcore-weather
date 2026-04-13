"""Radar GIF/PNG to 16x16 reflectivity grid pipeline.

Supports two sources:
  - IEM NEXRAD composites (internet, PNG, palette-indexed)
  - EMWIN RADALLUS.GIF (SDR/goestools, GIF, palette-indexed)

The IEM n0q composite uses a simple linear palette:
  pixel_index 0 = no data (background)
  pixel_index N = (N - 1) * 0.5 - 30 dBZ

We convert to 4-bit reflectivity levels (0x0-0xE) matching the MeshWX protocol.
"""

import io
import logging
from datetime import datetime, timezone

import httpx

from meshcore_weather.protocol.meshwx import (
    MSG_RADAR_COMPRESSED, REGIONS, V4SequenceCounter,
    pack_radar_compressed, pack_radar_grid,
)
from meshcore_weather.protocol.fec import fec_build_group

logger = logging.getLogger(__name__)

# IEM NEXRAD composite georeferencing (n0q product)
# https://mesonet.agron.iastate.edu/docs/nexrad_mosaic/
IEM_BOUNDS = {
    "lat_north": 50.0,
    "lat_south": 24.0,
    "lon_west": -126.0,
    "lon_east": -66.0,
}

# IEM composite base URL pattern
# {ts:%Y/%m/%d}/GIS/uscomp/n0q_{ts:%Y%m%d%H%M}.png
IEM_BASE = "https://mesonet.agron.iastate.edu/archive/data"


def _iem_url(ts: datetime) -> str:
    """Build IEM NEXRAD composite URL for a given timestamp."""
    # Round down to nearest 5 minutes
    minute = (ts.minute // 5) * 5
    ts = ts.replace(minute=minute, second=0, microsecond=0)
    return f"{IEM_BASE}/{ts:%Y/%m/%d}/GIS/uscomp/n0q_{ts:%Y%m%d%H%M}.png"


def _pixel_index_to_dbz(idx: int) -> float | None:
    """Convert IEM palette index to dBZ value."""
    if idx == 0:
        return None  # no data / background
    return (idx - 1) * 0.5 - 30.0


def _dbz_to_4bit(dbz: float | None) -> int:
    """Convert dBZ to 4-bit reflectivity level (0x0-0xE)."""
    if dbz is None or dbz < 5:
        return 0x0
    level = int(dbz / 5)
    return min(0xE, max(0x1, level))


def _latlon_to_pixel(
    lat: float, lon: float, img_w: int, img_h: int
) -> tuple[int, int]:
    """Convert lat/lon to pixel coordinates in the IEM composite."""
    b = IEM_BOUNDS
    x = int((lon - b["lon_west"]) / (b["lon_east"] - b["lon_west"]) * img_w)
    y = int((b["lat_north"] - lat) / (b["lat_north"] - b["lat_south"]) * img_h)
    return max(0, min(x, img_w - 1)), max(0, min(y, img_h - 1))


def extract_region_grid(
    img_data: bytes, region_id: int, grid_size: int = 16
) -> list[list[int]] | None:
    """Extract a 16x16 reflectivity grid for a region from a radar composite.

    Returns None if the region has no precipitation (all zeros).
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed — radar processing disabled")
        return None

    region = REGIONS.get(region_id)
    if not region:
        return None

    img = Image.open(io.BytesIO(img_data))
    if img.mode != "P":
        logger.warning("Radar image is not palette-indexed (mode=%s)", img.mode)
        return None

    w, h = img.size

    # Map region bounding box to pixel coordinates
    x1, y1 = _latlon_to_pixel(region["n"], region["w"], w, h)
    x2, y2 = _latlon_to_pixel(region["s"], region["e"], w, h)

    # Ensure correct ordering
    left, right = min(x1, x2), max(x1, x2)
    top, bottom = min(y1, y2), max(y1, y2)

    # Crop and downsample with NEAREST (preserves palette indices)
    cropped = img.crop((left, top, right, bottom))
    small = cropped.resize((grid_size, grid_size), Image.NEAREST)

    # Convert pixel indices to 4-bit reflectivity
    grid = [[0] * grid_size for _ in range(grid_size)]
    has_precip = False
    for y in range(grid_size):
        for x in range(grid_size):
            px = small.getpixel((x, y))
            dbz = _pixel_index_to_dbz(px)
            level = _dbz_to_4bit(dbz)
            grid[y][x] = level
            if level > 0:
                has_precip = True

    return grid if has_precip else None


async def fetch_radar_composite(client: httpx.AsyncClient) -> tuple[bytes, int] | None:
    """Fetch the latest IEM NEXRAD composite.

    Returns (image_bytes, timestamp_unix_min) or None on failure.
    The timestamp comes from the product URL, not the download time.
    """
    now = datetime.now(timezone.utc)
    url = _iem_url(now)

    try:
        resp = await client.get(url)
        resp.raise_for_status()
    except httpx.HTTPError:
        # Try 5 minutes earlier (latest may not be posted yet)
        from datetime import timedelta
        earlier = now - timedelta(minutes=5)
        url = _iem_url(earlier)
        try:
            resp = await client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch radar composite: %s", e)
            return None

    # Extract the actual product timestamp from the URL we fetched
    # URL ends with: n0q_YYYYMMDDHHmm.png
    ts_unix_min = _extract_url_timestamp(url)
    product_dt = datetime.fromtimestamp(ts_unix_min * 60, tz=timezone.utc)
    logger.info("Fetched radar composite (%d bytes, %s UTC)",
                len(resp.content), product_dt.strftime("%H:%M"))
    return resp.content, ts_unix_min


def _extract_url_timestamp(url: str) -> int:
    """Extract the product timestamp from an IEM composite URL.

    URL pattern: .../n0q_YYYYMMDDHHmm.png
    Returns Unix minutes.
    """
    import re
    m = re.search(r"n0q_(\d{12})\.png", url)
    if m:
        dt = datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
        return int(dt.timestamp()) // 60
    # Fallback: use current time rounded to 5 min
    now = datetime.now(timezone.utc)
    rounded = now.replace(minute=(now.minute // 5) * 5, second=0, microsecond=0)
    return int(rounded.timestamp()) // 60


def build_radar_messages(
    img_data: bytes,
    timestamp_utc_min: int,
    region_ids: set[int] | None = None,
) -> list[bytes]:
    """Build MeshWX 0x10 radar grid messages (legacy 16×16 flat format).

    If `region_ids` is provided, only those regions are considered.
    Regions with no precipitation are always skipped.
    """
    messages = []
    for region_id, region in REGIONS.items():
        if region_ids is not None and region_id not in region_ids:
            continue
        grid = extract_region_grid(img_data, region_id)
        if grid is None:
            continue  # no precip or region not applicable
        msg = pack_radar_grid(
            region_id=region_id,
            frame_seq=0,
            timestamp_utc_min=timestamp_utc_min,
            scale_km=region["scale"],
            grid=grid,
        )
        messages.append(msg)
        logger.debug("Radar grid for %s: %d non-zero cells",
                      region["name"],
                      sum(1 for row in grid for c in row if c > 0))
    return messages


def build_compressed_radar_messages(
    img_data: bytes,
    timestamp_utc_min: int,
    region_ids: set[int] | None = None,
    grid_size: int = 32,
) -> list[bytes]:
    """Build MeshWX 0x11 compressed radar messages (32×32 or 64×64).

    Uses adaptive sparse/RLE encoding, which compresses dramatically
    because radar grids are mostly empty (typically 5-20% of cells have
    any precipitation). A 32×32 grid usually fits in 1 message; 64×64
    in 1-4 messages depending on weather activity.

    If `region_ids` is provided, only those regions are considered.
    Regions with no precipitation are always skipped.
    """
    from meshcore_weather.protocol.meshwx import pack_radar_compressed

    messages = []
    for region_id, region in REGIONS.items():
        if region_ids is not None and region_id not in region_ids:
            continue
        grid = extract_region_grid(img_data, region_id, grid_size=grid_size)
        if grid is None:
            continue
        msgs = pack_radar_compressed(
            region_id=region_id,
            timestamp_utc_min=timestamp_utc_min,
            scale_km=region["scale"],
            grid=grid,
            grid_size=grid_size,
        )
        nonzero = sum(1 for row in grid for c in row if c > 0)
        enc = "sparse" if len(msgs) == 1 or True else "RLE"  # logged for debug
        logger.debug(
            "Compressed radar %s (%d×%d): %d non-zero, %d msg(s), %d total bytes",
            region["name"], grid_size, grid_size, nonzero,
            len(msgs), sum(len(m) for m in msgs),
        )
        messages.extend(msgs)
    return messages


# -- v4 FEC radar: spatial quadrants + XOR parity --


def _downsample_grid(grid: list[list[int]], from_size: int, to_size: int) -> list[list[int]]:
    """Downsample a grid by picking the max value in each block.

    Max-pooling preserves the highest reflectivity in each cell so the
    base layer preview doesn't hide storm cores.
    """
    ratio = from_size // to_size
    out = [[0] * to_size for _ in range(to_size)]
    for y in range(to_size):
        for x in range(to_size):
            best = 0
            for dy in range(ratio):
                for dx in range(ratio):
                    val = grid[y * ratio + dy][x * ratio + dx]
                    if val > best:
                        best = val
            out[y][x] = best
    return out


def _extract_quadrant(
    grid: list[list[int]], grid_size: int, quadrant: int,
) -> list[list[int]]:
    """Extract a quadrant from a grid.

    quadrant: 0=NW (top-left), 1=NE (top-right), 2=SW (bottom-left), 3=SE (bottom-right)
    Returns a grid_size/2 × grid_size/2 sub-grid.
    """
    half = grid_size // 2
    row_off = 0 if quadrant < 2 else half
    col_off = 0 if quadrant % 2 == 0 else half
    return [
        [grid[row_off + y][col_off + x] for x in range(half)]
        for y in range(half)
    ]


def build_fec_radar_messages(
    img_data: bytes,
    timestamp_utc_min: int,
    seq_counter: V4SequenceCounter,
    region_ids: set[int] | None = None,
    group_id: int = 0,
) -> list[bytes]:
    """Build v4 FEC radar for 64×64 grids with spatial quadrants + XOR parity.

    For each region:
      - Base layer: 32×32 downsampled from 64×64 (independently useful)
      - 4 data units: NW/NE/SW/SE quadrants, each 32×32
      - 1 parity unit: XOR of the 4 quadrant payloads

    Total: 6 messages per region. Client can display the base immediately,
    fill in quadrants as they arrive, and recover any single missing
    quadrant via XOR parity.

    Returns v4-framed messages ready for COBS encoding.
    """
    messages: list[bytes] = []

    for region_id, region in REGIONS.items():
        if region_ids is not None and region_id not in region_ids:
            continue

        # Extract full 64×64 grid
        grid_64 = extract_region_grid(img_data, region_id, grid_size=64)
        if grid_64 is None:
            continue

        # Base layer: downsample 64→32 (max-pool to preserve storm cores)
        grid_32 = _downsample_grid(grid_64, 64, 32)
        base_msgs = pack_radar_compressed(
            region_id=region_id,
            timestamp_utc_min=timestamp_utc_min,
            scale_km=region["scale"],
            grid=grid_32,
            grid_size=32,
        )
        # Base layer is usually a single message; take the first
        base_msg = base_msgs[0] if base_msgs else None

        # 4 quadrants: each 32×32
        quadrant_msgs: list[bytes] = []
        for q in range(4):
            q_grid = _extract_quadrant(grid_64, 64, q)
            q_msgs = pack_radar_compressed(
                region_id=region_id,
                timestamp_utc_min=timestamp_utc_min,
                scale_km=region["scale"],
                grid=q_grid,
                grid_size=32,
            )
            # Each quadrant should fit in one message
            if q_msgs:
                quadrant_msgs.append(q_msgs[0])

        if not quadrant_msgs:
            continue

        # Build FEC group: base + 4 quadrants + parity
        group = fec_build_group(
            data_units=quadrant_msgs,
            msg_type=MSG_RADAR_COMPRESSED,
            group_id=group_id % 4,
            seq_counter=seq_counter,
            base_layer=base_msg,
        )
        messages.extend(group)

        nonzero = sum(1 for row in grid_64 for c in row if c > 0)
        logger.debug(
            "FEC radar %s (64×64): %d non-zero, %d msgs (base+4Q+parity)",
            region["name"], nonzero, len(group),
        )
        group_id += 1

    return messages
