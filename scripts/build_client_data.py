#!/usr/bin/env python3
"""Build the client preload bundle for MeshWX-aware apps.

Outputs a directory `client_data/` with compact JSON files that iOS/web
clients ship with their app. With this preloaded, broadcasts only need to
transmit small IDs (zone codes, place indices, station ICAOs) instead of
full location names — the client looks up the full details locally.

Usage:
    python scripts/build_client_data.py [output_dir]

Defaults to `./client_data/`.
"""

import io
import json
import re
import sys
import zipfile
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEODATA = ROOT / "meshcore_weather" / "geodata"
EMWIN_CACHE = ROOT / "data" / "emwin_cache" / "products.jsonl"
SHAPEFILE_CACHE = ROOT / ".cache" / "nws_shapefiles"

# NWS public forecast zones shapefile (public domain US federal data).
# Filename encodes the effective date — NWS releases a new version a few
# times a year. Find current versions at https://www.weather.gov/gis/publiczones
# When NWS publishes an update, bump both the URL and the expected MD5.
NWS_ZONES_SHAPEFILE_URL = (
    "https://www.weather.gov/source/gis/Shapefiles/WSOM/z_16ap26.zip"
)
NWS_ZONES_SHAPEFILE_MD5 = "b883244e367c51f493d93ff4feaad9f0"

# Fallback EMWIN bundle URL if local cache is empty (dev-box builds)
EMWIN_BUNDLE_URL = (
    "https://tgftp.nws.noaa.gov/SL.us008001/CU.EMWIN/DF.xt/DC.gsatR/OPS/txthrs01.zip"
)


def build_zones(out_dir: Path) -> None:
    """Compact NWS zones file indexed by zone code.

    Input:  {zone_code: {n, w, s, la, lo, c}}
    Output: {zone_code: {name, wfo, state, lat, lon}}  (drop counties list)
    """
    src = json.loads((GEODATA / "zones.json").read_text())
    out = OrderedDict()
    for code, z in sorted(src.items()):
        out[code] = {
            "name": z.get("n", ""),
            "wfo": z.get("w", ""),
            "state": z.get("s", ""),
            "lat": z.get("la", 0),
            "lon": z.get("lo", 0),
        }
    path = out_dir / "zones.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    size = path.stat().st_size / 1024
    print(f"  zones.json:    {len(out):>6} entries, {size:.0f} KB")


def build_places(out_dir: Path) -> None:
    """Place index → name, state, lat, lon.

    Input:  [[NAME, STATE, lat, lon], ...]
    Output: {"places": [["Austin", "TX", 30.27, -97.74], ...]}
    The array INDEX is the place_id used in MeshWX binary messages.
    """
    src = json.loads((GEODATA / "places.json").read_text())
    out = {"places": src}  # already in the right shape
    path = out_dir / "places.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    size = path.stat().st_size / 1024 / 1024
    print(f"  places.json:   {len(src):>6} entries, {size:.1f} MB")


def build_stations(out_dir: Path) -> None:
    """METAR stations indexed by ICAO."""
    src = json.loads((GEODATA / "stations.json").read_text())
    out = OrderedDict()
    for icao, st in sorted(src.items()):
        out[icao] = {
            "name": st.get("n", ""),
            "state": st.get("s", ""),
            "lat": st.get("la", 0),
            "lon": st.get("lo", 0),
        }
    path = out_dir / "stations.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    size = path.stat().st_size / 1024
    print(f"  stations.json: {len(out):>6} entries, {size:.0f} KB")


def build_wfos(out_dir: Path) -> None:
    """Aggregate unique WFO codes from zones.json with their served state/region."""
    zones = json.loads((GEODATA / "zones.json").read_text())
    wfos: dict[str, dict] = {}
    for z in zones.values():
        w = z.get("w")
        if not w:
            continue
        if w not in wfos:
            wfos[w] = {"states": set(), "lat_sum": 0, "lon_sum": 0, "count": 0}
        wfos[w]["states"].add(z.get("s", ""))
        wfos[w]["lat_sum"] += z.get("la", 0)
        wfos[w]["lon_sum"] += z.get("lo", 0)
        wfos[w]["count"] += 1

    out = OrderedDict()
    for code in sorted(wfos):
        data = wfos[code]
        out[code] = {
            "states": sorted(data["states"]),
            "lat": round(data["lat_sum"] / data["count"], 4),
            "lon": round(data["lon_sum"] / data["count"], 4),
            "zone_count": data["count"],
        }
    path = out_dir / "wfos.json"
    path.write_text(json.dumps(out, separators=(",", ":")))
    size = path.stat().st_size / 1024
    print(f"  wfos.json:     {len(out):>6} entries, {size:.0f} KB")


