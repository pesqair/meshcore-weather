"""JSON/API routes for the portal (HTMX partials and data endpoints)."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from meshcore_weather.geodata import resolver
from meshcore_weather.protocol.coverage import Coverage
from meshcore_weather.protocol.warnings import extract_active_warnings

router = APIRouter()

_ENV_PATH = Path(".env")


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
    """Write coverage settings to .env and reload the broadcaster."""
    body = await request.json()
    cities = body.get("cities", [])
    states = body.get("states", [])
    wfos = body.get("wfos", [])

    if not isinstance(cities, list) or not isinstance(states, list) or not isinstance(wfos, list):
        raise HTTPException(400, "cities/states/wfos must be lists")

    # Validate
    resolver.load()
    for city in cities:
        if not resolver.resolve(city):
            raise HTTPException(400, f"Could not resolve city: {city}")
    for state in states:
        if len(state) != 2 or not state.isalpha():
            raise HTTPException(400, f"Invalid state code: {state}")
    for wfo in wfos:
        if not any(z["w"] == wfo.upper() for z in resolver._zones.values()):
            raise HTTPException(400, f"Unknown WFO: {wfo}")

    # Write to .env (update or append)
    _update_env_file({
        "MCW_HOME_CITIES": ",".join(cities),
        "MCW_HOME_STATES": ",".join(s.upper() for s in states),
        "MCW_HOME_WFOS": ",".join(w.upper() for w in wfos),
    })

    # Update in-memory settings and reload the broadcaster
    from meshcore_weather.config import settings
    settings.home_cities = ",".join(cities)
    settings.home_states = ",".join(s.upper() for s in states)
    settings.home_wfos = ",".join(w.upper() for w in wfos)

    bot = request.app.state.bot
    if bot and getattr(bot, "_broadcaster", None):
        bot._broadcaster.reload_coverage()
        new_cov = bot._broadcaster.coverage
        return JSONResponse({
            "ok": True,
            "summary": new_cov.summary(),
            "zone_count": len(new_cov.zones),
            "region_ids": sorted(new_cov.region_ids),
        })
    return JSONResponse({"ok": True, "summary": "broadcaster not running"})


def _update_env_file(updates: dict[str, str]) -> None:
    """Update or append given keys in .env, preserving other lines."""
    if not _ENV_PATH.exists():
        _ENV_PATH.write_text("")
    lines = _ENV_PATH.read_text().splitlines()
    seen = set()
    out = []
    for line in lines:
        stripped = line.strip()
        if "=" in stripped and not stripped.startswith("#"):
            key = stripped.split("=", 1)[0].strip()
            if key in updates:
                out.append(f"{key}={updates[key]}")
                seen.add(key)
                continue
        out.append(line)
    for key in updates:
        if key not in seen:
            out.append(f"{key}={updates[key]}")
    _ENV_PATH.write_text("\n".join(out) + "\n")


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

    # Tag each with coverage status
    for w in all_warnings:
        if coverage is None or coverage.is_empty():
            w["in_coverage"] = True
        else:
            zones = w.get("zones", [])
            w["in_coverage"] = (
                coverage.covers_any(zones)
                or coverage.covers_polygon(w.get("vertices", []))
            )
        # Strip vertices from JSON (large); just send bbox
        verts = w.get("vertices", [])
        if verts:
            lats = [v[0] for v in verts]
            lons = [v[1] for v in verts]
            w["bbox"] = [min(lats), min(lons), max(lats), max(lons)]
        else:
            w["bbox"] = None

    return JSONResponse({"warnings": all_warnings, "count": len(all_warnings)})


# -- EMWIN product browser --

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
    })


@router.post("/actions/broadcast")
async def trigger_broadcast(request: Request) -> JSONResponse:
    """Manually trigger a broadcast cycle."""
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    if not broadcaster:
        raise HTTPException(400, "Broadcaster not running")
    await broadcaster._broadcast_all()
    return JSONResponse({"ok": True})
