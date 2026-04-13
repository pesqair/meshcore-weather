"""Broadcast scheduler — drives the periodic broadcast cycle.

The scheduler owns a `BroadcastConfig` (list of jobs) and a reference
to a `BroadcastExecutor` that actually builds the wire messages. On
each `tick()`, it:

  1. Reloads the config from disk (picks up portal edits without restart)
  2. Fetches the latest radar composite (once per tick, shared by all
     radar jobs in this cycle)
  3. Walks each enabled job and runs any whose interval has elapsed
  4. Sends each built message on the data channel via the radio
  5. Tracks per-job last-run and total-bytes-sent stats

The scheduler is the ONLY place that talks to the radio for proactive
broadcasts. The separate `respond_to_data_request()` reactive path is
unchanged — it still sends directly when an iOS client asks for
something.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

import httpx

from meshcore_weather.activity import EventDir, activity_log
from meshcore_weather.config import settings
from meshcore_weather.meshcore.radio import MeshcoreRadio
from meshcore_weather.parser.weather import WeatherStore
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.meshwx import V4SequenceCounter, cobs_encode, v4_wrap

# Products that get FEC treatment on the v4 channel.
# For these, the scheduler builds separate FEC messages instead of
# just wrapping v3 messages with the v4 header.
_FEC_PRODUCTS = {"radar", "afd"}
from meshcore_weather.protocol.radar import fetch_radar_composite
from meshcore_weather.schedule.executor import BroadcastExecutor, ExecutorContext
from meshcore_weather.schedule.models import BroadcastConfig, BroadcastJob
from meshcore_weather.schedule.store import CONFIG_PATH, load_config, save_config

logger = logging.getLogger(__name__)

# Delay between consecutive LoRa transmissions (seconds) — avoid bursting
# many messages onto the channel within the same airtime window
TX_SPACING = 2

# How often the scheduler tick() runs. Not the same as a job's interval —
# the tick is how often we CHECK for due jobs. A job with a 60-min interval
# won't run every 30s, but the scheduler still ticks every 30s to minimize
# drift on when that 60-min job actually fires.
TICK_INTERVAL_SECONDS = 30


class Scheduler:
    """Owns the broadcast schedule and drives its execution.

    The main entry point is `start()`, which launches an asyncio task
    that calls `tick()` on a fixed cadence until `stop()` is called.
    """

    def __init__(self, store: WeatherStore, radio: MeshcoreRadio):
        self.store = store
        self.radio = radio
        self.executor = BroadcastExecutor()

        self._config: BroadcastConfig = BroadcastConfig()
        self._coverage: Coverage = Coverage.empty()
        self._pfm_points: list[dict] = []

        # Runtime state (not persisted)
        self._last_run: dict[str, float] = {}    # job_id → unix timestamp
        self._last_bytes: dict[str, int] = {}    # job_id → bytes sent last run
        self._total_bytes: dict[str, int] = {}   # job_id → cumulative bytes
        self._total_runs: dict[str, int] = {}    # job_id → run count
        self._last_msg_count: dict[str, int] = {}  # job_id → messages sent last run
        self._config_mtime: float = 0.0
        self._config_lock = asyncio.Lock()

        self._http_client: httpx.AsyncClient | None = None
        self._latest_radar: tuple[bytes, int] | None = None

        # Warning change tracking — persists across ticks so the delta
        # builder can compare current vs last-broadcast warning sets.
        # Key: warning identity (VTEC key string)
        # Value: (expires_unix_min, headline_hash)
        self._warning_tracking: dict[str, tuple[int, int]] = {}

        # v4 sequence counter — monotonic across all messages on the v4 channel
        self._v4_seq = V4SequenceCounter()

        self._task: asyncio.Task | None = None
        self._running = False

    # -- Lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Load config + coverage + PFM points, then start the tick loop."""
        self._http_client = httpx.AsyncClient(timeout=30.0)
        self.reload_coverage()
        self._load_pfm_points()
        await self._reload_config()

        self._running = True
        self._task = asyncio.create_task(self._tick_loop())
        logger.info(
            "Broadcast scheduler started: %d jobs, tick every %ds",
            len(self._config.jobs), TICK_INTERVAL_SECONDS,
        )

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

    # -- Coverage ------------------------------------------------------------

    def reload_coverage(self) -> None:
        """Rebuild coverage from current settings. Called on startup and
        when the operator changes MCW_HOME_* env vars (restart required
        for those, but the portal may still trigger this internally)."""
        self._coverage = Coverage.from_config()
        logger.info("Coverage: %s", self._coverage.summary())

    @property
    def coverage(self) -> Coverage:
        return self._coverage

    # -- PFM points ----------------------------------------------------------

    def _load_pfm_points(self) -> None:
        """Load pfm_points.json from the bundled client_data/ directory."""
        path = (
            Path(__file__).resolve().parent.parent
            / "client_data" / "pfm_points.json"
        )
        if not path.exists():
            logger.warning("pfm_points.json not found at %s", path)
            self._pfm_points = []
            return
        try:
            data = json.loads(path.read_text())
            self._pfm_points = [
                {"name": p[0], "wfo": p[1], "lat": p[2], "lon": p[3], "zone": p[4]}
                for p in data.get("points", [])
            ]
            logger.info("Loaded %d PFM points", len(self._pfm_points))
        except Exception as exc:
            logger.warning("Failed to load pfm_points.json: %s", exc)
            self._pfm_points = []

    # -- Config reload --------------------------------------------------------

    async def _reload_config(self) -> None:
        """Re-read broadcast_config.json if it has been modified since
        the last load. Called on startup and at the top of each tick so
        portal edits take effect without a bot restart."""
        async with self._config_lock:
            try:
                mtime = CONFIG_PATH.stat().st_mtime if CONFIG_PATH.exists() else 0.0
            except OSError:
                mtime = 0.0
            if mtime == self._config_mtime and self._config.jobs:
                # Already loaded this version
                return
            self._config = load_config()
            self._config_mtime = mtime
            logger.debug("Config reloaded: %d jobs", len(self._config.jobs))

    def current_config(self) -> BroadcastConfig:
        """Return the currently-loaded config. Portal routes use this."""
        return self._config

    async def save_config(self, cfg: BroadcastConfig) -> None:
        """Persist an updated config from the portal and refresh local state."""
        async with self._config_lock:
            save_config(cfg)
            self._config = cfg
            try:
                self._config_mtime = CONFIG_PATH.stat().st_mtime
            except OSError:
                self._config_mtime = time.time()

    # -- Tick loop ------------------------------------------------------------

    async def _tick_loop(self) -> None:
        # Wait a bit on startup before the first tick so the radio + store
        # have time to warm up
        await asyncio.sleep(15)
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler tick crashed")
            await asyncio.sleep(TICK_INTERVAL_SECONDS)

    async def tick(self) -> int:
        """One scheduler cycle. Returns the number of messages transmitted.

        Each call:
          1. Reloads config from disk if it's been modified
          2. Refreshes radar (once, shared across all radar jobs this tick)
          3. Walks all enabled jobs and runs due ones
          4. Transmits their messages with TX_SPACING between
        """
        await self._reload_config()

        # Refresh radar once per tick. If any radar jobs are due, they
        # use the cached composite from this tick.
        await self._refresh_radar()

        ctx = ExecutorContext(
            store=self.store,
            coverage=self._coverage,
            pfm_points=self._pfm_points,
            latest_radar=self._latest_radar,
            last_broadcast_warnings=self._warning_tracking,
        )

        now = time.time()
        total_sent = 0

        for job in list(self._config.jobs):  # list() to allow concurrent edits
            if not job.enabled:
                continue
            last_run = self._last_run.get(job.id, 0.0)
            elapsed = now - last_run
            if elapsed < job.interval_minutes * 60:
                continue

            # Due — run it
            try:
                msgs = self.executor.run_job(job, ctx)
            except Exception:
                logger.exception("Scheduler: exception running job %s", job.id)
                msgs = []

            self._last_run[job.id] = now
            self._total_runs[job.id] = self._total_runs.get(job.id, 0) + 1
            self._last_msg_count[job.id] = len(msgs)

            if not msgs:
                logger.debug("job %s: no data available this cycle", job.id)
                self._last_bytes[job.id] = 0
                continue

            # Build v4 messages — FEC for eligible products, v4 wrapping for others
            if job.product in _FEC_PRODUCTS:
                v4_msgs = self._build_fec_messages(job, ctx)
            else:
                v4_msgs = [v4_wrap(m, self._v4_seq.next()) for m in msgs]

            sent_bytes = 0
            for v4_msg in v4_msgs:
                try:
                    await self.radio.send_binary_channel(cobs_encode(v4_msg))
                    sent_bytes += len(v4_msg)
                    total_sent += 1
                    await asyncio.sleep(TX_SPACING)
                except Exception:
                    logger.exception("Scheduler: send failed for job %s", job.id)

            self._last_bytes[job.id] = sent_bytes
            self._total_bytes[job.id] = self._total_bytes.get(job.id, 0) + sent_bytes
            self._last_msg_count[job.id] = len(v4_msgs)
            activity_log.record(EventDir.OUT, "broadcast",
                f"Job {job.id}: {len(v4_msgs)} msg(s), {sent_bytes}B ({job.product})",
                {"job_id": job.id, "product": job.product, "messages": len(v4_msgs), "bytes": sent_bytes})
            activity_log.record_send(len(v4_msgs), sent_bytes)
            logger.info(
                "scheduler: %s → %d msg(s), %d bytes (%s)",
                job.id, len(msgs), sent_bytes, job.product,
            )

        return total_sent

    def _build_fec_messages(
        self, job: BroadcastJob, ctx: ExecutorContext,
    ) -> list[bytes]:
        """Build FEC-wrapped v4 messages for products that benefit from it.

        Radar 64×64 → spatial quadrants + XOR parity (6 msgs per region)
        AFD → per-section + XOR parity
        Falls back to simple v4 wrapping if FEC can't be applied.
        """
        if job.product == "radar" and ctx.latest_radar is not None:
            # Only apply FEC to 64×64 radar jobs
            loc_id = job.location_id.strip()
            grid_size = 32
            if ":" in loc_id:
                parts = loc_id.split(":", 1)
                loc_id = parts[0].strip()
                try:
                    grid_size = int(parts[1].strip())
                except ValueError:
                    pass

            if grid_size == 64:
                from meshcore_weather.protocol.radar import build_fec_radar_messages
                img, ts_min = ctx.latest_radar
                # Use the specific region from the job, not all coverage regions
                if job.location_type == "region" and loc_id:
                    try:
                        region_ids = {int(loc_id, 0)}
                    except (ValueError, TypeError):
                        region_ids = None
                else:
                    region_ids = (
                        ctx.coverage.region_ids
                        if not ctx.coverage.is_empty()
                        else None
                    )
                msgs = build_fec_radar_messages(
                    img, ts_min, self._v4_seq,
                    region_ids=region_ids,
                )
                if msgs:
                    logger.info("FEC radar: %d messages for region(s) %s",
                                len(msgs), region_ids)
                    return msgs

        if job.product == "afd":
            from meshcore_weather.protocol.encoders import encode_afd_fec
            from meshcore_weather.geodata import resolver

            query = job.location_id.strip() if job.location_type == "city" else ""
            if not query:
                return []
            resolved = resolver.resolve(query)
            if not resolved:
                return []
            origs = ctx.store._build_origs(resolved)
            afd = ctx.store._find_any_orig("AFD", origs)
            if afd is None:
                return []
            wfos = resolved.get("wfos", [])
            wfo = wfos[0] if wfos else "UNK"
            msgs = encode_afd_fec(wfo, afd.raw_text, self._v4_seq)
            if msgs:
                return msgs

        # Fallback: simple v4 wrapping (re-run the v3 builder)
        v3_msgs = self.executor.run_job(job, ctx) or []
        return [v4_wrap(m, self._v4_seq.next()) for m in v3_msgs]

    async def _refresh_radar(self) -> None:
        """Fetch the latest radar composite if we don't have one or it's stale."""
        if self._http_client is None:
            return
        result = await fetch_radar_composite(self._http_client)
        if result:
            self._latest_radar = result

    # -- Discovery ping/response -----------------------------------------------

    async def respond_to_discovery_ping(self) -> None:
        """Respond to a client ping on #meshwx-discover with our beacon.

        Called by the bot's channel message handler when it sees a ping
        on the discovery channel. All bots on the channel respond, so
        the client collects responses and presents a list.

        Random delay (1-5s) so multiple bots don't collide on air.
        """
        import random
        await asyncio.sleep(random.uniform(1.0, 5.0))

        from meshcore_weather.protocol.meshwx import (
            BEACON_ACCEPTING_REQUESTS, BEACON_HAS_FORECASTS,
            BEACON_HAS_FIRE_WEATHER, BEACON_HAS_NOWCAST,
            BEACON_HAS_QPF, BEACON_HAS_RADAR, BEACON_HAS_WARNINGS,
            cobs_encode, pack_beacon,
        )

        # Build beacon flags from what products the bot actually has
        flags = BEACON_ACCEPTING_REQUESTS | BEACON_HAS_FORECASTS | BEACON_HAS_WARNINGS
        if self._latest_radar is not None:
            flags |= BEACON_HAS_RADAR
        # Check if we have FWF/NOW/QPF products in the store
        for prod in self.store._products.values():
            if prod.product_type == "FWF":
                flags |= BEACON_HAS_FIRE_WEATHER
            elif prod.product_type == "NOW":
                flags |= BEACON_HAS_NOWCAST

        # Coverage center and radius
        lat, lon, radius_km = 0.0, 0.0, 0
        if not self._coverage.is_empty():
            bbox = self._coverage.bbox
            if bbox:
                lat = (bbox[0] + bbox[1]) / 2  # (south + north) / 2
                lon = (bbox[2] + bbox[3]) / 2  # (west + east) / 2
                # Rough radius from bbox
                import math
                dlat = (bbox[1] - bbox[0]) * 111 / 2
                dlon = (bbox[3] - bbox[2]) * 111 * math.cos(math.radians(lat)) / 2
                radius_km = int(max(dlat, dlon))

        # Active warnings count
        from meshcore_weather.protocol.warnings import extract_active_warnings
        warnings = extract_active_warnings(self.store, self._coverage)
        warning_count = len(warnings) if warnings else 0

        # Bot ID from radio (hash of node info)
        bot_id = 0
        if self.radio._mc and self.radio._mc.self_info:
            name = self.radio._mc.self_info.get("adv_name", "")
            bot_id = hash(name) & 0xFFFFFF

        # Channel name (the data channel clients should join)
        channel_name = settings.meshwx_channel.lstrip("#") if settings.meshwx_channel else ""

        beacon = pack_beacon(
            bot_id=bot_id,
            beacon_flags=flags,
            lat=lat, lon=lon,
            radius_km=min(255, radius_km),
            active_warnings=warning_count,
            channel_name=channel_name,
        )
        await self.radio.send_beacon(cobs_encode(beacon))
        activity_log.record(EventDir.OUT, "beacon",
            f"Beacon: {len(beacon)}B, {warning_count} warnings, radius {radius_km}km",
            {"bytes": len(beacon), "warnings": warning_count})

    # -- Stats (used by the portal /schedule page) ----------------------------

    def job_status(self, job_id: str) -> dict:
        """Return runtime status for one job: last run, bytes, next run."""
        now = time.time()
        job = self._config.get_job(job_id)
        if job is None:
            return {"job_id": job_id, "found": False}
        last_run = self._last_run.get(job_id, 0.0)
        next_due = last_run + job.interval_minutes * 60 if last_run else now
        return {
            "job_id": job_id,
            "found": True,
            "enabled": job.enabled,
            "last_run_unix": last_run if last_run else None,
            "last_run_seconds_ago": int(now - last_run) if last_run else None,
            "next_run_unix": next_due,
            "next_run_in_seconds": max(0, int(next_due - now)) if last_run else 0,
            "total_runs": self._total_runs.get(job_id, 0),
            "total_bytes": self._total_bytes.get(job_id, 0),
            "last_bytes": self._last_bytes.get(job_id, 0),
            "last_msg_count": self._last_msg_count.get(job_id, 0),
        }

    async def run_job_now(self, job_id: str) -> int:
        """Force-run a specific job immediately, ignoring its interval.

        Used by the portal's 'Run Now' button. Returns the number of
        messages sent.
        """
        job = self._config.get_job(job_id)
        if job is None:
            return 0
        await self._refresh_radar()
        ctx = ExecutorContext(
            store=self.store,
            coverage=self._coverage,
            pfm_points=self._pfm_points,
            latest_radar=self._latest_radar,
            last_broadcast_warnings=self._warning_tracking,
        )
        msgs = self.executor.run_job(job, ctx)
        now = time.time()
        self._last_run[job.id] = now
        self._total_runs[job.id] = self._total_runs.get(job.id, 0) + 1
        self._last_msg_count[job.id] = len(msgs)

        # Wrap in v4 frames (FEC for eligible products)
        if job.product in _FEC_PRODUCTS:
            v4_msgs = self._build_fec_messages(job, ctx)
        else:
            v4_msgs = [v4_wrap(m, self._v4_seq.next()) for m in msgs]

        sent_bytes = 0
        for v4_msg in v4_msgs:
            try:
                await self.radio.send_binary_channel(cobs_encode(v4_msg))
                sent_bytes += len(v4_msg)
                await asyncio.sleep(TX_SPACING)
            except Exception:
                logger.exception("run_job_now: send failed for %s", job_id)
        self._last_bytes[job.id] = sent_bytes
        self._total_bytes[job.id] = self._total_bytes.get(job.id, 0) + sent_bytes
        self._last_msg_count[job.id] = len(v4_msgs)
        activity_log.record(EventDir.OUT, "broadcast",
            f"Job {job.id}: {len(v4_msgs)} msg(s), {sent_bytes}B ({job.product})",
            {"job_id": job.id, "product": job.product, "messages": len(v4_msgs), "bytes": sent_bytes})
        activity_log.record_send(len(v4_msgs), sent_bytes)
        logger.info("run_job_now: %s → %d msg(s), %d bytes (%s)",
                     job.id, len(v4_msgs), sent_bytes, job.product)
        return len(v4_msgs)
