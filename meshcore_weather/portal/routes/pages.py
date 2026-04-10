"""HTML page routes for the portal."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    templates = request.app.state.templates
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    coverage = broadcaster.coverage if broadcaster else None
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "page": "index",
            "coverage": coverage,
            "product_count": len(bot.store._products),
            "channel_name": bot.radio.channel_idx if bot.radio else None,
            "data_channel": bot.radio.data_channel_idx if bot.radio else None,
        },
    )


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    templates = request.app.state.templates
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    coverage = broadcaster.coverage if broadcaster else None
    return templates.TemplateResponse(
        request,
        "config.html",
        {"page": "config", "coverage": coverage},
    )


@router.get("/data", response_class=HTMLResponse)
async def data_page(request: Request):
    templates = request.app.state.templates
    bot = request.app.state.bot
    broadcaster = getattr(bot, "_broadcaster", None)
    coverage = broadcaster.coverage if broadcaster else None
    return templates.TemplateResponse(
        request,
        "data.html",
        {"page": "data", "coverage": coverage},
    )


@router.get("/products", response_class=HTMLResponse)
async def products_page(request: Request):
    templates = request.app.state.templates
    bot = request.app.state.bot
    types = sorted({p.product_type for p in bot.store._products.values()})
    offices = sorted({p.office for p in bot.store._products.values() if p.office})
    states = sorted({p.state for p in bot.store._products.values() if p.state})
    return templates.TemplateResponse(
        request,
        "products.html",
        {
            "page": "products",
            "types": types,
            "offices": offices,
            "states": states,
        },
    )


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "status.html",
        {"page": "status"},
    )
