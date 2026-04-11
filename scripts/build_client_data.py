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

import json
import sys
from collections import OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GEODATA = ROOT / "meshcore_weather" / "geodata"


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
        "version": 3,
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

    total = sum(f.stat().st_size for f in out_dir.iterdir() if f.is_file())
    print(f"\nTotal: {total / 1024 / 1024:.2f} MB")
    print(f"Ship this directory with iOS and web clients.")


if __name__ == "__main__":
    main()
