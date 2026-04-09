"""MeshWX broadcast loop: periodically sends binary weather data on the data channel."""

import asyncio
import logging
import time

import httpx

from meshcore_weather.config import settings
from meshcore_weather.meshcore.radio import MeshcoreRadio
from meshcore_weather.parser.weather import WeatherStore
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

    async def start(self) -> None:
        self._running = True
        self._http_client = httpx.AsyncClient(timeout=30.0)
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
        """Fetch and broadcast radar grids for all regions with precipitation."""
        await self._fetch_radar()
        if not self._latest_radar:
            return 0
        img_data, ts_min = self._latest_radar
        msgs = build_radar_messages(img_data, ts_min)
        sent = 0
        for msg in msgs:
            await self.radio.send_binary_channel(msg)
            sent += 1
            await asyncio.sleep(TX_SPACING)
        return sent

    async def _broadcast_warnings(self) -> int:
        """Broadcast all active warning polygons on the data channel."""
        warnings = extract_active_warnings(self.store)
        msgs = warnings_to_binary(warnings)
        sent = 0
        for msg in msgs:
            await self.radio.send_binary_channel(msg)
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
                    await self.radio.send_binary_channel(msg)

        if request_type in (2, 3):
            await self._broadcast_warnings()

        logger.info("MeshWX refresh for region 0x%X (type=%d)", region_id, request_type)
