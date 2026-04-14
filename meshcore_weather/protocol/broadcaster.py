"""MeshWX broadcaster — reactive path (responds to client DMs).

The proactive periodic broadcast cycle lives in
`meshcore_weather.schedule.Scheduler` as of the unified schedule
refactor. This class now owns a Scheduler instance for proactive
broadcasts and handles the reactive paths:

  - `respond_to_data_request()` — builds a 0x30/0x31/0x32/... response
    to a client's 0x02 data request DM, with per-(data_type, location)
    rate limiting.
  - `broadcast_region()` — legacy 0x01 refresh request handler.

Both reactive paths share the radio with the Scheduler and use the same
builder methods (`_build_observation` etc.) under the hood. The
Scheduler uses its own executor for proactive broadcasts; the duplicate
builder code will be consolidated in a future cleanup.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from meshcore_weather.activity import EventDir, activity_log
from meshcore_weather.config import settings
from meshcore_weather.geodata import resolver
from meshcore_weather.meshcore.radio import MeshcoreRadio
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
    V4SequenceCounter,
    DATA_FORECAST,
    DATA_METAR,
    DATA_OUTLOOK,
    DATA_RAIN_OBS,
    DATA_STORM_REPORTS,
    DATA_TAF,
    DATA_WARNING_DETAIL,
    DATA_WARNINGS_NEAR,
    DATA_WX,
    LOC_LATLON,
    LOC_PFM_POINT,
    LOC_PLACE,
    LOC_STATION,
    LOC_WFO,
    LOC_ZONE,
    REASON_BOT_ERROR,
    REASON_LOCATION_UNRESOLVABLE,
    REASON_NO_DATA,
    REASON_PRODUCT_UNSUPPORTED,
    SEV_ADVISORY,
    SEV_WARNING,
    SEV_WATCH,
    cobs_encode,
    pack_not_available,
    pack_warnings_near,
)
from meshcore_weather.protocol.radar import (
    build_radar_messages,
    extract_region_grid,
    fetch_radar_composite,
)
from meshcore_weather.protocol.warnings import extract_active_warnings, warnings_to_binary

logger = logging.getLogger(__name__)

# Delay between consecutive LoRa transmissions (seconds)
TX_SPACING = 2


class MeshWXBroadcaster:
    """Reactive broadcast path — responds to client data requests.

    The proactive periodic broadcast cycle lives in the Scheduler
    (`meshcore_weather.schedule.scheduler.Scheduler`), which this class
    owns as a member. `start()` launches the scheduler's tick loop.
    `respond_to_data_request()` handles incoming client DMs by building
    the right wire message and broadcasting it on the data channel —
    separate from the scheduled path.
    """

    def __init__(self, store: WeatherStore, radio: MeshcoreRadio):
        # Local import avoids a circular dependency between this module
        # and meshcore_weather.schedule.executor (which also imports from
        # meshcore_weather.protocol).
        from meshcore_weather.schedule.scheduler import Scheduler

        self.store = store
        self.radio = radio
        self._last_refresh: dict[int, float] = {}  # region_id -> timestamp
        self._scheduler: Scheduler = Scheduler(store=store, radio=radio)

    @property
    def scheduler(self):
        """The owned Scheduler instance (for portal access)."""
        return self._scheduler

    @property
    def coverage(self) -> Coverage:
        """Current coverage — delegates to the Scheduler."""
        return self._scheduler.coverage

    def reload_coverage(self) -> None:
        """Rebuild coverage from current settings."""
        self._scheduler.reload_coverage()

    async def start(self) -> None:
        """Start the scheduled broadcast cycle."""
        await self._scheduler.start()

    async def stop(self) -> None:
        """Stop the scheduled broadcast cycle."""
        await self._scheduler.stop()

    # -- Legacy 0x01 refresh request handler --------------------------------

    async def broadcast_region(self, region_id: int, request_type: int = 3) -> None:
        """Broadcast data for a specific region (triggered by 0x01 refresh request).

        This is the legacy v1 refresh-request path. iOS clients should
        use 0x02 data requests instead; this handler remains for backward
        compat with v1 clients.

        request_type: 1=radar only, 2=warnings only, 3=both.
        """
        now = time.time()
        last = self._last_refresh.get(region_id, 0)
        cooldown = settings.meshwx_refresh_cooldown
        if now - last < cooldown:
            remaining = int(cooldown - (now - last))
            logger.info(
                "Refresh for region 0x%X throttled — cooldown %ds, retry in %ds",
                region_id, cooldown, remaining,
            )
            activity_log.record(EventDir.IN, "throttled",
                f"Region 0x{region_id:X} refresh throttled ({remaining}s remaining)",
                {"region_id": region_id, "cooldown": cooldown, "remaining": remaining})
            return
        self._last_refresh[region_id] = now

        # Reuse the scheduler's cached radar composite when available;
        # otherwise refresh it via a one-off fetch.
        if request_type in (1, 3):
            await self._scheduler._refresh_radar()
            if self._scheduler._latest_radar is not None:
                from meshcore_weather.protocol.meshwx import pack_radar_compressed, REGIONS
                img_data, ts_min = self._scheduler._latest_radar
                # Use the broadcast config's grid size (portal-editable),
                # falling back to the env-var default.
                cfg = self._scheduler.current_config()
                grid_size = getattr(cfg, "radar_grid_size", None) or settings.meshwx_radar_grid_size
                if grid_size not in (16, 32, 64):
                    grid_size = 32
                grid = extract_region_grid(img_data, region_id, grid_size=grid_size)
                if grid:
                    region = REGIONS[region_id]
                    msgs = pack_radar_compressed(
                        region_id=region_id,
                        timestamp_utc_min=ts_min,
                        scale_km=region["scale"],
                        grid=grid,
                        grid_size=grid_size,
                    )
                    for i, msg in enumerate(msgs):
                        await self.radio.send_binary_channel(cobs_encode(msg))
                        if i + 1 < len(msgs):
                            await asyncio.sleep(TX_SPACING)

        if request_type in (2, 3):
            # Broadcast all active warnings in the scheduler's coverage
            warnings = extract_active_warnings(
                self.store, coverage=self._scheduler.coverage
            )
            msgs = warnings_to_binary(warnings)
            for i, msg in enumerate(msgs):
                await self.radio.send_binary_channel(cobs_encode(msg))
                if i + 1 < len(msgs):
                    await asyncio.sleep(TX_SPACING)

        req_label = {1: "radar", 2: "warnings", 3: "radar+warnings"}.get(request_type, str(request_type))
        activity_log.record(EventDir.IN, "v1_refresh",
            f"Region 0x{region_id:X} refresh ({req_label})",
            {"region_id": region_id, "request_type": request_type})
        logger.info("MeshWX refresh for region 0x%X (type=%d)", region_id, request_type)

    # Response cache for reliability over multi-hop mesh
    # Key: f"{data_type}:{loc_key}"
    # Value: (timestamp_unix, raw_wire_bytes)
    # Cached responses are re-broadcast on retry within TTL instead of
    # being silently dropped. TTL matches the old rate-limit window.
    _V2_CACHE_TTL_SECONDS = 300           # 5 minutes
    # How many times to transmit each on-demand response. Multi-hop mesh
    # flood routing can drop a single broadcast, so we send each response
    # twice with a short gap to give far-away clients a second chance.
    _V2_RESEND_COUNT = 2
    _V2_RESEND_GAP_SECONDS = 3

    async def respond_to_data_request(self, req: dict) -> None:
        """Handle a v2 data request (0x02) and broadcast the response.

        Reliability model:
          - Fresh request: build the message, cache it, send it N times
          - Retry within TTL: re-send the cached message N times (no rebuild)
          - After TTL: treat as fresh request again

        This gives far-away clients multiple chances to receive the
        broadcast through the mesh — each retry by the client is a real
        re-transmission, not a silent drop. Rebuilding is still gated by
        TTL so the encoder pipeline isn't thrashed.
        """
        data_type = req["data_type"]
        loc = req["location"]

        loc_key = self._location_key(loc)
        cache_key = f"{data_type}:{loc_key}"
        now = time.time()

        _dt_names = {0: "wx", 1: "forecast", 2: "outlook", 3: "storm_reports",
                     4: "rain_obs", 5: "metar", 6: "taf", 7: "warnings_near"}
        dt_name = _dt_names.get(data_type, f"0x{data_type:02x}")
        activity_log.record(EventDir.IN, "v2_request",
            f"Data request: {dt_name} for {loc_key}",
            {"data_type": data_type, "data_type_name": dt_name, "location": loc_key})

        if not hasattr(self, "_v2_cache"):
            self._v2_cache: dict[str, tuple[float, bytes]] = {}

        # Hit the cache? Rebroadcast the existing bytes — no rebuild needed.
        cached = self._v2_cache.get(cache_key)
        if cached and (now - cached[0]) < self._V2_CACHE_TTL_SECONDS:
            cached_msg = cached[1]
            logger.info(
                "Re-broadcasting cached v2 response type=0x%02x for %s (%d bytes, %ds old)",
                cached_msg[0], cache_key, len(cached_msg), int(now - cached[0]),
            )
            await self._transmit_response(cached_msg)
            return

        # Cache miss or TTL expired — build a fresh response.
        location_name = self._location_to_query_string(loc)
        if not location_name:
            logger.info(
                "v2 request for %s: location unresolvable — sending NOT_AVAILABLE",
                cache_key,
            )
            await self._emit_not_available(
                data_type, REASON_LOCATION_UNRESOLVABLE, loc, cache_key, now,
            )
            return

        msg = None
        builder_exception: Exception | None = None
        try:
            if data_type == DATA_WX:
                msg = self._build_observation(loc, location_name)
            elif data_type == DATA_FORECAST:
                msg = self._build_forecast(loc, location_name)
            elif data_type == DATA_METAR:
                msg = self._build_metar(loc, location_name)
            elif data_type == DATA_OUTLOOK:
                msg = self._build_outlook(loc, location_name)
            elif data_type == DATA_STORM_REPORTS:
                msg = self._build_storm_reports(loc, location_name)
            elif data_type == DATA_RAIN_OBS:
                msg = self._build_rain_obs(loc, location_name)
            elif data_type == DATA_TAF:
                msg = self._build_taf(loc, location_name)
            elif data_type == DATA_WARNINGS_NEAR:
                msg = self._build_warnings_near(loc, location_name)
            elif data_type == DATA_WARNING_DETAIL:
                msgs = self._build_warning_detail(loc, location_name)
                if msgs:
                    for m in msgs:
                        await self._transmit_response(m)
                        self._v2_cache[cache_key] = (now, m)
                    activity_log.record(EventDir.OUT, "v2_response",
                        f"Warning detail: {len(msgs)} chunk(s) for {loc_key}",
                        {"data_type": data_type, "location": loc_key, "chunks": len(msgs)})
                    return
                msg = None
            else:
                logger.info(
                    "v2 request for %s: unsupported data_type %d — sending NOT_AVAILABLE",
                    cache_key, data_type,
                )
                await self._emit_not_available(
                    data_type, REASON_PRODUCT_UNSUPPORTED, loc, cache_key, now,
                )
                return
        except Exception as exc:
            builder_exception = exc
            logger.exception("v2 builder raised for %s", cache_key)

        if builder_exception is not None:
            await self._emit_not_available(
                data_type, REASON_BOT_ERROR, loc, cache_key, now,
            )
            return

        if msg is None:
            logger.info(
                "v2 request for %s: no data available — sending NOT_AVAILABLE",
                cache_key,
            )
            await self._emit_not_available(
                data_type, REASON_NO_DATA, loc, cache_key, now,
            )
            return

        # Cache the built message so retries within TTL rebroadcast it
        self._v2_cache[cache_key] = (now, msg)
        logger.info(
            "Sent v2 response type=0x%02x for %s (%d bytes)",
            msg[0], cache_key, len(msg),
        )
        activity_log.record(EventDir.OUT, "v2_response",
            f"Response: {dt_name} for {loc_key} ({len(msg)}B)",
            {"data_type_name": dt_name, "location": loc_key, "bytes": len(msg), "msg_type": f"0x{msg[0]:02x}"})
        activity_log.record_send(1, len(msg))
        await self._transmit_response(msg)

    async def _emit_not_available(
        self,
        data_type: int,
        reason: int,
        loc: dict,
        cache_key: str,
        now: float,
    ) -> None:
        """Send a 0x03 MSG_NOT_AVAILABLE telling the client we can't serve
        the request. Cached and double-transmitted like any other response
        so retries within TTL will re-emit the same NOT_AVAILABLE without
        re-running the expensive resolver + builder path.
        """
        loc_type = loc.get("type")
        if loc_type == LOC_ZONE:
            loc_id = loc.get("zone", "")
        elif loc_type == LOC_STATION:
            loc_id = loc.get("station", "")
        elif loc_type == LOC_PFM_POINT:
            loc_id = loc.get("pfm_point_id", 0)
        elif loc_type == LOC_WFO:
            loc_id = loc.get("wfo", "")
        elif loc_type == LOC_LATLON:
            loc_id = (loc.get("lat", 0.0), loc.get("lon", 0.0))
        else:
            # LOC_PLACE or unknown — use zero-valued placeholder so pack
            # never raises. Client still gets enough info to correlate
            # via data_type + reason.
            loc_type = LOC_ZONE
            loc_id = "AKZ000"  # minimal valid zone code
        try:
            msg = pack_not_available(data_type, reason, loc_type, loc_id)
        except Exception:
            logger.exception("failed to pack NOT_AVAILABLE for %s", cache_key)
            return
        # Cache so retries within TTL reuse this same NOT_AVAILABLE
        self._v2_cache[cache_key] = (now, msg)
        await self._transmit_response(msg)

    async def _transmit_response(self, msg: bytes) -> None:
        """Send a response on the data channel.

        Sends the v3 message directly (COBS-encoded) without v4 wrapping
        to avoid exceeding the companion radio's 160-byte channel MTU.
        Sends N times for reliability over multi-hop mesh.
        """
        cobs_msg = cobs_encode(msg)
        for i in range(self._V2_RESEND_COUNT):
            try:
                await self.radio.send_binary_channel(cobs_msg)
            except Exception:
                logger.exception("v2 response send failed on transmission %d", i + 1)
                return
            if i + 1 < self._V2_RESEND_COUNT:
                await asyncio.sleep(self._V2_RESEND_GAP_SECONDS)

    def _location_key(self, loc: dict) -> str:
        """Stable string key for a location (used for rate limiting)."""
        t = loc.get("type")
        if t == LOC_ZONE:
            return f"zone:{loc.get('zone')}"
        if t == LOC_STATION:
            return f"station:{loc.get('station')}"
        if t == LOC_PFM_POINT:
            return f"pfm:{loc.get('pfm_point_id')}"
        return str(loc)

    def _location_to_query_string(self, loc: dict) -> str | None:
        """Convert a location dict to a string the WeatherStore can resolve.

        For LOC_PFM_POINT, looks up the index in the bundled pfm_points.json
        and returns the point's canonical zone code so the existing ZFP-based
        forecast path can use it directly.
        """
        t = loc.get("type")
        if t == LOC_ZONE:
            return loc.get("zone")
        if t == LOC_STATION:
            return loc.get("station")
        if t == LOC_PFM_POINT:
            # Delegate to the scheduler's pfm_points list (loaded once at
            # startup). Falls back to an empty list if the scheduler hasn't
            # started yet, which is fine — the request just returns None.
            points = self._scheduler._pfm_points
            idx = loc.get("pfm_point_id")
            if idx is None or idx < 0 or idx >= len(points):
                logger.debug("PFM point index %s out of range (0..%d)", idx, len(points))
                return None
            return points[idx].get("zone")
        return None

    def _build_observation(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x30 observation message for the given location.

        For LOC_PFM_POINT requests, the response carries LOC_PLACE in its
        location field so the client can display proper city names (from
        places.json) instead of NWS airport-oriented PFM point names.
        """
        resolved = resolver.resolve(query)
        if not resolved:
            return None

        # For PFM point requests, respond with LOC_PLACE so the client
        # gets a proper city name from places.json.  Use the PFM point's
        # own lat/lon (not the zone centroid) to find the nearest place.
        resp_loc_type = None
        resp_loc_id = None
        if loc.get("type") == LOC_PFM_POINT:
            idx = loc.get("pfm_point_id")
            points = self._scheduler._pfm_points
            if idx is not None and 0 <= idx < len(points):
                pt = points[idx]
                place_idx = resolver.find_place_index(pt["lat"], pt["lon"])
                if place_idx is not None:
                    resp_loc_type = LOC_PLACE
                    resp_loc_id = place_idx

        station = resolved.get("station")
        if station:
            raw = self.store._find_metar_raw(station)
            if raw:
                metar_text, _ts = raw
                msg = encode_metar(
                    station, metar_text, now_utc_minutes(),
                    loc_type=resp_loc_type, loc_id=resp_loc_id,
                )
                if msg:
                    return msg

        # Fall back: try RWR via WFO
        zones = resolved.get("zones", [])
        if zones:
            zone = zones[0]
            for wfo in resolved.get("wfos", []):
                state = zone[:2]
                rwr = self.store._find("RWR", f"{wfo}{state}")
                if rwr:
                    city = resolved["name"].split(",")[0].strip().upper()
                    line = self.store._parse_rwr_city_raw(rwr.raw_text, city)
                    if line:
                        return encode_rwr_city(
                            zone, line, now_utc_minutes(),
                            loc_type=resp_loc_type, loc_id=resp_loc_id,
                        )
        return None

    def _build_forecast(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x31 forecast message for the given location.

        Tries PFM (canonical NWS Point Forecast Matrix, structured numeric
        data) first via encode_forecast_from_pfm, then falls back to ZFP
        narrative parsing if no PFM product is available for the zone.
        Same 0x31 wire format on the output regardless of source — the
        client sees no difference, just better data quality when PFM is
        available.

        For LOC_PFM_POINT requests, the response carries LOC_PFM_POINT in
        its location field (not LOC_ZONE) so the client can correlate the
        broadcast with its original request.

        Uses _build_origs() + _find_any_orig() to handle the SJU→JSJ
        (San Juan PR) and GUM→GUA (Guam) AWIPS aliases — the resolver
        returns the canonical WFO code but EMWIN product filenames use
        the AWIPS alias.
        """
        resolved = resolver.resolve(query)
        if not resolved:
            return None
        zones = resolved.get("zones", [])
        if not zones:
            return None
        zone = zones[0]

        # If this was a LOC_PFM_POINT request, pass the PFM point ID through
        # to the encoder so the response echoes the requested location type.
        resp_loc_type = None
        resp_loc_id = None
        if loc.get("type") == LOC_PFM_POINT:
            resp_loc_type = LOC_PFM_POINT
            resp_loc_id = loc.get("pfm_point_id")

        origs = self.store._build_origs(resolved)

        # Primary path: PFM (structured numeric forecast data)
        pfm = self.store._find_any_orig("PFM", origs)
        if pfm:
            hours_ago = int(
                (now_utc_minutes() - pfm.timestamp.hour * 60 - pfm.timestamp.minute) / 60
            )
            msg = encode_forecast_from_pfm(
                pfm.raw_text, zone, max(0, hours_ago),
                loc_type=resp_loc_type,
                loc_id=resp_loc_id,
            )
            if msg is not None:
                logger.debug("forecast: PFM source for %s", zone)
                return msg
            logger.debug("forecast: PFM found for %s but no usable data", zone)

        # Fallback: ZFP narrative parsing
        zfp = self.store._find_any_orig("ZFP", origs)
        if zfp:
            zone_text = self.store._parse_zfp_zone(zfp.raw_text, zone)
            if zone_text:
                hours_ago = int(
                    (now_utc_minutes() - zfp.timestamp.hour * 60 - zfp.timestamp.minute) / 60
                )
                logger.debug("forecast: ZFP fallback for %s", zone)
                return encode_forecast_from_zfp(
                    zone, zfp.raw_text, max(0, hours_ago),
                    loc_type=resp_loc_type,
                    loc_id=resp_loc_id,
                )
        return None

    def _build_metar(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x35 METAR message (uses 0x30 observation format)."""
        # For now, same as observation for station queries
        return self._build_observation(loc, query)

    def _build_outlook(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x32 Hazardous Weather Outlook for the given location.

        Looks up the latest HWO product for the location's WFO and runs
        encode_hwo() to produce the outlook message. HWOs are typically
        issued once daily by each WFO and cover days 1-7.
        """
        resolved = resolver.resolve(query)
        if not resolved:
            return None
        zones = resolved.get("zones") or []
        if not zones:
            return None
        zone = zones[0]

        origs = self.store._build_origs(resolved)
        hwo = self.store._find_any_orig("HWO", origs)
        if hwo is None:
            # Wider fallback: any HWO whose UGC line covers our zone
            from meshcore_weather.parser.weather import _expand_zone_ranges
            loc_zones = set(zones)
            best = None
            for prod in self.store._products.values():
                if prod.product_type != "HWO":
                    continue
                if loc_zones & _expand_zone_ranges(prod.raw_text):
                    if best is None or prod.timestamp > best.timestamp:
                        best = prod
            hwo = best
        if hwo is None:
            return None

        issued_min = hwo.timestamp.hour * 60 + hwo.timestamp.minute
        return encode_hwo(zone, hwo.raw_text, issued_min)

    def _build_storm_reports(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x33 Local Storm Reports message for the location.

        Walks LSR products in the store, filters to entries from the
        location's state, and runs encode_lsr_reports() to pack up to 16
        most recent reports into the wire format.
        """
        resolved = resolver.resolve(query)
        if not resolved:
            return None
        zones = resolved.get("zones") or []
        if not zones:
            return None
        zone = zones[0]
        state = zone[:2]

        # Collect deduplicated entries from LSR products newest first
        seen: set[str] = set()
        entries: list[dict] = []
        for prod in sorted(
            self.store._products.values(), key=lambda p: p.timestamp, reverse=True
        ):
            if prod.product_type != "LSR":
                continue
            # Filter by state. We accept either an exact filename-state match
            # or a text-derived affected state, since LSR filenames don't
            # always agree with the state of the actual report.
            if prod.state != state:
                affected = self.store._affected_state(prod)
                if affected != state:
                    continue
            for entry in self.store._parse_lsr_entries(prod.raw_text):
                # Filter to reports actually in the requested state
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
            return None
        return encode_lsr_reports(zone, entries, now_utc_minutes())

    def _build_rain_obs(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x34 rain observations message for the location's region.

        Scans RWR (Regional Weather Roundup) products from the location's
        WFO for cities currently reporting any form of precipitation.
        Encodes the list as 0x34 with each city referenced by its place_id.
        """
        resolved = resolver.resolve(query)
        if not resolved:
            return None
        zones = resolved.get("zones") or []
        if not zones:
            return None
        zone = zones[0]

        origs = self.store._build_origs(resolved)
        rwr = self.store._find_any_orig("RWR", origs)
        if rwr is None:
            return None

        # Scan the RWR table for cities with precipitation. Reuses the same
        # heuristic as WeatherStore.scan_rain but extracts structured data
        # instead of pre-formatted text.
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
            # Strip the leading "*" flag (NWS RWR uses it to mark significant
            # weather rows) from the first part so the city name is clean
            # for the place_id lookup.
            if parts and parts[0].startswith("*"):
                parts[0] = parts[0][1:]
            # Walk forward extracting city name until we hit a sky-word or a number
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
            # First number after the sky word is the temperature
            if rain_text:
                idx = parts.index(rain_text) if rain_text in parts else -1
                for tp in parts[idx + 1 :]:
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
            return None
        return encode_rain_cities(zone, rainy, now_utc_minutes())

    def _build_taf(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x36 TAF (Terminal Aerodrome Forecast) message.

        TAF is keyed to a station ICAO. Walks the store for any TAF product
        that contains a TAF block for the requested station, then runs
        encode_taf() to extract the BASE forecast group and pack it as 0x36.
        """
        # TAF is station-keyed. Resolve the location to a station.
        if loc.get("type") == LOC_STATION:
            station = loc.get("station")
        else:
            resolved = resolver.resolve(query)
            if not resolved:
                return None
            station = resolved.get("station")
        if not station:
            return None

        # Find a product whose text contains a TAF block for this station.
        # NWS TAF products use AFOS like "TAFEWX" with multiple stations
        # in one file, so we have to scan rather than direct-lookup.
        target_marker = f"TAF {station}"
        amend_marker = f"TAF AMD {station}"
        candidate = None
        for prod in sorted(
            self.store._products.values(), key=lambda p: p.timestamp, reverse=True
        ):
            if prod.product_type != "TAF":
                continue
            text = prod.raw_text
            if target_marker in text or amend_marker in text or f"\n{station} " in text:
                candidate = prod
                break
        if candidate is None:
            return None

        issued_hours_ago = max(
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
        return encode_taf(station, candidate.raw_text, issued_hours_ago)

    def _build_warnings_near(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x37 'warnings near location' summary.

        Pulls all currently-active warnings from extract_active_warnings(),
        filters to those that affect the requested location's zone, and packs
        them as a compact 0x37 reply with type/severity/expiry per entry.

        Works for both land zones (which the resolver knows about via
        zones.json) AND marine zones (PMZ###, GMZ###, etc. which aren't
        in the resolver but ARE valid UGC codes that pyIEM extracts from
        warning products). For marine zones we skip the polygon fallback
        and rely purely on UGC code matching.
        """
        # If the request was for a bare zone code, use it directly without
        # going through the resolver — that lets us serve marine zones
        # (PMZ172 etc.) which aren't in zones.json.
        zone: str = ""
        if loc.get("type") == LOC_ZONE:
            zone = loc.get("zone", "")
        if not zone:
            resolved = resolver.resolve(query)
            if not resolved:
                return None
            zones_list = resolved.get("zones") or []
            if not zones_list:
                return None
            zone = zones_list[0]

        all_warnings = extract_active_warnings(self.store, coverage=None)
        # Filter to warnings whose UGCs include our zone (or whose polygon
        # contains the zone's centroid as a fallback for land zones).
        nearby: list[dict] = []
        z_meta = resolver._zones.get(zone, {})
        z_lat = z_meta.get("la", 0.0)
        z_lon = z_meta.get("lo", 0.0)
        for w in all_warnings:
            ugcs = set(w.get("ugcs") or w.get("zones", []))
            in_zone = zone in ugcs
            if not in_zone and w.get("vertices"):
                # Polygon containment check (point-in-polygon for the zone centroid)
                from meshcore_weather.protocol.coverage import _point_in_polygon
                if _point_in_polygon(z_lat, z_lon, w["vertices"]):
                    in_zone = True
            if not in_zone:
                continue
            # Pick a representative zone for the per-entry zone reference
            entry_zone = zone if zone in ugcs else (sorted(ugcs)[0] if ugcs else "")
            expires_at = w.get("expires_at")
            expires_unix_min = (
                int(expires_at.timestamp() / 60) if expires_at else 0
            )
            nearby.append({
                "warning_type": w.get("warning_type", 0),
                "severity": w.get("severity", SEV_WARNING),
                "expires_unix_min": expires_unix_min,
                "zone": entry_zone if len(entry_zone) == 6 and entry_zone[2] == "Z" else "",
            })

        if not nearby:
            return None
        return pack_warnings_near(LOC_ZONE, zone, nearby)

    def _build_warning_detail(self, loc: dict, query: str) -> list[bytes] | None:
        """Build 0x40 text chunks with warning description for a zone.

        The client sends this request after receiving a compact 0x20/0x21
        warning and wanting the full detail (wind, humidity, impacts, etc.).
        Returns text chunks for all warnings affecting the zone.
        """
        from meshcore_weather.protocol.meshwx import (
            LOC_WFO, TEXT_SUBJECT_GENERAL, pack_text_chunks,
        )

        zone = ""
        if loc.get("type") == LOC_ZONE:
            zone = loc.get("zone", "")
        if not zone:
            resolved = resolver.resolve(query)
            if not resolved:
                return None
            zones_list = resolved.get("zones") or []
            if not zones_list:
                return None
            zone = zones_list[0]

        all_warnings = extract_active_warnings(self.store, coverage=None)

        descriptions: list[str] = []
        for w in all_warnings:
            ugcs = set(w.get("ugcs") or w.get("zones", []))
            if zone not in ugcs:
                continue
            desc = w.get("description", "")
            headline = w.get("headline", "")
            if desc:
                descriptions.append(f"{headline}\n{desc}" if headline else desc)
            elif headline:
                descriptions.append(headline)

        if not descriptions:
            return None

        full_text = "\n---\n".join(descriptions)
        wfo = "UNK"
        # Use the first warning's office
        for w in all_warnings:
            if w.get("vtec_office"):
                wfo = w["vtec_office"]
                break

        return pack_text_chunks(
            subject_type=TEXT_SUBJECT_GENERAL,
            loc_type=LOC_WFO,
            loc_id=wfo,
            text=full_text,
        )