def build_state_index(out_dir: Path) -> None:
    """Copy the state index file used for compact zone encoding."""
    src = GEODATA / "state_index.json"
    dst = out_dir / "state_index.json"
    dst.write_text(src.read_text())
    size = dst.stat().st_size / 1024
    print(f"  state_index:   {size:.1f} KB")


def build_dictionary(out_dir: Path) -> None:
    """Dictionary of top phrases for text compression in 0x40 chunks."""
    # Top-128 phrases ordered by estimated frequency in NWS products.
    # Index = code (escape byte 0xFE followed by 1 byte code).
    phrases = [
        # VTEC / status (0-15)
        "IN EFFECT UNTIL", "REMAINS IN EFFECT", "CANCELLED", "EXPIRES AT",
        "CONTINUES", "UPGRADED TO", "DOWNGRADED TO", "EXTENDED UNTIL",
        "WILL EXPIRE", "HAS EXPIRED", "NEW", "ROUTINE", "URGENT",
        "IMMEDIATE", "EXPECTED", "POSSIBLE",

        # Warning types (16-39)
        "TORNADO WARNING", "SEVERE THUNDERSTORM WARNING",
        "FLASH FLOOD WARNING", "FLOOD WARNING", "FLOOD ADVISORY",
        "FLOOD STATEMENT", "WIND ADVISORY", "HIGH WIND WARNING",
        "HIGH WIND WATCH", "WINTER STORM WARNING", "WINTER WEATHER ADVISORY",
        "BLIZZARD WARNING", "ICE STORM WARNING", "FREEZE WARNING",
        "FROST ADVISORY", "HEAT ADVISORY", "EXCESSIVE HEAT WARNING",
        "RED FLAG WARNING", "FIRE WEATHER WATCH", "DENSE FOG ADVISORY",
        "COASTAL FLOOD ADVISORY", "RIP CURRENT STATEMENT",
        "SEVERE WEATHER STATEMENT", "SPECIAL WEATHER STATEMENT",

        # Impacts (40-63)
        "TAKE SHELTER NOW", "MOVE INDOORS", "SEEK SHELTER", "STAY INDOORS",
        "AVOID TRAVEL", "HAZARDOUS DRIVING", "LIFE-THREATENING",
        "DAMAGING WINDS", "LARGE HAIL", "FLASH FLOODING",
        "HEAVY RAINFALL", "STRONG WINDS", "GUSTY WINDS",
        "DAMAGING WIND GUSTS", "EXPECT DAMAGE", "PEOPLE OUTSIDE",
        "MOBILE HOMES", "TREES DOWN", "POWER OUTAGES",
        "HAIL SIZE", "GOLF BALL", "TENNIS BALL", "BASEBALL SIZE",
        "PENNY SIZE",

        # Time phrases (64-87)
        "UNTIL", "AT", "THROUGH", "FROM", "TONIGHT", "TOMORROW",
        "THIS AFTERNOON", "THIS EVENING", "OVERNIGHT", "EARLY MORNING",
        "LATE TONIGHT", "PM CDT", "AM CDT", "PM EDT", "AM EDT",
        "PM PDT", "AM PDT", "PM MDT", "AM MDT", "PM EST", "AM EST",
        "HOURS", "MINUTES", "EXPIRE AT",

        # Places / modifiers (88-111)
        "COUNTY", "COUNTIES", "PARISH", "ZONE", "ZONES",
        "NATIONAL WEATHER SERVICE", "NORTHWEST", "NORTHEAST",
        "SOUTHWEST", "SOUTHEAST", "NORTHERN", "SOUTHERN", "EASTERN",
        "WESTERN", "CENTRAL", "MILES", "INCHES", "FEET", "MPH",
        "MPH OR GREATER", "QUARTER SIZE", "HALF DOLLAR", "SIZE",
        "RADAR INDICATED",

        # Common verbs/conjunctions (112-127)
        "IS", "ARE", "WAS", "WERE", "WILL", "WILL BE", "HAS BEEN",
        "HAVE BEEN", "IS IN EFFECT", "ISSUED", "INCLUDES", "INCLUDING",
        "AFFECTING", "EXPECTED TO", "CAPABLE OF", "LIKELY",
    ]

    out = {
        "version": 1,
        "escape_byte": 0xFE,
        "phrase_count": len(phrases),
        "phrases": phrases,
    }
    path = out_dir / "weather_dict.json"
    path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    size = path.stat().st_size / 1024
    print(f"  weather_dict:  {len(phrases):>6} phrases, {size:.0f} KB")


