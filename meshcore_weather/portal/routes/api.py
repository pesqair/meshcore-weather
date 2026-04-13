"""JSON/API routes for the portal (HTMX partials and data endpoints)."""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from meshcore_weather.activity import activity_log
from meshcore_weather.geodata import resolver
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.warnings import extract_active_warnings
from meshcore_weather.schedule.models import (
    BroadcastJob,
    LOCATION_TYPES,
    PRODUCT_TYPES,
)

router = APIRouter()


def _get_scheduler(request: Request):
    """Return the bot's active Scheduler instance or raise 503."""
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    if broadcaster is None or not hasattr(broadcaster, "scheduler"):
        raise HTTPException(503, "scheduler not available")
    return broadcaster.scheduler


# -- Coverage preview & save --

@router.get("/coverage/preview")
async def coverage_preview(
    cities: str = Query("", description="Comma-separated city,state pairs"),
    states: str = Query("", description="Comma-separated 2-letter state codes"),
    wfos: str = Query("", description="Comma-separated 3-letter WFO codes"),
) -> JSONResponse:
    """Compute coverage from the given inputs without saving it."""
    city_list = [c.strip() for c in cities.split(",") if c.strip()]
    state_list = [s.strip() for s in states.split(",") if s.strip()]
    wfo_list = [w.strip() for w in wfos.split(",") if w.strip()]

    cov = Coverage.from_sources(cities=city_list, states=state_list, wfos=wfo_list)
    return JSONResponse({
        "zones": sorted(cov.zones),
        "zone_count": len(cov.zones),
        "bbox": cov.bbox,
        "region_ids": sorted(cov.region_ids),
        "summary": cov.summary(),
    })


@router.post("/coverage/save")
async def coverage_save(request: Request) -> JSONResponse:
    """Deprecated — coverage is bootstrap config loaded from environment.

    Previously this endpoint tried to rewrite `.env` in place, which
    crashed because the container runs as a non-root user and the file
    is owned by root. More importantly, the whole premise — "web UI
    edits environment variables that the process has already loaded" —
    doesn't actually work cleanly since env vars are process-start
    state.

    Runtime broadcast configuration lives in `data/broadcast_config.json`
    now and is managed by the /schedule page. This endpoint remains so
    the existing /config form doesn't 404, but it returns a clear
    explanation instead of attempting a file write.
    """
    return JSONResponse(
        {
            "ok": False,
            "error": "coverage_is_bootstrap_config",
            "message": (
                "Coverage (home_cities/home_states/home_wfos) is loaded "
                "from environment variables at bot startup and cannot be "
                "changed live from the portal. To change coverage, edit "
                ".env on the host and restart the container. To change "
                "what the bot broadcasts without touching coverage, use "
                "the Schedule page (/schedule) — that lets you add, "
                "remove, and configure individual broadcast jobs at "
                "runtime without a restart."
            ),
        },
        status_code=400,
    )


# -- Autocomplete helpers --

@router.get("/autocomplete/city")
async def autocomplete_city(q: str = Query("", min_length=2)) -> JSONResponse:
    """Suggest city+state matches for the config form."""
    resolver.load()
    q_upper = q.upper().strip()
    matches = []
    for place in resolver._places:
        name, state = place[0], place[1]
        if name.upper().startswith(q_upper):
            matches.append(f"{name.title()}, {state}")
            if len(matches) >= 10:
                break
    return JSONResponse({"matches": matches})


@router.get("/autocomplete/wfo")
async def autocomplete_wfo(q: str = Query("", min_length=1)) -> JSONResponse:
    """Suggest WFO codes."""
    resolver.load()
    q_upper = q.upper().strip()
    wfos = sorted({z["w"] for z in resolver._zones.values() if z.get("w")})
    matches = [w for w in wfos if w.startswith(q_upper)][:10]
    return JSONResponse({"matches": matches})


# -- Warnings --

