"""Main entry point - wires EMWIN data, parser, and Meshcore radio together."""

import asyncio
import json
import logging
import re
import signal
import sys
import time
from pathlib import Path

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
    "metar/taf <ICAO> | more\n"
    "DM me for private replies"
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

    _CONTACTS_FILE = Path(settings.data_dir) / "known_contacts.json"

    def __init__(self):
        self.emwin = create_source()
        self.radio = MeshcoreRadio()
        self.store = WeatherStore()
        self._running = False
        self._refresh_task: asyncio.Task | None = None
        self._broadcaster = None  # MeshWXBroadcaster, created if data channel configured
        self._portal = None  # PortalServer, created if portal enabled
        self._paging: dict[str, dict] = {}  # sender_key -> {full, offset, ts}
        self._rate_limit: dict[str, float] = {}
        # Map sender names to pubkey prefixes — persisted to disk
        self._known_contacts: dict[str, str] = {}  # name -> pubkey_prefix
        # Names where DM has failed — don't try again until they re-advert
        self._dm_blocked: set[str] = set()
        # Track channel usage for unknown contacts
        self._channel_uses: dict[str, int] = {}  # sender_name -> count
        self._max_channel_replies = 3  # free channel replies before cutoff
        # Track consecutive channel msgs from known contacts (DM may not be working)
        self._dm_misses: dict[str, int] = {}  # sender_name -> consecutive ch msgs without DM
        self._max_dm_misses = 2  # after this many, assume DM isn't working
        self._load_known_contacts()

    async def start(self) -> None:
        logger.info("Starting Meshcore Weather Bot")
        logger.info("  Serial port: %s", settings.serial_port)
        logger.info("  EMWIN source: %s", settings.emwin_source)
        logger.info("  Channel: %s", settings.meshcore_channel)

        resolver.load()
        self.radio.on_channel_message(self._handle_channel_message)
        self.radio.on_dm(self._handle_dm)
        self.radio.on_advert(self._handle_advert)

        await self.emwin.start()
        await self.radio.start()
        await self._refresh_store()

        self._running = True
        self._refresh_task = asyncio.create_task(self._refresh_loop())

        # Start MeshWX binary broadcaster if data channel is configured
        if self.radio.data_channel_idx is not None:
            from meshcore_weather.protocol.broadcaster import MeshWXBroadcaster
            self._broadcaster = MeshWXBroadcaster(self.store, self.radio)
            await self._broadcaster.start()

            # Register discovery ping handler — bots respond to pings on #meshwx-discover
            if self.radio.discover_channel_idx is not None:
                self.radio.on_discover_ping(self._broadcaster.scheduler.respond_to_discovery_ping)

        # Start local operator web portal if enabled
        if settings.portal_enabled:
            try:
                from meshcore_weather.portal.server import PortalServer
                self._portal = PortalServer(self)
                await self._portal.start()
            except ImportError as e:
                logger.warning("Portal disabled: %s (run `pip install meshcore-weather[portal]`)", e)

        logger.info(
            "Weather bot is running. Listening on channel %d (%s) + DMs",
            self.radio.channel_idx,
            settings.meshcore_channel,
        )

    async def stop(self) -> None:
        logger.info("Shutting down Weather Bot")
        self._running = False
        if self._portal:
            await self._portal.stop()
        if self._broadcaster:
            await self._broadcaster.stop()
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

    # -- Message handling --

    async def _handle_channel_message(self, channel: str, sender: str, text: str) -> None:
        """Handle a message received on a channel."""
        ch = int(channel)
        if ch != self.radio.channel_idx or ch == 0:
            return

        text = text.strip()
        if not text:
            return

        # Binary data requests (WXQ / MWX prefix) work on the text channel
        # too, not just via DM. This is the multi-hop reliability fallback:
        # DM routing can be flaky over 1-2 hops but channel flood routing
        # is more robust. Clients that can't DM (e.g. a future e-ink
        # receiver) also use this path. Responses still go out on the data
        # channel regardless of how the request arrived.
        if text.startswith("WXQ") and self._broadcaster:
            logger.info("Channel WXQ request from %s (ch %d)", sender, ch)
            await self._handle_meshwx_data_request(text, "", sender)
            return
        if text.startswith("MWX") and len(text) >= 7 and self._broadcaster:
            logger.info("Channel MWX request from %s (ch %d)", sender, ch)
            await self._handle_meshwx_refresh(text, "", sender)
            return

        if not self._rate_check(sender):
            return

        command, location = await self._parse(text)

        # Try to resolve sender for DM; if unknown, advert so they can discover us
        pubkey = self._resolve_sender_key(sender)
        if not pubkey:
            await self.radio._send_advert()
        if pubkey:
            # Track consecutive channel msgs — if they keep using the channel
            # instead of DM, they probably aren't receiving our DMs
            misses = self._dm_misses.get(sender, 0) + 1
            self._dm_misses[sender] = misses

            if misses > self._max_dm_misses:
                # DM isn't working — block until they re-advert
                logger.info("%s sent %d channel msgs without DMing back — blocking DM, using channel",
                            sender, misses)
                self._dm_blocked.add(sender)
                self._known_contacts.pop(sender, None)
                self._dm_misses.pop(sender, None)
                await self._respond_channel(ch, sender, command, location)
            else:
                self._channel_uses.pop(sender, None)
                await self._respond_dm(pubkey, sender, command, location)
        else:
            await self._respond_channel(ch, sender, command, location)

    async def _handle_dm(self, pubkey_prefix: str, sender_name: str, text: str) -> None:
        """Handle a direct message."""
        text = text.strip()
        if not text:
            return

        prefix = self._normalize_key(pubkey_prefix)
        # Binary data requests (WXQ/MWX prefixes) bypass the 5-second per-user
        # rate check — they have their own per-(data_type, location) rate limit
        # at the broadcaster level (5 min), and iOS clients legitimately fire
        # multiple binary requests back-to-back when fetching different data
        # types for the same location. The 5-second check is meant for human
        # users typing text commands like "wx austin" / "forecast", not for
        # apps doing structured queries.
        is_binary_request = text.startswith("WXQ") or text.startswith("MWX")
        if not is_binary_request and not self._rate_check(prefix):
            logger.debug("DM rate-limited from %s", sender_name)
            return

        # Parse @lat,lng prefix for location-aware commands
        loc_match = re.match(r"^@(-?\d+\.?\d*),(-?\d+\.?\d*)\s+(.*)", text)
        if loc_match:
            lat = float(loc_match.group(1))
            lon = float(loc_match.group(2))
            text = loc_match.group(3)
            if not hasattr(self, "_user_locations"):
                self._user_locations = {}
            self._user_locations[prefix] = (lat, lon)
            logger.info("Cached location for %s: %.4f, %.4f", sender_name, lat, lon)

        # They're DMing us — DMs work both ways, clear all blocks
        if sender_name and sender_name != "unknown":
            is_new = sender_name not in self._known_contacts
            self._known_contacts[sender_name] = prefix
            self._dm_misses.pop(sender_name, None)
            self._dm_blocked.discard(sender_name)
            if is_new:
                self._save_known_contacts()
            self._channel_uses.pop(sender_name, None)

        # MeshWX refresh request (e.g. "MWX310000")
        if text.startswith("MWX") and len(text) >= 7 and self._broadcaster:
            await self._handle_meshwx_refresh(text, prefix, sender_name)
            return

        # MeshWX v2 data request (e.g. "WXQ" + hex-encoded 0x02 message)
        if text.startswith("WXQ") and self._broadcaster:
            await self._handle_meshwx_data_request(text, prefix, sender_name)
            return

        # Admin commands (DM-only, verified by pubkey)
        if self._is_admin(prefix):
            result = await self._handle_admin(text, prefix, sender_name)
            if result is not None:
                return

        command, location = await self._parse(text)
        await self._respond_dm(prefix, sender_name, command, location)

    async def _handle_advert(self, contact_name: str, pubkey_prefix: str) -> None:
        """Handle a new advert — only greet users who were using the channel."""
        prefix = self._normalize_key(pubkey_prefix)

        # If they already DM us fine, just update the mapping — no greeting
        if contact_name in self._known_contacts:
            return

        # Remember this new contact
        if contact_name and contact_name != "unknown":
            self._known_contacts[contact_name] = prefix
            self._save_known_contacts()

        # Unblock DM if they were blocked
        was_blocked = contact_name in self._dm_blocked
        self._dm_blocked.discard(contact_name)

        # Only welcome users who were hitting the channel (they needed to advert)
        uses = self._channel_uses.pop(contact_name, 0)
        if uses > 0 or was_blocked:
            # Re-advert so the user's device picks us up too
            await self.radio._send_advert()
            await asyncio.sleep(2)
            logger.info("Advert from channel user %s — sending DM welcome", contact_name)
            await self.radio.send_dm(
                prefix,
                f"Hi {contact_name}! I can now reply via DM.\n"
                "Send me wx/forecast/warn commands here."
            )

    async def _handle_meshwx_refresh(self, text: str, prefix: str, sender_name: str) -> None:
        """Handle a MeshWX refresh request DM (e.g. 'MWX310000')."""
        try:
            region_byte = int(text[3:5], 16)
            region_id = (region_byte >> 4) & 0x0F
            request_type = region_byte & 0x0F
            client_newest = int(text[5:9], 16) if len(text) >= 9 else 0
        except (ValueError, IndexError):
            return
        logger.info("MeshWX refresh from %s: region=0x%X type=%d newest=%d",
                     sender_name, region_id, request_type, client_newest)
        await self._broadcaster.broadcast_region(region_id, request_type)

    async def _handle_meshwx_data_request(
        self, text: str, prefix: str, sender_name: str
    ) -> None:
        """Handle a MeshWX v2 data request DM.

        Format: 'WXQ' + hex-encoded 0x02 data request message.
        The bot parses the request, builds the response, and broadcasts
        it on the data channel so all listeners benefit.
        """
        from meshcore_weather.protocol.meshwx import unpack_data_request
        try:
            payload = bytes.fromhex(text[3:].strip())
            req = unpack_data_request(payload)
        except (ValueError, IndexError) as e:
            logger.warning("Bad WXQ request from %s: %s", sender_name, e)
            return

        logger.info(
            "MeshWX data request from %s: type=%d loc=%s",
            sender_name, req["data_type"], req["location"],
        )

        try:
            await self._broadcaster.respond_to_data_request(req)
        except Exception:
            logger.exception("Data request handler failed")

    def _is_admin(self, pubkey_prefix: str) -> bool:
        admin = settings.admin_key.lower().strip()
        return bool(admin) and pubkey_prefix.startswith(admin)

    async def _handle_admin(self, text: str, prefix: str, sender_name: str) -> str | None:
        """Handle admin commands. Returns response string, or None if not an admin command."""
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower() if parts else ""
        arg = parts[1].strip() if len(parts) > 1 else ""

        if cmd == "contacts":
            await self.radio._mc.ensure_contacts(follow=True)
            contacts = self.radio._mc._contacts or {}
            if not contacts:
                reply = "No contacts on device."
            else:
                lines = [f"{len(contacts)} contacts:"]
                for c in contacts.values():
                    name = c.get("adv_name", "?")
                    key = c.get("public_key", "")[:12]
                    lines.append(f" {name} ({key})")
                reply = "\n".join(lines)
            await self._send_dm_paginated(prefix, sender_name, reply)
            return reply

        if cmd == "remove" and arg:
            await self.radio._mc.ensure_contacts(follow=True)
            contact = self.radio._mc.get_contact_by_name(arg)
            if not contact:
                await self.radio.send_dm(prefix, f"Contact '{arg}' not found.")
                return "not found"
            key = contact.get("public_key", "")
            name = contact.get("adv_name", "?")
            # Confirm before removing (can't DM after delete)
            is_self = self._normalize_key(key) == prefix
            if is_self:
                await self.radio.send_dm(prefix, f"Removing: {name} (you). Re-advert to reconnect.")
            else:
                await self.radio.send_dm(prefix, f"Removing: {name}")
            try:
                await self.radio._mc.commands.remove_contact(key)
                self._known_contacts.pop(name, None)
                logger.info("Admin %s removed contact: %s", sender_name, name)
            except Exception as e:
                logger.warning("Failed to remove %s: %s", name, e)
            return "removed"

        if cmd == "clear-contacts":
            await self.radio._mc.ensure_contacts(follow=True)
            contacts = self.radio._mc._contacts or {}
            if not contacts:
                await self.radio.send_dm(prefix, "No contacts to remove.")
                return "empty"
            contact_list = list(contacts.values())
            await self.radio.send_dm(prefix, f"Clearing {len(contact_list)} contacts. Re-advert to reconnect.")
            removed = 0
            for c in contact_list:
                try:
                    await self.radio._mc.commands.remove_contact(c["public_key"])
                    self._known_contacts.pop(c.get("adv_name", ""), None)
                    removed += 1
                except Exception:
                    pass
            logger.info("Admin %s cleared %d/%d contacts", sender_name, removed, len(contact_list))
            return "cleared"

        if cmd == "advert":
            await self.radio._send_advert()
            await self.radio._mc.ensure_contacts(follow=True)
            await self.radio.send_dm(prefix, "Advert sent + contacts refreshed.")
            return "advert"

        if cmd == "refresh":
            await self.radio._mc.ensure_contacts(follow=True)
            count = len(self.radio._mc._contacts or [])
            await self.radio.send_dm(prefix, f"Contacts refreshed: {count} contacts.")
            return "refresh"

        if cmd == "broadcast":
            if not self._broadcaster:
                await self.radio.send_dm(prefix, "MeshWX broadcast not enabled.")
                return "disabled"
            await self.radio.send_dm(prefix, "Running scheduler tick...")
            try:
                sent = await self._broadcaster.scheduler.tick()
                await self.radio.send_dm(prefix, f"Scheduler tick sent {sent} message(s).")
            except Exception as e:
                await self.radio.send_dm(prefix, f"Broadcast error: {e}")
            return "broadcast"

        if cmd == "radar":
            if not self._broadcaster:
                await self.radio.send_dm(prefix, "MeshWX broadcast not enabled.")
                return "disabled"
            await self.radio.send_dm(prefix, "Running radar job now...")
            try:
                sent = await self._broadcaster.scheduler.run_job_now("radar-coverage")
                await self.radio.send_dm(prefix, f"Sent {sent} radar grid(s).")
            except Exception as e:
                await self.radio.send_dm(prefix, f"Radar error: {e}")
            return "radar"

        if cmd == "warnings-broadcast":
            if not self._broadcaster:
                await self.radio.send_dm(prefix, "MeshWX broadcast not enabled.")
                return "disabled"
            try:
                sent = await self._broadcaster.scheduler.run_job_now("warnings-coverage")
                await self.radio.send_dm(prefix, f"Sent {sent} warning message(s).")
            except Exception as e:
                await self.radio.send_dm(prefix, f"Warning broadcast error: {e}")
            return "warnings-broadcast"

        if cmd == "test-data-ch":
            ch = self.radio.data_channel_idx
            if ch is None:
                await self.radio.send_dm(prefix, "No data channel configured.")
                return "no ch"
            await self.radio._mc.commands.send_chan_msg(ch, "MeshWX test ping")
            await self.radio.send_dm(prefix, f"Sent text test on ch {ch}.")
            return "test"

        if cmd == "admin":
            reply = (
                "Admin commands (DM only):\n"
                "contacts - list contacts\n"
                "remove <name> - remove contact\n"
                "clear-contacts - remove all\n"
                "advert - send advert now\n"
                "refresh - reload contacts\n"
                "broadcast - send radar+warnings\n"
                "radar - send radar only\n"
                "warnings-broadcast - send warnings"
            )
            await self._send_dm_paginated(prefix, sender_name, reply)
            return reply

        return None  # Not an admin command, fall through to normal handling

    async def _send_dm_paginated(self, pubkey: str, sender_name: str, text: str) -> None:
        """Send a potentially long response as paginated DMs."""
        chunk, offset, has_more = paginate(text, 0)
        if has_more:
            self._paging[pubkey] = {"full": text, "offset": offset, "ts": time.time()}
        await self.radio.send_dm(pubkey, chunk)

    # -- Response methods --

    async def _respond_channel(self, channel: int, sender: str, command: str, location: str) -> None:
        """Send response on the channel for unknown contacts.

        Allows a few free replies, then asks them to advert for DM.
        Every reply includes a DM nudge that gets more insistent.
        """
        uses = self._channel_uses.get(sender, 0) + 1
        self._channel_uses[sender] = uses

        if uses > self._max_channel_replies:
            await self.radio.send_channel_message(
                channel,
                f"@[{sender}] Please send an advert so I can DM you. "
                "Channel replies are limited to reduce spam."
            )
            return

        response, sender_key, already_paginated = self._get_response(command, location, sender)
        if not response:
            return

        if already_paginated:
            chunk = response
        else:
            chunk, offset, has_more = paginate(response, 0)
            if has_more:
                self._paging[sender_key] = {"full": response, "offset": offset, "ts": time.time()}
            elif sender_key in self._paging:
                del self._paging[sender_key]

        logger.info("Response to %s (ch %d/%d): %s",
                     sender, uses, self._max_channel_replies,
                     chunk.replace("\n", " | "))
        await self.radio.send_channel_message(channel, chunk)

        # Append DM nudge after a brief delay (radio needs time between sends)
        await asyncio.sleep(2)
        if uses == 1:
            nudge = f"@[{sender}] Tip: send an advert & DM me for private replies"
        else:
            nudge = f"@[{sender}] Send an advert so I can reply via DM ({self._max_channel_replies - uses} ch replies left)"
        logger.info("Nudge to %s: %s", sender, nudge)
        await self.radio.send_channel_message(channel, nudge)

    async def _respond_dm(self, pubkey_prefix: str, sender_name: str, command: str, location: str) -> None:
        """Send response as a DM. Falls back to channel if DM fails."""
        sender_key = pubkey_prefix
        response, sender_key, already_paginated = self._get_response(command, location, sender_key)
        if not response:
            return

        if already_paginated:
            chunk = response
        else:
            chunk, offset, has_more = paginate(response, 0)
            if has_more:
                self._paging[sender_key] = {"full": response, "offset": offset, "ts": time.time()}
            elif sender_key in self._paging:
                del self._paging[sender_key]

        success = await self.radio.send_dm(pubkey_prefix, chunk)
        if success:
            logger.info("Response to %s (DM): %s", sender_name, chunk.replace("\n", " | "))
        else:
            # DM failed — block further DM attempts until they re-advert
            logger.info("DM to %s failed, blocking DM and falling back to channel", sender_name)
            self._dm_blocked.add(sender_name)
            self._known_contacts.pop(sender_name, None)
            self._dm_misses.pop(sender_name, None)
            await self.radio._send_advert()
            await self.radio.send_channel_message(self.radio.channel_idx, chunk)
            await asyncio.sleep(2)
            await self.radio.send_channel_message(
                self.radio.channel_idx,
                f"@[{sender_name}] Send an advert so I can reply via DM"
            )

    def _get_response(self, command: str, location: str, sender_key: str) -> tuple[str | None, str, bool]:
        """Process command and handle pagination.

        Returns (response_text, sender_key, already_paginated).
        If already_paginated is True, the caller should send as-is without re-paginating.
        """
        now = time.time()

        # Clean expired paging sessions
        cutoff = now - 300
        self._paging = {k: v for k, v in self._paging.items() if v["ts"] > cutoff}

        # Handle "more" pagination — returns a ready-to-send chunk
        if command == "more":
            session = self._paging.get(sender_key)
            if session:
                chunk, new_offset, has_more = paginate(session["full"], session["offset"])
                if has_more:
                    session["offset"] = new_offset
                    session["ts"] = now
                else:
                    del self._paging[sender_key]
                return chunk, sender_key, True
            return "No more data. Send a command first.", sender_key, True

        response = self._process_command(command, location)
        return response, sender_key, False

    # -- Helpers --

    @staticmethod
    def _normalize_key(key: str) -> str:
        """Normalize a pubkey to 12-char prefix for consistent session keying."""
        return key[:12].lower()

    def _resolve_sender_key(self, sender_name: str) -> str | None:
        """Try to find a pubkey prefix for a channel message sender so we can DM them."""
        # Don't try DM for contacts where it has previously failed
        if sender_name in self._dm_blocked:
            return None
        # Check our learned contacts first
        if sender_name in self._known_contacts:
            return self._known_contacts[sender_name]
        # Try the device's contact list
        contact = self.radio.find_contact_by_name(sender_name)
        if contact:
            pubkey = contact.get("public_key", "")
            if pubkey:
                prefix = self._normalize_key(pubkey)
                self._known_contacts[sender_name] = prefix
                self._save_known_contacts()
                return prefix
        return None

    def _load_known_contacts(self) -> None:
        try:
            if self._CONTACTS_FILE.exists():
                self._known_contacts = json.loads(self._CONTACTS_FILE.read_text())
                logger.info("Loaded %d known contacts from disk", len(self._known_contacts))
        except Exception:
            logger.debug("Could not load known contacts")

    def _save_known_contacts(self) -> None:
        try:
            self._CONTACTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            self._CONTACTS_FILE.write_text(json.dumps(self._known_contacts))
        except Exception:
            logger.debug("Could not save known contacts")

    def _rate_check(self, sender_key: str) -> bool:
        now = time.time()
        last = self._rate_limit.get(sender_key, 0)
        if now - last < 5:
            return False
        self._rate_limit[sender_key] = now
        return True

    async def _parse(self, text: str) -> tuple[str, str]:
        text = text[:200]
        text = "".join(c for c in text if c.isprintable() or c in "\n ")
        intent = await parse_intent(text)
        command = intent["command"]
        location = intent["location"]
        location = "".join(c for c in location if c.isalnum() or c in " ,.-'")[:50]
        return command, location

    @staticmethod
    def _to_state_code(text: str) -> str | None:
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