def build_protocol_codes(out_dir: Path) -> None:
    """Export message type + code constants for client use."""
    out = {
        "version": 4,
        "messages": {
            "refresh_request": 0x01,
            "data_request": 0x02,
            "radar_grid": 0x10,
            "warning_polygon": 0x20,
            "warning_zones": 0x21,
            "observation": 0x30,
            "forecast": 0x31,
            "outlook": 0x32,
            "storm_reports": 0x33,
            "rain_obs": 0x34,
            "metar": 0x35,
            "taf": 0x36,
            "warnings_near": 0x37,
            "text_chunk": 0x40,
        },
        "location_types": {
            "zone": 0x01,
            "station": 0x02,
            "place": 0x03,
            "latlon": 0x04,
            "wfo": 0x05,
            "pfm_point": 0x06,
        },
        "warning_types": {
            "tornado": 0x1,
            "severe_thunder": 0x2,
            "flash_flood": 0x3,
            "flood": 0x4,
            "winter_storm": 0x5,
            "high_wind": 0x6,
            "fire": 0x7,
            "marine": 0x8,
            "special": 0x9,
            "other": 0xF,
        },
        "severities": {
            "advisory": 0x1,
            "watch": 0x2,
            "warning": 0x3,
            "emergency": 0x4,
        },
        "sky_codes": {
            "clear": 0x0, "few": 0x1, "scattered": 0x2, "broken": 0x3,
            "overcast": 0x4, "fog": 0x5, "smoke": 0x6, "haze": 0x7,
            "rain": 0x8, "snow": 0x9, "thunderstorm": 0xA, "drizzle": 0xB,
            "mist": 0xC, "squall": 0xD, "sand": 0xE, "other": 0xF,
        },
        "hazard_types": {
            "thunderstorm": 0x0, "severe_thunder": 0x1, "tornado": 0x2,
            "flood": 0x3, "flash_flood": 0x4, "excessive_heat": 0x5,
            "winter_storm": 0x6, "blizzard": 0x7, "ice": 0x8,
            "high_wind": 0x9, "fire_weather": 0xA, "dense_fog": 0xB,
            "rip_current": 0xC, "hurricane": 0xD, "marine": 0xE, "other": 0xF,
        },
        "risk_levels": {
            "none": 0, "slight": 1, "limited": 2, "enhanced": 3,
            "moderate": 4, "high": 5, "extreme": 6,
        },
        "event_types": {
            "tornado": 0x0, "funnel": 0x1, "hail": 0x2, "wind_damage": 0x3,
            "non_tstm_wind": 0x4, "tstm_wind": 0x5, "flood": 0x6,
            "flash_flood": 0x7, "heavy_rain": 0x8, "snow": 0x9, "ice": 0xA,
            "lightning": 0xB, "debris_flow": 0xC, "other": 0xF,
        },
        "rain_types": {
            "light": 0x0, "moderate": 0x1, "heavy": 0x2, "shower": 0x3,
            "tstorm": 0x4, "drizzle": 0x5, "snow": 0x6, "freezing": 0x7, "mix": 0x8,
        },
    }
    path = out_dir / "protocol.json"
    path.write_text(json.dumps(out, indent=2))
    size = path.stat().st_size / 1024
    print(f"  protocol.json: {size:.1f} KB")


def _load_pfm_products() -> list[tuple[str, str]]:
    """Return a list of (filename, raw_text) tuples for PFM products.

    Prefers the bot's local EMWIN cache at data/emwin_cache/products.jsonl
    if it exists and has content. Falls back to downloading a fresh 3-hour
    EMWIN bundle from NOAA (dev-box only; production never runs this).
    """
    pfms: list[tuple[str, str]] = []

    # Try local cache first
    if EMWIN_CACHE.exists():
        try:
            with EMWIN_CACHE.open() as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    fname = rec.get("filename", "")
                    text = rec.get("raw_text", "")
                    if text and "PFM" in fname.upper():
                        pfms.append((fname, text))
            if pfms:
                print(f"  (loaded {len(pfms)} PFM products from local cache)")
                return pfms
        except Exception as exc:
            print(f"  (local cache read failed: {exc})")

    # Fallback: fetch from NOAA (needs internet)
    print(f"  (no local cache, fetching {EMWIN_BUNDLE_URL})")
    try:
        import httpx
    except ImportError:
        print("  (httpx not installed, skipping PFM fetch)")
        return []

    try:
        resp = httpx.get(EMWIN_BUNDLE_URL, timeout=60.0)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  (bundle fetch failed: {exc})")
        return []

    def _extract(data: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".zip"):
                        try:
                            _extract(zf.read(name))
                        except Exception:
                            pass
                    elif name.lower().endswith(".txt") and "PFM" in name.upper():
                        try:
                            text = zf.read(name).decode(
                                "utf-8", errors="replace"
                            ).strip()
                            if text:
                                pfms.append((name, text))
                        except Exception:
                            pass
        except zipfile.BadZipFile:
            pass

    _extract(resp.content)
    print(f"  (extracted {len(pfms)} PFM products from bundle)")
    return pfms