@router.get("/warnings")
async def list_warnings(request: Request) -> JSONResponse:
    """List all active warnings with coverage tag."""
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    coverage = broadcaster.coverage if broadcaster else None

    # Get all warnings (no filter)
    all_warnings = extract_active_warnings(bot.store, coverage=None)

    # Build JSON-safe response objects. Warning dicts contain a datetime
    # `expires_at` field (added in the v3 pyIEM port) which json.dumps can't
    # serialize directly — convert to ISO 8601 string here.
    out: list[dict] = []
    for w in all_warnings:
        if coverage is None or coverage.is_empty():
            in_cov = True
        else:
            # Use the full UGC list (zones + county FIPS) for coverage
            # matching, not just the Z-only zones list. County-FIPS-only
            # warnings (FFW, SVR with TXC### codes) would be missed
            # if we only checked Z-codes.
            ugcs = w.get("ugcs") or w.get("zones", [])
            in_cov = (
                coverage.covers_any(ugcs)
                or coverage.covers_polygon(w.get("vertices", []))
            )

        verts = w.get("vertices", [])
        bbox = None
        if verts:
            lats = [v[0] for v in verts]
            lons = [v[1] for v in verts]
            bbox = [min(lats), min(lons), max(lats), max(lons)]

        expires_at = w.get("expires_at")
        out.append({
            "warning_type": w.get("warning_type"),
            "severity": w.get("severity"),
            "expires_at": expires_at.isoformat() if expires_at else None,
            "expiry_minutes": w.get("expiry_minutes"),
            "headline": w.get("headline"),
            "zones": w.get("zones", []),
            "ugcs": w.get("ugcs", []),
            "product_type": w.get("product_type"),
            "vtec_action": w.get("vtec_action"),
            "vtec_phenomenon": w.get("vtec_phenomenon"),
            "vtec_significance": w.get("vtec_significance"),
            "vtec_office": w.get("vtec_office"),
            "vtec_etn": w.get("vtec_etn"),
            "in_coverage": in_cov,
            "bbox": bbox,
            "vertices": verts,  # kept for /data map view
        })

    return JSONResponse({"warnings": out, "count": len(out)})


# -- EMWIN product browser --

@router.get("/products/filters")
async def product_filters(request: Request) -> JSONResponse:
    """Return distinct filter values for the product browser dropdowns."""
    bot = request.app.state.bot
    types = sorted({p.product_type for p in bot.store._products.values()})
    offices = sorted({p.office for p in bot.store._products.values() if p.office})
    states = sorted({p.state for p in bot.store._products.values() if p.state})
    return JSONResponse({"types": types, "offices": offices, "states": states})


@router.get("/products")
async def list_products(
    request: Request,
    type: str = Query(""),
    office: str = Query(""),
    state: str = Query(""),
    q: str = Query(""),
    limit: int = Query(100),
) -> JSONResponse:
    """List ingested EMWIN products with optional filters."""
    bot = request.app.state.bot
    results = []
    q_lower = q.lower().strip()

    for prod in sorted(bot.store._products.values(), key=lambda p: p.timestamp, reverse=True):
        if type and prod.product_type != type:
            continue
        if office and prod.office != office:
            continue
        if state and prod.state != state:
            continue
        if q_lower and q_lower not in prod.raw_text.lower():
            continue
        # Get first non-empty line as preview
        preview = ""
        for line in prod.raw_text.splitlines():
            line = line.strip()
            if line and not line.startswith("$$"):
                preview = line[:120]
                break
        results.append({
            "filename": prod.filename,
            "emwin_id": prod.emwin_id,
            "product_type": prod.product_type,
            "office": prod.office,
            "state": prod.state,
            "timestamp": prod.timestamp.isoformat(),
            "preview": preview,
        })
        if len(results) >= limit:
            break

    return JSONResponse({"products": results, "count": len(results)})


@router.get("/products/{filename}")
async def get_product(request: Request, filename: str) -> JSONResponse:
    """Get the full raw text of a specific product."""
    bot = request.app.state.bot
    prod = bot.store._products.get(filename)
    if not prod:
        raise HTTPException(404, "Product not found")
    return JSONResponse({
        "filename": prod.filename,
        "emwin_id": prod.emwin_id,
        "product_type": prod.product_type,
        "office": prod.office,
        "state": prod.state,
        "timestamp": prod.timestamp.isoformat(),
        "raw_text": prod.raw_text,
    })


# -- Status + actions --

@router.get("/status")
async def get_status(request: Request) -> JSONResponse:
    """Bot operational status."""
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    scheduler = broadcaster.scheduler if broadcaster else None
    cfg = scheduler.current_config() if scheduler else None
    return JSONResponse({
        "radio": {
            "channel_idx": bot.radio.channel_idx,
            "data_channel_idx": bot.radio.data_channel_idx,
        },
        "store": {
            "product_count": len(bot.store._products),
        },
        "broadcaster": {
            "running": broadcaster is not None,
            "coverage": broadcaster.coverage.summary() if broadcaster else None,
        },
        "contacts": {
            "known": len(bot._known_contacts) if hasattr(bot, "_known_contacts") else 0,
        },
        "settings": {
            "radar_grid_size": cfg.radar_grid_size if cfg else 32,
        },
    })


@router.get("/activity")
async def get_activity(
    limit: int = Query(100, ge=1, le=500),
) -> JSONResponse:
    """Return the most recent activity log entries."""
    return JSONResponse({"events": activity_log.recent(limit)})


@router.get("/activity/stream")
async def activity_stream():
    """Server-Sent Events stream of real-time activity log entries.

    The portal's activity feed connects to this endpoint and receives
    new events as they happen — no polling, no refresh needed. Each
    SSE event is a JSON-encoded activity log entry.

    Usage in JavaScript:
        const es = new EventSource('/api/activity/stream');
        es.onmessage = (e) => {
            const event = JSON.parse(e.data);
            appendToActivityLog(event);
        };
    """
    import json
    from starlette.responses import StreamingResponse

    async def event_generator():
        async for event in activity_log.subscribe():
            data = json.dumps(event.to_dict())
            yield f"data: {data}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # nginx compatibility
        },
    )


@router.get("/stats")
async def get_stats(
    window: int = Query(60, ge=1, le=1440, description="Time window in minutes"),
) -> JSONResponse:
    """Return aggregate send stats for the given time window."""
    windows = [window]
    # Always include a few standard windows for the UI
    for w in [5, 15, 60, 360, 1440]:
        if w not in windows:
            windows.append(w)
    windows.sort()
    return JSONResponse({
        "stats": [activity_log.stats(w) for w in windows],
    })


@router.post("/settings/radar-grid-size")
async def set_radar_grid_size(request: Request) -> JSONResponse:
    """Update the default radar grid size for on-demand requests."""
    body = await request.json()
    grid_size = body.get("radar_grid_size")
    if grid_size not in (16, 32, 64):
        raise HTTPException(400, "radar_grid_size must be 16, 32, or 64")
    scheduler = _get_scheduler(request)
    cfg = scheduler.current_config()
    cfg.radar_grid_size = grid_size
    await scheduler.save_config(cfg)
    return JSONResponse({"ok": True, "radar_grid_size": grid_size})


@router.post("/settings/channels")
async def set_channels(request: Request) -> JSONResponse:
    """Save channel configuration to the .env file.

    Channel changes require a bot restart to take effect since the radio
    resolves channels at startup. We persist to .env so the next startup
    picks up the new values.
    """
    body = await request.json()
    text_ch = body.get("text_channel", "").strip()
    data_ch = body.get("data_channel", "").strip()

    # Validate: channel names should start with # or be a numeric index
    for name, val in [("text_channel", text_ch), ("data_channel", data_ch)]:
        if val and not val.startswith("#") and not val.isdigit():
            raise HTTPException(400, f"{name} must start with '#' or be a numeric index")

    # Persist to .env file
    from pathlib import Path
    env_path = Path(".env")
    env_lines: list[str] = []
    if env_path.exists():
        env_lines = env_path.read_text().splitlines()

    env_map = {
        "MCW_MESHCORE_CHANNEL": text_ch,
        "MCW_MESHWX_CHANNEL": data_ch,
    }
    for key, val in env_map.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}=") or line.startswith(f"# {key}="):
                env_lines[i] = f"{key}={val}" if val else f"# {key}="
                found = True
                break
        if not found and val:
            env_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(env_lines) + "\n")

    return JSONResponse({
        "ok": True,
        "text_channel": text_ch,
        "data_channel": data_ch,
        "note": "Restart required for changes to take effect",
    })


@router.post("/actions/broadcast")
async def trigger_broadcast(request: Request) -> JSONResponse:
    """Manually trigger a scheduler tick — runs any jobs whose interval
    has elapsed right now. For per-job control use the /api/schedule/jobs/
    {id}/run-now endpoint which force-runs a specific job regardless of
    its schedule.
    """
    scheduler = _get_scheduler(request)
    sent = await scheduler.tick()
    return JSONResponse({"ok": True, "messages_sent": sent})


@router.post("/actions/v2-request")
async def trigger_v2_request(request: Request) -> JSONResponse:
    """Simulate a v2 data request for testing (bypasses rate limit with force flag).

    Body: {"data_type": "wx"|"forecast"|"metar", "location": "Austin TX"}
    """
    from meshcore_weather.protocol.meshwx import (
        DATA_FORECAST, DATA_METAR, DATA_WX, LOC_STATION, LOC_ZONE
    )
    body = await request.json()
    data_type_str = body.get("data_type", "wx")
    location_str = body.get("location", "")

    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    if not broadcaster:
        raise HTTPException(400, "Broadcaster not running")

    # Resolve the location string to a zone or station
    resolved = resolver.resolve(location_str)
    if not resolved:
        raise HTTPException(400, f"Could not resolve: {location_str}")

    # Prefer zone, fall back to station
    zones = resolved.get("zones", [])
    station = resolved.get("station")
    if zones:
        loc = {"type": LOC_ZONE, "zone": zones[0]}
    elif station:
        loc = {"type": LOC_STATION, "station": station}
    else:
        raise HTTPException(400, "Could not build location ref")

    data_map = {"wx": DATA_WX, "forecast": DATA_FORECAST, "metar": DATA_METAR}
    data_type = data_map.get(data_type_str)
    if data_type is None:
        raise HTTPException(400, f"Unknown data_type: {data_type_str}")

    # Bypass rate limit by clearing this entry
    loc_key = broadcaster._location_key(loc)
    rate_key = f"{data_type}:{loc_key}"
    if hasattr(broadcaster, "_v2_rate_limit"):
        broadcaster._v2_rate_limit.pop(rate_key, None)

    req = {"data_type": data_type, "location": loc, "client_newest": 0, "flags": 0}
    await broadcaster.respond_to_data_request(req)
    return JSONResponse({"ok": True, "location": loc, "data_type": data_type_str})


# -- Broadcast schedule (CRUD) ------------------------------------------------


@router.get("/schedule/meta")
async def schedule_meta() -> JSONResponse:
    """Return rich metadata for the job form: products with descriptions,
    location types, which locations each product supports, and radar options."""

    product_info = {
        "radar":          {"label": "Radar",                  "desc": "Compressed radar grid (0x11)", "locations": ["coverage", "region"]},
        "warnings":       {"label": "Warnings (full)",        "desc": "Re-broadcast ALL active warnings (safety net)", "locations": ["coverage"]},
        "warnings_delta": {"label": "Warnings (delta)",       "desc": "Only new/changed warnings since last cycle", "locations": ["coverage"]},
        "warnings_near":  {"label": "Warnings near zone",     "desc": "Warnings affecting a specific zone", "locations": ["zone"]},
        "observation":    {"label": "Observation",            "desc": "Current conditions for a point", "locations": ["city", "station", "zone"]},
        "forecast":       {"label": "Forecast",              "desc": "Multi-day forecast for a point", "locations": ["city", "zone", "pfm_point"]},
        "outlook":        {"label": "Hazardous Outlook",     "desc": "Hazardous weather outlook", "locations": ["coverage", "wfo"]},
        "metar":          {"label": "METAR",                 "desc": "Raw METAR observation for a station", "locations": ["station"]},
        "taf":            {"label": "TAF",                   "desc": "Terminal aerodrome forecast for a station", "locations": ["station"]},
        "storm_reports":  {"label": "Storm Reports",         "desc": "Local storm reports (LSR)", "locations": ["coverage", "wfo"]},
        "rain_obs":       {"label": "Rain Observations",     "desc": "Rain-reporting cities", "locations": ["coverage"]},
        "afd":            {"label": "Area Forecast Discussion", "desc": "AFD text from a forecast office", "locations": ["wfo"]},
        "space_weather":  {"label": "Space Weather",         "desc": "SWPC space weather indices", "locations": ["coverage"]},
    }

    location_info = {
        "coverage":  {"label": "Coverage area",    "desc": "All zones in the operator's configured coverage", "placeholder": "(leave empty)"},
        "region":    {"label": "Radar region",     "desc": "MeshWX radar region 0\u20139", "placeholder": "e.g. 3"},
        "city":      {"label": "City",             "desc": "Resolved to nearest NWS zone", "placeholder": "e.g. Austin TX"},
        "station":   {"label": "Station (ICAO)",   "desc": "4-letter ICAO code", "placeholder": "e.g. KAUS"},
        "zone":      {"label": "NWS Zone",         "desc": "6-character UGC zone code", "placeholder": "e.g. TXZ192"},
        "wfo":       {"label": "Forecast Office",  "desc": "3-letter WFO code", "placeholder": "e.g. EWX"},
        "pfm_point": {"label": "PFM Point",        "desc": "Numeric index into pfm_points.json", "placeholder": "e.g. 103"},
    }

    return JSONResponse({
        "products": sorted(PRODUCT_TYPES),
        "location_types": sorted(LOCATION_TYPES),
        "product_info": product_info,
        "location_info": location_info,
        "radar_grid_sizes": [
            {"value": 32, "label": "32x32 (standard)", "desc": "~42 km/cell, 4-5 messages"},
            {"value": 64, "label": "64x64 (high-res)", "desc": "~21 km/cell, 10-15 messages"},
        ],
    })


@router.get("/schedule/jobs")
async def list_jobs(request: Request) -> JSONResponse:
    """List all configured broadcast jobs with their runtime status."""
    scheduler = _get_scheduler(request)
    cfg = scheduler.current_config()
    out = []
    for job in cfg.jobs:
        status = scheduler.job_status(job.id)
        out.append({
            **job.model_dump(),
            "last_run_unix": status.get("last_run_unix"),
            "last_run_seconds_ago": status.get("last_run_seconds_ago"),
            "next_run_in_seconds": status.get("next_run_in_seconds"),
            "total_runs": status.get("total_runs", 0),
            "total_bytes": status.get("total_bytes", 0),
            "last_bytes": status.get("last_bytes", 0),
            "last_msg_count": status.get("last_msg_count", 0),
        })
    return JSONResponse({"jobs": out, "count": len(out)})


@router.post("/schedule/jobs")
async def create_job(request: Request) -> JSONResponse:
    """Create a new broadcast job from a JSON body."""
    scheduler = _get_scheduler(request)
    body = await request.json()
    try:
        job = BroadcastJob(**body)
    except Exception as exc:
        raise HTTPException(400, f"invalid job: {exc}")
    cfg = scheduler.current_config()
    if cfg.get_job(job.id) is not None:
        raise HTTPException(409, f"job {job.id!r} already exists")
    cfg.upsert_job(job)
    await scheduler.save_config(cfg)
    return JSONResponse({"ok": True, "job": job.model_dump()})


@router.put("/schedule/jobs/{job_id}")
async def update_job(job_id: str, request: Request) -> JSONResponse:
    """Update an existing broadcast job. Body is the new job dict."""
    scheduler = _get_scheduler(request)
    body = await request.json()
    # The URL param is authoritative for id so the client can't
    # accidentally rename by changing the body.
    body["id"] = job_id
    try:
        job = BroadcastJob(**body)
    except Exception as exc:
        raise HTTPException(400, f"invalid job: {exc}")
    cfg = scheduler.current_config()
    if cfg.get_job(job_id) is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    cfg.upsert_job(job)
    await scheduler.save_config(cfg)
    return JSONResponse({"ok": True, "job": job.model_dump()})


@router.delete("/schedule/jobs/{job_id}")
async def delete_job(job_id: str, request: Request) -> JSONResponse:
    """Delete a broadcast job by id."""
    scheduler = _get_scheduler(request)
    cfg = scheduler.current_config()
    if not cfg.delete_job(job_id):
        raise HTTPException(404, f"job {job_id!r} not found")
    await scheduler.save_config(cfg)
    return JSONResponse({"ok": True, "deleted": job_id})


@router.post("/schedule/jobs/{job_id}/toggle")
async def toggle_job(job_id: str, request: Request) -> JSONResponse:
    """Flip the `enabled` flag on a job."""
    scheduler = _get_scheduler(request)
    cfg = scheduler.current_config()
    job = cfg.get_job(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    job.enabled = not job.enabled
    cfg.upsert_job(job)
    await scheduler.save_config(cfg)
    return JSONResponse({"ok": True, "id": job_id, "enabled": job.enabled})


@router.post("/schedule/jobs/{job_id}/run-now")
async def run_job_now(job_id: str, request: Request) -> JSONResponse:
    """Force-run a specific job immediately, ignoring its interval."""
    scheduler = _get_scheduler(request)
    cfg = scheduler.current_config()
    if cfg.get_job(job_id) is None:
        raise HTTPException(404, f"job {job_id!r} not found")
    n_msgs = await scheduler.run_job_now(job_id)
    return JSONResponse({"ok": True, "id": job_id, "messages_sent": n_msgs})
