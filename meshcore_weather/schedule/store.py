"""Persistence for the broadcast schedule config.

Stores `BroadcastConfig` as JSON at `data/broadcast_config.json`.
Writes are atomic (temp file + rename) so a crash mid-write can't
corrupt the file. Reads are tolerant of missing/corrupt files and
fall back to a sane default synthesized from the operator's
environment-variable coverage settings.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from meshcore_weather.config import settings
from meshcore_weather.schedule.models import BroadcastConfig, BroadcastJob

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(settings.data_dir) / "broadcast_config.json"


def load_config() -> BroadcastConfig:
    """Load broadcast config from disk.

    Returns a fresh default (synthesized from env vars) if the file
    doesn't exist OR if it exists but fails to parse — we'd rather
    keep broadcasting with sensible defaults than silently stop
    broadcasting because a config file got corrupted.
    """
    if not CONFIG_PATH.exists():
        logger.info(
            "No broadcast_config.json at %s — synthesizing default jobs from env",
            CONFIG_PATH,
        )
        cfg = default_config_for_bootstrap()
        # Persist the default so the operator can see/edit it
        try:
            save_config(cfg)
        except Exception as exc:
            logger.warning("Could not persist default broadcast config: %s", exc)
        return cfg

    try:
        raw = CONFIG_PATH.read_text()
        data = json.loads(raw)
        return BroadcastConfig(**data)
    except Exception as exc:
        logger.warning(
            "broadcast_config.json at %s is invalid (%s) — falling back to defaults",
            CONFIG_PATH, exc,
        )
        return default_config_for_bootstrap()


def save_config(cfg: BroadcastConfig) -> None:
    """Write the config to disk atomically.

    Writes to a temp file in the same directory, then renames. This
    guarantees the destination is either the old valid file or the
    new valid file — never a half-written mess.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = CONFIG_PATH.with_suffix(".json.tmp")
    json_text = cfg.model_dump_json(indent=2)
    tmp_path.write_text(json_text + "\n")
    os.replace(tmp_path, CONFIG_PATH)
    logger.debug("Wrote broadcast config to %s (%d jobs)", CONFIG_PATH, len(cfg.jobs))


# -- Default bootstrap --


def default_config_for_bootstrap() -> BroadcastConfig:
    """Synthesize a default BroadcastConfig from the operator's env vars.

    This is what fresh deployments get on first run — and what preserves
    backward compatibility with the pre-schedule-system behavior. The
    default jobs exactly mirror what the old hardcoded
    `_broadcast_all()` used to do:

    1. Radar for the operator's coverage area (one job per region),
       every 15 minutes
    2. Active warnings for the operator's coverage area, every 5 minutes
    3. Observation + forecast for each configured home city, every
       60 minutes

    Operators can edit / delete / add to this via the /schedule portal
    page. The synthesis only runs ONCE — when the JSON file doesn't
    exist. After that, the file is the source of truth.
    """
    jobs: list[BroadcastJob] = []

    # Coverage-wide jobs
    jobs.append(
        BroadcastJob(
            id="radar-coverage",
            name="Radar (coverage regions)",
            product="radar",
            location_type="coverage",
            location_id="",
            interval_minutes=15,
            enabled=True,
        )
    )
    jobs.append(
        BroadcastJob(
            id="warnings-coverage",
            name="Active warnings (coverage)",
            product="warnings",
            location_type="coverage",
            location_id="",
            interval_minutes=5,
            enabled=True,
        )
    )

    # Per-home-city observation and forecast jobs
    home_cities = _split_csv(settings.home_cities)
    for city in home_cities:
        slug = _slugify(city)
        jobs.append(
            BroadcastJob(
                id=f"obs-{slug}",
                name=f"Observation: {city}",
                product="observation",
                location_type="city",
                location_id=city,
                interval_minutes=60,
                enabled=True,
            )
        )
        jobs.append(
            BroadcastJob(
                id=f"forecast-{slug}",
                name=f"Forecast: {city}",
                product="forecast",
                location_type="city",
                location_id=city,
                interval_minutes=60,
                enabled=True,
            )
        )

    logger.info(
        "Bootstrap schedule: %d default jobs (%d home cities → obs+forecast pairs)",
        len(jobs), len(home_cities),
    )
    return BroadcastConfig(version=1, jobs=jobs)


# -- Helpers --


def _split_csv(s: str) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


_SLUG_CHARS = re.compile(r"[^a-z0-9]+")


def _slugify(s: str) -> str:
    """Convert a display string like 'Austin TX' → 'austin-tx'."""
    s = s.strip().lower()
    s = _SLUG_CHARS.sub("-", s)
    s = s.strip("-")
    return s or "item"
