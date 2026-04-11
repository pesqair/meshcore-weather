"""Broadcast executor — runs a single scheduled job and produces wire messages.

The executor owns a registry mapping product names to builder functions.
Each builder takes (job, context) and returns a list of raw wire-format
bytes (`list[bytes]`) ready for COBS encoding and radio transmission.

Adding a new product type means:
  1. Add the product name to `models.PRODUCT_TYPES`
  2. Write a `_build_<product>(job, ctx)` function here
  3. Register it in `PRODUCT_BUILDERS` at the bottom of this file

The executor does NOT know about:
  - Scheduling (when to run) — that's the scheduler's job
  - Transport (how to send) — the scheduler hands the messages to the radio
  - Rate limiting across multiple jobs — each job has its own interval

Error handling: a builder that raises is caught and logged but doesn't
break the rest of the cycle. A builder that returns an empty list is
a legitimate "no data available right now" — the scheduler logs it at
debug level, records the run timestamp, and moves on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from meshcore_weather.geodata import resolver
from meshcore_weather.parser.weather import WeatherStore
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.encoders import (
    encode_forecast_from_pfm,
    encode_forecast_from_zfp,
    encode_hwo,
    encode_lsr_reports,
    encode_metar,
    encode_rain_cities,
    encode_rwr_city,
    encode_taf,
    now_utc_minutes,
)
from meshcore_weather.protocol.meshwx import (
    LOC_PFM_POINT,
    LOC_STATION,
    LOC_ZONE,
    SEV_WARNING,
    pack_warnings_near,
)
from meshcore_weather.protocol.radar import (
    build_radar_messages,
    extract_region_grid,
    fetch_radar_composite,
)
from meshcore_weather.protocol.warnings import (
    extract_active_warnings,
    warnings_to_binary,
)
from meshcore_weather.schedule.models import BroadcastJob

logger = logging.getLogger(__name__)


# -- Execution context -------------------------------------------------------


@dataclass
class ExecutorContext:
    """Per-execution shared state the scheduler hands each builder.

    Scheduler + executor share this object so builders can reach the
    store, the active Coverage, the PFM points index, and anything else
    that should be resolved once per cycle rather than per-job.
    """

    store: WeatherStore
    coverage: Coverage
    pfm_points: list[dict]           # from pfm_points.json (or empty if unavailable)
    latest_radar: tuple[bytes, int] | None  # (image_bytes, timestamp_utc_min)


# -- Builders ----------------------------------------------------------------


def _build_radar(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Radar grid broadcast. `location_type=coverage` emits one grid per
    region in the operator's coverage; `location_type=region` emits just
    one region (specified by `location_id`)."""
    if ctx.latest_radar is None:
        return []
    img, ts_min = ctx.latest_radar

    if job.location_type == "region":
        try:
            region_id = int(job.location_id, 0)  # allow "3" or "0x3"
        except (ValueError, TypeError):
            logger.warning("radar job %s: bad region id %r", job.id, job.location_id)
            return []
        grid = extract_region_grid(img, region_id)
        if not grid:
            return []
        from meshcore_weather.protocol.meshwx import pack_radar_grid, REGIONS
        region = REGIONS[region_id]
        return [pack_radar_grid(region_id, 0, ts_min, region["scale"], grid)]

    # coverage (default): emit grids for all regions in the coverage area
    region_ids = ctx.coverage.region_ids if not ctx.coverage.is_empty() else None
    return build_radar_messages(img, ts_min, region_ids=region_ids)


def _build_warnings(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Active warnings broadcast. Always uses coverage for filtering;
    the job's `location_type` + `location_id` are ignored (the whole
    coverage area is the filter)."""
    warnings = extract_active_warnings(ctx.store, coverage=ctx.coverage)
    return warnings_to_binary(warnings)


def _build_observation(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Current-conditions observation (0x30). Prefers a METAR station
    for the location; falls back to an RWR city row."""
    query = _location_to_query(job)
    if not query:
        return []
    resolved = resolver.resolve(query)
    if not resolved:
        return []

    # Try METAR first (most accurate)
    station = resolved.get("station")
    if station:
        raw = ctx.store._find_metar_raw(station)
        if raw:
            metar_text, _ts = raw
            msg = encode_metar(station, metar_text, now_utc_minutes())
            if msg:
                return [msg]

    # Fall back: RWR city row via the location's WFO
    zones = resolved.get("zones") or []
    if zones:
        zone = zones[0]
        origs = ctx.store._build_origs(resolved)
        rwr = ctx.store._find_any_orig("RWR", origs)
        if rwr:
            city = resolved["name"].split(",")[0].strip().upper()
            line = ctx.store._parse_rwr_city_raw(rwr.raw_text, city)
            if line:
                msg = encode_rwr_city(zone, line, now_utc_minutes())
                if msg:
                    return [msg]
    return []


