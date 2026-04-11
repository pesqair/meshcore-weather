"""Unified broadcast schedule system.

Runtime-editable schedule of broadcast jobs, each defined as:

    BroadcastJob(
        id: str,              # stable unique identifier
        name: str,            # display label
        product: str,         # "radar" / "observation" / "forecast" / ...
        location_type: str,   # "station" / "zone" / "wfo" / "pfm_point" / "region" / "coverage"
        location_id: str,     # e.g. "KAUS", "TXZ192", "EWX", "103", "3", ""
        interval_minutes: int,
        enabled: bool,
    )

The scheduler (`scheduler.Scheduler`) ticks periodically, walks the
enabled jobs, and executes any whose interval has elapsed via the
executor (`executor.BroadcastExecutor`). The executor dispatches each
job to a product-specific builder function registered in a central
registry, so adding a new product type means adding one entry to the
registry.

Persistent state lives in `data/broadcast_config.json` (writable volume).
On first run, if the file doesn't exist, a default configuration is
synthesized from the operator's env-var coverage config
(MCW_HOME_CITIES / MCW_HOME_STATES / MCW_HOME_WFOS). This preserves
backward compat for existing deployments without requiring any
manual migration.

The /schedule portal page provides CRUD management of jobs via the
/api/schedule/* endpoints.
"""

from meshcore_weather.schedule.models import BroadcastJob, BroadcastConfig
from meshcore_weather.schedule.store import (
    load_config,
    save_config,
    default_config_for_bootstrap,
)

__all__ = [
    "BroadcastJob",
    "BroadcastConfig",
    "load_config",
    "save_config",
    "default_config_for_bootstrap",
]
