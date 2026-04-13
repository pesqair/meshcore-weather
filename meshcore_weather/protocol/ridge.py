"""NWS RIDGE radar image extraction — color-matching pipeline.

Extracts reflectivity data from NWS RIDGE standard radar images
(the same images transmitted via GOES HRIT/EMWIN satellite downlink).

Unlike the IEM n0q composites (clean palette-indexed data), RIDGE images
are rendered with map overlays (roads, borders, terrain, city labels) and
watch/warning polygon fills. This module uses:
  1. Color-matching against the NWS standard reflectivity color table
  2. Local variance filtering to reject warning overlay fills
     (smooth uniform blocks vs noisy natural radar texture)

Supports:
  - CONUS composite (RADREFUS.GIF / CONUS_0.gif)
  - Single-site images: PR (TJUA), Hawaii (PHWA), Alaska (PAKC), Guam (PGUA)

Each RIDGE image type has different georeferencing:
  - CONUS: approximate equirectangular (50N-22N, 126W-66W)
  - Single-site: 460km radius from radar station center
"""

import io
import logging
import math
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)


# -- NWS reflectivity color table --
# These are the standard RIDGE renderer colors for radar returns.
# Source: NWS RIDGE documentation / visual inspection of official images.
NWS_REFLECTIVITY_COLORS = [
    # (dBZ, R, G, B)
    (5,    0, 236, 236),    # Cyan
    (10,   1, 160, 246),    # Light blue
    (15,   0,   0, 246),    # Blue
    (20,   0, 255,   0),    # Bright green
    (25,   0, 200,   0),    # Medium green
    (30,   0, 144,   0),    # Dark green
    (35, 255, 255,   0),    # Yellow
    (40, 231, 192,   0),    # Dark yellow
    (45, 255, 144,   0),    # Orange
    (50, 255,   0,   0),    # Red
    (55, 214,   0,   0),    # Dark red
    (60, 192,   0,   0),    # Darker red
    (65, 255,   0, 255),    # Magenta
    (70, 153,  85, 201),    # Purple
]

# Maximum color distance for a match (Euclidean RGB distance)
MAX_COLOR_DIST = 45

# Minimum color saturation to consider (rejects greys = roads/borders/text)
MIN_SATURATION = 80

# Minimum local variance to keep (rejects watch/warning fills)
MIN_LOCAL_VARIANCE = 100


# -- RIDGE image sources --
# URL patterns for internet-mode fetching.
# For SDR/EMWIN mode, the same images arrive as GIF files via satellite.

RIDGE_SOURCES = {
    "conus": {
        "url": "https://radar.weather.gov/ridge/standard/CONUS_0.gif",
        "emwin_name": "RADREFUS.GIF",
        "bounds": {"n": 50.0, "s": 22.0, "w": -126.0, "e": -66.0},
        "title_rows": 24,     # rows to crop from top (title bar)
        "legend_rows": 24,    # rows to crop from bottom (legend)
    },
    "pr": {
        "url": "https://radar.weather.gov/ridge/standard/TJUA_0.gif",
        "emwin_name": "RADALLPR.PNG",
        "station": {"lat": 18.1156, "lon": -66.0781},
        "range_km": 460,
        "title_rows": 24,
        "legend_rows": 24,
    },
    "hawaii": {
        "url": "https://radar.weather.gov/ridge/standard/HAWAII_0.gif",
        "emwin_name": "RADALLHI.GIF",
        "bounds": {"n": 23.0, "s": 18.0, "w": -161.0, "e": -154.0},
        "title_rows": 24,
        "legend_rows": 24,
    },
    "alaska": {
        "url": "https://radar.weather.gov/ridge/standard/ALASKA_0.gif",
        "emwin_name": "RADALLAK.GIF",
        "bounds": {"n": 72.0, "s": 51.0, "w": -180.0, "e": -130.0},
        "title_rows": 24,
        "legend_rows": 24,
    },
    "guam": {
        "url": "https://radar.weather.gov/ridge/standard/GUAM_0.gif",
        "emwin_name": "RADALLGU.PNG",
        "station": {"lat": 13.4543, "lon": 144.8111},
        "range_km": 460,
        "title_rows": 24,
        "legend_rows": 24,
    },
}

