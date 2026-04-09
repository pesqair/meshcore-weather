"""Parse EMWIN weather products following the Vitality GOES approach.

Products are matched by their EMWIN 8-character identifier:
    chars 0-2: product type (ZFP, RWR, SVR, etc.)
    chars 3-5: NWS office code (EWX, MFL, BUF, etc.)
    chars 6-7: state abbreviation (TX, FL, NY, etc.)

For a location like "Austin TX", we resolve to:
    orig = "EWXTX" (office EWX + state TX)
    wxZone = "TXZ192"
    city = "AUSTIN"
    lat/lon for geofencing

Then find EMWIN products: ZFP+EWXTX, RWR+EWXTX, PFM+EWXTX, etc.
"""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from meshcore_weather.geodata import resolver

logger = logging.getLogger(__name__)

# Meshcore channel messages: 300 byte frame, 7 bytes overhead = 293 usable
MAX_MSG_BYTES = 136


def _fit_message(text: str) -> str:
    if len(text) <= MAX_MSG_BYTES:
        return text
    return text[: MAX_MSG_BYTES - 3].rstrip() + "..."


MORE_TAG = " [more]"


def paginate(text: str, offset: int = 0) -> tuple[str, int, bool]:
    """Return (chunk, new_offset, has_more) for the given offset into text.

    Tries to break on a newline boundary so messages don't cut mid-line,
    but only if that doesn't waste more than half the available space.
    """
    remaining = text[offset:]
    if len(remaining) <= MAX_MSG_BYTES:
        return remaining, offset + len(remaining), False
    usable = MAX_MSG_BYTES - len(MORE_TAG)
    cut = remaining[:usable]
    # Try to break at the last newline, but only if it uses >60% of space
    nl = cut.rfind("\n")
    if nl > usable * 3 // 5:
        cut = cut[:nl]
    chunk = cut.rstrip() + MORE_TAG
    return chunk, offset + len(cut), True


def _expand_zone_ranges(text: str) -> set[str]:
    """Expand NWS zone range notation like 'TXZ021>044' into individual zones.

    Searches the first 25 lines of text for zone codes and ranges.
    """
    zones: set[str] = set()
    for line in text.splitlines()[:25]:
        s = line.strip()
        # Match explicit zones and ranges: "TXZ021>044" or "TXZ192"
        for m in re.finditer(r"([A-Z]{2}Z)(\d{3})(?:>(\d{3}))?", s):
            prefix = m.group(1)
            start = int(m.group(2))
            end = int(m.group(3)) if m.group(3) else start
            for n in range(start, end + 1):
                zones.add(f"{prefix}{n:03d}")
    return zones


def extract_warning_polygon(text: str) -> list[tuple[float, float]]:
    """Extract LAT...LON polygon vertices from an NWS warning product."""
    coords: list[tuple[float, float]] = []
    in_polygon = False
    for line in text.splitlines():
        stripped = line.strip()
        if "LAT...LON" in stripped:
            in_polygon = True
            nums = re.findall(r"\d{4,5}", stripped.split("LAT...LON")[-1])
            for j in range(0, len(nums) - 1, 2):
                try:
                    coords.append((int(nums[j]) / 100, -(int(nums[j + 1]) / 100)))
                except (ValueError, IndexError):
                    pass
            continue
        if in_polygon:
            if not stripped or stripped.startswith("TIME") or stripped.startswith("$$"):
                break
            nums = re.findall(r"\d{4,5}", stripped)
            for j in range(0, len(nums) - 1, 2):
                try:
                    coords.append((int(nums[j]) / 100, -(int(nums[j + 1]) / 100)))
                except (ValueError, IndexError):
                    pass
    return coords


def parse_vtec(text: str) -> dict | None:
    """Parse VTEC line from an NWS warning product.

    Returns dict with action, phenomenon, significance, and end time,
    or None if no VTEC line found.
    """
    for line in text.splitlines()[:30]:
        s = line.strip()
        m = re.match(
            r"/O\.(\w+)\.\w{4}\.(\w{2})\.(\w)\.\d{4}\.\d{6}T\d{4}Z-(\d{6}T\d{4}Z)/",
            s,
        )
        if m:
            return {
                "action": m.group(1),
                "phenomenon": m.group(2),
                "significance": m.group(3),
                "end": m.group(4),
            }
    return None


def _age_str(ts: datetime) -> str:
    now = datetime.now(timezone.utc)
    secs = max(0, int((now - ts).total_seconds()))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h"
    return f"{secs // 86400}d"


# EMWIN 8-char identifier regex from filename
# e.g. A_FPUS54KOUN072238_C_KWIN_20260407222840_123456-2-ZFPOUNOK.TXT
#                                                          ^^^^^^^^
EMWIN_ID_RE = re.compile(r"-([A-Z0-9]{8})\.TXT$", re.IGNORECASE)

# Timestamp from EMWIN filename (5th underscore-delimited field)
EMWIN_TS_RE = re.compile(r"_(\d{14})_")


