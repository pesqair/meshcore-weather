"""Tests for the unified broadcast schedule system."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from meshcore_weather.schedule.models import (
    BroadcastConfig,
    BroadcastJob,
    LOCATION_TYPES,
    PRODUCT_TYPES,
)
from meshcore_weather.schedule import store as store_module


# -- Model validation --------------------------------------------------------


class TestBroadcastJobValidation:
    def test_minimal_valid_job(self):
        job = BroadcastJob(
            id="test-1",
            name="Test job",
            product="radar",
            location_type="coverage",
            interval_minutes=15,
        )
        assert job.id == "test-1"
        assert job.enabled is True  # default
        assert job.location_id == ""

    def test_id_gets_lowercased(self):
        job = BroadcastJob(
            id="UPPER-CASE",
            name="x",
            product="radar",
            location_type="coverage",
            interval_minutes=5,
        )
        assert job.id == "upper-case"

    def test_id_rejects_spaces(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="has space",
                name="x",
                product="radar",
                location_type="coverage",
                interval_minutes=5,
            )

    def test_id_rejects_special_chars(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="has/slash",
                name="x",
                product="radar",
                location_type="coverage",
                interval_minutes=5,
            )

    def test_unknown_product_rejected(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="ok",
                name="x",
                product="something_bogus",
                location_type="coverage",
                interval_minutes=5,
            )

    def test_unknown_location_type_rejected(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="ok",
                name="x",
                product="radar",
                location_type="not_a_real_type",
                interval_minutes=5,
            )

    def test_zero_interval_rejected(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="ok",
                name="x",
                product="radar",
                location_type="coverage",
                interval_minutes=0,
            )

    def test_negative_interval_rejected(self):
        with pytest.raises(Exception):
            BroadcastJob(
                id="ok",
                name="x",
                product="radar",
                location_type="coverage",
                interval_minutes=-5,
            )

    def test_all_products_are_accepted(self):
        """Every product in PRODUCT_TYPES constructs a valid job."""
        for product in PRODUCT_TYPES:
            job = BroadcastJob(
                id=f"test-{product}",
                name=product,
                product=product,
                location_type="coverage",
                interval_minutes=60,
            )
            assert job.product == product

    def test_all_location_types_are_accepted(self):
        for lt in LOCATION_TYPES:
            job = BroadcastJob(
                id=f"test-{lt}",
                name=lt,
                product="radar",
                location_type=lt,
                interval_minutes=60,
            )
            assert job.location_type == lt


class TestBroadcastConfigMutations:
    def test_upsert_insert(self):
        cfg = BroadcastConfig()
        job = BroadcastJob(
            id="a",
            name="A",
            product="radar",
            location_type="coverage",
            interval_minutes=5,
        )
        cfg.upsert_job(job)
        assert len(cfg.jobs) == 1
        assert cfg.get_job("a") is not None

    def test_upsert_replaces_existing(self):
        cfg = BroadcastConfig()
        cfg.upsert_job(
            BroadcastJob(
                id="a",
                name="A",
                product="radar",
                location_type="coverage",
                interval_minutes=5,
            )
        )
        cfg.upsert_job(
            BroadcastJob(
                id="a",
                name="A prime",
                product="radar",
                location_type="coverage",
                interval_minutes=15,
            )
        )
        assert len(cfg.jobs) == 1
        assert cfg.get_job("a").name == "A prime"
        assert cfg.get_job("a").interval_minutes == 15

    def test_delete(self):
        cfg = BroadcastConfig()
        cfg.upsert_job(
            BroadcastJob(
                id="a",
                name="A",
                product="radar",
                location_type="coverage",
                interval_minutes=5,
            )
        )
        assert cfg.delete_job("a") is True
        assert cfg.delete_job("a") is False  # second delete is a no-op
        assert len(cfg.jobs) == 0

    def test_json_roundtrip(self):
        cfg = BroadcastConfig(
            version=1,
            jobs=[
                BroadcastJob(
                    id=f"job-{i}",
                    name=f"Job {i}",
                    product="radar",
                    location_type="coverage",
                    interval_minutes=15 + i,
                )
                for i in range(5)
            ],
        )
        js = cfg.model_dump_json()
        restored = BroadcastConfig(**json.loads(js))
        assert len(restored.jobs) == 5
        for i, job in enumerate(restored.jobs):
            assert job.id == f"job-{i}"
            assert job.interval_minutes == 15 + i


# -- Persistence -------------------------------------------------------------


class TestStorePersistence:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        # Redirect CONFIG_PATH to a temp location
        tmp_cfg = tmp_path / "broadcast_config.json"
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        cfg = BroadcastConfig(
            version=1,
            jobs=[
                BroadcastJob(
                    id="round-trip",
                    name="Round trip",
                    product="forecast",
                    location_type="city",
                    location_id="Austin TX",
                    interval_minutes=60,
                )
            ],
        )
        store_module.save_config(cfg)
        assert tmp_cfg.exists()
        loaded = store_module.load_config()
        assert len(loaded.jobs) == 1
        assert loaded.jobs[0].id == "round-trip"
        assert loaded.jobs[0].location_id == "Austin TX"

    def test_corrupt_file_falls_back_to_defaults(self, tmp_path, monkeypatch):
        tmp_cfg = tmp_path / "broadcast_config.json"
        tmp_cfg.write_text("{this is not valid json")
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        cfg = store_module.load_config()
        # Corrupt file → falls back to default_config_for_bootstrap, which
        # always emits at least the radar + warnings-delta + warnings-full.
        ids = {j.id for j in cfg.jobs}
        assert "radar-coverage" in ids
        assert "warnings-delta" in ids
        assert "warnings-full" in ids

    def test_missing_file_bootstraps_and_saves(self, tmp_path, monkeypatch):
        tmp_cfg = tmp_path / "broadcast_config.json"
        assert not tmp_cfg.exists()
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        cfg = store_module.load_config()
        # Default bootstrap ran and the result was persisted
        assert tmp_cfg.exists()
        assert len(cfg.jobs) >= 2  # at minimum radar + warnings

    def test_atomic_write_does_not_leave_tmp(self, tmp_path, monkeypatch):
        """save_config should use temp-then-rename so no .tmp files survive."""
        tmp_cfg = tmp_path / "broadcast_config.json"
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)
        cfg = BroadcastConfig(version=1, jobs=[])
        store_module.save_config(cfg)
        # The temp file should not exist after a successful write
        tmp_file = tmp_cfg.with_suffix(".json.tmp")
        assert not tmp_file.exists()


# -- Default bootstrap -------------------------------------------------------


class TestBootstrap:
    def test_bootstrap_includes_core_jobs(self):
        cfg = store_module.default_config_for_bootstrap()
        ids = {j.id for j in cfg.jobs}
        # Radar + warnings-delta + warnings-full always present
        assert "radar-coverage" in ids
        assert "warnings-delta" in ids
        assert "warnings-full" in ids

    def test_bootstrap_adds_home_city_pairs(self, monkeypatch):
        """Each configured home city gets an obs + forecast pair."""
        from meshcore_weather.config import settings
        monkeypatch.setattr(settings, "home_cities", "Austin TX,Dallas TX")
        cfg = store_module.default_config_for_bootstrap()
        ids = {j.id for j in cfg.jobs}
        # Two cities × 2 jobs each = 4 city-specific jobs
        assert "obs-austin-tx" in ids
        assert "forecast-austin-tx" in ids
        assert "obs-dallas-tx" in ids
        assert "forecast-dallas-tx" in ids

    def test_bootstrap_with_no_home_cities_still_has_radar_and_warnings(self, monkeypatch):
        from meshcore_weather.config import settings
        monkeypatch.setattr(settings, "home_cities", "")
        cfg = store_module.default_config_for_bootstrap()
        assert len(cfg.jobs) == 3  # radar + warnings-delta + warnings-full


# -- Scheduler semantics -----------------------------------------------------


class TestSchedulerTick:
    @pytest.mark.asyncio
    async def test_job_does_not_run_before_interval_elapsed(self, tmp_path, monkeypatch):
        """A job with interval=60min that ran 1s ago should NOT run on the next tick."""
        from meshcore_weather.parser.weather import WeatherStore
        from meshcore_weather.schedule.scheduler import Scheduler

        tmp_cfg = tmp_path / "broadcast_config.json"
        tmp_cfg.write_text(
            BroadcastConfig(
                version=1,
                jobs=[
                    BroadcastJob(
                        id="far-future",
                        name="shouldn't run",
                        product="warnings",
                        location_type="coverage",
                        interval_minutes=60,
                    )
                ],
            ).model_dump_json()
        )
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        store = WeatherStore()
        radio = MagicMock()
        radio.send_lock = asyncio.Lock()
        radio.send_binary_channel = AsyncMock()
        sched = Scheduler(store, radio)

        # Skip start() to avoid opening an HTTP client; just initialize state manually
        sched._coverage = sched._coverage  # no-op; Coverage.empty() by default
        await sched._reload_config()
        # Fake "already ran this tick"
        sched._last_run["far-future"] = time.time()
        # Prevent radar fetch from hitting the network
        sched._latest_radar = None
        sched._http_client = None

        sent_count = await sched.tick()
        assert sent_count == 0
        # send_binary_channel should not have been called
        radio.send_binary_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disabled_job_is_skipped(self, tmp_path, monkeypatch):
        from meshcore_weather.parser.weather import WeatherStore
        from meshcore_weather.schedule.scheduler import Scheduler

        tmp_cfg = tmp_path / "broadcast_config.json"
        tmp_cfg.write_text(
            BroadcastConfig(
                version=1,
                jobs=[
                    BroadcastJob(
                        id="disabled-job",
                        name="off",
                        product="warnings",
                        location_type="coverage",
                        interval_minutes=1,
                        enabled=False,
                    )
                ],
            ).model_dump_json()
        )
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        store = WeatherStore()
        radio = MagicMock()
        radio.send_lock = asyncio.Lock()
        radio.send_binary_channel = AsyncMock()
        sched = Scheduler(store, radio)

        await sched._reload_config()
        sched._latest_radar = None
        sched._http_client = None

        sent_count = await sched.tick()
        assert sent_count == 0
        radio.send_binary_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_one_bad_builder_does_not_break_other_jobs(self, tmp_path, monkeypatch):
        """A builder that raises should log but not stop the tick from
        processing other jobs."""
        from meshcore_weather.parser.weather import WeatherStore
        from meshcore_weather.schedule import executor as executor_module
        from meshcore_weather.schedule.scheduler import Scheduler

        tmp_cfg = tmp_path / "broadcast_config.json"
        tmp_cfg.write_text(
            BroadcastConfig(
                version=1,
                jobs=[
                    BroadcastJob(
                        id="bad-one",
                        name="raises",
                        product="warnings",
                        location_type="coverage",
                        interval_minutes=1,
                    ),
                    BroadcastJob(
                        id="good-one",
                        name="works",
                        product="warnings",
                        location_type="coverage",
                        interval_minutes=1,
                    ),
                ],
            ).model_dump_json()
        )
        monkeypatch.setattr(store_module, "CONFIG_PATH", tmp_cfg)

        # Patch the warnings builder so the FIRST call raises, the SECOND returns []
        call_count = {"n": 0}
        original_builder = executor_module.PRODUCT_BUILDERS["warnings"]

        def flaky_warnings(job, ctx):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated builder failure")
            return []

        monkeypatch.setitem(executor_module.PRODUCT_BUILDERS, "warnings", flaky_warnings)

        store = WeatherStore()
        radio = MagicMock()
        radio.send_lock = asyncio.Lock()
        radio.send_binary_channel = AsyncMock()
        sched = Scheduler(store, radio)
        await sched._reload_config()
        sched._latest_radar = None
        sched._http_client = None

        # Both jobs should execute — the exception is caught per-job
        await sched.tick()
        assert call_count["n"] == 2


# -- Broadcaster MSG_NOT_AVAILABLE emission --------------------------------


class TestBroadcasterNotAvailable:
    """Verify respond_to_data_request emits MSG_NOT_AVAILABLE instead of
    silently dropping requests it can't fulfill."""

    def _make_broadcaster(self):
        from meshcore_weather.parser.weather import WeatherStore
        from meshcore_weather.protocol.broadcaster import MeshWXBroadcaster

        store = WeatherStore()  # empty — no data to find
        radio = MagicMock()
        radio.send_lock = asyncio.Lock()
        radio.send_lock = asyncio.Lock()
        sent = []

        async def cap(p):
            sent.append(p)

        radio.send_binary_channel = cap
        bc = MeshWXBroadcaster(store, radio)
        return bc, sent

    @staticmethod
    def _unwrap_v4(raw: bytes) -> bytes:
        """Strip v4 frame header if present."""
        if raw and raw[0] == 0x04:
            from meshcore_weather.protocol.meshwx import v4_unwrap
            raw, _, _ = v4_unwrap(raw)
        return raw

    @pytest.mark.asyncio
    async def test_no_data_emits_not_available(self):
        """Empty store + valid request → NOT_AVAILABLE with REASON_NO_DATA."""
        from meshcore_weather.protocol.meshwx import (
            cobs_decode, unpack_not_available, MSG_NOT_AVAILABLE,
            LOC_STATION, DATA_METAR, REASON_NO_DATA,
        )

        bc, sent = self._make_broadcaster()
        # Monkey-patch TX_SPACING to 0 for test speed
        import meshcore_weather.protocol.broadcaster as bc_mod
        original_gap = bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS
        bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = 0
        try:
            req = {
                "data_type": DATA_METAR,
                "location": {"type": LOC_STATION, "station": "KAUS"},
            }
            await bc.respond_to_data_request(req)
        finally:
            bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = original_gap

        assert len(sent) == 2  # double-transmit
        raw = self._unwrap_v4(cobs_decode(sent[0]))
        assert raw[0] == MSG_NOT_AVAILABLE
        d = unpack_not_available(raw)
        assert d["data_type"] == DATA_METAR
        assert d["reason"] == REASON_NO_DATA
        assert d["location"]["type"] == LOC_STATION
        assert d["location"]["station"] == "KAUS"

    @pytest.mark.asyncio
    async def test_unresolvable_location_emits_not_available(self):
        """Out-of-range PFM point index → NOT_AVAILABLE with REASON_LOCATION_UNRESOLVABLE.

        LOC_PFM_POINT requests go through _location_to_query_string which
        looks up the index in the bundled pfm_points.json. An index way
        beyond the end of the list makes that helper return None, which
        triggers REASON_LOCATION_UNRESOLVABLE at the top of
        respond_to_data_request — distinct from the "deeper failure" path
        where a valid location string resolves to a store that has no
        data.
        """
        from meshcore_weather.protocol.meshwx import (
            cobs_decode, unpack_not_available,
            LOC_PFM_POINT, DATA_FORECAST, REASON_LOCATION_UNRESOLVABLE,
        )

        bc, sent = self._make_broadcaster()
        import meshcore_weather.protocol.broadcaster as bc_mod
        original_gap = bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS
        bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = 0
        try:
            req = {
                "data_type": DATA_FORECAST,
                # Out-of-range PFM index — there are only ~1873 valid entries
                "location": {"type": LOC_PFM_POINT, "pfm_point_id": 99999},
            }
            await bc.respond_to_data_request(req)
        finally:
            bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = original_gap

        assert len(sent) == 2
        raw = self._unwrap_v4(cobs_decode(sent[0]))
        d = unpack_not_available(raw)
        assert d["reason"] == REASON_LOCATION_UNRESOLVABLE
        assert d["location"]["type"] == LOC_PFM_POINT
        assert d["location"]["pfm_point_id"] == 99999

    @pytest.mark.asyncio
    async def test_retry_rebroadcasts_cached_not_available(self):
        """A second request for the same failing thing should cache-rebroadcast
        the NOT_AVAILABLE without re-running the builder."""
        from meshcore_weather.protocol.meshwx import (
            cobs_decode, unpack_not_available,
            LOC_STATION, DATA_METAR, REASON_NO_DATA,
        )

        bc, sent = self._make_broadcaster()
        import meshcore_weather.protocol.broadcaster as bc_mod
        original_gap = bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS
        bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = 0
        try:
            req = {
                "data_type": DATA_METAR,
                "location": {"type": LOC_STATION, "station": "KAUS"},
            }
            # First request — fresh build, returns NOT_AVAILABLE
            await bc.respond_to_data_request(req)
            assert len(sent) == 2

            # Retry — should hit cache, rebroadcast same NOT_AVAILABLE
            sent.clear()
            await bc.respond_to_data_request(req)
            assert len(sent) == 2
            raw = self._unwrap_v4(cobs_decode(sent[0]))
            d = unpack_not_available(raw)
            assert d["reason"] == REASON_NO_DATA
        finally:
            bc_mod.MeshWXBroadcaster._V2_RESEND_GAP_SECONDS = original_gap
