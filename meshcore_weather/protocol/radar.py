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

from meshcore_weather.protocol.meshwx import REGIONS, pack_radar_grid

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

    Returns (image_bytes, timestamp_utc_minutes) or None on failure.
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
            now = earlier
        except httpx.HTTPError as e:
            logger.warning("Failed to fetch radar composite: %s", e)
            return None

    # Timestamp as minutes since midnight UTC
    ts_minutes = now.hour * 60 + now.minute
    logger.info("Fetched radar composite (%d bytes, %02d:%02d UTC)",
                len(resp.content), now.hour, now.minute)
    return resp.content, ts_minutes


def build_radar_messages(
    img_data: bytes, timestamp_utc_min: int
) -> list[bytes]:
    """Build MeshWX radar grid messages for all regions with precipitation."""
    messages = []
    for region_id, region in REGIONS.items():
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