def _build_forecast(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Multi-day forecast (0x31). Prefers PFM data; falls back to ZFP."""
    query = _location_to_query(job)
    if not query:
        return []
    resolved = resolver.resolve(query)
    if not resolved:
        return []
    zones = resolved.get("zones") or []
    if not zones:
        return []
    zone = zones[0]

    # If the job targets a PFM point directly, echo that back in the
    # response so clients can correlate their cached-point entries with
    # the broadcast.
    resp_loc_type = None
    resp_loc_id = None
    if job.location_type == "pfm_point":
        try:
            idx = int(job.location_id)
            if 0 <= idx < len(ctx.pfm_points):
                resp_loc_type = LOC_PFM_POINT
                resp_loc_id = idx
        except ValueError:
            pass
    elif job.location_type == "city":
        # Find the nearest PFM point for this city so iOS clients doing
        # city search recognize the broadcast
        lat = resolved.get("lat", 0.0)
        lon = resolved.get("lon", 0.0)
        idx = _nearest_pfm_point_index(lat, lon, ctx.pfm_points)
        if idx is not None:
            resp_loc_type = LOC_PFM_POINT
            resp_loc_id = idx

    origs = ctx.store._build_origs(resolved)

    # PFM primary path
    pfm = ctx.store._find_any_orig("PFM", origs)
    if pfm:
        hours_ago = max(
            0,
            int(
                (
                    now_utc_minutes()
                    - pfm.timestamp.hour * 60
                    - pfm.timestamp.minute
                )
                / 60
            ),
        )
        msg = encode_forecast_from_pfm(
            pfm.raw_text, zone, hours_ago,
            loc_type=resp_loc_type, loc_id=resp_loc_id,
        )
        if msg:
            return [msg]

    # ZFP fallback
    zfp = ctx.store._find_any_orig("ZFP", origs)
    if zfp:
        zone_text = ctx.store._parse_zfp_zone(zfp.raw_text, zone)
        if zone_text:
            hours_ago = max(
                0,
                int(
                    (
                        now_utc_minutes()
                        - zfp.timestamp.hour * 60
                        - zfp.timestamp.minute
                    )
                    / 60
                ),
            )
            msg = encode_forecast_from_zfp(
                zone, zfp.raw_text, hours_ago,
                loc_type=resp_loc_type, loc_id=resp_loc_id,
            )
            if msg:
                return [msg]

    return []


def _build_outlook(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Hazardous Weather Outlook (0x32)."""
    query = _location_to_query(job)
    if not query:
        return []
    resolved = resolver.resolve(query)
    if not resolved:
        return []
    zones = resolved.get("zones") or []
    if not zones:
        return []
    zone = zones[0]

    origs = ctx.store._build_origs(resolved)
    hwo = ctx.store._find_any_orig("HWO", origs)
    if hwo is None:
        from meshcore_weather.parser.weather import _expand_zone_ranges
        loc_zones = set(zones)
        best = None
        for prod in ctx.store._products.values():
            if prod.product_type != "HWO":
                continue
            if loc_zones & _expand_zone_ranges(prod.raw_text):
                if best is None or prod.timestamp > best.timestamp:
                    best = prod
        hwo = best
    if hwo is None:
        return []

    issued_min = hwo.timestamp.hour * 60 + hwo.timestamp.minute
    msg = encode_hwo(zone, hwo.raw_text, issued_min)
    return [msg] if msg else []


def _build_storm_reports(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Local Storm Reports (0x33)."""
    query = _location_to_query(job)
    if not query:
        return []
    resolved = resolver.resolve(query)
    if not resolved:
        return []
    zones = resolved.get("zones") or []
    if not zones:
        return []
    zone = zones[0]
    state = zone[:2]

    seen: set[str] = set()
    entries: list[dict] = []
    for prod in sorted(
        ctx.store._products.values(), key=lambda p: p.timestamp, reverse=True
    ):
        if prod.product_type != "LSR":
            continue
        if prod.state != state:
            affected = ctx.store._affected_state(prod)
            if affected != state:
                continue
        for entry in ctx.store._parse_lsr_entries(prod.raw_text):
            if entry.get("state") and entry["state"] != state:
                continue
            key = f"{entry.get('time','')}_{entry.get('event','')}_{entry.get('location','')}"
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
            if len(entries) >= 16:
                break
        if len(entries) >= 16:
            break

    if not entries:
        return []
    msg = encode_lsr_reports(zone, entries, now_utc_minutes())
    return [msg] if msg else []


def _build_rain_obs(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Rain observations (0x34)."""
    query = _location_to_query(job)
    if not query:
        return []
    resolved = resolver.resolve(query)
    if not resolved:
        return []
    zones = resolved.get("zones") or []
    if not zones:
        return []
    zone = zones[0]

    origs = ctx.store._build_origs(resolved)
    rwr = ctx.store._find_any_orig("RWR", origs)
    if rwr is None:
        return []

    rain_keywords = {
        "RAIN", "LGT RAIN", "HVY RAIN", "TSTORM", "T-STORM",
        "DRIZZLE", "SHOWERS", "SHOWER", "SNOW",
    }
    rainy: list[dict] = []
    seen_names: set[str] = set()
    in_table = False
    for line in rwr.raw_text.splitlines():
        stripped = line.strip()
        if "SKY/WX" in stripped and "TMP" in stripped:
            in_table = True
            continue
        if not in_table or not stripped:
            continue
        if stripped.startswith("$$"):
            break
        upper = stripped.upper()
        if not any(kw in upper for kw in rain_keywords):
            continue
        parts = stripped.split()
        if parts and parts[0].startswith("*"):
            parts[0] = parts[0][1:]
        sky_words = rain_keywords | {
            "SUNNY", "MOSUNNY", "PTSUNNY", "CLEAR", "MOCLDY", "PTCLDY",
            "CLOUDY", "FAIR", "FOG", "HAZE", "WINDY", "LGT", "HVY",
        }
        city_parts: list[str] = []
        rain_text = ""
        temp_f = 60
        for p in parts:
            if p.upper() in sky_words:
                rain_text = p
                break
            if p.lstrip("-").isdigit():
                break
            city_parts.append(p)
        if rain_text and rain_text in parts:
            idx2 = parts.index(rain_text)
            for tp in parts[idx2 + 1 :]:
                if tp.lstrip("-").isdigit():
                    try:
                        temp_f = int(tp)
                    except ValueError:
                        pass
                    break
        city_name = " ".join(city_parts).title().strip()
        if not city_name or city_name in seen_names:
            continue
        seen_names.add(city_name)
        rainy.append({
            "name": city_name,
            "state": zone[:2],
            "rain_text": rain_text or "rain",
            "temp_f": temp_f,
        })

    if not rainy:
        return []
    msg = encode_rain_cities(zone, rainy, now_utc_minutes())
    return [msg] if msg else []


def _build_metar(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """METAR current conditions for a station. Same wire format as 0x30
    observation — this exists as a separate product so operators can
    schedule METAR-specific station broadcasts."""
    if job.location_type != "station":
        # Fall back to the same observation path for non-station locations
        return _build_observation(job, ctx)
    station = job.location_id.strip().upper()
    if not station:
        return []
    raw = ctx.store._find_metar_raw(station)
    if not raw:
        return []
    metar_text, _ts = raw
    msg = encode_metar(station, metar_text, now_utc_minutes())
    return [msg] if msg else []


def _build_taf(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """TAF forecast (0x36)."""
    if job.location_type == "station":
        station = job.location_id.strip().upper()
    else:
        query = _location_to_query(job)
        resolved = resolver.resolve(query) if query else None
        station = (resolved or {}).get("station", "") if resolved else ""
    if not station:
        return []

    target_marker = f"TAF {station}"
    amend_marker = f"TAF AMD {station}"
    candidate = None
    for prod in sorted(
        ctx.store._products.values(), key=lambda p: p.timestamp, reverse=True
    ):
        if prod.product_type != "TAF":
            continue
        if (target_marker in prod.raw_text
            or amend_marker in prod.raw_text
            or f"\n{station} " in prod.raw_text):
            candidate = prod
            break
    if candidate is None:
        return []

    hours_ago = max(
        0,
        int(
            (
                now_utc_minutes()
                - candidate.timestamp.hour * 60
                - candidate.timestamp.minute
            )
            / 60
        ),
    )
    msg = encode_taf(station, candidate.raw_text, hours_ago)
    return [msg] if msg else []


def _build_warnings_near(job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
    """Warnings-near-location summary (0x37)."""
    # Accept a bare zone directly for land + marine zone support
    zone = ""
    if job.location_type == "zone":
        zone = job.location_id.strip().upper()
    else:
        query = _location_to_query(job)
        resolved = resolver.resolve(query) if query else None
        if resolved:
            zones = resolved.get("zones") or []
            if zones:
                zone = zones[0]
    if not zone:
        return []

    all_warnings = extract_active_warnings(ctx.store, coverage=None)
    z_meta = resolver._zones.get(zone, {})
    z_lat = z_meta.get("la", 0.0)
    z_lon = z_meta.get("lo", 0.0)

    nearby: list[dict] = []
    for w in all_warnings:
        ugcs = set(w.get("ugcs") or w.get("zones", []))
        in_zone = zone in ugcs
        if not in_zone and w.get("vertices"):
            from meshcore_weather.protocol.coverage import _point_in_polygon
            if _point_in_polygon(z_lat, z_lon, w["vertices"]):
                in_zone = True
        if not in_zone:
            continue
        entry_zone = zone if zone in ugcs else (sorted(ugcs)[0] if ugcs else "")
        expires_at = w.get("expires_at")
        expires_unix_min = int(expires_at.timestamp() / 60) if expires_at else 0
        nearby.append({
            "warning_type": w.get("warning_type", 0),
            "severity": w.get("severity", SEV_WARNING),
            "expires_unix_min": expires_unix_min,
            "zone": entry_zone if len(entry_zone) == 6 and entry_zone[2] == "Z" else "",
        })

    if not nearby:
        return []
    msg = pack_warnings_near(LOC_ZONE, zone, nearby)
    return [msg]


# -- Product registry --------------------------------------------------------


PRODUCT_BUILDERS: dict[str, Callable[[BroadcastJob, ExecutorContext], list[bytes]]] = {
    "radar": _build_radar,
    "warnings": _build_warnings,
    "observation": _build_observation,
    "forecast": _build_forecast,
    "outlook": _build_outlook,
    "storm_reports": _build_storm_reports,
    "rain_obs": _build_rain_obs,
    "metar": _build_metar,
    "taf": _build_taf,
    "warnings_near": _build_warnings_near,
}


class BroadcastExecutor:
    """Executes scheduled broadcast jobs and returns the wire messages
    they produced. Does NOT transmit — the scheduler handles radio I/O.
    """

    def run_job(self, job: BroadcastJob, ctx: ExecutorContext) -> list[bytes]:
        """Run one job, catching exceptions so one bad job doesn't
        break the rest of a cycle."""
        builder = PRODUCT_BUILDERS.get(job.product)
        if builder is None:
            logger.warning(
                "job %s: no builder registered for product %r", job.id, job.product
            )
            return []
        try:
            return builder(job, ctx) or []
        except Exception:
            logger.exception("job %s: builder raised", job.id)
            return []


# -- Helpers -----------------------------------------------------------------


def _location_to_query(job: BroadcastJob) -> str:
    """Convert a job's location to a string the resolver understands."""
    lt = job.location_type
    lid = job.location_id.strip()
    if lt == "city":
        return lid
    if lt == "zone":
        return lid
    if lt == "station":
        return lid
    if lt == "wfo":
        # WFO alone doesn't resolve directly — return empty and let
        # the builder fall back to any of the WFO's products
        return ""
    if lt == "pfm_point":
        # The resolver doesn't know about PFM points by index, but the
        # forecast builder has direct access to ctx.pfm_points. Return
        # empty and let the builder handle it.
        return ""
    return ""


def _nearest_pfm_point_index(
    lat: float, lon: float, points: list[dict], max_deg_sq: float = 0.2
) -> int | None:
    """Return the index of the nearest PFM point to (lat, lon), or None
    if nothing is within ~50 km."""
    if not points:
        return None
    best_idx: int | None = None
    best_d2 = float("inf")
    for i, p in enumerate(points):
        dlat = lat - p["lat"]
        dlon = lon - p["lon"]
        d2 = dlat * dlat + dlon * dlon
        if d2 < best_d2:
            best_d2 = d2
            best_idx = i
    if best_d2 > max_deg_sq:
        return None
    return best_idx