# PFM forecast point header patterns
_PFM_AFOS_RE = re.compile(r"^PFM([A-Z]{3})$")
_PFM_UGC_RE = re.compile(r"^([A-Z]{2}Z\d{3})(?:[->]\d{3})*-\d{6}-\s*$")
_PFM_COORD_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)N\s+(\d+(?:\.\d+)?)W\s+Elev")


def _scrape_pfm_points(text: str) -> list[dict]:
    """Scrape forecast-point headers from a single PFM product.

    Returns a list of dicts with keys: name, wfo, lat, lon, zone.
    Uses a lightweight line-by-line scan. This is scope-limited to what's
    needed for pfm_points.json (name + coords + zone) — not a full PFM
    column parser, which is Commit 2 scope.
    """
    lines = [line.strip() for line in text.splitlines()]

    # Find the AFOS line to get the WFO
    wfo = "???"
    for line in lines[:15]:
        m = _PFM_AFOS_RE.match(line)
        if m:
            wfo = m.group(1)
            break

    points: list[dict] = []
    i = 0
    while i < len(lines):
        m_ugc = _PFM_UGC_RE.match(lines[i])
        if not m_ugc:
            i += 1
            continue
        zone = m_ugc.group(1)

        # Next non-empty line = point name
        j = i + 1
        while j < len(lines) and not lines[j]:
            j += 1
        if j >= len(lines):
            break
        name = lines[j]

        # Next non-empty line = coordinates
        k = j + 1
        while k < len(lines) and not lines[k]:
            k += 1
        if k >= len(lines):
            break
        m_coord = _PFM_COORD_RE.match(lines[k])
        if m_coord:
            try:
                lat = float(m_coord.group(1))
                lon = -float(m_coord.group(2))
            except ValueError:
                i = k
                continue
            points.append({
                "name": name,
                "wfo": wfo,
                "lat": round(lat, 4),
                "lon": round(lon, 4),
                "zone": zone,
            })
        i = k + 1
    return points


def _fetch_shapefile(url: str, expected_md5: str | None = None) -> Path:
    """Download a shapefile ZIP to the .cache/nws_shapefiles/ directory.

    Caches forever — re-uses the local file on subsequent runs. Returns the
    path to the cached ZIP. Raises if download fails and no cached copy
    exists.
    """
    import hashlib

    SHAPEFILE_CACHE.mkdir(parents=True, exist_ok=True)
    dest = SHAPEFILE_CACHE / url.rsplit("/", 1)[-1]

    if dest.exists():
        if expected_md5:
            actual = hashlib.md5(dest.read_bytes()).hexdigest()
            if actual != expected_md5:
                print(f"  (cache MD5 mismatch for {dest.name}, re-downloading)")
                dest.unlink()
        if dest.exists():
            print(f"  (using cached {dest.name})")
            return dest

    print(f"  (fetching {url})")
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx required to download shapefile") from exc

    with httpx.Client(timeout=120.0, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)

    if expected_md5:
        actual = hashlib.md5(dest.read_bytes()).hexdigest()
        if actual != expected_md5:
            dest.unlink()
            raise RuntimeError(
                f"Downloaded shapefile MD5 mismatch: got {actual}, expected {expected_md5}"
            )
    print(f"  (cached to {dest})")
    return dest


