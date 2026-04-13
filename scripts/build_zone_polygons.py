#!/usr/bin/env python3
"""Build zone_polygons.json from the NWS API for iOS app bundling.

Fetches polygon geometry for every zone in zones.json from the NWS
forecast zone API. Simplifies coordinates and outputs a compact JSON
file that the iOS app ships in its bundle for offline zone rendering.

This script hits the NWS API and takes ~5-10 minutes to run. The
output file only needs regenerating when NWS updates zone boundaries
(typically twice a year).

Output: meshcore_weather/client_data/zone_polygons.json
Format: {"TXZ192": [[[lon,lat], ...]], ...}
"""

import json
import sys
import time
import urllib.request
from pathlib import Path

ZONES_PATH = Path(__file__).resolve().parent.parent / "meshcore_weather" / "geodata" / "zones.json"
OUTPUT_PATH = Path(__file__).resolve().parent.parent / "meshcore_weather" / "client_data" / "zone_polygons.json"

NWS_API = "https://api.weather.gov/zones/forecast"
USER_AGENT = "meshcore-weather/1.0 (zone polygon builder)"

# Coordinate precision (4 decimals ≈ 11m)
PRECISION = 4

# Rate limiting
BATCH_SIZE = 10
BATCH_DELAY = 1.0  # seconds between batches


def fetch_zone_geometry(zone_code: str) -> list | None:
    """Fetch polygon geometry for one zone from the NWS API."""
    # Convert zone code to API format: TXZ192 → TXZ192
    url = f"{NWS_API}/{zone_code}"
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/geo+json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=15)
        data = json.loads(resp.read())
    except Exception:
        return None

    geom = data.get("geometry")
    if not geom:
        return None

    return simplify_geometry(geom)


def simplify_geometry(geom: dict) -> list | None:
    """Convert GeoJSON geometry to compact ring list.

    Returns [[[lon,lat], ...], <holes>] or None.
    """
    gtype = geom.get("type", "")
    coords = geom.get("coordinates", [])

    if gtype == "Polygon":
        return _simplify_polygon(coords)
    elif gtype == "MultiPolygon":
        rings = []
        for poly_coords in coords:
            r = _simplify_polygon(poly_coords)
            if r:
                rings.extend(r)
        return rings if rings else None
    return None


def _simplify_polygon(coords: list) -> list | None:
    """Simplify one polygon's coordinate rings."""
    if not coords:
        return None
    rings = []
    for ring in coords:
        simplified = _simplify_ring(ring)
        if simplified and len(simplified) >= 4:
            rings.append(simplified)
    return rings if rings else None


def _simplify_ring(coords: list) -> list:
    """Round coordinates and remove redundant points."""
    result = []
    prev = None
    for point in coords:
        lon = round(point[0], PRECISION)
        lat = round(point[1], PRECISION)
        p = [lon, lat]
        if p != prev:
            result.append(p)
            prev = p
    return result


def main():
    zones = json.loads(ZONES_PATH.read_text())
    zone_codes = sorted(zones.keys())
    print(f"Fetching polygons for {len(zone_codes)} zones from NWS API...")

    results: dict[str, list] = {}
    failed = 0
    start = time.time()

    for i, code in enumerate(zone_codes):
        rings = fetch_zone_geometry(code)
        if rings:
            results[code] = rings
        else:
            failed += 1

        # Progress
        if (i + 1) % 50 == 0 or i == len(zone_codes) - 1:
            elapsed = time.time() - start
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            eta = (len(zone_codes) - i - 1) / rate if rate > 0 else 0
            print(f"  {i+1}/{len(zone_codes)} ({len(results)} ok, {failed} failed) "
                  f"[{rate:.1f}/s, ETA {eta:.0f}s]")

        # Rate limit
        if (i + 1) % BATCH_SIZE == 0:
            time.sleep(BATCH_DELAY)

    # Write output
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(results, f, separators=(",", ":"))

    size_mb = OUTPUT_PATH.stat().st_size / 1024 / 1024
    total_vertices = sum(
        sum(len(ring) for ring in rings)
        for rings in results.values()
    )

    print(f"\nDone! {len(results)} zones → {OUTPUT_PATH}")
    print(f"Failed: {failed} zones (no geometry available)")
    print(f"File size: {size_mb:.1f} MB")
    print(f"Total vertices: {total_vertices:,}")
    print(f"Time: {time.time() - start:.0f}s")


if __name__ == "__main__":
    main()