# Map MeshWX region IDs to RIDGE sources
REGION_TO_RIDGE = {
    0x0: "conus",   # Northeast
    0x1: "conus",   # Southeast
    0x2: "conus",   # Upper Midwest
    0x3: "conus",   # Southern
    0x4: "conus",   # Central
    0x5: "conus",   # Mountain
    0x6: "conus",   # Pacific
    0x7: "alaska",
    0x8: "hawaii",
    0x9: "pr",
}


# -- Color classification --


def _classify_pixel(r: int, g: int, b: int) -> int:
    """Classify a pixel as a 4-bit reflectivity level (0-14) or 0 (no data).

    Rejects map features (low saturation), dark backgrounds, and
    state boundary lines (dark reds with low saturation).
    """
    brightness = (r + g + b) / 3
    if brightness < 20:
        return 0  # black background
    saturation = max(r, g, b) - min(r, g, b)
    if saturation < MIN_SATURATION and brightness < 200:
        return 0  # grey (roads, borders, text, terrain)
    # State boundaries: dark maroon (R~155, G~15, B~15), saturation ~140
    # Real radar reds: pure (R>200, G~0, B~0), saturation >190
    if r > 100 and g < 40 and b < 40 and saturation < 180:
        return 0

    best_dbz = None
    best_dist = MAX_COLOR_DIST
    for dbz, tr, tg, tb in NWS_REFLECTIVITY_COLORS:
        d = math.sqrt((r - tr) ** 2 + (g - tg) ** 2 + (b - tb) ** 2)
        if d < best_dist:
            best_dist = d
            best_dbz = dbz
    if best_dbz is None:
        return 0
    return min(14, max(1, best_dbz // 5))


def _local_variance(img, x: int, y: int, w: int, h: int, radius: int = 4) -> float:
    """Calculate color variance in a pixel neighborhood.

    High variance = natural radar texture (keep).
    Low variance = smooth warning fill or uniform map feature (reject).
    """
    colors = []
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            nx, ny = x + dx, y + dy
            if 0 <= nx < w and 0 <= ny < h:
                colors.append(img.getpixel((nx, ny)))
    if len(colors) < 4:
        return 999
    avg_r = sum(c[0] for c in colors) / len(colors)
    avg_g = sum(c[1] for c in colors) / len(colors)
    avg_b = sum(c[2] for c in colors) / len(colors)
    return sum(
        (c[0] - avg_r) ** 2 + (c[1] - avg_g) ** 2 + (c[2] - avg_b) ** 2
        for c in colors
    ) / len(colors)


# -- Georeferencing --


def _compute_station_bounds(station_lat: float, station_lon: float, range_km: float) -> dict:
    """Compute lat/lon bounds for a single-site radar image."""
    km_per_deg_lat = 111.0
    km_per_deg_lon = 111.0 * math.cos(math.radians(station_lat))
    lat_span = range_km / km_per_deg_lat
    lon_span = range_km / km_per_deg_lon
    return {
        "n": station_lat + lat_span,
        "s": station_lat - lat_span,
        "w": station_lon - lon_span,
        "e": station_lon + lon_span,
    }


def _get_source_bounds(source: dict) -> dict:
    """Get georeferencing bounds for a RIDGE source."""
    if "bounds" in source:
        return source["bounds"]
    if "station" in source:
        return _compute_station_bounds(
            source["station"]["lat"],
            source["station"]["lon"],
            source["range_km"],
        )
    raise ValueError("RIDGE source has no bounds or station info")


def _region_to_pixels(
    region: dict, img_w: int, img_h: int, bounds: dict,
) -> tuple[int, int, int, int]:
    """Convert a MeshWX region's lat/lon bounds to pixel coordinates."""
    x1 = int((region["w"] - bounds["w"]) / (bounds["e"] - bounds["w"]) * img_w)
    y1 = int((bounds["n"] - region["n"]) / (bounds["n"] - bounds["s"]) * img_h)
    x2 = int((region["e"] - bounds["w"]) / (bounds["e"] - bounds["w"]) * img_w)
    y2 = int((bounds["n"] - region["s"]) / (bounds["n"] - bounds["s"]) * img_h)
    return (max(0, x1), max(0, y1), min(img_w, x2), min(img_h, y2))


# -- Grid extraction --


def extract_ridge_grid(
    img_data: bytes,
    source_key: str,
    region: dict,
    grid_size: int = 32,
) -> list[list[int]] | None:
    """Extract a reflectivity grid from a RIDGE image for a specific region.

    Args:
        img_data: raw image bytes (GIF or PNG)
        source_key: RIDGE source key (e.g. "conus", "pr")
        region: MeshWX region dict with n/s/w/e bounds
        grid_size: output grid size (32 or 64)

    Returns:
        grid_size x grid_size list of 4-bit reflectivity levels, or None
        if no precipitation detected.
    """
    try:
        from PIL import Image
    except ImportError:
        logger.warning("Pillow not installed — RIDGE radar disabled")
        return None

    source = RIDGE_SOURCES.get(source_key)
    if source is None:
        return None

    img = Image.open(io.BytesIO(img_data)).convert("RGB")
    w, h = img.size

    # Crop title and legend
    crop_top = source.get("title_rows", 0)
    crop_bot = source.get("legend_rows", 0)
    if crop_top or crop_bot:
        img = img.crop((0, crop_top, w, h - crop_bot))
        w, h = img.size

    # Get source georeferencing bounds
    bounds = _get_source_bounds(source)

    # Map region to pixel coordinates within the cropped image
    x1, y1, x2, y2 = _region_to_pixels(region, w, h, bounds)
    if x2 <= x1 or y2 <= y1:
        return None

    crop_w = x2 - x1
    crop_h = y2 - y1

    # Build the grid using max-pool sampling with color classification
    grid = [[0] * grid_size for _ in range(grid_size)]
    has_precip = False

    for gy in range(grid_size):
        for gx in range(grid_size):
            best_level = 0
            # Sample multiple points per cell (3x3 subgrid)
            for sy in range(3):
                for sx in range(3):
                    px = int(x1 + (gx + (sx + 0.5) / 3) * crop_w / grid_size)
                    py = int(y1 + (gy + (sy + 0.5) / 3) * crop_h / grid_size)
                    px = max(0, min(px, w - 1))
                    py = max(0, min(py, h - 1))
                    r, g, b = img.getpixel((px, py))
                    level = _classify_pixel(r, g, b)
                    if level > 0:
                        # Check local variance to reject warning fills
                        var = _local_variance(img, px, py, w, h)
                        if var >= MIN_LOCAL_VARIANCE and level > best_level:
                            best_level = level
            grid[gy][gx] = best_level
            if best_level > 0:
                has_precip = True

    return grid if has_precip else None


# -- Fetching --


async def fetch_ridge_image(
    client: httpx.AsyncClient, source_key: str,
) -> tuple[bytes, int] | None:
    """Fetch a RIDGE image from the NWS radar website.

    Returns (image_bytes, timestamp_unix_min) or None on failure.
    """
    source = RIDGE_SOURCES.get(source_key)
    if source is None:
        return None

    url = source["url"]
    try:
        resp = await client.get(url, headers={"User-Agent": "meshcore-weather/1.0"})
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("Failed to fetch RIDGE %s: %s", source_key, e)
        return None

    now = datetime.now(timezone.utc)
    # RIDGE images update every ~2 minutes. Use current time rounded to
    # nearest 5 minutes as the effective timestamp (same as IEM approach).
    minute = (now.minute // 5) * 5
    ts = now.replace(minute=minute, second=0, microsecond=0)
    ts_unix_min = int(ts.timestamp()) // 60

    logger.info(
        "Fetched RIDGE %s (%d bytes, %02d:%02d UTC)",
        source_key, len(resp.content), now.hour, now.minute,
    )
    return resp.content, ts_unix_min
