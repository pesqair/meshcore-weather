"""FastAPI app factory + uvicorn lifecycle for the operator web portal.

Runs alongside the bot as an asyncio task. Serves:
- Static assets (all bundled locally, no CDN)
- Config UI (region targeting)
- Live data viewer (warnings, radar)
- EMWIN product browser
- Bot status dashboard
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from meshcore_weather.config import settings

logger = logging.getLogger(__name__)

_PORTAL_DIR = Path(__file__).parent
_STATIC_DIR = _PORTAL_DIR / "static"
_TEMPLATES_DIR = _PORTAL_DIR / "templates"


def create_app(bot: Any) -> FastAPI:
    """Create a FastAPI app wired to a running bot instance.

    The bot exposes store, radio, emwin, and _broadcaster attributes
    that the routes read from (and occasionally trigger actions on).
    """
    app = FastAPI(
        title="Meshcore Weather Portal",
        docs_url=None,  # disable Swagger UI (depends on CDN assets)
        redoc_url=None,
    )

    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))
    app.state.bot = bot
    app.state.templates = templates

    # Mount static files (served from local disk, no CDN)
    app.mount(
        "/static",
        StaticFiles(directory=str(_STATIC_DIR)),
        name="static",
    )

    # Register routes
    from meshcore_weather.portal.routes import pages, api
    app.include_router(pages.router)
    app.include_router(api.router, prefix="/api")

    return app


class PortalServer:
    """Manages the uvicorn lifecycle as an asyncio task."""

    def __init__(self, bot: Any):
        self.bot = bot
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        app = create_app(self.bot)
        config = uvicorn.Config(
            app,
            host=settings.portal_host,
            port=settings.portal_port,
            log_level="warning",  # uvicorn logs are noisy, let the app log
            access_log=False,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        logger.info(
            "Portal running at http://%s:%d",
            settings.portal_host,
            settings.portal_port,
        )

    async def stop(self) -> None:
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
