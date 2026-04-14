"""Offline geolocation: resolve city names, station IDs, and zone codes.

Bundled data files (~1.7 MB total):
    zones.json   - 4,029 NWS forecast zones with centroids, counties, WFO IDs
    places.json  - 32,333 US Census places with coordinates
    stations.json - 2,237 active US METAR stations with coordinates
"""

import json
import logging
import math
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent


def _load_json(name: str):
    path = _DATA_DIR / name
    with open(path) as f:
        return json.load(f)


def _normalize(text: str) -> str:
    """Strip accents and normalize to ASCII uppercase for matching."""
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).upper()


class LocationResolver:
    """Resolve natural language locations to NWS zone codes, offline.

    Handles:
        - Station IDs: "KAUS" → zone TXZ192, WFO EWX
        - Zone codes: "TXZ192" → directly
        - City + state: "Austin TX" → zone TXZ192
        - City only: "Austin" → best guess (largest/first match)
        - State only: "TX" → all zones in state
    """

    def __init__(self):
        self._zones: dict = {}      # zone_code -> {n, w, s, la, lo, c}
        self._places: list = []     # [[NAME, STATE, lat, lon], ...]
        self._stations: dict = {}   # ICAO -> {n, s, la, lo}
        self._loaded = False

    def load(self) -> None:
        if self._loaded:
            return
        self._zones = _load_json("zones.json")
        self._places = _load_json("places.json")
        self._stations = _load_json("stations.json")
        self._build_three_letter_index()
        self._loaded = True
        logger.info(
            "Location data loaded: %d zones, %d places, %d stations",
            len(self._zones), len(self._places), len(self._stations),
        )

    # Common IATA codes that differ from the ICAO suffix.
    # Only needed where dropping the ICAO prefix doesn't yield the IATA code.
    _IATA_TO_ICAO = {
        "SJU": "TJSJ",  # San Juan PR
        "BQN": "TJBQ",  # Aguadilla PR
        "SIG": "TJIG",  # Isla Grande PR
        "NRR": "TJNR",  # Roosevelt Roads PR
    }

    def _build_three_letter_index(self) -> None:
        """Build a 3-letter → ICAO lookup from station data.

        For collisions, prefer K (CONUS) > PH (Hawaii) > TJ (Caribbean)
        > PA (Alaska) > others.
        """
        priority = {"K": 0, "PH": 1, "TJ": 1, "PA": 2}
        index: dict[str, str] = {}
        for icao in self._stations:
            if len(icao) != 4:
                continue
            three = icao[1:].upper()
            prefix = icao[:2] if icao[0] in ("P", "T") else icao[0]
            new_pri = priority.get(prefix, 3)
            if three in index:
                old_icao = index[three]
                old_prefix = old_icao[:2] if old_icao[0] in ("P", "T") else old_icao[0]
                old_pri = priority.get(old_prefix, 3)
                if new_pri >= old_pri:
                    continue
            index[three] = icao
        # Add IATA overrides for stations where IATA != ICAO suffix
        for iata, icao in self._IATA_TO_ICAO.items():
            if icao in self._stations:
                index[iata] = icao
        self._three_to_icao = index

    def resolve(self, query: str) -> dict | None:
        """Resolve a location query to zone info.

        Returns dict with keys:
            zones: list[str]  - NWS zone codes (e.g. ["TXZ192"])
            wfos: list[str]   - WFO IDs (e.g. ["EWX"])
            station: str|None - nearest METAR station (e.g. "KAUS")
            name: str         - human-readable resolved name
            lat: float
            lon: float
        Or None if unresolvable.
        """
        self.load()
        query = query.strip()
        if not query:
            return None

        upper = query.upper()

        # 1. Direct zone code (e.g. TXZ192)
        if len(upper) >= 5 and upper[2] == "Z" and upper[3:].isdigit():
            return self._resolve_zone(upper)

        # 2. Station ID (e.g. KAUS, TJSJ - any 4-letter ICAO code in our database)
        if len(upper) == 4 and upper.isalpha() and upper in self._stations:
            return self._resolve_station(upper)

        # 2b. 3-letter code (e.g. AUS → KAUS, SJU → TJSJ, HNL → PHNL)
        if len(upper) == 3 and upper.isalpha() and upper in self._three_to_icao:
            return self._resolve_station(self._three_to_icao[upper])

        # 3. City + State (e.g. "Austin TX", "Round Rock, TX")
        parts = upper.replace(",", " ").split()
        if len(parts) >= 2:
            state = parts[-1]
            if len(state) == 2 and state.isalpha():
                city = " ".join(parts[:-1])
                result = self._resolve_city_state(city, state)
                if result:
                    return result

        # 4. Try as city name without state
        return self._resolve_city(upper)

    def _resolve_zone(self, zone_code: str) -> dict | None:
        zone = self._zones.get(zone_code)
        if not zone:
            return None
        station = self._nearest_station(zone["la"], zone["lo"])
        return {
            "zones": [zone_code],
            "wfos": [zone["w"]],
            "station": station,
            "name": f"{zone['n']}, {zone['s']}",
            "lat": zone["la"],
            "lon": zone["lo"],
        }

    def _resolve_station(self, icao: str) -> dict | None:
        st = self._stations.get(icao)
        if not st:
            return None
        zones = self._nearest_zones(st["la"], st["lo"], n=2)
        wfos = list({self._zones[z]["w"] for z in zones if z in self._zones})
        # Shorten verbose airport names for LoRa display
        name = self._short_station_name(st["n"], st["s"])
        return {
            "zones": zones,
            "wfos": wfos,
            "station": icao,
            "name": name,
            "lat": st["la"],
            "lon": st["lo"],
        }

    @staticmethod
    def _short_station_name(name: str, state: str) -> str:
        """Shorten station names for compact display."""
        n = name.title()
        # Strip common airport suffixes
        for suffix in [" International Airport", " Intl Airport", " Intl Ap",
                       " Regional Airport", " Municipal Airport", " Airport",
                       " Arpt", " Ap", " Field"]:
            if n.lower().endswith(suffix.lower()):
                n = n[:-len(suffix)].rstrip(" -/")
                break
        return f"{n}, {state}"

    def _resolve_city_state(self, city: str, state: str) -> dict | None:
        city_n = _normalize(city)
        matches = [p for p in self._places if p[1] == state and _normalize(p[0]) == city_n]
        if not matches:
            matches = [p for p in self._places if p[1] == state and city_n in _normalize(p[0])]
        if not matches:
            return None
        place = matches[0]
        lat, lon = place[2], place[3]
        zones = self._nearest_zones(lat, lon, n=2)
        wfos = list({self._zones[z]["w"] for z in zones if z in self._zones})
        station = self._nearest_station(lat, lon)
        return {
            "zones": zones,
            "wfos": wfos,
            "station": station,
            "name": f"{place[0].title()}, {place[1]}",
            "lat": lat,
            "lon": lon,
        }

    def _resolve_city(self, city: str) -> dict | None:
        city_n = _normalize(city)
        matches = [p for p in self._places if _normalize(p[0]) == city_n]
        if not matches:
            matches = [p for p in self._places if city_n in _normalize(p[0])]
        if not matches:
            return None
        # Pick the first match (could improve with population data)
        place = matches[0]
        lat, lon = place[2], place[3]
        zones = self._nearest_zones(lat, lon, n=2)
        wfos = list({self._zones[z]["w"] for z in zones if z in self._zones})
        station = self._nearest_station(lat, lon)
        return {
            "zones": zones,
            "wfos": wfos,
            "station": station,
            "name": f"{place[0].title()}, {place[1]}",
            "lat": lat,
            "lon": lon,
        }

    def resolve_by_place_index(self, idx: int) -> dict | None:
        """Resolve a place index to zone info (for LOC_PLACE requests)."""
        self.load()
        if idx < 0 or idx >= len(self._places):
            return None
        p = self._places[idx]
        return self.resolve_by_coords(p[2], p[3])

    def resolve_by_coords(self, lat: float, lon: float) -> dict | None:
        """Resolve GPS coordinates to zone info (for location-aware DM)."""
        self.load()
        zones = self._nearest_zones(lat, lon, n=2)
        if not zones:
            return None
        wfos = list({self._zones[z]["w"] for z in zones if z in self._zones})
        station = self._nearest_station(lat, lon)
        # Find nearest place name
        best_place = None
        best_d = float("inf")
        for p in self._places:
            d = _haversine(lat, lon, p[2], p[3])
            if d < best_d:
                best_d = d
                best_place = p
        name = f"{best_place[0].title()}, {best_place[1]}" if best_place else f"{lat:.2f}, {lon:.2f}"
        return {
            "zones": zones,
            "wfos": wfos,
            "station": station,
            "name": name,
            "lat": lat,
            "lon": lon,
        }

    def _nearest_zones(self, lat: float, lon: float, n: int = 2) -> list[str]:
        """Find the n nearest NWS zones by Haversine distance to centroids."""
        dists = []
        for code, z in self._zones.items():
            d = _haversine(lat, lon, z["la"], z["lo"])
            dists.append((d, code))
        dists.sort()
        return [code for _, code in dists[:n]]

    def find_place_index(self, lat: float, lon: float) -> int | None:
        """Find the index of the nearest place in places.json by coordinates.

        Returns the array index (uint24 place_id for LOC_PLACE) or None if
        no places are loaded.
        """
        self.load()
        best_idx = None
        best_d = float("inf")
        for i, p in enumerate(self._places):
            d = _haversine(lat, lon, p[2], p[3])
            if d < best_d:
                best_d = d
                best_idx = i
        return best_idx

    def _nearest_station(self, lat: float, lon: float) -> str | None:
        """Find the nearest METAR station."""
        best_d = float("inf")
        best = None
        for icao, s in self._stations.items():
            d = _haversine(lat, lon, s["la"], s["lo"])
            if d < best_d:
                best_d = d
                best = icao
        return best


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine distance in km between two lat/lon points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.asin(math.sqrt(a))


# Module-level singleton
resolver = LocationResolver()
