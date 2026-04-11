"""Coverage: the bot's broadcast area, built from operator-configured
cities, states, and NWS WFOs.

Used to filter warnings and radar grids before broadcast so we only use
airtime for data that affects the bot's mesh area.
"""

import logging
from collections.abc import Iterable

from meshcore_weather.config import settings
from meshcore_weather.geodata import resolver
from meshcore_weather.protocol.meshwx import REGIONS


def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon test."""
    n = len(polygon)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        yi, xi = polygon[i]
        yj, xj = polygon[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi
        ):
            inside = not inside
        j = i
    return inside

logger = logging.getLogger(__name__)

BBox = tuple[float, float, float, float]  # (north, south, west, east)


def _split_csv(s: str) -> list[str]:
    """Split comma-separated string, strip whitespace, drop empties."""
    return [p.strip() for p in s.split(",") if p.strip()]


class Coverage:
    """Canonical set of NWS zones that the bot cares about.

    Sources (all optional, all additive):
      - Cities: "Austin TX" → each city's resolved zones
      - States: "TX" → all zones where zone.s == "TX"
      - WFOs: "EWX" → all zones where zone.w == "EWX"

    Empty coverage (no sources) means "broadcast everything" — legacy behavior.
    """

    def __init__(
        self,
        zones: set[str] | None = None,
        sources: dict | None = None,
    ):
        self.zones: set[str] = zones or set()
        self.sources: dict = sources or {"cities": [], "states": [], "wfos": []}
        self.bbox: BBox | None = None
        self.region_ids: set[int] = set()
        # States the operator *explicitly* covers (via home_states or via WFOs
        # whose served state we can determine). Used to match county-FIPS UGCs
        # (e.g. TXC029) which are not in the zones set.
        self.explicit_states: set[str] = set()
        for s in self.sources.get("states", []) or []:
            if s:
                self.explicit_states.add(s.upper())
        self._recompute_bbox_and_regions()
        self._derive_wfo_states()

    def _derive_wfo_states(self) -> None:
        """For each explicit WFO, add the states it serves to explicit_states."""
        wfos = {w.upper() for w in self.sources.get("wfos", []) or []}
        if not wfos:
            return
        for z in resolver._zones.values():
            if z.get("w", "").upper() in wfos:
                st = z.get("s", "").upper()
                if st:
                    self.explicit_states.add(st)

    @classmethod
    def empty(cls) -> "Coverage":
        """Empty coverage = broadcast everything (legacy behavior)."""
        return cls()

    @classmethod
    def from_config(cls) -> "Coverage":
        """Build coverage from current settings.home_cities/states/wfos."""
        return cls.from_sources(
            cities=_split_csv(settings.home_cities),
            states=_split_csv(settings.home_states),
            wfos=_split_csv(settings.home_wfos),
        )

    @classmethod
    def from_sources(
        cls,
        cities: list[str] | None = None,
        states: list[str] | None = None,
        wfos: list[str] | None = None,
    ) -> "Coverage":
        """Build coverage from explicit lists of cities/states/WFOs."""
        cities = cities or []
        states = [s.upper() for s in (states or [])]
        wfos = [w.upper() for w in (wfos or [])]

        resolver.load()
        zones: set[str] = set()

        # Cities → resolved zones
        for city in cities:
            loc = resolver.resolve(city)
            if loc and loc.get("zones"):
                zones.update(loc["zones"])

        # States → all zones in that state
        if states:
            for code, z in resolver._zones.items():
                if z.get("s") in states:
                    zones.add(code)

        # WFOs → all zones served by that office
        if wfos:
            for code, z in resolver._zones.items():
                if z.get("w") in wfos:
                    zones.add(code)

        return cls(
            zones=zones,
            sources={"cities": cities, "states": states, "wfos": wfos},
        )

    # -- Derived properties --

    def is_empty(self) -> bool:
        """No coverage set → broadcast everything (legacy behavior)."""
        return not self.zones

    def _recompute_bbox_and_regions(self) -> None:
        """Compute bbox of all zone centroids + overlapping MeshWX regions."""
        if not self.zones:
            self.bbox = None
            self.region_ids = set()
            return

        lats: list[float] = []
        lons: list[float] = []
        for code in self.zones:
            z = resolver._zones.get(code)
            if z:
                lats.append(z["la"])
                lons.append(z["lo"])

        if not lats:
            self.bbox = None
            self.region_ids = set()
            return

        # Pad the bbox by ~1 degree so region overlap is tolerant
        pad = 1.0
        self.bbox = (
            max(lats) + pad,
            min(lats) - pad,
            min(lons) - pad,
            max(lons) + pad,
        )

        # Find MeshWX regions that overlap the bbox
        self.region_ids = set()
        n, s, w, e = self.bbox
        for rid, region in REGIONS.items():
            # Standard AABB overlap test
            if not (region["s"] > n or region["n"] < s or region["w"] > e or region["e"] < w):
                self.region_ids.add(rid)

    # -- Filtering API --

    def covers_zone(self, zone_code: str) -> bool:
        """Is this exact zone code in our coverage?"""
        return zone_code in self.zones

    def covers_any(self, zone_codes: Iterable[str]) -> bool:
        """Does any of these UGC codes intersect our coverage?

        Accepts both NWS zone codes (TXZ192) and county FIPS (TXC029). Match
        rules, in order:
          1. Exact zone match against our zones set (narrow, precise)
          2. If the operator explicitly covers a state (home_states or a WFO
             in that state), accept any UGC whose 2-letter prefix matches.
             This handles warnings whose UGC line uses county FIPS codes,
             which are NOT in our zones set.
        """
        if not self.zones:
            return True  # empty coverage = accept everything
        codes = list(zone_codes)
        for code in codes:
            if code in self.zones:
                return True
        if self.explicit_states:
            for code in codes:
                if len(code) >= 2 and code[:2].upper() in self.explicit_states:
                    return True
        return False

    def covers_polygon(self, vertices: list[tuple[float, float]]) -> bool:
        """Does any zone centroid fall inside the given polygon?

        A rough but effective check: if the polygon contains any of our
        zone centroids, the warning affects us.
        """
        if not self.zones or not vertices or len(vertices) < 3:
            return self.is_empty()  # empty coverage = accept
        for code in self.zones:
            z = resolver._zones.get(code)
            if z and _point_in_polygon(z["la"], z["lo"], vertices):
                return True
        return False

    def summary(self) -> str:
        """Human-readable summary for logging/portal display."""
        if self.is_empty():
            return "all regions (no filter)"
        parts = []
        if self.sources.get("cities"):
            parts.append(f"{len(self.sources['cities'])} cities")
        if self.sources.get("states"):
            parts.append(f"states={','.join(self.sources['states'])}")
        if self.sources.get("wfos"):
            parts.append(f"wfos={','.join(self.sources['wfos'])}")
        return f"{len(self.zones)} zones ({', '.join(parts)}); {len(self.region_ids)} radar regions"
