"""Fetch EMWIN weather products from NOAA internet sources.

Strategy:
1. On startup, download the 3-hour bundle for initial coverage
2. Every 2 minutes, download the 2-minute bundle (~43KB) for new products
3. Accumulate products over time, expire after 12 hours
4. This ensures zone forecasts (issued every 6-12h) stay available

The EMWIN ZIP bundles use the same file format as the GOES satellite
downlink, so switching to SDR later requires no parser changes.
"""

import asyncio
import io
import json
import logging
import re
import zipfile
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

from meshcore_weather.config import settings

logger = logging.getLogger(__name__)

EMWIN_FILENAME_RE = re.compile(
    r"A_(\w{4,6})(\w{4})(\d{6,8})_C_KWIN_\d+_\d+-\d+-(\w+)\.TXT",
    re.IGNORECASE,
)

EMWIN_TS_RE = re.compile(r"_(\d{14})_")


class EMWINSource(ABC):
    @abstractmethod
    async def fetch_products(self) -> list[dict]:
        ...

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...


CACHE_FILE = Path(settings.data_dir) / "emwin_cache" / "products.jsonl"


class InternetSource(EMWINSource):
    """Fetch EMWIN products, accumulate over time, expire old ones."""

    def __init__(self):
        self._client: httpx.AsyncClient | None = None
        self._products: dict[str, dict] = {}  # keyed by filename for dedup
        self._poll_task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=60.0)
        self._running = True

        # Restore cached products from disk
        self._load_cache()

        # Initial load: 3-hour bundle for broad coverage
        logger.info("Initial load from 3-hour bundle...")
        await self._fetch_bundle(settings.emwin_base_url)

        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info(
            "EMWIN source started (%d products, polling every %ds)",
            len(self._products),
            settings.emwin_poll_interval,
        )

    async def stop(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._save_cache()
        if self._client:
            await self._client.aclose()
        logger.info("EMWIN source stopped (%d products in store)", len(self._products))

    async def _poll_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.emwin_poll_interval)
            try:
                # Poll the small 2-minute bundle for new products
                new = await self._fetch_bundle(settings.emwin_poll_url)
                self._expire_old()
                if new:
                    logger.info(
                        "Poll: +%d new products (%d total)",
                        new, len(self._products),
                    )
                    self._save_cache()
            except Exception:
                logger.exception("Error polling EMWIN data")

    async def _fetch_bundle(self, url: str) -> int:
        """Download a ZIP bundle and add products to the store. Returns count of new products."""
        logger.info("Fetching %s", url)
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("HTTP error: %s", e)
            return 0

        extracted = self._extract_zip(resp.content)
        new_count = 0
        for prod in extracted:
            fname = prod.get("filename", "")
            if fname and fname not in self._products:
                self._products[fname] = prod
                new_count += 1
        return new_count

    def _expire_old(self) -> None:
        """Remove products older than max_age_hours."""
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.emwin_max_age_hours)
        before = len(self._products)
        self._products = {
            k: v for k, v in self._products.items()
            if v.get("timestamp", datetime.now(timezone.utc)) > cutoff
        }
        removed = before - len(self._products)
        if removed:
            logger.debug("Expired %d old products", removed)

    def _load_cache(self) -> None:
        """Load products from disk cache on startup."""
        if not CACHE_FILE.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.emwin_max_age_hours)
        count = 0
        try:
            with open(CACHE_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    rec["timestamp"] = datetime.fromisoformat(rec["timestamp"])
                    if rec["timestamp"] < cutoff:
                        continue
                    fname = rec.get("filename", "")
                    if fname and fname not in self._products:
                        self._products[fname] = rec
                        count += 1
        except Exception:
            logger.exception("Error loading cache")
        if count:
            logger.info("Restored %d products from cache", count)

    def _save_cache(self) -> None:
        """Persist current products to disk."""
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                for prod in self._products.values():
                    rec = dict(prod)
                    rec["timestamp"] = rec["timestamp"].isoformat()
                    f.write(json.dumps(rec) + "\n")
            logger.info("Saved %d products to cache", len(self._products))
        except Exception:
            logger.exception("Error saving cache")

    def _extract_zip(self, data: bytes) -> list[dict]:
        products = []
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for name in zf.namelist():
                    if name.lower().endswith(".zip"):
                        try:
                            products.extend(self._extract_zip(zf.read(name)))
                        except Exception:
                            pass
                    elif name.lower().endswith(".txt"):
                        try:
                            raw_text = zf.read(name).decode("utf-8", errors="replace").strip()
                            if raw_text:
                                prod = self._parse_emwin_file(name, raw_text)
                                if prod:
                                    products.append(prod)
                        except Exception:
                            pass
        except zipfile.BadZipFile:
            logger.warning("Invalid ZIP data")
        return products

    def _parse_emwin_file(self, filename: str, raw_text: str) -> dict | None:
        product_id = "UNKNOWN"
        station = "UNKNOWN"
        awips_id = ""

        m = EMWIN_FILENAME_RE.search(filename)
        if m:
            product_id = m.group(1)
            station = m.group(2)
            awips_id = m.group(4)

        if product_id == "UNKNOWN":
            for line in raw_text.splitlines()[:5]:
                parts = line.strip().split()
                if len(parts) >= 2 and len(parts[0]) >= 4 and parts[0].isalnum():
                    product_id = parts[0]
                    station = parts[1]
                    break

        # Extract timestamp from filename
        ts = datetime.now(timezone.utc)
        m_ts = EMWIN_TS_RE.search(filename)
        if m_ts:
            try:
                ts = datetime.strptime(m_ts.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        return {
            "product_id": product_id,
            "station": station,
            "awips_id": awips_id,
            "timestamp": ts,
            "raw_text": raw_text,
            "filename": filename,
        }

    async def fetch_products(self) -> list[dict]:
        return list(self._products.values())


class SDRSource(EMWINSource):
    """Future: Receive EMWIN products via SDR from GOES-16 satellite."""

    async def start(self) -> None:
        raise NotImplementedError(
            "SDR source not yet implemented. "
            "Set MCW_EMWIN_SOURCE=internet to use internet source."
        )

    async def stop(self) -> None:
        pass

    async def fetch_products(self) -> list[dict]:
        return []


def create_source() -> EMWINSource:
    if settings.emwin_source == "sdr":
        return SDRSource()
    return InternetSource()
