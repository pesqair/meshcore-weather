"""HTML page routes for the portal — single SPA entry point."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from meshcore_weather.config import settings

router = APIRouter()

# Legacy path → hash-section mapping
_LEGACY = {
    "/config": "/#system",
    "/schedule": "/#broadcast",
    "/data": "/#map",
    "/products": "/#broadcast",
    "/status": "/#overview",
}


@router.get("/", response_class=HTMLResponse)
async def app_page(request: Request):
    """Serve the single-page app with bootstrap data."""
    templates = request.app.state.templates
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    coverage = broadcaster.coverage if broadcaster else None

    boot = {
        "coverage_sources": (
            coverage.sources
            if coverage
            else {"cities": [], "states": [], "wfos": []}
        ),
        "coverage_summary": (
            coverage.summary()
            if coverage and not coverage.is_empty()
            else None
        ),
        "zone_count": (
            len(coverage.zones) if coverage and not coverage.is_empty() else 0
        ),
        "region_count": (
            len(coverage.region_ids)
            if coverage and not coverage.is_empty()
            else 0
        ),
        "product_count": len(bot.store._products),
        "channel_idx": bot.radio.channel_idx if bot.radio else None,
        "data_channel": bot.radio.data_channel_idx if bot.radio else None,
        "discover_channel": bot.radio.discover_channel_idx if bot.radio else None,
        "channel_config": {
            "text_channel": settings.meshcore_channel,
            "data_channel": settings.meshwx_channel,
            "discover_channel": settings.meshwx_discover_channel,
        },
    }
    return templates.TemplateResponse(
        request,
        "app.html",
        {"boot_json": json.dumps(boot)},
    )


# Legacy redirects so old bookmarks still work
@router.get("/config")
async def legacy_config():
    return RedirectResponse(_LEGACY["/config"])


@router.get("/schedule")
async def legacy_schedule():
    return RedirectResponse(_LEGACY["/schedule"])


@router.get("/data")
async def legacy_data():
    return RedirectResponse(_LEGACY["/data"])


@router.get("/products")
async def legacy_products():
    return RedirectResponse(_LEGACY["/products"])


@router.get("/status")
async def legacy_status():
    return RedirectResponse(_LEGACY["/status"])
