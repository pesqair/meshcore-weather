"""Convert the bot's parsed weather data into MeshWX v2 binary messages.

Each function takes the existing Python dicts/strings that the bot's text
commands already produce and turns them into compact binary for broadcast.
"""

import logging
import re
from datetime import datetime, timezone

from meshcore_weather.protocol.meshwx import (
    LOC_ZONE, LOC_STATION,
    SKY_CLEAR, SKY_FEW, SKY_SCATTERED, SKY_BROKEN, SKY_OVERCAST,
    SKY_FOG, SKY_SMOKE, SKY_HAZE, SKY_RAIN, SKY_SNOW,
    SKY_THUNDERSTORM, SKY_DRIZZLE, SKY_MIST, SKY_OTHER,
    HAZARD_THUNDERSTORM, HAZARD_SEVERE_THUNDER, HAZARD_TORNADO,
    HAZARD_FLOOD, HAZARD_FLASH_FLOOD, HAZARD_EXCESSIVE_HEAT,
    HAZARD_WINTER_STORM, HAZARD_BLIZZARD, HAZARD_ICE,
    HAZARD_HIGH_WIND, HAZARD_FIRE_WEATHER, HAZARD_DENSE_FOG,
    HAZARD_RIP_CURRENT, HAZARD_HURRICANE, HAZARD_MARINE, HAZARD_OTHER,
    RISK_SLIGHT, RISK_LIMITED, RISK_ENHANCED, RISK_MODERATE, RISK_HIGH,
    EVENT_TORNADO, EVENT_FUNNEL, EVENT_HAIL, EVENT_WIND_DAMAGE,
    EVENT_NON_TSTM_WIND, EVENT_TSTM_WIND, EVENT_FLOOD, EVENT_FLASH_FLOOD,
    EVENT_HEAVY_RAIN, EVENT_SNOW, EVENT_ICE, EVENT_LIGHTNING, EVENT_OTHER,
    RAIN_LIGHT, RAIN_MODERATE, RAIN_HEAVY, RAIN_SHOWER, RAIN_TSTORM,
    RAIN_DRIZZLE, RAIN_SNOW, RAIN_FREEZING,
    pack_observation,
    pack_forecast,
    pack_outlook,
    pack_storm_reports,
    pack_rain_obs,
    pack_warning_zones,
    wind_dir_to_nibble,
)

logger = logging.getLogger(__name__)


# -- Sky condition mapping --

# Free-text weather words → sky code. Longer/more specific matches first.
_SKY_KEYWORDS = [
    ("thunderstorm", SKY_THUNDERSTORM),
    ("tstorm", SKY_THUNDERSTORM),
    ("tstm", SKY_THUNDERSTORM),
    ("snow", SKY_SNOW),
    ("drizzle", SKY_DRIZZLE),
    ("rain", SKY_RAIN),
    ("shower", SKY_RAIN),
    ("mist", SKY_MIST),
    ("fog", SKY_FOG),
    ("haze", SKY_HAZE),
    ("smoke", SKY_SMOKE),
    ("overcast", SKY_OVERCAST),
    ("ovc", SKY_OVERCAST),
    ("cloudy", SKY_OVERCAST),
    ("mocldy", SKY_OVERCAST),
    ("broken", SKY_BROKEN),
    ("bkn", SKY_BROKEN),
    ("ptcldy", SKY_BROKEN),
    ("scattered", SKY_SCATTERED),
    ("sct", SKY_SCATTERED),
    ("few", SKY_FEW),
    ("ptsunny", SKY_FEW),
    ("mosunny", SKY_FEW),
    ("sunny", SKY_CLEAR),
    ("clear", SKY_CLEAR),
    ("clr", SKY_CLEAR),
    ("fair", SKY_CLEAR),
]


def classify_sky(text: str) -> int:
    """Infer a sky code from free-form weather text."""
    if not text:
        return SKY_OTHER
    t = text.lower()
    for keyword, code in _SKY_KEYWORDS:
        if keyword in t:
            return code
    return SKY_OTHER


# -- METAR observation encoding --

def encode_metar(station_icao: str, metar_text: str, ts_minutes_utc: int) -> bytes | None:
    """Build a 0x30 observation from a raw METAR string.

    Example METAR: "KAUS 082151Z 17010KT 10SM SCT040 BKN070 28/18 A3010"
    """
    try:
        parts = metar_text.split()
    except AttributeError:
        return None

    temp_f = None
    dewpoint_f = None
    wind_dir_deg = None
    wind_speed_mph = 0
    wind_gust_mph = 0
    visibility_mi = 10
    sky_code = SKY_CLEAR
    pressure_inhg = 29.92

    for p in parts:
        # Wind: dddffKT or dddffGggKT (dir in degrees, speed in knots)
        m_wind = re.match(r"^(\d{3})(\d{2,3})(?:G(\d{2,3}))?KT$", p)
        if m_wind:
            wind_dir_deg = int(m_wind.group(1))
            wind_speed_mph = round(int(m_wind.group(2)) * 1.15078)
            if m_wind.group(3):
                wind_gust_mph = round(int(m_wind.group(3)) * 1.15078)
            continue
        # Variable wind: VRBxxKT
        m_vrb = re.match(r"^VRB(\d{2,3})KT$", p)
        if m_vrb:
            wind_dir_deg = 0
            wind_speed_mph = round(int(m_vrb.group(1)) * 1.15078)
            continue
        # Visibility: NNSM or N/NSM
        m_vis = re.match(r"^(\d{1,2})(?:/(\d))?SM$", p)
        if m_vis:
            visibility_mi = int(m_vis.group(1))
            continue
        # Temp/dewpoint: TT/DD or MTT/DD (M = negative)
        m_td = re.match(r"^(M?\d{2})/(M?\d{2})$", p)
        if m_td:
            t_c = int(m_td.group(1).replace("M", "-"))
            d_c = int(m_td.group(2).replace("M", "-"))
            temp_f = round(t_c * 9 / 5 + 32)
            dewpoint_f = round(d_c * 9 / 5 + 32)
            continue
        # Altimeter: AXXXX (inches of Hg * 100)
        m_alt = re.match(r"^A(\d{4})$", p)
        if m_alt:
            pressure_inhg = int(m_alt.group(1)) / 100
            continue
        # Sky condition
        for code, sky in [("CLR", SKY_CLEAR), ("SKC", SKY_CLEAR),
                          ("FEW", SKY_FEW), ("SCT", SKY_SCATTERED),
                          ("BKN", SKY_BROKEN), ("OVC", SKY_OVERCAST)]:
            if p.startswith(code):
                if sky > sky_code:  # pick worst conditions for summary
                    sky_code = sky
                break
        # Precipitation codes (RA=rain, SN=snow, TS=thunder, etc.)
        if "TS" in p:
            sky_code = SKY_THUNDERSTORM
        elif "RA" in p and sky_code < SKY_RAIN:
            sky_code = SKY_RAIN
        elif "SN" in p and sky_code < SKY_SNOW:
            sky_code = SKY_SNOW
        elif "FG" in p and sky_code < SKY_FOG:
            sky_code = SKY_FOG

    if temp_f is None:
        return None

    return pack_observation(
        LOC_STATION, station_icao,
        timestamp_utc_min=ts_minutes_utc,
        temp_f=temp_f,
        dewpoint_f=dewpoint_f if dewpoint_f is not None else temp_f,
        wind_dir_deg=wind_dir_deg if wind_dir_deg is not None else 0,
        sky_code=sky_code,
        wind_speed_mph=wind_speed_mph,
        wind_gust_mph=wind_gust_mph,
        visibility_mi=visibility_mi,
        pressure_inhg=pressure_inhg,
    )


# -- RWR (Regional Weather Roundup) observation encoding --

# Column positions in a typical RWR line after the city:
# SKY/WX  TMP DP RH  WIND    PRES  RMK
# "SUNNY  85  55 40  S10     30.05"
_RWR_SPLIT_RE = re.compile(r"\s+")


def encode_rwr_city(
    zone_code: str,
    rwr_line: str,
    ts_minutes_utc: int,
) -> bytes | None:
    """Build a 0x30 observation from an RWR line for a specific city.

    The caller is responsible for finding the correct line — we parse it.
    """
    if not rwr_line:
        return None
    parts = _RWR_SPLIT_RE.split(rwr_line.strip())
    if len(parts) < 4:
        return None

    # Find first numeric (temp). Everything before it is the sky/wx description.
    temp_idx = None
    for i, p in enumerate(parts):
        if p.lstrip("-").isdigit():
            temp_idx = i
            break
    if temp_idx is None or temp_idx + 1 >= len(parts):
        return None

    try:
        temp_f = int(parts[temp_idx])
        dewpoint_f = int(parts[temp_idx + 1])
    except (ValueError, IndexError):
        return None

    sky_text = " ".join(parts[:temp_idx])
    sky_code = classify_sky(sky_text)

    # Wind: find something like S10 or SE15G25 or CALM
    wind_dir_deg = 0
    wind_speed_mph = 0
    wind_gust_mph = 0
    for p in parts[temp_idx + 2:]:
        if p.upper() == "CALM":
            break
        m = re.match(r"^([NESW]{1,3})(\d+)(?:G(\d+))?$", p.upper())
        if m:
            dir_map = {"N": 0, "NNE": 22.5, "NE": 45, "ENE": 67.5,
                       "E": 90, "ESE": 112.5, "SE": 135, "SSE": 157.5,
                       "S": 180, "SSW": 202.5, "SW": 225, "WSW": 247.5,
                       "W": 270, "WNW": 292.5, "NW": 315, "NNW": 337.5}
            wind_dir_deg = int(dir_map.get(m.group(1), 0))
            wind_speed_mph = int(m.group(2))
            if m.group(3):
                wind_gust_mph = int(m.group(3))
            break

    return pack_observation(
        LOC_ZONE, zone_code,
        timestamp_utc_min=ts_minutes_utc,
        temp_f=temp_f,
        dewpoint_f=dewpoint_f,
        wind_dir_deg=wind_dir_deg,
        sky_code=sky_code,
        wind_speed_mph=wind_speed_mph,
        wind_gust_mph=wind_gust_mph,
    )


# -- Forecast period encoding --

# Period name → period_id mapping
_PERIOD_IDS = {
    "TONIGHT": 0,
    "TODAY": 1,
    "THIS AFTERNOON": 14,
    "THIS EVENING": 15,
    "LATE TONIGHT": 16,
    "TOMORROW": 2,
    "TOMORROW NIGHT": 3,
    "MONDAY": 4,
    "MONDAY NIGHT": 5,
    "TUESDAY": 6,
    "TUESDAY NIGHT": 7,
    "WEDNESDAY": 8,
    "WEDNESDAY NIGHT": 9,
    "THURSDAY": 10,
    "THURSDAY NIGHT": 11,
    "FRIDAY": 12,
    "FRIDAY NIGHT": 13,
    "SATURDAY": 4,
    "SATURDAY NIGHT": 5,
    "SUNDAY": 6,
    "SUNDAY NIGHT": 7,
}


def _extract_temp(text: str, pattern: str) -> int | None:
    """Extract a temperature from text like 'HIGHS AROUND 85' or 'LOWS IN THE UPPER 60S'."""
    m = re.search(pattern + r".*?(\d{2,3})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return None


def _extract_wind(text: str) -> tuple[int, int]:
    """Return (wind_dir_nibble, wind_speed_5mph_units) from period text."""
    # Example: "Southeast winds 5 to 10 mph" or "WEST WINDS 10 TO 15 MPH"
    m = re.search(
        r"(north|south|east|west|northeast|northwest|southeast|southwest)\s+winds?\s+(\d+)(?:\s+to\s+(\d+))?\s+mph",
        text,
        re.IGNORECASE,
    )
    if not m:
        return 0, 0
    dir_map = {
        "n": 0, "north": 0, "ne": 2, "northeast": 2,
        "e": 4, "east": 4, "se": 6, "southeast": 6,
        "s": 8, "south": 8, "sw": 10, "southwest": 10,
        "w": 12, "west": 12, "nw": 14, "northwest": 14,
    }
    direction = m.group(1).lower()
    speed = int(m.group(3) or m.group(2))  # use upper bound if range
    return (
        dir_map.get(direction, 0),
        min(15, speed // 5),
    )


def _extract_precip(text: str) -> int:
    """Extract precipitation chance from text like '40 percent chance of...'"""
    m = re.search(r"(\d{1,3})\s*percent", text, re.IGNORECASE)
    if m:
        return min(100, int(m.group(1)))
    # Fall back: check for precip keywords
    t = text.lower()
    if "slight chance" in t:
        return 15
    if "chance" in t:
        return 40
    if "likely" in t:
        return 70
    if "occasional" in t or "scattered showers" in t:
        return 50
    return 0


def encode_forecast_from_zfp(
    zone_code: str,
    zfp_text: str,
    issued_hours_ago: int,
) -> bytes | None:
    """Parse a ZFP zone forecast and encode it as a 0x31 forecast message.

    ZFP format has periods like:
        .TONIGHT...Mostly clear. Lows around 60. Southeast winds around 5 mph.
        .THURSDAY...Sunny. Highs in the upper 80s. ...
    """
    if not zfp_text:
        return None

    periods = []
    current_name = None
    current_text: list[str] = []

    for line in zfp_text.splitlines():
        s = line.strip()
        if not s:
            if current_name and current_text:
                periods.append((current_name, " ".join(current_text)))
                current_name = None
                current_text = []
            continue
        if s.startswith(".") and "..." in s:
            # Flush previous
            if current_name and current_text:
                periods.append((current_name, " ".join(current_text)))
            # Extract period name between first . and first ...
            head, _, rest = s.lstrip(".").partition("...")
            current_name = head.strip().upper()
            current_text = [rest.strip()] if rest.strip() else []
        elif current_name:
            current_text.append(s)

    if current_name and current_text:
        periods.append((current_name, " ".join(current_text)))

    if not periods:
        return None

    encoded_periods = []
    for name, text in periods[:7]:  # up to 7 periods
        period_id = _PERIOD_IDS.get(name, 0)
        sky_code = classify_sky(text)

        is_night = "NIGHT" in name or name == "TONIGHT"
        if is_night:
            high_f = 127  # N/A
            low_f = _extract_temp(text, r"lows?")
        else:
            high_f = _extract_temp(text, r"highs?")
            low_f = _extract_temp(text, r"lows?")

        wind_dir, wind_speed = _extract_wind(text)
        precip = _extract_precip(text)

        flags = 0
        t_lower = text.lower()
        if "thunder" in t_lower:
            flags |= 0x01
        if "frost" in t_lower:
            flags |= 0x02
        if "fog" in t_lower:
            flags |= 0x04
        if "high wind" in t_lower:
            flags |= 0x08
        if "freezing rain" in t_lower or "sleet" in t_lower:
            flags |= 0x10
        if "heavy rain" in t_lower:
            flags |= 0x20
        if "heavy snow" in t_lower:
            flags |= 0x40

        encoded_periods.append({
            "period_id": period_id,
            "high_f": high_f if high_f is not None else 127,
            "low_f": low_f if low_f is not None else 127,
            "sky_code": sky_code,
            "precip_pct": precip,
            "wind_dir_nibble": wind_dir,
            "wind_speed_5mph": wind_speed,
            "condition_flags": flags,
        })

    return pack_forecast(
        LOC_ZONE, zone_code,
        issued_hours_ago=issued_hours_ago,
        periods=encoded_periods,
    )


def now_utc_minutes() -> int:
    """Minutes since midnight UTC (uint16)."""
    now = datetime.now(timezone.utc)
    return now.hour * 60 + now.minute


# -- Place lookup (uses resolver.places for nearest match) --

def find_nearest_place_id(lat: float, lon: float, max_km: float = 50) -> int | None:
    """Find the index into resolver._places closest to a lat/lon point.

    Returns None if no place within max_km.
    """
    from meshcore_weather.geodata import resolver
    resolver.load()
    import math

    best_d = float("inf")
    best_idx = None
    for i, p in enumerate(resolver._places):
        dlat = lat - p[2]
        dlon = lon - p[3]
        # Quick squared distance (fine for nearest within small area)
        d2 = dlat * dlat + dlon * dlon
        if d2 < best_d:
            best_d = d2
            best_idx = i

    if best_idx is None:
        return None
    # Convert squared to rough km (1 degree ≈ 111 km)
    approx_km = math.sqrt(best_d) * 111
    if approx_km > max_km:
        return None
    return best_idx


def find_place_id_by_name(name: str, state: str = "") -> int | None:
    """Find the index of a place by name + optional state."""
    from meshcore_weather.geodata import resolver
    resolver.load()
    name_upper = name.upper().strip()
    state_upper = state.upper().strip() if state else ""
    for i, p in enumerate(resolver._places):
        if p[0].upper() == name_upper:
            if not state_upper or p[1] == state_upper:
                return i
    return None


# -- HWO (Hazardous Weather Outlook) encoding --

# Map NWS HWO hazard text → hazard code
_HAZARD_KEYWORDS = [
    ("tornado", HAZARD_TORNADO),
    ("severe thunderstorm", HAZARD_SEVERE_THUNDER),
    ("severe thunder", HAZARD_SEVERE_THUNDER),
    ("severe storm", HAZARD_SEVERE_THUNDER),
    ("severe weather", HAZARD_SEVERE_THUNDER),
    ("flash flood", HAZARD_FLASH_FLOOD),
    ("flood", HAZARD_FLOOD),
    ("excessive heat", HAZARD_EXCESSIVE_HEAT),
    ("heat", HAZARD_EXCESSIVE_HEAT),
    ("blizzard", HAZARD_BLIZZARD),
    ("winter storm", HAZARD_WINTER_STORM),
    ("winter weather", HAZARD_WINTER_STORM),
    ("snow", HAZARD_WINTER_STORM),
    ("ice", HAZARD_ICE),
    ("freezing rain", HAZARD_ICE),
    ("high wind", HAZARD_HIGH_WIND),
    ("wind", HAZARD_HIGH_WIND),
    ("fire weather", HAZARD_FIRE_WEATHER),
    ("fire", HAZARD_FIRE_WEATHER),
    ("dense fog", HAZARD_DENSE_FOG),
    ("fog", HAZARD_DENSE_FOG),
    ("rip current", HAZARD_RIP_CURRENT),
    ("hurricane", HAZARD_HURRICANE),
    ("tropical", HAZARD_HURRICANE),
    ("marine", HAZARD_MARINE),
    ("thunderstorm", HAZARD_THUNDERSTORM),
]

# Risk level text → code
_RISK_KEYWORDS = {
    "extreme": RISK_HIGH,
    "high": RISK_HIGH,
    "moderate": RISK_MODERATE,
    "enhanced": RISK_ENHANCED,
    "elevated": RISK_ENHANCED,
    "limited": RISK_LIMITED,
    "slight": RISK_SLIGHT,
    "low": RISK_SLIGHT,
}


def _classify_hazards(text: str) -> list[tuple[int, int]]:
    """Extract (hazard_code, risk_level) tuples from HWO section text."""
    t = text.lower()
    found = {}

    # Look for explicit RISK... patterns per section
    risk_matches = re.finditer(r"risk[.\s]+(\w+)", t, re.IGNORECASE)
    risks_in_text = []
    for m in risk_matches:
        word = m.group(1).lower()
        if word in _RISK_KEYWORDS:
            risks_in_text.append(_RISK_KEYWORDS[word])

    # Default risk from first mention, fall back to slight if hazard present
    default_risk = risks_in_text[0] if risks_in_text else RISK_SLIGHT

    # Find hazard mentions
    for keyword, code in _HAZARD_KEYWORDS:
        if keyword in t and code not in found:
            found[code] = default_risk

    return list(found.items())


def encode_hwo(
    zone_code: str,
    hwo_text: str,
    issued_utc_min: int,
) -> bytes | None:
    """Parse an HWO product text and encode as 0x32 outlook."""
    if not hwo_text:
        return None

    # Split into .DAY ONE / .DAYS TWO THROUGH SEVEN sections
    lines = hwo_text.splitlines()
    sections: dict[int, str] = {}  # day_offset -> text
    current_day = None
    current_text: list[str] = []

    for line in lines:
        s = line.strip()
        up = s.upper()
        if up.startswith(".DAY ONE") or up.startswith(".DAY 1"):
            if current_day is not None:
                sections[current_day] = " ".join(current_text)
            current_day = 1
            current_text = []
            continue
        if "DAYS TWO THROUGH SEVEN" in up or "DAYS 2" in up:
            if current_day is not None:
                sections[current_day] = " ".join(current_text)
            current_day = 2  # represents day range 2-7
            current_text = []
            continue
        if up.startswith(".SPOTTER") or s.startswith("$$"):
            break
        if current_day is not None and s:
            if not (s.startswith("&&") or re.match(r"^[A-Z]{2}Z\d{3}", s)):
                current_text.append(s)

    if current_day is not None and current_text:
        sections[current_day] = " ".join(current_text)

    if not sections:
        return None

    days = []
    for day_offset, text in sorted(sections.items()):
        hazards = _classify_hazards(text)
        if day_offset == 1:
            days.append({"day_offset": 1, "hazards": hazards})
        else:
            # "Days 2-7" gets replicated as day 2 with the same hazards
            # (rough but practical; clients can display as "Days 2-7")
            days.append({"day_offset": 2, "hazards": hazards})

    if not days:
        return None

    return pack_outlook(LOC_ZONE, zone_code, issued_utc_min, days)


# -- LSR (Local Storm Reports) encoding --

_LSR_EVENT_MAP = {
    "tornado": EVENT_TORNADO,
    "funnel cloud": EVENT_FUNNEL,
    "funnel": EVENT_FUNNEL,
    "hail": EVENT_HAIL,
    "tstm wnd dmg": EVENT_WIND_DAMAGE,
    "thunderstorm wind damage": EVENT_WIND_DAMAGE,
    "tstm wnd gst": EVENT_TSTM_WIND,
    "thunderstorm wind": EVENT_TSTM_WIND,
    "non-tstm wnd gst": EVENT_NON_TSTM_WIND,
    "high wind": EVENT_NON_TSTM_WIND,
    "flash flood": EVENT_FLASH_FLOOD,
    "flood": EVENT_FLOOD,
    "heavy rain": EVENT_HEAVY_RAIN,
    "snow": EVENT_SNOW,
    "ice storm": EVENT_ICE,
    "sleet": EVENT_ICE,
    "lightning": EVENT_LIGHTNING,
}


def _classify_lsr_event(event_text: str) -> int:
    """Map an LSR event string to an event code."""
    t = event_text.lower().strip()
    for keyword, code in _LSR_EVENT_MAP.items():
        if keyword in t:
            return code
    return EVENT_OTHER


def _parse_lsr_magnitude(mag_str: str, event_type: int) -> int:
    """Parse an LSR magnitude string into a single byte.

    Hail: size in inches * 4 (so 1.5 inch → 6)
    Wind: mph directly
    Rain: 0.01 in units (so 2.5" → 250, clamped)
    """
    if not mag_str:
        return 0
    m = re.search(r"(\d+(?:\.\d+)?)", mag_str)
    if not m:
        return 0
    value = float(m.group(1))
    if event_type == EVENT_HAIL:
        return min(255, int(round(value * 4)))
    if event_type in (EVENT_TSTM_WIND, EVENT_NON_TSTM_WIND, EVENT_WIND_DAMAGE):
        return min(255, int(round(value)))
    if event_type in (EVENT_HEAVY_RAIN, EVENT_FLASH_FLOOD, EVENT_FLOOD):
        return min(255, int(round(value * 100 / 10)))  # coarse inches
    return min(255, int(round(value)))


def encode_lsr_reports(
    zone_code: str,
    lsr_entries: list[dict],
    now_min: int,
) -> bytes | None:
    """Convert a list of LSR entries (from _parse_lsr_entries) to a 0x33 message.

    Entries come from weather.py's _parse_lsr_entries and have:
      time, event, location (text), mag, state
    """
    if not lsr_entries:
        return None

    from meshcore_weather.geodata import resolver
    resolver.load()

    reports = []
    for entry in lsr_entries[:16]:  # cap for size
        event_type = _classify_lsr_event(entry.get("event", ""))
        magnitude = _parse_lsr_magnitude(entry.get("mag", ""), event_type)

        # Extract a place name from the location text.
        # Location text looks like "5 S Hoover" or "8 NNE Austin"
        loc_text = entry.get("location", "")
        name_match = re.search(r"([A-Z][A-Za-z][A-Za-z\s]+?)$", loc_text)
        place_id = 0
        if name_match:
            place_name = name_match.group(1).strip()
            state = entry.get("state", "")
            pid = find_place_id_by_name(place_name, state)
            if pid is not None:
                place_id = pid

        # Parse event time like "1249 PM" into minutes ago (rough)
        time_str = entry.get("time", "")
        minutes_ago = 0
        m_time = re.match(r"(\d{4})\s+([AP]M)", time_str)
        if m_time:
            hhmm = int(m_time.group(1))
            hour = hhmm // 100
            minute = hhmm % 100
            if m_time.group(2) == "PM" and hour != 12:
                hour += 12
            elif m_time.group(2) == "AM" and hour == 12:
                hour = 0
            report_minutes = hour * 60 + minute
            diff = now_min - report_minutes
            minutes_ago = diff % 1440  # handle rollover

        reports.append({
            "event_type": event_type,
            "magnitude": magnitude,
            "minutes_ago": minutes_ago,
            "place_id": place_id,
        })

    if not reports:
        return None
    return pack_storm_reports(LOC_ZONE, zone_code, reports)


# -- RWR rain city list → 0x34 --

_RAIN_TYPE_MAP = {
    "lgt rain": RAIN_LIGHT,
    "light rain": RAIN_LIGHT,
    "rain": RAIN_MODERATE,
    "hvy rain": RAIN_HEAVY,
    "heavy rain": RAIN_HEAVY,
    "showers": RAIN_SHOWER,
    "shower": RAIN_SHOWER,
    "tstorm": RAIN_TSTORM,
    "t-storm": RAIN_TSTORM,
    "drizzle": RAIN_DRIZZLE,
    "snow": RAIN_SNOW,
    "fz rain": RAIN_FREEZING,
    "frz rain": RAIN_FREEZING,
}


def _classify_rain(text: str) -> int:
    t = text.lower()
    for keyword, code in _RAIN_TYPE_MAP.items():
        if keyword in t:
            return code
    return RAIN_LIGHT


def encode_rain_cities(
    region_zone: str,
    rainy_cities: list[dict],
    now_min: int,
) -> bytes | None:
    """Convert a list of rainy city observations to a 0x34 message.

    rainy_cities: list of {"name": str, "state": str, "rain_text": str, "temp_f": int}
    """
    if not rainy_cities:
        return None

    cities = []
    for c in rainy_cities[:20]:  # cap
        place_id = find_place_id_by_name(c["name"], c.get("state", ""))
        if place_id is None:
            continue
        cities.append({
            "place_id": place_id,
            "rain_type": _classify_rain(c.get("rain_text", "")),
            "temp_f": int(c.get("temp_f", 60)),
        })

    if not cities:
        return None
    return pack_rain_obs(LOC_ZONE, region_zone, now_min, cities)


# -- Warning → zone-coded (0x21) from NWS warning product --

def encode_warning_zones(
    warning_type: int,
    severity: int,
    expires_unix_min: int,
    zones: list[str],
    headline: str,
) -> bytes:
    """Thin wrapper around pack_warning_zones for symmetry with polygon encoder."""
    return pack_warning_zones(warning_type, severity, expires_unix_min, zones, headline)
