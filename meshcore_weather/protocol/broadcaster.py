"""MeshWX broadcast loop: periodically sends binary weather data on the data channel."""

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from meshcore_weather.config import settings
from meshcore_weather.geodata import resolver
from meshcore_weather.meshcore.radio import MeshcoreRadio
from meshcore_weather.parser.weather import WeatherStore
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.encoders import (
    encode_forecast_from_zfp,
    encode_metar,
    encode_rwr_city,
    now_utc_minutes,
)
from meshcore_weather.protocol.meshwx import (
    DATA_FORECAST,
    DATA_METAR,
    DATA_WX,
    LOC_PFM_POINT,
    LOC_STATION,
    LOC_ZONE,
    cobs_encode,
)
from meshcore_weather.protocol.radar import fetch_radar_composite, build_radar_messages
from meshcore_weather.protocol.warnings import extract_active_warnings, warnings_to_binary

logger = logging.getLogger(__name__)

# Delay between consecutive LoRa transmissions (seconds)
TX_SPACING = 2


class MeshWXBroadcaster:
    """Broadcasts binary weather data on the MeshWX data channel."""

    def __init__(self, store: WeatherStore, radio: MeshcoreRadio):
        self.store = store
        self.radio = radio
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_refresh: dict[int, float] = {}  # region_id -> timestamp
        self._http_client: httpx.AsyncClient | None = None
        self._latest_radar: tuple[bytes, int] | None = None  # (img_bytes, ts_min)
        self._coverage: Coverage = Coverage.empty()
        self._pfm_points: list[dict] | None = None  # loaded lazily from bundle
        # client_data/ ships inside the package (see pyproject.toml
        # package-data), so it travels with the pip install regardless of
        # deployment location (Docker, bare install, editable mode).
        # __file__ = .../meshcore_weather/protocol/broadcaster.py
        # .parent.parent = .../meshcore_weather
        self._pfm_points_path = (
            Path(__file__).resolve().parent.parent
            / "client_data"
            / "pfm_points.json"
        )

    def _load_pfm_points(self) -> list[dict]:
        """Load pfm_points.json once and cache. Returns empty list if missing."""
        if self._pfm_points is not None:
            return self._pfm_points
        if not self._pfm_points_path.exists():
            logger.warning(
                "pfm_points.json not found at %s — LOC_PFM_POINT requests will fail",
                self._pfm_points_path,
            )
            self._pfm_points = []
            return self._pfm_points
        try:
            data = json.loads(self._pfm_points_path.read_text())
            # Compact array form: [[name, wfo, lat, lon, zone], ...]
            points = [
                {"name": p[0], "wfo": p[1], "lat": p[2], "lon": p[3], "zone": p[4]}
                for p in data.get("points", [])
            ]
            self._pfm_points = points
            logger.info("Loaded %d PFM points from bundle", len(points))
            return self._pfm_points
        except Exception as exc:
            logger.warning("Failed to load pfm_points.json: %s", exc)
            self._pfm_points = []
            return self._pfm_points

    def reload_coverage(self) -> None:
        """Rebuild coverage from current settings. Called on startup + config changes."""
        self._coverage = Coverage.from_config()
        logger.info("Coverage: %s", self._coverage.summary())

    @property
    def coverage(self) -> Coverage:
        return self._coverage

    async def start(self) -> None:
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self.reload_coverage()
        self._task = asyncio.create_task(self._broadcast_loop())
        logger.info("MeshWX broadcaster started (interval=%ds)", settings.meshwx_broadcast_interval)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._http_client:
            await self._http_client.aclose()

    async def _broadcast_loop(self) -> None:
        # Wait a bit on startup before first broadcast
        await asyncio.sleep(30)
        while self._running:
            try:
                await self._broadcast_all()
            except Exception:
                logger.exception("Error in MeshWX broadcast cycle")
            await asyncio.sleep(settings.meshwx_broadcast_interval)

    async def _broadcast_all(self) -> None:
        """Run one full broadcast cycle: radar + warnings."""
        radar_count = await self._broadcast_radar()
        warn_count = await self._broadcast_warnings()
        if radar_count or warn_count:
            logger.info("MeshWX broadcast: %d radar grid(s), %d warning(s)",
                        radar_count, warn_count)

    async def _fetch_radar(self) -> None:
        """Fetch latest radar composite from IEM."""
        if not self._http_client:
            return
        result = await fetch_radar_composite(self._http_client)
        if result:
            self._latest_radar = result

    async def _broadcast_radar(self) -> int:
        """Fetch and broadcast COBS-encoded radar grids (filtered by coverage)."""
        await self._fetch_radar()
        if not self._latest_radar:
            return 0
        img_data, ts_min = self._latest_radar
        region_ids = self._coverage.region_ids if not self._coverage.is_empty() else None
        msgs = build_radar_messages(img_data, ts_min, region_ids=region_ids)
        sent = 0
        for msg in msgs:
            await self.radio.send_binary_channel(cobs_encode(msg))
            sent += 1
            await asyncio.sleep(TX_SPACING)
        return sent

    async def _broadcast_warnings(self) -> int:
        """Broadcast COBS-encoded warning polygons (filtered by coverage)."""
        warnings = extract_active_warnings(self.store, coverage=self._coverage)
        msgs = warnings_to_binary(warnings)
        sent = 0
        for msg in msgs:
            await self.radio.send_binary_channel(cobs_encode(msg))
            sent += 1
            if sent < len(msgs):
                await asyncio.sleep(TX_SPACING)
        return sent

    async def broadcast_region(self, region_id: int, request_type: int = 3) -> None:
        """Broadcast data for a specific region (triggered by refresh request).

        request_type: 1=radar only, 2=warnings only, 3=both.
        """
        now = time.time()
        last = self._last_refresh.get(region_id, 0)
        if now - last < settings.meshwx_refresh_cooldown:
            logger.debug("Refresh for region 0x%X throttled (cooldown)", region_id)
            return
        self._last_refresh[region_id] = now

        if request_type in (1, 3):
            await self._fetch_radar()
            if self._latest_radar:
                from meshcore_weather.protocol.radar import extract_region_grid
                from meshcore_weather.protocol.meshwx import pack_radar_grid, REGIONS
                img_data, ts_min = self._latest_radar
                grid = extract_region_grid(img_data, region_id)
                if grid:
                    region = REGIONS[region_id]
                    msg = pack_radar_grid(region_id, 0, ts_min, region["scale"], grid)
                    await self.radio.send_binary_channel(cobs_encode(msg))

        if request_type in (2, 3):
            await self._broadcast_warnings()

        logger.info("MeshWX refresh for region 0x%X (type=%d)", region_id, request_type)

    async def respond_to_data_request(self, req: dict) -> None:
        """Handle a v2 data request (0x02) and broadcast the response.

        Rate-limited per (data_type, location) for 5 minutes.
        Looks up the right data from the weather store and encodes it
        using the v2 encoders, then broadcasts on the data channel.
        """
        data_type = req["data_type"]
        loc = req["location"]

        # Rate-limit key
        loc_key = self._location_key(loc)
        rate_key = f"{data_type}:{loc_key}"
        now = time.time()
        if not hasattr(self, "_v2_rate_limit"):
            self._v2_rate_limit: dict[str, float] = {}
        last = self._v2_rate_limit.get(rate_key, 0)
        if now - last < 300:  # 5 min per (type, location)
            logger.debug("v2 request throttled: %s", rate_key)
            return
        self._v2_rate_limit[rate_key] = now

        # Resolve the location dict to something the store can query
        location_name = self._location_to_query_string(loc)
        if not location_name:
            logger.debug("Could not resolve location for v2 request: %s", loc)
            return

        msg = None
        if data_type == DATA_WX:
            msg = self._build_observation(loc, location_name)
        elif data_type == DATA_FORECAST:
            msg = self._build_forecast(loc, location_name)
        elif data_type == DATA_METAR:
            msg = self._build_metar(loc, location_name)
        else:
            logger.debug("Unsupported v2 data type: %d", data_type)
            return

        if msg is None:
            logger.info("No v2 response data available for %s", rate_key)
            return

        await self.radio.send_binary_channel(cobs_encode(msg))
        logger.info("Sent v2 response type=0x%02x for %s (%d bytes)",
                    msg[0], rate_key, len(msg))

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
            points = self._load_pfm_points()
            idx = loc.get("pfm_point_id")
            if idx is None or idx < 0 or idx >= len(points):
                logger.debug("PFM point index %s out of range (0..%d)", idx, len(points))
                return None
            # Use the canonical zone from the PFM point for ZFP lookup.
            # Phase 2 will swap this for a direct PFM product lookup, but the
            # wire format won't change.
            return points[idx].get("zone")
        return None

    def _build_observation(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x30 observation message for the given location."""
        resolved = resolver.resolve(query)
        if not resolved:
            return None

        station = resolved.get("station")
        if station:
            raw = self.store._find_metar_raw(station)
            if raw:
                metar_text, _ts = raw
                msg = encode_metar(station, metar_text, now_utc_minutes())
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
                        return encode_rwr_city(zone, line, now_utc_minutes())
        return None

    def _build_forecast(self, loc: dict, query: str) -> bytes | None:
        """Build a 0x31 forecast message for the given location.

        For LOC_PFM_POINT requests, the response will carry LOC_PFM_POINT
        in its location field (not LOC_ZONE) so the client can correlate
        the broadcast with its original request. Under the hood we still
        use the existing ZFP-based forecast path — data quality will
        improve to PFM-sourced structured data in Commit 2 without any
        wire format change.
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

        # Find the ZFP product. Use _build_origs() + _find_any_orig() which
        # handle the SJU→JSJ (San Juan PR) and GUM→GUA (Guam) AWIPS aliases
        # — the resolver returns the canonical WFO code ("SJU") but the
        # EMWIN product filenames use the AWIPS alias ("JSJ"). Constructing
        # "{wfo}{state}" directly from the resolver output would fail for
        # every Puerto Rico and Guam forecast request.
        origs = self.store._build_origs(resolved)
        zfp = self.store._find_any_orig("ZFP", origs)
        if zfp:
            zone_text = self.store._parse_zfp_zone(zfp.raw_text, zone)
            if zone_text:
                hours_ago = int(
                    (now_utc_minutes() - zfp.timestamp.hour * 60 - zfp.timestamp.minute) / 60
                )
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
