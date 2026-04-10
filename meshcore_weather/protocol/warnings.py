"""Extract active warnings from WeatherStore and pack as MeshWX binary messages."""

import logging
from datetime import datetime, timezone

from meshcore_weather.parser.weather import (
    WeatherStore,
    _expand_zone_ranges,
    extract_warning_polygon,
    parse_vtec,
)
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.meshwx import (
    PRODUCT_TYPE_MAP,
    VTEC_SEVERITY_MAP,
    SEV_WARNING,
    WARN_OTHER,
    pack_warning_polygon,
)

logger = logging.getLogger(__name__)


def _vtec_end_to_minutes(end_str: str) -> int:
    """Convert VTEC end time (DDHHMMZ format e.g. '090145Z') to minutes from now."""
    try:
        now = datetime.now(timezone.utc)
        day = int(end_str[:2])
        hour = int(end_str[2:4])
        minute = int(end_str[4:6])
        # Build target datetime in current month
        end = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
        # Handle month rollover
        if end < now:
            if now.month == 12:
                end = end.replace(year=now.year + 1, month=1)
            else:
                end = end.replace(month=now.month + 1)
        delta = end - now
        return max(0, int(delta.total_seconds() / 60))
    except (ValueError, OverflowError):
        return 120  # default 2 hours


def extract_active_warnings(
    store: WeatherStore,
    coverage: Coverage | None = None,
) -> list[dict]:
    """Extract all active warnings with polygons from the store.

    If `coverage` is provided and non-empty, warnings are filtered to only
    those affecting the coverage zones (by zone code first, polygon fallback).

    Returns list of dicts with keys:
        warning_type, severity, expiry_minutes, vertices, headline, zones
    """
    warn_types = {"SVS", "SPS", "NPW", "FLS", "FLW", "WSW", "SWO", "MWS", "DSW", "EWW", "SQW"}
    results = []
    seen = set()
    filter_active = coverage is not None and not coverage.is_empty()

    for prod in sorted(store._products.values(), key=lambda p: p.timestamp, reverse=True):
        if prod.product_type not in warn_types:
            continue
        key = f"{prod.product_type}_{prod.orig}"
        if key in seen:
            continue
        seen.add(key)
        if store._is_cancelled(prod.raw_text):
            continue

        # Extract polygon
        vertices = extract_warning_polygon(prod.raw_text)
        if len(vertices) < 3:
            continue  # no polygon, can't broadcast as binary

        # Extract affected zones from the UGC line (supports TXZ021>044 ranges)
        zones = _expand_zone_ranges(prod.raw_text)

        # Apply coverage filter if set
        if filter_active:
            # Fast path: zone code intersection
            if zones and coverage.covers_any(zones):
                pass  # accepted
            elif coverage.covers_polygon(vertices):
                pass  # accepted via polygon geometry
            else:
                continue  # outside coverage, skip

        # Determine warning type nibble
        wtype = PRODUCT_TYPE_MAP.get(prod.product_type, WARN_OTHER)

        # Determine severity from VTEC
        vtec = parse_vtec(prod.raw_text)
        if vtec:
            severity = VTEC_SEVERITY_MAP.get(vtec["significance"], SEV_WARNING)
            expiry = _vtec_end_to_minutes(vtec["end"])
        else:
            severity = SEV_WARNING
            expiry = 120  # default

        # Get headline
        headline = store._short_headline(prod.raw_text)

        results.append({
            "warning_type": wtype,
            "severity": severity,
            "expiry_minutes": expiry,
            "vertices": vertices,
            "headline": headline,
            "zones": sorted(zones),
        })

    return results


def warnings_to_binary(warnings: list[dict]) -> list[bytes]:
    """Pack a list of warning dicts into MeshWX binary messages."""
    msgs = []
    for w in warnings:
        try:
            msg = pack_warning_polygon(
                warning_type=w["warning_type"],
                severity=w["severity"],
                expiry_minutes=w["expiry_minutes"],
                vertices=w["vertices"],
                headline=w["headline"],
            )
            msgs.append(msg)
        except Exception:
            logger.debug("Failed to pack warning: %s", w.get("headline", "?")[:40])
    return msgs
