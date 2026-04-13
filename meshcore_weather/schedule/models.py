"""Pydantic models for the broadcast schedule config.

One `BroadcastConfig` per bot, containing a list of `BroadcastJob`
entries. Persistence is JSON; pydantic handles serialization and
validation automatically.
"""

from __future__ import annotations

import re
from pydantic import BaseModel, Field, field_validator


# Supported product names. Adding a new product requires adding one
# entry here AND one entry in executor.PRODUCT_BUILDERS.
PRODUCT_TYPES = {
    "radar",           # 0x10/0x11 radar grid (per region)
    "warnings",        # 0x20/0x21 full re-broadcast of ALL active warnings (safety net, slow cycle)
    "warnings_delta",  # 0x20/0x21 only NEW or CHANGED warnings since last cycle (fast cycle)
    "observation",     # 0x30 current conditions for a point
    "forecast",        # 0x31 multi-day forecast for a point
    "outlook",         # 0x32 hazardous weather outlook
    "storm_reports",   # 0x33 local storm reports
    "rain_obs",        # 0x34 rain-reporting cities
    "metar",           # 0x30 METAR for a station (alias for observation)
    "taf",             # 0x36 TAF for a station
    "warnings_near",   # 0x37 warnings near a specific zone
    "afd",             # 0x40 Area Forecast Discussion (text chunks)
    "space_weather",   # 0x40 SWPC space weather indices (text chunks)
}

# Supported location types. Each tells the executor how to interpret the
# job's `location_id` string.
LOCATION_TYPES = {
    "station",     # 4-letter ICAO, e.g. "KAUS"
    "zone",        # 6-char UGC zone, e.g. "TXZ192"
    "wfo",         # 3-letter WFO code, e.g. "EWX"
    "pfm_point",   # numeric index into pfm_points.json, e.g. "103"
    "region",      # MeshWX radar region id 0-9, e.g. "3"
    "coverage",    # expands at execution time to the operator's configured coverage area
    "city",        # human city name resolved via resolver, e.g. "Austin TX"
}


_ID_PATTERN = re.compile(r"^[a-z0-9_-]+$")


class BroadcastJob(BaseModel):
    """One scheduled broadcast job.

    Identity is stable across config reloads via `id`. The scheduler uses
    `id` to track last-run timestamps so a rename or interval change
    doesn't reset the schedule.
    """

    id: str = Field(..., description="Stable unique identifier (slug)")
    name: str = Field(..., description="Human-readable label for the portal UI")
    product: str = Field(..., description="Product type (see PRODUCT_TYPES)")
    location_type: str = Field(..., description="Location type (see LOCATION_TYPES)")
    location_id: str = Field(
        "",
        description="Type-specific location identifier. Empty for 'coverage'.",
    )
    interval_minutes: int = Field(
        ..., gt=0, description="How often this job runs, in minutes"
    )
    enabled: bool = Field(True, description="If False, scheduler skips this job")

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("id must not be empty")
        if len(v) > 64:
            raise ValueError("id too long (max 64 chars)")
        if not _ID_PATTERN.match(v):
            raise ValueError(
                "id must contain only lowercase letters, digits, hyphens, "
                "and underscores"
            )
        return v

    @field_validator("product")
    @classmethod
    def _validate_product(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in PRODUCT_TYPES:
            raise ValueError(
                f"unknown product {v!r}; must be one of: "
                f"{', '.join(sorted(PRODUCT_TYPES))}"
            )
        return v

    @field_validator("location_type")
    @classmethod
    def _validate_location_type(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in LOCATION_TYPES:
            raise ValueError(
                f"unknown location_type {v!r}; must be one of: "
                f"{', '.join(sorted(LOCATION_TYPES))}"
            )
        return v

    @field_validator("location_id")
    @classmethod
    def _validate_location_id(cls, v: str) -> str:
        # Further type-specific validation happens at executor time; at
        # the model level we just strip.
        return v.strip()


class BroadcastConfig(BaseModel):
    """Top-level broadcast schedule configuration.

    Contains a list of jobs plus a schema version for future
    compatibility. Persisted at `data/broadcast_config.json`.
    """

    version: int = Field(1, description="Schema version")
    jobs: list[BroadcastJob] = Field(default_factory=list)
    radar_grid_size: int = Field(
        64,
        description="Default radar grid size for on-demand requests (16, 32, or 64)",
    )

    def get_job(self, job_id: str) -> BroadcastJob | None:
        """Look up a job by ID; returns None if not found."""
        for job in self.jobs:
            if job.id == job_id:
                return job
        return None

    def upsert_job(self, job: BroadcastJob) -> None:
        """Replace the existing job with the same ID, or append if new."""
        for i, existing in enumerate(self.jobs):
            if existing.id == job.id:
                self.jobs[i] = job
                return
        self.jobs.append(job)

    def delete_job(self, job_id: str) -> bool:
        """Remove a job by ID. Returns True if one was deleted."""
        before = len(self.jobs)
        self.jobs = [j for j in self.jobs if j.id != job_id]
        return len(self.jobs) < before