@dataclass
class EMWINProduct:
    """A parsed EMWIN product with metadata from its filename."""
    filename: str
    emwin_id: str       # 8-char identifier e.g. "ZFPEWXTX"
    product_type: str   # first 3 chars e.g. "ZFP"
    orig: str           # last 5 chars e.g. "EWXTX"
    office: str         # chars 3-5 e.g. "EWX"
    state: str          # chars 6-7 e.g. "TX"
    timestamp: datetime
    raw_text: str


class WeatherStore:
    """EMWIN product store using Vitality GOES-style filename matching."""

    def __init__(self):
        self._products: dict[str, EMWINProduct] = {}  # keyed by filename

    def ingest(self, raw_products: list[dict]) -> int:
        count = 0
        for raw in raw_products:
            prod = self._parse(raw)
            if prod:
                self._products[prod.filename] = prod
                count += 1
        # Expire old products from the store
        cutoff = datetime.now(timezone.utc) - timedelta(hours=12)
        before = len(self._products)
        self._products = {
            k: v for k, v in self._products.items()
            if v.timestamp > cutoff
        }
        expired = before - len(self._products)
        logger.info("Ingested %d/%d products%s",
                     count, len(raw_products),
                     f" (expired {expired})" if expired else "")
        return count

    def _parse(self, raw: dict) -> EMWINProduct | None:
        filename = raw.get("filename", "")
        raw_text = raw.get("raw_text", "")

        # Extract 8-char EMWIN identifier from filename
        m = EMWIN_ID_RE.search(filename)
        if not m:
            return None
        emwin_id = m.group(1).upper()
        if len(emwin_id) != 8:
            return None

        product_type = emwin_id[:3]
        orig = emwin_id[3:]
        office = orig[:3]
        state = orig[3:]

        # Extract timestamp from filename
        ts = datetime.now(timezone.utc)
        m_ts = EMWIN_TS_RE.search(filename)
        if m_ts:
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return EMWINProduct(
            filename=filename,
            emwin_id=emwin_id,
            product_type=product_type,
            orig=orig,
            office=office,
            state=state,
            timestamp=ts,
            raw_text=raw_text,
        )

    # -- Finding products by type + orig --

    def _find(self, product_type: str, orig: str) -> EMWINProduct | None:
        """Find the newest product matching type+orig (e.g. ZFP + EWXTX)."""
        target = (product_type + orig).upper()
        best = None
        for prod in self._products.values():
            if prod.emwin_id == target:
                if best is None or prod.timestamp > best.timestamp:
                    best = prod
        return best

    def _find_any_orig(self, product_type: str, origs: list[str]) -> EMWINProduct | None:
        """Try multiple orig values, return newest match."""
        for orig in origs:
            p = self._find(product_type, orig)
            if p:
                return p
        return None

    def _find_warnings(self, origs: list[str], lat: float, lon: float) -> list[EMWINProduct]:
        """Find active warnings for the given origs, optionally geofenced."""
        warn_types = ["SVS", "SPS", "NPW", "FLS", "FLW", "WSW", "SWO", "MWS", "DSW", "EWW", "SQW"]
        results = []
        for prod in self._products.values():
            if prod.product_type in warn_types and prod.orig in [o.upper() for o in origs]:
                if self._is_cancelled(prod.raw_text):
                    continue
                # Check if warning is geofenced and applies to our location
                if lat and lon and not self._in_warning_polygon(prod.raw_text, lat, lon):
                    continue
                results.append(prod)
        # Deduplicate by type, keep newest
        seen = {}
        for p in sorted(results, key=lambda x: x.timestamp, reverse=True):
            key = p.product_type
            if key not in seen:
                seen[key] = p
        return list(seen.values())

    # -- Building location context from resolver --

    # WFO codes that differ between zone database and AWIPS product IDs
    _WFO_AWIPS_ALIASES = {
        "SJU": "JSJ",  # San Juan PR
        "GUM": "GUA",  # Guam
    }

    def _build_origs(self, loc: dict) -> list[str]:
        """Build possible orig values (office+state) from resolver result."""
        origs = []
        state = ""
        name = loc.get("name", "")
        if ", " in name:
            state = name.split(", ")[-1].strip()

        wfos = list(loc.get("wfos", []))
        # Add AWIPS aliases (SJU->JSJ, etc.)
        for wfo in list(wfos):
            alias = self._WFO_AWIPS_ALIASES.get(wfo)
            if alias and alias not in wfos:
                wfos.append(alias)

        for wfo in wfos:
            if state and len(state) == 2:
                orig = f"{wfo}{state}"
                if orig not in origs:
                    origs.append(orig)
            for zone in loc.get("zones", []):
                zone_state = zone[:2]
                orig = f"{wfo}{zone_state}"
                if orig not in origs:
                    origs.append(orig)
        return [o.upper() for o in origs]

    # -- Public API --

    def get_summary(self, location: str) -> str:
        """Build weather summary for a location. Fits in one Meshcore message."""
        loc = resolver.resolve(location)
        if not loc:
            return f"Unknown location: {location}"

        origs = self._build_origs(loc)
        zone = loc["zones"][0] if loc["zones"] else ""
        city = loc["name"].split(",")[0].strip().upper()
        # Clean PR suffixes
        for trim in [" ZONA URBANA", " COMUNIDAD", " MUNICIPIO", " CDP"]:
            if trim in city:
                city = city[: city.index(trim)]
        lat = loc.get("lat", 0)
        lon = loc.get("lon", 0)
        station = loc.get("station", "")
        loc_name = loc["name"]

        parts = []

        # 1. Warnings (geofenced)
        warnings = self._find_warnings(origs, lat, lon)
        if warnings:
            w = warnings[0]
            summary = self._extract_warning_headline(w.raw_text)
            parts.append(f"!! {w.product_type}({_age_str(w.timestamp)}): {summary}")

        # 2. Current conditions: try RWR city table first, fall back to METAR
        obs_found = False
        rwr = self._find_any_orig("RWR", origs)
        if rwr:
            conditions = self._parse_rwr_city(rwr.raw_text, city)
            if conditions:
                parts.append(f"OBS({_age_str(rwr.timestamp)}): {conditions}")
                obs_found = True

        if not obs_found and station:
            metar = self._find_metar(station)
            if metar:
                parts.append(f"OBS({_age_str(metar[1])}): {metar[0]}")
                obs_found = True

        # 4. Zone forecast from ZFP
        zfp = self._find_any_orig("ZFP", origs)
        if zfp and zone:
            forecast = self._parse_zfp_zone(zfp.raw_text, zone)
            if forecast:
                parts.append(f"FCST({_age_str(zfp.timestamp)}): {forecast}")

        # 5. Fallback: try AFD discussion summary
        if not zfp:
            afd = self._find_any_orig("AFD", origs)
            if afd:
                summary = self._parse_afd_summary(afd.raw_text)
                if summary:
                    parts.append(f"DSCN({_age_str(afd.timestamp)}): {summary}")

        if not parts:
            return f"{loc_name}\nNo weather data available. Data refreshes every 5m."

        return f"{loc_name}\n" + "\n".join(parts)

    def get_warnings(self, location: str) -> str:
        loc = resolver.resolve(location)
        if not loc:
            return f"Unknown location: {location}"
        origs = self._build_origs(loc)
        lat = loc.get("lat", 0)
        lon = loc.get("lon", 0)
        warnings = self._find_warnings(origs, lat, lon)
        if not warnings:
            return f"No active warnings for {loc['name']}."
        lines = [f"!! {len(warnings)} warn near {loc['name']}:"]
        for w in warnings:
            headline = self._short_headline(w.raw_text)
            lines.append(f" {headline}")
        return "\n".join(lines)

    def get_forecast(self, location: str) -> str:
        loc = resolver.resolve(location)
        if not loc:
            return f"Unknown location: {location}"
        origs = self._build_origs(loc)
        zone = loc["zones"][0] if loc["zones"] else ""

        zfp = self._find_any_orig("ZFP", origs)
        if zfp and zone:
            forecast = self._parse_zfp_zone(zfp.raw_text, zone)
            if forecast:
                return f"{loc['name']} forecast ({_age_str(zfp.timestamp)}):\n{forecast}"

        # Fallback to AFD
        afd = self._find_any_orig("AFD", origs)
        if afd:
            summary = self._parse_afd_summary(afd.raw_text)
            if summary:
                return f"{loc['name']} discussion ({_age_str(afd.timestamp)}):\n{summary}"

        # Fallback: show current conditions from wx if no forecast available
        summary = self.get_summary(location)
        if "No weather data" not in summary:
            return f"No forecast yet for {loc['name']}. Current:\n{summary}"

        return f"No forecast for {loc['name']}. No data in last 3hrs."

    def get_raw_metar(self, station: str) -> str:
        """Get raw METAR for a station ID."""
        station = station.upper().strip()
        for prod in self._products.values():
            if "METAR" not in prod.raw_text[:200] and prod.product_type != "SAH":
                continue
            for line in prod.raw_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(station) and re.match(r"^[A-Z]{4}\s+\d{6}Z", stripped):
                    # Grab continuation lines (METAR can wrap with leading spaces)
                    full = stripped
                    idx = prod.raw_text.index(stripped)
                    remaining = prod.raw_text[idx + len(stripped):]
                    for cont in remaining.splitlines():
                        if cont.startswith("     ") or cont.startswith("\t"):
                            full += " " + cont.strip()
                        else:
                            break
                    return f"METAR {full}"
        return f"No METAR for {station}."

    def get_raw_taf(self, station: str) -> str:
        """Get raw TAF for a station ID."""
        station = station.upper().strip()
        for prod in self._products.values():
            if prod.product_type != "TAF" and "TAF" not in prod.raw_text[:200]:
                continue
            lines = prod.raw_text.splitlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith(f"TAF {station}") or (
                    stripped.startswith(station) and "TAF" in prod.raw_text[:50]
                ):
                    # Grab the TAF block until next station or end
                    taf_lines = [stripped]
                    for j in range(i + 1, min(i + 15, len(lines))):
                        s = lines[j].strip()
                        if not s or s.startswith("=") or (
                            re.match(r"^[A-Z]{4}\s+\d{6}Z", s) and j > i + 1
                        ):
                            break
                        taf_lines.append(s)
                    return "TAF " + " ".join(taf_lines)
        return f"No TAF for {station}."

    def _short_headline(self, text: str) -> str:
        """Make a warning headline compact for LoRa display."""
        h = self._extract_warning_headline(text)
        # Skip junk headlines
        if any(junk in h.lower() for junk in ("www.", "http", "graphic product")):
            h = "Active warning"
        # Strip common verbose prefixes
        for prefix in ("A ", "AN "):
            if h.startswith(prefix):
                h = h[len(prefix):]
        # Shorten common phrases — order matters to avoid double subs
        h = (h.replace("REMAINS IN EFFECT UNTIL ", "til ")
              .replace("IN EFFECT UNTIL ", "til ")
              .replace("REMAINS IN EFFECT", "")
              .replace("IN EFFECT ", "")
              .replace("WILL IMPACT", "for")
              .replace("UNTIL ", "til ")
              .replace("THROUGH ", "thru ")
              .replace("THIS EVENING", "eve")
              .replace("THIS AFTERNOON", "aft")
              .replace("THIS MORNING", "morn"))
        # Collapse multiple spaces
        h = re.sub(r"  +", " ", h).strip()
        return h

    def _count_warnings(self) -> dict[str, int]:
        """Count active (non-cancelled) warnings per affected state."""
        warn_types = {"SVS", "SPS", "NPW", "FLS", "FLW", "WSW", "SWO", "MWS", "DSW", "EWW", "SQW"}
        from collections import Counter
        state_counts: Counter[str] = Counter()
        seen: set[str] = set()
        for prod in sorted(self._products.values(), key=lambda p: p.timestamp, reverse=True):
            if prod.product_type not in warn_types:
                continue
            key = f"{prod.product_type}_{prod.orig}"
            if key in seen:
                continue
            seen.add(key)
            if self._is_cancelled(prod.raw_text):
                continue
            st = self._affected_state(prod)
            if st:
                state_counts[st] += 1
        return dict(state_counts)

    def _count_rain(self, state: str = "") -> int:
        """Count areas reporting rain, consistent with scan_rain."""
        result = self.scan_rain(state)
        # Parse count from "Rain ... (N area(s)):" or "No rain..."
        m = re.search(r"\((\d+) area", result)
        return int(m.group(1)) if m else 0

    def _count_storm_reports(self, state: str = "") -> int:
        """Count storm reports, consistent with get_storm_reports."""
        result = self.get_storm_reports(state)
        m = re.search(r"\((\d+)\):", result)
        return int(m.group(1)) if m else 0

    def national_overview(self) -> str:
        """Top-level overview of weather activity nationwide."""
        warn_counts = self._count_warnings()
        total_warn = sum(warn_counts.values())
        rain_count = self._count_rain()
        storm_count = self._count_storm_reports()

        lines = ["GOES-E EMWIN Weather:"]

        # Warnings
        if total_warn:
            warn_states = sorted(warn_counts.keys())
            lines.append(f"!! {total_warn} warnings: {' '.join(warn_states)}")
        else:
            lines.append("No active warnings")

        # Rain & storms
        activity = []
        if rain_count:
            activity.append(f"{rain_count} rain")
        if storm_count:
            activity.append(f"{storm_count} storm rpts")
        if activity:
            lines.append(" | ".join(activity))

        lines.append("Send <ST> or wx <city ST>")
        return "\n".join(lines)

    def state_overview(self, state: str) -> str:
        """Overview of weather data available for a state."""
        state = state.upper().strip()
        lines = [f"{state} Weather:"]

        # Warnings
        warn_counts = self._count_warnings()
        nw = warn_counts.get(state, 0)
        if nw:
            lines.append(f"!! {nw} warning(s) - send: warn {state}")

        # Rain
        nr = self._count_rain(state)
        if nr:
            lines.append(f"{nr} rain area(s) - send: rain {state}")

        # Storm reports
        ns = self._count_storm_reports(state)
        if ns:
            lines.append(f"{ns} storm rpt(s) - send: storm {state}")

        # Available data types
        avail = set()
        for prod in self._products.values():
            if prod.state == state:
                avail.add(prod.product_type)
        cmds = []
        if avail & {"ZFP", "AFD"}:
            cmds.append("forecast")
        if "HWO" in avail:
            cmds.append("outlook")
        if avail & {"SAH", "RWR"}:
            cmds.append("wx")
        if cmds:
            lines.append(f"Try: {'/'.join(cmds)} <city {state}>")

        if len(lines) == 1:
            lines.append("No data available")
        return "\n".join(lines)

    def warn_summary(self) -> str:
        """Return a compact overview: total count + list of states with warnings."""
        state_counts = self._count_warnings()
        total = sum(state_counts.values())
        if not total:
            return "No active warnings."
        states = []
        for st in sorted(state_counts):
            n = state_counts[st]
            states.append(f"{st}({n})" if n > 1 else st)
        return f"!! {total} warnings in {len(state_counts)} states:\n" + " ".join(states) + "\nwarn <ST> for details"

    def scan_warnings(self, state_filter: str = "") -> str:
        """Scan all products for active warnings, optionally filtered by state."""
        warn_types = {"SVS", "SPS", "NPW", "FLS", "FLW", "WSW", "SWO", "MWS", "DSW", "EWW", "SQW"}
        state_filter = state_filter.upper().strip()
        warnings = []
        seen = set()
        for prod in sorted(self._products.values(), key=lambda p: p.timestamp, reverse=True):
            if prod.product_type not in warn_types:
                continue
            st = self._affected_state(prod)
            if state_filter and st != state_filter:
                continue
            key = f"{prod.product_type}_{prod.orig}"
            if key in seen:
                continue
            seen.add(key)
            if self._is_cancelled(prod.raw_text):
                continue
            headline = self._short_headline(prod.raw_text)
            if state_filter:
                warnings.append(headline)
            else:
                warnings.append(f"{st}: {headline}")

        label = state_filter if state_filter else "nationwide"
        if not warnings:
            return f"No active warnings {label}."
        warnings.sort()
        lines = [f"!! {len(warnings)} warn {label}:"]
        for w in warnings:
            lines.append(f" {w}")
        if not state_filter and len(warnings) > 5:
            lines.append("warn <ST> for details")
        return "\n".join(lines)

    def scan_rain(self, state_filter: str = "") -> str:
        """Scan RWR tables for cities currently reporting rain."""
        state_filter = state_filter.upper().strip()
        rain_keywords = {"RAIN", "LGT RAIN", "HVY RAIN", "TSTORM", "DRIZZLE", "SHOWERS"}
        rainy = []
        for prod in self._products.values():
            if prod.product_type != "RWR":
                continue
            if state_filter and prod.state != state_filter:
                continue
            in_table = False
            for line in prod.raw_text.splitlines():
                stripped = line.strip()
                if "SKY/WX" in stripped and "TMP" in stripped:
                    in_table = True
                    continue
                if not in_table or not stripped:
                    continue
                if stripped.startswith("$$"):
                    in_table = False
                    continue
                upper = stripped.upper()
                all_sky = rain_keywords | {
                    "SUNNY", "MOSUNNY", "PTSUNNY", "CLEAR", "MOCLDY",
                    "PTCLDY", "CLOUDY", "FAIR", "FOG", "HAZE", "WINDY",
                    "LGT", "HVY",
                }
                for kw in rain_keywords:
                    if kw in upper:
                        parts = stripped.split()
                        city_parts = []
                        for p in parts:
                            if p.upper() in all_sky or p.lstrip("-").isdigit():
                                break
                            city_parts.append(p)
                        city = " ".join(city_parts).title()
                        if city and city not in [r.split(":")[0] for r in rainy]:
                            for tp in parts:
                                if tp.lstrip("-").isdigit():
                                    rainy.append(f"{city}: Rain {tp}F")
                                    break
                            else:
                                rainy.append(f"{city}: Rain")
                        break

        label = f"in {state_filter}" if state_filter else "anywhere"
        if not rainy:
            return f"No rain reported {label}."
        lines = [f"Rain {label} ({len(rainy)} area(s)):"]
        for r in rainy:
            lines.append(f" {r}")
        return "\n".join(lines)

    def get_outlook(self, location: str) -> str:
        """Get Hazardous Weather Outlook (HWO) for a location."""
        loc = resolver.resolve(location)
        if not loc:
            return f"Unknown location: {location}"
        origs = self._build_origs(loc)
        hwo = self._find_any_orig("HWO", origs)
        # Fallback: try any HWO that covers this location's zones
        if not hwo:
            loc_zones = set(loc.get("zones", []))
            if loc_zones:
                best = None
                for prod in self._products.values():
                    if prod.product_type != "HWO":
                        continue
                    # Check if any of the product's zone codes match
                    # HWO zone lines use ranges like "TXZ021>044"
                    prod_zones = _expand_zone_ranges(prod.raw_text)
                    if prod_zones and loc_zones & prod_zones:
                        if best is None or prod.timestamp > best.timestamp:
                            best = prod
                hwo = best
        if not hwo:
            return f"No outlook available for {loc['name']}."
        summary = self._parse_hwo(hwo.raw_text)
        return f"{loc['name']} outlook ({_age_str(hwo.timestamp)}):\n{summary}"

    def _parse_hwo(self, text: str) -> str:
        """Extract the key sections from a Hazardous Weather Outlook."""
        lines = text.splitlines()
        parts = []
        collecting = False
        current_section = ""
        for line in lines:
            stripped = line.strip()
            # Section markers: .DAY ONE, .DAYS TWO THROUGH SEVEN, .SPOTTER
            if stripped.startswith(".DAY"):
                if parts:
                    parts.append("")  # separator
                current_section = stripped.lstrip(".").strip()
                # Shorten section names
                current_section = (current_section
                    .replace("DAYS TWO THROUGH SEVEN...", "Days 2-7:")
                    .replace("DAY ONE...", "Today:"))
                parts.append(current_section)
                collecting = True
                continue
            if stripped.startswith(".SPOTTER") or stripped.startswith("$$"):
                break
            if collecting:
                if stripped.startswith("&&"):
                    break
                if not stripped:
                    continue
                # Skip zone/county header lines and timestamps
                if re.match(r"^[A-Z]{2}Z\d{3}", stripped):
                    continue
                if re.match(r"^\d{3,4}\s+(AM|PM)\s+\w+", stripped):
                    continue
                if stripped.startswith("See the Graphical"):
                    continue
                if stripped.startswith("http"):
                    continue
                # Compact structured fields (RISK..., AREA..., ONSET...)
                if re.match(r"^(RISK|AREA|ONSET|DISCUSSION)\.\.\.", stripped):
                    field = stripped.replace("...", ": ", 1).rstrip(".")
                    parts.append(field)
                    continue
                parts.append(stripped)
        if not parts:
            return "No hazards identified."
        # Join lines within sections with spaces, but keep section breaks
        result = []
        for p in parts:
            if not p:
                continue
            if p.endswith(":") and result:
                result.append("\n" + p)
            elif result and result[-1].endswith(":"):
                result.append(" " + p)
            else:
                result.append(" " + p if result else p)
        return "".join(result)

    def get_storm_reports(self, state_filter: str = "") -> str:
        """Get Local Storm Reports (LSR) - confirmed severe weather reports."""
        state_filter = state_filter.upper().strip()
        reports = []
        seen = set()
        for prod in sorted(self._products.values(), key=lambda p: p.timestamp, reverse=True):
            if prod.product_type != "LSR":
                continue
            if state_filter and prod.state != state_filter:
                # Also check affected state from text
                affected = self._affected_state(prod)
                if affected != state_filter:
                    continue
            for entry in self._parse_lsr_entries(prod.raw_text):
                key = f"{entry['time']}_{entry['event']}_{entry['location']}"
                if key in seen:
                    continue
                seen.add(key)
                reports.append(entry)

        if not reports:
            label = state_filter if state_filter else "nationwide"
            return f"No storm reports {label}."
        label = state_filter if state_filter else "nationwide"
        lines = [f"Storm reports {label} ({len(reports)}):"]
        for r in reports:
            # Shorten event names for LoRa
            event = (r['event']
                .replace("Non-Tstm Wnd Gst", "Wind")
                .replace("Tstm Wnd Gst", "T-Wind")
                .replace("Tstm Wnd Dmg", "T-Wind Dmg")
                .replace("Funnel Cloud", "Funnel")
                .replace("Flash Flood", "FlashFld")
                .replace("Heavy Rain", "HvyRain"))
            mag = f" {r['mag']}" if r['mag'] else ""
            st = f" {r['state']}" if r['state'] and not state_filter else ""
            lines.append(f" {event}{mag} {r['location']}{st}")
        return "\n".join(lines)

    @staticmethod
    def _parse_lsr_entries(text: str) -> list[dict]:
        """Parse individual storm report entries from an LSR product."""
        entries = []
        lines = text.splitlines()
        # LSR entries are in fixed-width format:
        # TIME     EVENT            CITY LOCATION          LAT.LON
        # DATE     MAG              COUNTY LOCATION  ST    SOURCE
        i = 0
        while i < len(lines) - 1:
            line1 = lines[i]
            # Look for report lines: start with time like "0249 PM" or "1044 AM"
            m = re.match(r"^(\d{4}\s+[AP]M)\s+(\S.*\S)\s{2,}(.*?)\s+\d{2,3}\.\d{2}[NS]", line1)
            if m:
                time_str = m.group(1).strip()
                event = m.group(2).strip()
                location = m.group(3).strip()
                # Next non-blank line has magnitude, county, state
                mag = ""
                state = ""
                for j in range(i + 1, min(i + 3, len(lines))):
                    line2 = lines[j].strip()
                    if not line2:
                        continue
                    # e.g. "04/08/2026  M55 mph          Butte              SD   Public"
                    m2 = re.match(r"\d{2}/\d{2}/\d{4}\s+(\S+.*?)\s{2,}(\S.*?)\s+([A-Z]{2})\s", line2)
                    if m2:
                        mag = m2.group(1).strip()
                        state = m2.group(3)
                    break
                entries.append({
                    "time": time_str,
                    "event": event,
                    "location": location,
                    "mag": mag,
                    "state": state,
                })
            i += 1
        return entries

    # -- Product-specific parsers (following Vitality GOES patterns) --

    def _parse_rwr_city(self, text: str, city: str) -> str:
        """Extract city conditions from RWR fixed-width table."""
        if not city:
            return ""
        in_table = False
        for line in text.splitlines():
            stripped = line.strip()
            if "SKY/WX" in stripped and "TMP" in stripped:
                in_table = True
                continue
            if not in_table or not stripped:
                continue
            if stripped.startswith("$$"):
                in_table = False
                continue
            # Check if city name appears at start of line
            if not stripped.upper().startswith(city):
                continue
            # Parse: everything after the city name
            after_city = stripped[len(city):].strip()
            return self._format_rwr_conditions(after_city)
        return ""

    def _format_rwr_conditions(self, raw: str) -> str:
        """Format raw RWR condition columns into readable text."""
        parts = raw.split()
        if len(parts) < 4:
            return raw[:100]

        # Merge sky/wx words until we hit a number (temperature)
        sky_parts = []
        temp_idx = 0
        for i, p in enumerate(parts):
            if p.lstrip("-").isdigit():
                temp_idx = i
                break
            sky_parts.append(p)

        sky_map = {
            "SUNNY": "Sunny", "MOSUNNY": "Mostly Sunny", "PTSUNNY": "Partly Sunny",
            "CLEAR": "Clear", "MOCLDY": "Mostly Cloudy", "PTCLDY": "Partly Cloudy",
            "CLOUDY": "Cloudy", "FAIR": "Fair", "HAZE": "Haze", "FOG": "Fog",
        }
        sky_raw = " ".join(sky_parts).upper()
        sky = sky_map.get(sky_raw, " ".join(sky_parts).title())

        # Handle multi-word conditions like "LGT RAIN"
        for phrase, label in [("LGT RAIN", "Lt Rain"), ("HVY RAIN", "Hvy Rain"),
                               ("RAIN", "Rain"), ("SNOW", "Snow"), ("TSTORM", "T-Storm")]:
            if phrase in sky_raw:
                sky = label
                break

        try:
            temp_f = parts[temp_idx]
            pieces = [sky, f"{temp_f}F"]
            # Wind is usually after temp, dewpoint, humidity
            for p in parts[temp_idx + 1:]:
                if re.match(r"^[NESWVRB]+\d+", p) or p == "CALM":
                    pieces.append(f"Wind {p}")
                    break
            return " | ".join(pieces)
        except (IndexError, ValueError):
            return raw[:100]

    def _find_metar(self, station: str) -> tuple[str, datetime] | None:
        """Find and decode the newest METAR for a specific station."""
        if not station:
            return None
        best: tuple[str, datetime] | None = None
        for prod in sorted(self._products.values(), key=lambda p: p.timestamp, reverse=True):
            if prod.product_type != "SAH" and "METAR" not in prod.raw_text[:200]:
                continue
            for line in prod.raw_text.splitlines():
                stripped = line.strip()
                if stripped.startswith(station) and re.match(r"^[A-Z]{4}\s+\d{6}Z", stripped):
                    if best is None or prod.timestamp > best[1]:
                        best = (self._decode_metar(stripped), prod.timestamp)
                    break  # found in this product, check next product
        return best

    def _decode_metar(self, metar: str) -> str:
        """Decode METAR into compact human-readable string."""
        parts = metar.split()
        if len(parts) < 4:
            return metar[:120]
        pieces = [parts[0]]  # Station ID
        for part in parts[1:]:
            m = re.match(r"(\d{3})(\d{2,3})(G(\d{2,3}))?KT", part)
            if m:
                wind = f"Wind {m.group(1)}@{m.group(2)}kt"
                if m.group(4):
                    wind += f"g{m.group(4)}"
                pieces.append(wind)
                continue
            if part.endswith("SM"):
                pieces.append(f"Vis {part[:-2]}mi")
                continue
            m = re.match(r"^(M?\d{2})/(M?\d{2})$", part)
            if m:
                t = m.group(1).replace("M", "-")
                d = m.group(2).replace("M", "-")
                pieces.append(f"{t}/{d}C")
                continue
            for code, desc in [("CLR", "Clear"), ("FEW", "Few"), ("SCT", "Sct"),
                                ("BKN", "Bkn"), ("OVC", "Ovc")]:
                if part.startswith(code):
                    alt = part[len(code):]
                    pieces.append(f"{desc}{int(alt)*100}ft" if alt else desc)
                    break
        return " | ".join(pieces)

    def _parse_zfp_zone(self, text: str, zone: str) -> str:
        """Extract forecast for a specific zone from a ZFP product."""
        lines = text.splitlines()
        in_zone = False
        forecast_parts = []

        for line in lines:
            stripped = line.strip()
            if not in_zone:
                # Look for zone code at start of line (e.g. "TXZ192-TXZ193-")
                if zone in stripped and re.match(r"^[A-Z]{2}Z\d{3}", stripped):
                    in_zone = True
                continue

            # End of zone section
            if stripped.startswith("$$") or stripped.startswith("&&"):
                break

            # Forecast periods start with "."
            if stripped.startswith("."):
                period = stripped.lstrip(".").strip()
                forecast_parts.append(period)
                continue

            # Continuation of current period
            if forecast_parts and stripped:
                forecast_parts[-1] += " " + stripped

        if not forecast_parts:
            return ""

        # Return first period (e.g. "TODAY...Sunny with a high of 72.")
        return forecast_parts[0]

    def _parse_afd_summary(self, text: str) -> str:
        """Extract summary from Area Forecast Discussion."""
        lines = text.splitlines()
        # Prefer .KEY MESSAGES first, then .SYNOPSIS, .DISCUSSION, etc.
        markers = [".KEY MESSAGES", ".SYNOPSIS", ".DISCUSSION", ".NEAR TERM", ".SHORT TERM"]
        for marker in markers:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if marker not in stripped.upper():
                    continue
                # KEY MESSAGES: read entire section until && or next marker
                # Others: use blank-line threshold to skip sub-headers
                use_threshold = marker not in (".KEY MESSAGES",)
                para = []
                for j in range(i + 1, min(i + 50, len(lines))):
                    s = lines[j].strip()
                    if s.startswith("$$") or s.startswith("&&"):
                        break
                    if s.startswith(".") and len(s) > 3:
                        break  # Next section
                    if not s:
                        if use_threshold and para and len(" ".join(para)) > 80:
                            break
                        continue
                    # Skip boilerplate lines
                    if s.startswith("Issued at ") or s.startswith("Updated "):
                        continue
                    if re.match(r"^\(.*\)\s*$", s):
                        continue
                    if re.match(r"^(Tonight|Today|This|Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b", s) and len(s) < 50:
                        continue
                    para.append(s)
                if para:
                    return " ".join(para)
        return ""

    @staticmethod
    def _affected_state(prod: "EMWINProduct") -> str:
        """Get the state actually affected by a warning product.

        NWS offices near state borders issue warnings for neighboring
        states.  The zone code line (e.g. ``OKZ003-``) is authoritative;
        fall back to the issuing office state from the filename.
        """
        for line in prod.raw_text.splitlines()[:25]:
            s = line.strip()
            m = re.match(r"^([A-Z]{2})Z\d{3}", s)
            if m:
                return m.group(1)
            # Also check FIPS county codes like "FLC099-"
            m = re.match(r"^([A-Z]{2})C\d{3}", s)
            if m:
                return m.group(1)
        return prod.state

    @staticmethod
    def _is_cancelled(text: str) -> bool:
        """Check if a warning product has been cancelled or expired via VTEC."""
        for line in text.splitlines()[:30]:
            s = line.strip()
            if s.startswith("/O."):
                # VTEC action: CAN=cancelled, EXP=expired, UPG=upgraded
                m = re.match(r"/O\.(\w+)\.", s)
                if m and m.group(1) in ("CAN", "EXP", "UPG"):
                    return True
            upper = s.upper()
            if "IS CANCELLED" in upper or "WILL EXPIRE AT" in upper:
                return True
            if "WILL EXPIRE" in upper or "HAS WEAKENED" in upper:
                return True
        return False

    def _extract_warning_headline(self, text: str) -> str:
        """Extract a concise headline from a warning product for LoRa display.

        EMWIN products insert blank lines between every content line, so
        a headline like ``...WINTER STORM WATCH IN EFFECT...`` may span
        multiple non-blank lines separated by blanks.
        """
        lines = text.splitlines()

        # 1. Best source: ...headline... blocks (NWS standard).
        #    Collect text between the opening "..." and the closing "...".
        collecting = False
        parts: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                if collecting:
                    continue  # skip blank lines inside headline
                continue

            if not collecting:
                if stripped.startswith("...") and len(stripped) > 6:
                    # Skip office / boilerplate lines
                    if any(stripped.strip(".").strip().startswith(p)
                           for p in ("National Weather", "NWS ")):
                        continue
                    collecting = True
                    parts.append(stripped.lstrip(".").strip().rstrip("."))
                    if stripped.endswith("...") and len(stripped) > 8:
                        # Entire headline on one line
                        return " ".join(parts)[:120]
                continue

            # Still collecting: append until we hit the closing "..."
            chunk = stripped.rstrip(".").strip()
            if chunk:
                parts.append(chunk)
            if stripped.endswith("..."):
                return " ".join(parts)[:120]
            # Safety: don't collect forever
            if len(parts) > 5:
                return " ".join(parts)[:120]

        if parts:
            return " ".join(parts)[:120]

        # 2. SWO/MCD products: "Areas affected...X" + "Concerning...Y"
        for i, line in enumerate(lines):
            if "Areas affected" in line:
                area = line.split("...", 1)[-1].strip().strip(".")
                for j in range(i + 1, min(i + 8, len(lines))):
                    if "Concerning" in lines[j]:
                        concern = lines[j].split("...", 1)[-1].strip().strip(".")
                        return f"{area} - {concern}"[:120]
                if area:
                    return area[:120]

        # 3. Product title line (e.g. "URGENT - WINTER WEATHER MESSAGE")
        for line in lines[:10]:
            stripped = line.strip()
            if any(kw in stripped for kw in
                   ("URGENT", "WARNING", "WATCH", "ADVISORY", "STATEMENT", "FLOOD")):
                if len(stripped) > 10 and not stripped.startswith("WW"):
                    return stripped[:120]

        return "Active warning"

    def _in_warning_polygon(self, text: str, lat: float, lon: float) -> bool:
        """Check if lat/lon is inside a warning's LAT...LON polygon."""
        coords = extract_warning_polygon(text)
        if len(coords) < 3:
            return True  # No polygon data = don't filter it out
        return self._point_in_polygon(lat, lon, coords)

    @staticmethod
    def _point_in_polygon(lat: float, lon: float, polygon: list[tuple]) -> bool:
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            yi, xi = polygon[i]
            yj, xj = polygon[j]
            if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside
