"""Parse NWS Point Forecast Matrix (PFM) products into structured forecasts.

PFM is a fixed-column text format issued by NWS WFOs for specific forecast
points (airports, cities). Each PFM product contains multiple forecast
points. Each point has:

  - A 3-hourly table for days 1-3 (Temp, Dewpt, RH, Wind, Clouds, PoP, etc.)
  - A 6-hourly table for days 4-7 (Max/Min, Temp, Wind char, etc.)

pyIEM does not have a PFM parser, so we write our own. The parser is
intentionally lightweight — it extracts only the fields we need to
populate the 0x31 Forecast wire format (high/low temp, sky code, PoP,
wind, condition flags). It does NOT aim to reproduce the full PFM data
model.

Column parsing strategy:
  - Find the "<TZ> 3hrly" header row for table 1 (or "6hrly" for table 2).
  - Record the character position of each time-column in that row.
  - For every data row below, extract the value occupying each column.
  - An empty column (spaces only) means "no data for that time slot".

Row parsing is tolerant of label variations ("Rain shwrs" vs "Rain") and
of optional rows (Wind gust, Heat index, Drizzle, etc.).

The UTC row is always present after the local-time row and is what we
use to compute absolute timestamps, so we never have to deal with local
timezones or DST.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone

logger = logging.getLogger(__name__)


# -- Label / row detection -----------------------------------------------------

# The label portion of a PFM row occupies ~the first 14 chars. Everything
# after that is column-aligned data. Some WFO rows vary in label length so
# we match a prefix loosely.
_LABEL_END = 14

# Rows we care about. Keys are canonical labels; values are regexes that
# match the *start* of the label (case-insensitive, anchored to column 0).
_ROW_PATTERNS = {
    "temp": re.compile(r"^\s*Temp\b", re.IGNORECASE),
    "dewpt": re.compile(r"^\s*Dewpt\b", re.IGNORECASE),
    "rh": re.compile(r"^\s*RH\b", re.IGNORECASE),
    "wind_dir": re.compile(r"^\s*Wind\s*dir\b", re.IGNORECASE),
    "wind_spd": re.compile(r"^\s*Wind\s*spd\b", re.IGNORECASE),
    "wind_gust": re.compile(r"^\s*Wind\s*gust\b", re.IGNORECASE),
    "wind_char": re.compile(r"^\s*Wind\s*char\b", re.IGNORECASE),  # 6hrly table
    "pwind_dir": re.compile(r"^\s*PWind\s*dir\b", re.IGNORECASE),  # 6hrly table
    "clouds": re.compile(r"^\s*(Avg\s+)?Clouds\b", re.IGNORECASE),
    "pop12": re.compile(r"^\s*PoP\s*12hr\b", re.IGNORECASE),
    "qpf12": re.compile(r"^\s*QPF\s*12hr\b", re.IGNORECASE),
    "snow12": re.compile(r"^\s*Snow\s*12hr\b", re.IGNORECASE),
    "rain": re.compile(r"^\s*Rain(\s+shwrs?)?\b", re.IGNORECASE),
    "tstm": re.compile(r"^\s*Tstms?\b", re.IGNORECASE),
    "obvis": re.compile(r"^\s*Obvis\b", re.IGNORECASE),
    "min_max": re.compile(r"^\s*(Min/Max|Max/Min)\b", re.IGNORECASE),
    "max_min": re.compile(r"^\s*(Min/Max|Max/Min)\b", re.IGNORECASE),  # 6hrly same row
}

# Detect the hour header rows (local TZ and UTC)
_HRLY_LOCAL_RE = re.compile(
    r"^(?P<label>\s*[A-Z]{3,4}\s+(?P<interval>3hrly|6hrly))\s+(?P<hours>.+)$",
    re.IGNORECASE,
)
_HRLY_UTC_RE = re.compile(
    r"^\s*UTC\s+(?P<interval>3hrly|6hrly)\s+(?P<hours>.+)$",
    re.IGNORECASE,
)

# Point header: UGC line, then a non-blank name, then a coord line.
_UGC_RE = re.compile(r"^([A-Z]{2}[ZC]\d{3})(?:[->]\d{3})*-\d{6}-\s*$")
_COORD_RE = re.compile(
    r"^\s*(?P<lat>\d+(?:\.\d+)?)N\s+(?P<lon>\d+(?:\.\d+)?)W"
    r"(?:\s+Elev\.?\s+(?P<elev>\d+)\s*ft)?"
)
# Product issue time: e.g. "1245 PM CDT Fri Apr 10 2026"
_ISSUE_RE = re.compile(
    r"^\s*(\d{1,4})\s*(AM|PM)\s+([A-Z]{3})\s+\w{3}\s+(\w{3})\s+(\d{1,2})\s+(\d{4})",
    re.IGNORECASE,
)
# Product AFOS line (e.g. "PFMEWX")
_AFOS_RE = re.compile(r"^\s*PFM([A-Z]{3})\s*$")


# -- Dataclasses --------------------------------------------------------------


@dataclass
class PFMSlot:
    """One time-column slot in a PFM table."""
    dt: datetime                      # UTC datetime at the start of this slot
    interval_hours: int               # 3 or 6
    temp_f: int | None = None
    dewpt_f: int | None = None
    rh_pct: int | None = None
    wind_dir: str = ""                # compass string (e.g. "SE")
    wind_spd_mph: int | None = None
    wind_gust_mph: int | None = None
    cloud: str = ""                   # CL / FW / SC / B1 / B2 / OV
    pop_pct: int | None = None        # 12hr PoP (sparse — only at 12hr boundaries)
    qpf_in: float | None = None       # 12hr QPF
    rain: str = ""                    # C / L / S / D likelihood
    tstm: str = ""
    obvis: str = ""                   # obstruction code (PF / F / BS / ...)


@dataclass
class PFMPoint:
    """One forecast point parsed from a PFM product."""
    name: str
    lat: float
    lon: float
    elev_ft: int | None = None
    zone: str = ""                    # UGC code
    issue_time: datetime | None = None  # UTC
    wfo: str = ""                     # 3-letter WFO code
    tz_offset_hours: int = 0          # LOCAL - UTC (e.g. -5 for CDT). Used
                                      # by the downsampler to group slots by
                                      # LOCAL calendar day.
    slots: list[PFMSlot] = field(default_factory=list)

    def local_date(self, dt: datetime) -> date:
        """Return the calendar date of `dt` in the point's local timezone."""
        return (dt + timedelta(hours=self.tz_offset_hours)).date()

    def local_hour(self, dt: datetime) -> int:
        """Return the local hour (0-23) of `dt`."""
        return (dt + timedelta(hours=self.tz_offset_hours)).hour


# -- Column position helpers --------------------------------------------------


def _find_column_positions(hrly_line: str, label_end: int) -> list[int]:
    """Return the start column of each time slot in a "<tz> 3hrly" header row.

    Data rows below this header place their values at the same columns.
    Each value is right-aligned in a 3-char-wide slot (2 chars value + space).
    We scan the hours field and record every non-space run.
    """
    positions: list[int] = []
    i = label_end
    while i < len(hrly_line):
        if hrly_line[i] != " ":
            # Start of a value; record and skip to next space
            positions.append(i)
            while i < len(hrly_line) and hrly_line[i] != " ":
                i += 1
        else:
            i += 1
    return positions


def _extract_slot_value(row: str, start_col: int, width: int = 3) -> str:
    """Extract the value at a column position from a data row.

    The column is `width` chars wide, centered on start_col. Real PFM uses
    right-aligned 3-char slots, so we extract chars [start_col - 1 .. start_col + 1]
    and strip whitespace.
    """
    # Right-align: value is typically 2 chars ending at start_col + 1
    # But cloud codes, wind dirs etc. can be in different positions.
    # Use a 3-char window starting at start_col - 1.
    lo = max(0, start_col - 1)
    hi = min(len(row), start_col + 2)
    return row[lo:hi].strip()


def _parse_int(s: str) -> int | None:
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _parse_float(s: str) -> float | None:
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


# -- Timestamp construction ---------------------------------------------------


_MONTH = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def _parse_issue_time(line: str) -> datetime | None:
    """Parse the PFM issue time line, e.g. '1245 PM CDT Fri Apr 10 2026'.

    Returns a UTC datetime. We actually don't care about the local time
    for table parsing (we use the UTC row directly) — this is only used as
    a stable "issued at" reference for the 0x31 issued_hours_ago field.
    """
    m = _ISSUE_RE.match(line)
    if not m:
        return None
    hhmm, ampm, tz, mon_str, day_s, year_s = m.groups()
    try:
        year = int(year_s)
        mon = _MONTH.get(mon_str.upper())
        if not mon:
            return None
        day = int(day_s)
        if len(hhmm) <= 2:
            hh, mm = int(hhmm), 0
        else:
            hh, mm = int(hhmm[:-2]), int(hhmm[-2:])
        if ampm.upper() == "PM" and hh < 12:
            hh += 12
        elif ampm.upper() == "AM" and hh == 12:
            hh = 0
        # Convert local → UTC using a simple offset table. Good enough for
        # a reference timestamp; we don't depend on this for table parsing.
        offset = _TZ_OFFSET.get(tz.upper(), 0)
        local_naive = datetime(year, mon, day, hh, mm)
        return (local_naive + timedelta(hours=-offset)).replace(tzinfo=timezone.utc)
    except (ValueError, KeyError):
        return None


# US timezone offsets (hours ahead of UTC). Negative = west of UTC.
# Sign convention here: local_utc = local + (-offset) so positive means
# LOCAL is ahead of UTC. For CDT (UTC-5), offset = -5.
_TZ_OFFSET = {
    "EST": -5, "EDT": -4,
    "CST": -6, "CDT": -5,
    "MST": -7, "MDT": -6,
    "PST": -8, "PDT": -7,
    "AKST": -9, "AKDT": -8,
    "HST": -10,
    "AST": -4,   # Atlantic (Puerto Rico — no DST)
    "ChST": 10,  # Chamorro (Guam)
}


def _build_slot_times(
    utc_hours: list[int], issue_time: datetime, interval: int
) -> list[datetime]:
    """Build a list of UTC datetimes for each time slot.

    The UTC hours come out of the PFM header row (e.g. "21 00 03 06 ...").
    We start at the issue date and walk forward, advancing the date whenever
    the hour wraps (next hour < previous hour).
    """
    if not utc_hours:
        return []
    # The first slot is the smallest UTC hour >= issue_time's hour, on the
    # issue_time's date (or the next day if it wraps). Actually simpler: the
    # first slot IS the first non-missing value's timestamp, which by
    # convention is the first hour in the header that equals or follows
    # issue_time rounded up to the next interval boundary.
    #
    # For our purposes the exact anchor isn't critical — we need CONSISTENT
    # datetimes across the series, and we use "day offset from issue date"
    # to group them. So:
    #   - start date = issue_time.date() in UTC
    #   - first hour < issue_time.hour means we've already rolled into tomorrow
    start = datetime(
        issue_time.year, issue_time.month, issue_time.day,
        tzinfo=timezone.utc,
    )
    if utc_hours[0] < issue_time.hour:
        start = start + timedelta(days=1)

    times: list[datetime] = []
    prev_hour = -1
    cur = start
    for h in utc_hours:
        if h <= prev_hour:
            # Wrapped past midnight — advance a day
            cur = cur + timedelta(days=1)
        cur = cur.replace(hour=h)
        times.append(cur)
        prev_hour = h
    return times


# -- Main parser --------------------------------------------------------------


def parse_pfm(text: str) -> list[PFMPoint]:
    """Parse a full PFM product into a list of forecast points.

    Tolerant of missing rows, label variations, and empty columns.
    Returns one PFMPoint per forecast point in the product.
    """
    lines = text.splitlines()

    # Find the product-level WFO (from the AFOS line) and issue time
    wfo = ""
    product_issue: datetime | None = None
    for i, line in enumerate(lines[:20]):
        m = _AFOS_RE.match(line)
        if m:
            wfo = m.group(1)
        if product_issue is None:
            t = _parse_issue_time(line)
            if t is not None:
                product_issue = t

    # Scan for forecast points
    points: list[PFMPoint] = []
    i = 0
    while i < len(lines):
        # Look for a UGC line
        m_ugc = _UGC_RE.match(lines[i])
        if not m_ugc:
            i += 1
            continue
        zone = m_ugc.group(1)

        # Next non-blank line is the point name
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        name = lines[j].strip()

        # Next non-blank line is the coord/elev line
        k = j + 1
        while k < len(lines) and not lines[k].strip():
            k += 1
        if k >= len(lines):
            break
        m_coord = _COORD_RE.match(lines[k])
        if not m_coord:
            # Not a forecast point — maybe a UGC line that isn't followed by
            # a name/coord pair. Skip past it and keep looking.
            i = k
            continue
        lat = float(m_coord.group("lat"))
        lon = -float(m_coord.group("lon"))
        elev = _parse_int(m_coord.group("elev")) if m_coord.group("elev") else None

        # Per-point issue time line (may be the same as the product issue time)
        point_issue = product_issue
        k2 = k + 1
        while k2 < len(lines) and not lines[k2].strip():
            k2 += 1
        if k2 < len(lines):
            t = _parse_issue_time(lines[k2])
            if t is not None:
                point_issue = t

        # Parse both tables (3hrly and 6hrly) until we hit another UGC line
        # or the end of the product.
        next_ugc = len(lines)
        for mm in range(k2 + 1, len(lines)):
            if _UGC_RE.match(lines[mm]):
                next_ugc = mm
                break
        table_lines = lines[k2:next_ugc]

        # Scan table_lines for the first <TZ> hrly header to extract the
        # point's local TZ offset. Used by the downsampler to group slots
        # by local calendar day rather than UTC.
        tz_offset = 0
        for line in table_lines:
            m_local = _HRLY_LOCAL_RE.match(line)
            if m_local and "UTC" not in line.upper():
                # Extract the timezone abbreviation from the label
                label = m_local.group("label").strip()
                parts = label.split()
                if parts:
                    tz = parts[0].upper()
                    tz_offset = _TZ_OFFSET.get(tz, 0)
                break

        slots = _parse_point_tables(table_lines, point_issue or datetime.now(timezone.utc))

        points.append(
            PFMPoint(
                name=name,
                lat=lat,
                lon=lon,
                elev_ft=elev,
                zone=zone,
                issue_time=point_issue,
                wfo=wfo,
                tz_offset_hours=tz_offset,
                slots=slots,
            )
        )
        i = next_ugc

    return points


def _parse_point_tables(lines: list[str], issue_time: datetime) -> list[PFMSlot]:
    """Parse both the 3-hourly and 6-hourly tables for a single forecast point.

    Returns a combined time-sorted list of PFMSlot entries.
    """
    slots: list[PFMSlot] = []

    # Find all occurrences of "<TZ> 3hrly" or "<TZ> 6hrly" header rows
    # (could be 1 or 2 tables per point)
    i = 0
    while i < len(lines):
        m_local = _HRLY_LOCAL_RE.match(lines[i])
        if not m_local or "UTC" in m_local.group("label").upper():
            i += 1
            continue

        interval = 3 if "3" in m_local.group("interval") else 6

        # The next non-blank line should be the UTC header
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j >= len(lines):
            break
        m_utc = _HRLY_UTC_RE.match(lines[j])
        if not m_utc:
            i = j
            continue
        utc_hours = [int(x) for x in m_utc.group("hours").split() if x.isdigit()]

        # Column positions come from the UTC header (it has the same alignment)
        positions = _find_column_positions(lines[j], _LABEL_END)
        if len(positions) != len(utc_hours):
            # Misaligned — fall back to the local hrly line
            positions = _find_column_positions(lines[i], _LABEL_END)

        # Build timestamps for each slot
        times = _build_slot_times(utc_hours, issue_time, interval)

        # Scan data rows until we hit another hrly header or run out
        table_slots = [PFMSlot(dt=t, interval_hours=interval) for t in times]
        for k in range(j + 1, len(lines)):
            row = lines[k]
            if _HRLY_LOCAL_RE.match(row) and "UTC" not in row.upper():
                break  # Next table
            row_kind = _classify_row(row)
            if row_kind is None:
                continue
            _apply_row(row_kind, row, positions, table_slots)

        slots.extend(table_slots)

        # Advance past this table
        i = k + 1 if k > j else j + 1

    return slots


def _classify_row(row: str) -> str | None:
    """Return the canonical row kind ('temp', 'wind_dir', etc.) or None."""
    # Only the label portion (first _LABEL_END chars) matters for classification
    label = row[:_LABEL_END + 5]  # +5 for multi-word labels like "Wind gust"
    for kind, pattern in _ROW_PATTERNS.items():
        if pattern.match(label):
            return kind
    return None


def _apply_row(kind: str, row: str, positions: list[int], slots: list[PFMSlot]) -> None:
    """Apply a data row's values to the matching slot fields."""
    values = [_extract_slot_value(row, pos) for pos in positions]
    for slot, val in zip(slots, values):
        if not val:
            continue
        if kind == "temp":
            n = _parse_int(val)
            if n is not None:
                slot.temp_f = n
        elif kind == "dewpt":
            n = _parse_int(val)
            if n is not None:
                slot.dewpt_f = n
        elif kind == "rh":
            n = _parse_int(val)
            if n is not None:
                slot.rh_pct = n
        elif kind in ("wind_dir", "pwind_dir"):
            slot.wind_dir = val
        elif kind == "wind_spd":
            n = _parse_int(val)
            if n is not None:
                slot.wind_spd_mph = n
        elif kind == "wind_gust":
            n = _parse_int(val)
            if n is not None:
                slot.wind_gust_mph = n
        elif kind == "clouds":
            slot.cloud = val
        elif kind == "pop12":
            n = _parse_int(val)
            if n is not None:
                slot.pop_pct = n
        elif kind == "qpf12":
            f = _parse_float(val)
            if f is not None:
                slot.qpf_in = f
        elif kind == "rain":
            slot.rain = val
        elif kind == "tstm":
            slot.tstm = val
        elif kind == "obvis":
            slot.obvis = val
        # snow12, min_max, max_min, wind_char intentionally not applied —
        # we derive highs/lows from the Temp row instead, which gives us
        # better granularity.


# -- Utilities ----------------------------------------------------------------


def find_point(points: list[PFMPoint], name: str = "", zone: str = "") -> PFMPoint | None:
    """Locate a specific point by name or by UGC zone code."""
    for p in points:
        if zone and p.zone == zone:
            return p
        if name and p.name == name:
            return p
    return None


# -- Daily downsampler → 0x31 periods -----------------------------------------

# Sky code nibbles (match meshcore_weather.protocol.meshwx SKY_*). We
# can't import from protocol here without creating a cycle, so these are
# duplicated. Keep in sync with meshwx.py.
_SKY_CLEAR = 0x0
_SKY_FEW = 0x1
_SKY_SCATTERED = 0x2
_SKY_BROKEN = 0x3
_SKY_OVERCAST = 0x4
_SKY_FOG = 0x5
_SKY_RAIN = 0x8
_SKY_SNOW = 0x9
_SKY_THUNDERSTORM = 0xA

# PFM cloud codes → sky nibble. CL=clear, FW=few, SC=scattered, B1/B2=broken,
# OV=overcast. Source: NWS PFM format spec.
_CLOUD_TO_SKY = {
    "CL": _SKY_CLEAR,
    "FW": _SKY_FEW,
    "SC": _SKY_SCATTERED,
    "B1": _SKY_BROKEN,
    "B2": _SKY_BROKEN,
    "OV": _SKY_OVERCAST,
}

# 16-point compass → nibble (match wind_dir_to_nibble in meshwx.py)
_COMPASS_TO_NIBBLE = {
    "N": 0, "NNE": 1, "NE": 2, "ENE": 3,
    "E": 4, "ESE": 5, "SE": 6, "SSE": 7,
    "S": 8, "SSW": 9, "SW": 10, "WSW": 11,
    "W": 12, "WNW": 13, "NW": 14, "NNW": 15,
}

# Condition flag bits (match encode_forecast_from_zfp in encoders.py)
_COND_THUNDER = 0x01
_COND_FROST = 0x02
_COND_FOG = 0x04
_COND_HIGH_WIND = 0x08
_COND_FREEZING_RAIN = 0x10
_COND_HEAVY_RAIN = 0x20
_COND_HEAVY_SNOW = 0x40


def _derive_sky_code(slot: PFMSlot) -> int:
    """Map a single slot's weather codes to a 4-bit sky nibble.

    Precedence: thunderstorm > rain > snow > fog > cloud cover.
    This way a "partly cloudy with thunderstorms" slot gets the TSTM
    code which is the most actionable thing to display.
    """
    if slot.tstm and slot.tstm in ("C", "L", "D"):
        return _SKY_THUNDERSTORM
    if slot.rain and slot.rain in ("C", "L", "D"):
        return _SKY_RAIN
    if slot.obvis == "BS":
        return _SKY_SNOW
    if slot.obvis in ("F", "PF"):
        return _SKY_FOG
    return _CLOUD_TO_SKY.get(slot.cloud, _SKY_CLEAR)


def _derive_condition_flags(slots: list[PFMSlot]) -> int:
    """OR together condition flag bits from all slots in a period."""
    flags = 0
    for s in slots:
        if s.tstm and s.tstm in ("C", "L", "D"):
            flags |= _COND_THUNDER
        if s.obvis in ("F", "PF"):
            flags |= _COND_FOG
        if s.obvis == "BS":
            flags |= _COND_HEAVY_SNOW
        # Heavy rain: PoP ≥ 70 AND rain likely/definite
        if s.pop_pct is not None and s.pop_pct >= 70 and s.rain in ("L", "D"):
            flags |= _COND_HEAVY_RAIN
        if s.qpf_in is not None and s.qpf_in >= 1.0:
            flags |= _COND_HEAVY_RAIN
        # High wind: sustained ≥ 30 mph OR gusts ≥ 40 mph
        if s.wind_spd_mph is not None and s.wind_spd_mph >= 30:
            flags |= _COND_HIGH_WIND
        if s.wind_gust_mph is not None and s.wind_gust_mph >= 40:
            flags |= _COND_HIGH_WIND
        # Frost: low temp ≤ 36°F (overnight)
        if s.temp_f is not None and s.temp_f <= 36:
            flags |= _COND_FROST
        # Freezing rain / sleet
        if s.obvis in ("IP", "ZR", "ZL"):
            flags |= _COND_FREEZING_RAIN
    return flags


def _dominant_sky(slots: list[PFMSlot]) -> int:
    """Return the most severe sky condition across the slots (precip > clouds)."""
    if not slots:
        return _SKY_CLEAR
    codes = [_derive_sky_code(s) for s in slots]
    # Precedence — pick the highest-priority code that appears
    for priority in (_SKY_THUNDERSTORM, _SKY_RAIN, _SKY_SNOW, _SKY_FOG,
                     _SKY_OVERCAST, _SKY_BROKEN, _SKY_SCATTERED, _SKY_FEW, _SKY_CLEAR):
        if priority in codes:
            return priority
    return _SKY_CLEAR


def _dominant_wind_dir(slots: list[PFMSlot]) -> int:
    """Return the most common wind direction nibble across the slots."""
    from collections import Counter
    dirs = [s.wind_dir for s in slots if s.wind_dir]
    if not dirs:
        return 0
    most_common = Counter(dirs).most_common(1)[0][0]
    return _COMPASS_TO_NIBBLE.get(most_common, 0)


def _avg_wind_speed_5mph(slots: list[PFMSlot]) -> int:
    """Return the average wind speed in 5 mph buckets (0-15)."""
    speeds = [s.wind_spd_mph for s in slots if s.wind_spd_mph is not None]
    if not speeds:
        return 0
    avg = sum(speeds) / len(speeds)
    return max(0, min(15, int(round(avg / 5))))


@dataclass
class DailyPeriod:
    """One daily forecast period derived from PFM slots. Mirrors the 0x31 period dict."""
    day_offset: int        # 0 = first local date in the data, 1 = next, etc.
    high_f: int | None
    low_f: int | None
    sky_code: int
    precip_pct: int
    wind_dir_nibble: int
    wind_speed_5mph: int
    condition_flags: int

    def to_encoder_dict(self) -> dict:
        """Return a dict compatible with meshwx.pack_forecast()."""
        return {
            "period_id": self.day_offset,
            "high_f": 127 if self.high_f is None else self.high_f,
            "low_f": 127 if self.low_f is None else self.low_f,
            "sky_code": self.sky_code,
            "precip_pct": self.precip_pct,
            "wind_dir_nibble": self.wind_dir_nibble,
            "wind_speed_5mph": self.wind_speed_5mph,
            "condition_flags": self.condition_flags,
        }


def downsample_to_daily(
    point: PFMPoint, max_days: int = 7, min_slots_per_day: int = 4
) -> list[DailyPeriod]:
    """Aggregate a PFMPoint's 3-hourly + 6-hourly slots into daily periods.

    Grouping is by LOCAL calendar date (using the point's tz_offset_hours).
    Day 0 is the first local date that has at least `min_slots_per_day`
    temperature readings — this skips partial days at the start/end of
    the PFM window that would otherwise produce misleading aggregates
    (e.g. a "high" equal to an early-morning temp because the afternoon
    slots aren't in the PFM yet).

    For each day:
      high_f        = max Temp across daytime slots (local hour 6-18)
      low_f         = min Temp across nighttime slots (rest of the day)
      sky_code      = most severe weather condition during daytime
      precip_pct    = max 12hr PoP value that falls inside the day
      wind_dir_nibble = most common direction during daytime
      wind_speed_5mph = average sustained wind speed during daytime
      condition_flags = OR of flags from all slots in the day

    Invariant: for every returned period, `high_f >= low_f`.
    """
    if not point.slots:
        return []

    # Group slots by local date
    from collections import defaultdict
    by_local_date: dict[date, list[PFMSlot]] = defaultdict(list)
    for s in point.slots:
        by_local_date[point.local_date(s.dt)].append(s)

    # Filter to days with enough data. We keep contiguous runs starting
    # from the first day with enough data, so we don't have gaps in the
    # middle of the series.
    sorted_dates = sorted(by_local_date.keys())
    valid_dates: list[date] = []
    for d in sorted_dates:
        temps = [s.temp_f for s in by_local_date[d] if s.temp_f is not None]
        if len(temps) >= min_slots_per_day:
            valid_dates.append(d)
        elif valid_dates:
            # We've hit a partial day AFTER finding valid ones — stop here
            # rather than emitting a truncated last day with bogus aggregates.
            break

    if not valid_dates:
        return []
    base_date = valid_dates[0]

    periods: list[DailyPeriod] = []
    for d in valid_dates[:max_days]:
        day_slots = by_local_date[d]
        daytime = [s for s in day_slots if 6 <= point.local_hour(s.dt) <= 18]
        nighttime = [s for s in day_slots
                     if point.local_hour(s.dt) < 6 or point.local_hour(s.dt) > 18]

        daytime_temps = [s.temp_f for s in daytime if s.temp_f is not None]
        night_temps = [s.temp_f for s in nighttime if s.temp_f is not None]
        all_temps = [s.temp_f for s in day_slots if s.temp_f is not None]

        high_f = max(daytime_temps) if daytime_temps else max(all_temps)
        low_f = min(night_temps) if night_temps else min(all_temps)

        # Sky/wind derivations prefer daytime values; fall back to whole day.
        sky_src = daytime if daytime else day_slots
        sky_code = _dominant_sky(sky_src)
        wind_dir_nibble = _dominant_wind_dir(sky_src)
        wind_speed_5mph = _avg_wind_speed_5mph(sky_src)

        # Max PoP 12hr across any slot that has one
        pops = [s.pop_pct for s in day_slots if s.pop_pct is not None]
        precip_pct = max(pops) if pops else 0

        # Condition flags aggregated over the whole day
        flags = _derive_condition_flags(day_slots)

        periods.append(
            DailyPeriod(
                day_offset=(d - base_date).days,
                high_f=high_f,
                low_f=low_f,
                sky_code=sky_code,
                precip_pct=precip_pct,
                wind_dir_nibble=wind_dir_nibble,
                wind_speed_5mph=wind_speed_5mph,
                condition_flags=flags,
            )
        )

    return periods
