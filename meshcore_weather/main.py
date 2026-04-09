"""Main entry point - wires EMWIN data, parser, and Meshcore radio together."""

import asyncio
import logging
import signal
import sys

from meshcore_weather.config import settings
from meshcore_weather.emwin.fetcher import create_source
from meshcore_weather.geodata import resolver
from meshcore_weather.meshcore.radio import MeshcoreRadio
from meshcore_weather.nlp import parse_intent
from meshcore_weather.parser.weather import WeatherStore, paginate

logger = logging.getLogger(__name__)

HELP_TEXT = (
    "GOES-E EMWIN off-grid weather\n"
    "wx = overview | wx <ST> = state\n"
    "wx/forecast/warn <city ST>\n"
    "outlook/rain/storm\n"
    "metar/taf <ICAO> | more"
)


STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "puerto rico": "PR", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC", "guam": "GU", "virgin islands": "VI",
}

VALID_STATES = set(STATE_NAMES.values())


class WeatherBot:
    """Main application: bridges EMWIN weather data to Meshcore radio."""

    def __init__(self):
        self.emwin = create_source()
        self.radio = MeshcoreRadio()
        self.store = WeatherStore()
        self._running = False
        self._refresh_task: asyncio.Task | None = None
        self._paging: dict[str, dict] = {}  # sender -> {full, offset, ts}

    async def start(self) -> None:
        logger.info("Starting Meshcore Weather Bot")
        logger.info("  Serial port: %s", settings.serial_port)
        logger.info("  EMWIN source: %s", settings.emwin_source)
        logger.info("  Channel: %s", settings.meshcore_channel)

        resolver.load()
        self.radio.on_message(self._handle_message)

        await self.emwin.start()
        await self.radio.start()
        await self._refresh_store()

        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        logger.info(
            "Weather bot is running. Listening on channel %d (%s)",
            self.radio.channel_idx,
            settings.meshcore_channel,
        )

    async def stop(self) -> None:
        logger.info("Shutting down Weather Bot")
        self._running = False
        if self._refresh_task:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        await self.radio.stop()
        await self.emwin.stop()
        logger.info("Weather bot stopped")

    async def _refresh_loop(self) -> None:
        while self._running:
            await asyncio.sleep(settings.emwin_poll_interval)
            await self._refresh_store()

    async def _refresh_store(self) -> None:
        products = await self.emwin.fetch_products()
        if products:
            self.store.ingest(products)

    async def _handle_message(self, channel: str, sender: str, text: str) -> None:
        ch = int(channel)
        # Strict channel guard: only respond on our dedicated channel, never ch 0 (public)
        if ch != self.radio.channel_idx or ch == 0:
            return
        text = text.strip()
        if not text:
            return

        # Rate limiting: max 1 request per sender per 5 seconds
        import time
        now = time.time()
        if not hasattr(self, '_rate_limit'):
            self._rate_limit = {}
        last = self._rate_limit.get(sender, 0)
        if now - last < 5:
            return
        self._rate_limit[sender] = now

        # Clean expired paging sessions (older than 5 minutes)
        cutoff = now - 300
        self._paging = {k: v for k, v in self._paging.items() if v["ts"] > cutoff}

        # Input sanitization: limit length, strip control chars
        text = text[:200]
        text = "".join(c for c in text if c.isprintable() or c in "\n ")

        # Parse intent via local LLM (or rigid fallback)
        intent = await parse_intent(text)
        command = intent["command"]
        location = intent["location"]

        # Sanitize location output from LLM - only allow safe chars
        location = "".join(c for c in location if c.isalnum() or c in " ,.-'")[:50]

        # Handle "more" pagination
        if command == "more":
            session = self._paging.get(sender)
            if session:
                chunk, new_offset, has_more = paginate(session["full"], session["offset"])
                if has_more:
                    session["offset"] = new_offset
                    session["ts"] = now
                else:
                    del self._paging[sender]
                logger.info("More to %s: %s", sender, chunk.replace("\n", " | "))
                await self.radio.send_channel_message(int(channel), chunk)
            else:
                await self.radio.send_channel_message(
                    int(channel), "No more data. Send a command first."
                )
            return

        response = self._process_command(command, location)
        if response:
            chunk, offset, has_more = paginate(response, 0)
            if has_more:
                self._paging[sender] = {
                    "full": response,
                    "offset": offset,
                    "ts": now,
                }
            elif sender in self._paging:
                del self._paging[sender]
            logger.info("Response to %s: %s", sender, chunk.replace("\n", " | "))
            await self.radio.send_channel_message(int(channel), chunk)

    @staticmethod
    def _to_state_code(text: str) -> str | None:
        """Convert state name or abbreviation to 2-letter code, or None."""
        t = text.strip()
        if len(t) == 2 and t.upper() in VALID_STATES:
            return t.upper()
        name = t.lower()
        if name in STATE_NAMES:
            return STATE_NAMES[name]
        return None

    def _process_command(self, command: str, location: str) -> str | None:
        if command == "help":
            return HELP_TEXT

        # --- Navigation: bare commands show overview, state narrows, city gets detail ---

        if command == "wx":
            if not location:
                return self.store.national_overview()
            state = self._to_state_code(location)
            if state:
                return self.store.state_overview(state)
            return self.store.get_summary(location)

        if command == "warn":
            if not location:
                return self.store.warn_summary()
            state = self._to_state_code(location)
            if state:
                return self.store.scan_warnings(state)
            return self.store.get_warnings(location)

        if command == "forecast":
            if not location:
                return "Usage: forecast <city ST>"
            return self.store.get_forecast(location)

        if command == "outlook":
            if not location:
                return "Usage: outlook <city ST>"
            return self.store.get_outlook(location)

        if command == "rain":
            state = self._to_state_code(location) if location else ""
            return self.store.scan_rain(state or location)

        if command == "storm":
            state = self._to_state_code(location) if location else ""
            return self.store.get_storm_reports(state or location)

        if command == "metar":
            if not location:
                return "Usage: metar <ICAO>\nEx: metar KAUS"
            return self.store.get_raw_metar(location)

        if command == "taf":
            if not location:
                return "Usage: taf <ICAO>\nEx: taf KJFK"
            return self.store.get_raw_taf(location)

        return None


def main():
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    bot = WeatherBot()
    loop = asyncio.new_event_loop()

    def shutdown(sig):
        logger.info("Received signal %s, shutting down...", sig.name)
        loop.create_task(bot.stop())

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown, sig)

    try:
        loop.run_until_complete(bot.start())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.run_until_complete(bot.stop())
        loop.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