def build_zones_geojson(out_dir: Path) -> None:
    """Generate client_data/zones.geojson from the NWS public zones shapefile.

    Downloads the NWS shapefile on first run (into .cache/, git-ignored),
    loads with geopandas, joins against our zones.json to filter to known
    zones, simplifies geometries with shapely (~1 km tolerance), and writes
    a compact GeoJSON keyed by zone code. Client renders warning polygons
    from this file directly — no runtime geometry transmission.
    """
    try:
        import geopandas as gpd
    except ImportError:
        print("  zones.geojson: SKIPPED (geopandas not installed)")
        return

    try:
        shapefile_zip = _fetch_shapefile(
            NWS_ZONES_SHAPEFILE_URL, NWS_ZONES_SHAPEFILE_MD5
        )
    except Exception as exc:
        print(f"  zones.geojson: SKIPPED (shapefile fetch failed: {exc})")
        return

    # geopandas can read a zipped shapefile directly via zip:// URI
    try:
        gdf = gpd.read_file(f"zip://{shapefile_zip}")
    except Exception as exc:
        print(f"  zones.geojson: SKIPPED (shapefile read failed: {exc})")
        return

    # The shapefile's STATE_ZONE field is "TX192"; our canonical form is "TXZ192"
    # (matches zones.json keys). Build the canonical code and drop rows that
    # can't be matched to our metadata.
    known_zones: set[str] = set(json.loads((GEODATA / "zones.json").read_text()).keys())

    def to_canonical(state_zone: str) -> str | None:
        if not state_zone or len(state_zone) < 3:
            return None
        code = f"{state_zone[:2]}Z{state_zone[2:]}"
        return code if code in known_zones else None

    gdf["code"] = gdf["STATE_ZONE"].apply(to_canonical)
    filtered = gdf[gdf["code"].notna()].copy()
    dropped = len(gdf) - len(filtered)

    # Simplify: 0.01° ≈ ~1 km tolerance. NWS zones are huge; this is
    # visually indistinguishable at any map zoom a weather app uses, and
    # cuts the GeoJSON size dramatically.
    filtered["geometry"] = filtered["geometry"].simplify(
        tolerance=0.01, preserve_topology=True
    )

    # Keep only the fields we actually need in the bundle
    out_gdf = filtered[["code", "geometry"]].rename(columns={"code": "code"})

    # Write as minified GeoJSON
    path = out_dir / "zones.geojson"
    # geopandas writes pretty JSON by default; we want minified for bundle size
    geojson_text = out_gdf.to_json(drop_id=True)
    # to_json is already single-line/compact — write directly
    path.write_text(geojson_text)

    size_mb = path.stat().st_size / 1024 / 1024
    matched = len(filtered)
    missing_from_shapefile = len(known_zones) - len(
        set(filtered["code"])
    )
    print(
        f"  zones.geojson: {matched:>6} features, {size_mb:.1f} MB  "
        f"(dropped {dropped} unknown, missing {missing_from_shapefile} of ours)"
    )


def build_pfm_points(out_dir: Path) -> None:
    """Generate client_data/pfm_points.json from real PFM products.

    Output format (array form for compact JSON):
        {"version": 1, "points": [[name, wfo, lat, lon, zone], ...]}

    Array INDEX is the pfm_point_id used in LOC_PFM_POINT wire encoding.
    Ordering is deterministic (sorted by name) so indices are stable across
    rebuilds as long as the set of PFMs doesn't change.
    """
    pfms = _load_pfm_products()
    if not pfms:
        print("  pfm_points:   SKIPPED (no PFM products available)")
        return

    # Scrape all points from all PFMs
    all_points: list[dict] = []
    for _name, text in pfms:
        all_points.extend(_scrape_pfm_points(text))

    # Deduplicate by (name, wfo) — same point can appear in updates
    seen: dict[tuple[str, str], dict] = {}
    for p in all_points:
        key = (p["name"], p["wfo"])
        # Keep the first occurrence (PFMs within one bundle are homogeneous)
        if key not in seen:
            seen[key] = p

    # Deterministic ordering for stable indices across rebuilds
    ordered = sorted(seen.values(), key=lambda p: (p["name"], p["wfo"]))

    # Compact array form — see docstring
    out = {
        "version": 1,
        "points": [
            [p["name"], p["wfo"], p["lat"], p["lon"], p["zone"]]
            for p in ordered
        ],
    }
    path = out_dir / "pfm_points.json"
    path.write_text(json.dumps(out, separators=(",", ":"), ensure_ascii=False))
    size = path.stat().st_size / 1024
    print(f"  pfm_points:    {len(ordered):>6} points, {size:.0f} KB")


def main():
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "client_data"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"Building client preload bundle in {out_dir}/")
    build_zones(out_dir)
    build_places(out_dir)
    build_stations(out_dir)
    build_wfos(out_dir)
    build_state_index(out_dir)
    build_dictionary(out_dir)
    build_protocol_codes(out_dir)
    build_pfm_points(out_dir)
    build_zones_geojson(out_dir)

    total = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
    print(f"\nTotal: {total / 1024 / 1024:.2f} MB")
    print(f"Ship this directory with iOS and web clients.")


if __name__ == "__main__":
    main()
