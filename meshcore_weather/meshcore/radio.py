"""Meshcore radio interface using the official meshcore Python library.

Uses meshcore_py to communicate with a Meshcore device over USB serial.
Listens for incoming channel messages and DMs, sends responses.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from meshcore import MeshCore, EventType

from meshcore_weather.config import settings

logger = logging.getLogger(__name__)

# How often to re-advertise and refresh contacts (seconds)
ADVERT_INTERVAL = 900  # 15 minutes
CONTACTS_REFRESH = 120  # 2 minutes


class MeshcoreRadio:
    """Interface to a Meshcore radio device using the official library."""

    def __init__(self):
        self._mc: MeshCore | None = None
        self._running = False
        self._channel_idx: int | None = None
        self._data_channel_idx: int | None = None
        self._discover_channel_idx: int | None = None
        self._channel_handler: Callable | None = None
        self._discover_handler: Callable | None = None
        self._dm_handler: Callable | None = None
        self._advert_handler: Callable | None = None
        self._advert_task: asyncio.Task | None = None
        self._contacts_task: asyncio.Task | None = None
        # Shared send lock — prevents the scheduler and on-demand
        # request handler from interleaving messages on the data channel.
        # Without this, a client DM triggering respond_to_data_request
        # while the scheduler is mid-tick sending radar chunks would
        # cause mixed messages on the wire.
        self.send_lock: asyncio.Lock = asyncio.Lock()

    def on_channel_message(self, handler: Callable) -> None:
        """Register handler: async def handler(channel, sender_name, text)"""
        self._channel_handler = handler

    # Keep old name for backwards compat during transition
    def on_message(self, handler: Callable) -> None:
        self._channel_handler = handler

    def on_advert(self, handler: Callable) -> None:
        """Register handler: async def handler(contact_name, pubkey_prefix)"""
        self._advert_handler = handler

    def on_discover_ping(self, handler: Callable) -> None:
        """Register handler: async def handler() — called when a ping arrives on discovery channel"""
        self._discover_handler = handler

    def on_dm(self, handler: Callable) -> None:
        """Register handler: async def handler(pubkey_prefix, sender_name, text)"""
        self._dm_handler = handler

    async def start(self) -> None:
        """Connect to Meshcore radio via serial or TCP."""
        port = settings.serial_port
        baud = settings.serial_baud

        if port.startswith("tcp://"):
            host_port = port[6:]
            host, tcp_port = host_port.rsplit(":", 1)
            logger.info("Connecting to Meshcore radio via TCP %s:%s", host, tcp_port)
            self._mc = await MeshCore.create_tcp(host, int(tcp_port))
        else:
            logger.info("Connecting to Meshcore radio on %s @ %d baud", port, baud)
            self._mc = await MeshCore.create_serial(port, baud)
        self._running = True

        # Resolve channel name to index
        self._channel_idx = await self._resolve_channel(settings.meshcore_channel)
        logger.info("Listening on channel %d (%s)", self._channel_idx, settings.meshcore_channel)

        # Resolve data channel for MeshWX binary protocol (if configured)
        if settings.meshwx_channel:
            try:
                self._data_channel_idx = await self._resolve_channel(settings.meshwx_channel)
                logger.info("Data channel %d (%s)", self._data_channel_idx, settings.meshwx_channel)
            except ValueError:
                # Channel doesn't exist — create it on a free slot
                created = await self._create_channel(settings.meshwx_channel)
                if created is not None:
                    self._data_channel_idx = created
                    logger.info("Created data channel %d (%s)", created, settings.meshwx_channel)
                else:
                    logger.warning("Could not create data channel '%s' — no free slots",
                                   settings.meshwx_channel)

        # Resolve discovery channel for beacon broadcasts
        if settings.meshwx_discover_channel:
            try:
                self._discover_channel_idx = await self._resolve_channel(settings.meshwx_discover_channel)
                logger.info("Discovery channel %d (%s)", self._discover_channel_idx, settings.meshwx_discover_channel)
            except ValueError:
                created = await self._create_channel(settings.meshwx_discover_channel)
                if created is not None:
                    self._discover_channel_idx = created
                    logger.info("Created discovery channel %d (%s)", created, settings.meshwx_discover_channel)
                else:
                    logger.warning("Could not create discovery channel '%s' — no free slots",
                                   settings.meshwx_discover_channel)

        # Subscribe to channel messages, DMs, and new adverts
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
        self._mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm)
        self._mc.subscribe(EventType.ADVERTISEMENT, self._on_advert)

        # Start auto-fetching messages from the device
        await self._mc.start_auto_message_fetching()

        # Ensure auto-add contacts is enabled so adverts create contacts
        try:
            await self._mc.commands.set_autoadd_config(1)
            logger.info("Auto-add contacts enabled")
        except Exception:
            logger.debug("Could not set auto-add config")

        # Auto-refresh contacts when adverts arrive
        self._mc.auto_update_contacts = True

        # Load contacts and advertise ourselves
        await self._mc.ensure_contacts()
        await self._send_advert()

        # Periodic tasks: re-advert and refresh contacts
        self._advert_task = asyncio.create_task(self._advert_loop())
        self._contacts_task = asyncio.create_task(self._contacts_loop())

        logger.info("Meshcore radio connected. Node: %s", self._mc.self_info.get("adv_name", "?"))

    async def _create_channel(self, channel_name: str) -> int | None:
        """Create a channel on the first free or reusable slot. Returns index or None."""
        slots: dict[int, str] = {}
        for i in range(8):
            try:
                ch = await self._mc.commands.get_channel(i)
                name = ch.payload.get("channel_name", "")
                slots[i] = name
            except Exception:
                break

        # First: check if there's a stale version of this channel (e.g. #name vs name)
        bare = channel_name.lstrip("#")
        for i, name in slots.items():
            if i == 0:
                continue
            if name.lstrip("#") == bare and name != channel_name:
                logger.info("Overwriting stale channel %d (%s -> %s)", i, name, channel_name)
                try:
                    result = await self._mc.commands.set_channel(i, channel_name)
                    if result.type == EventType.OK:
                        return i
                except Exception:
                    pass

        # Otherwise find a free slot
        for i in range(1, 8):
            if not slots.get(i):
                try:
                    result = await self._mc.commands.set_channel(i, channel_name)
                    if result.type == EventType.OK:
                        return i
                except Exception:
                    logger.debug("Failed to create channel on slot %d", i)
        return None

    async def _resolve_channel(self, channel_ref: str) -> int:
        try:
            return int(channel_ref)
        except ValueError:
            pass
        for i in range(8):
            try:
                ch = await self._mc.commands.get_channel(i)
                name = ch.payload.get("channel_name", "")
                if name == channel_ref:
                    return i
            except Exception:
                break
        raise ValueError(
            f"Channel '{channel_ref}' not found on this device. "
            f"Create it first or use a channel index (0-7)."
        )

    async def stop(self) -> None:
        self._running = False
        for task in (self._advert_task, self._contacts_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        if self._mc:
            await self._mc.disconnect()
        logger.info("Meshcore radio disconnected")

    # -- Sending --

    async def send_channel_message(self, channel: int, text: str) -> None:
        """Send a message on our dedicated channel. Never sends on ch 0."""
        if not self._mc:
            logger.error("Cannot send - not connected")
            return
        if channel == 0 or channel != self._channel_idx:
            logger.warning("Blocked send on ch %d (our ch is %d)", channel, self._channel_idx)
            return
        try:
            await self._mc.commands.send_chan_msg(channel, text)
            logger.debug("Sent to ch %d: %s", channel, text[:60])
        except Exception:
            logger.exception("Failed to send channel message")

    async def send_binary_channel(self, payload: bytes) -> None:
        """Send raw binary data on the MeshWX data channel.

        Acquires the shared send_lock so that scheduled broadcasts and
        on-demand request responses never interleave their messages.
        Without this, a client DM arriving mid-broadcast-tick would
        cause mixed message sequences on the wire (e.g., radar chunk 3
        followed by a forecast response followed by radar chunk 4).

        Bypasses send_chan_msg (which UTF-8 encodes) by constructing
        the channel message packet directly with raw bytes.
        """
        if not self._mc or self._data_channel_idx is None:
            return
        async with self.send_lock:
            import time as _time
            ts_bytes = int(_time.time()).to_bytes(4, "little")
            data = (
                b"\x03\x00"
                + self._data_channel_idx.to_bytes(1, "little")
                + ts_bytes
                + payload
            )
            try:
                result = await self._mc.commands.send(data, [EventType.OK, EventType.ERROR])
                if result.type == EventType.ERROR:
                    logger.warning("Binary send failed on data ch %d: %s", self._data_channel_idx, result.payload)
                else:
                    logger.info("Binary sent on ch %d: %d bytes (type 0x%02x)",
                                self._data_channel_idx, len(payload), payload[0] if payload else 0)
            except Exception:
                logger.exception("Failed to send binary on data channel")

    async def send_beacon(self, payload: bytes) -> None:
        """Send a beacon on the discovery channel."""
        if not self._mc or self._discover_channel_idx is None:
            return
        async with self.send_lock:
            import time as _time
            ts_bytes = int(_time.time()).to_bytes(4, "little")
            data = (
                b"\x03\x00"
                + self._discover_channel_idx.to_bytes(1, "little")
                + ts_bytes
                + payload
            )
            try:
                result = await self._mc.commands.send(data, [EventType.OK, EventType.ERROR])
                if result.type == EventType.ERROR:
                    logger.warning("Beacon send failed on ch %d", self._discover_channel_idx)
                else:
                    logger.info("Beacon sent on discovery ch %d (%d bytes)",
                                self._discover_channel_idx, len(payload))
            except Exception:
                logger.exception("Failed to send beacon")

    async def send_dm(self, pubkey_prefix: str, text: str) -> bool:
        """Send a direct message to a contact by their public key prefix."""
        if not self._mc:
            logger.error("Cannot send DM - not connected")
            return False
        try:
            result = await self._mc.commands.send_msg(pubkey_prefix, text)
            if result.type == EventType.ERROR:
                logger.warning("DM to %s failed: %s", pubkey_prefix[:8], result.payload)
                return False
            logger.debug("DM sent to %s: %s", pubkey_prefix[:8], text[:60])
            return True
        except Exception:
            logger.exception("Failed to send DM to %s", pubkey_prefix[:8])
            return False

    # -- Contact lookup --

    def find_contact_by_name(self, name: str) -> dict | None:
        """Look up a contact by advertised name. Returns contact dict or None."""
        if not self._mc:
            return None
        return self._mc.get_contact_by_name(name)

    def find_contact_by_key(self, pubkey_prefix: str) -> dict | None:
        """Look up a contact by public key prefix. Returns contact dict or None."""
        if not self._mc:
            return None
        return self._mc.get_contact_by_key_prefix(pubkey_prefix)

    # -- Event handlers --

    async def _on_channel_msg(self, event) -> None:
        payload = event.payload
        channel_idx = payload.get("channel_idx", 0)
        text = payload.get("text", "")

        if channel_idx == 0:
            return

        # Discovery channel — check for ping
        if channel_idx == self._discover_channel_idx and self._discover_handler:
            # Any message on the discovery channel triggers a beacon response
            logger.info("Discovery ping received on ch %d", channel_idx)
            try:
                await self._discover_handler()
            except Exception:
                logger.exception("Error in discovery handler")
            return

        # Text command channel
        if channel_idx != self._channel_idx:
            return

        sender = "unknown"
        if ": " in text:
            sender, text = text.split(": ", 1)

        logger.info("Channel msg from %s on ch %d: %s", sender, channel_idx, text[:80])

        if self._channel_handler:
            try:
                await self._channel_handler(str(channel_idx), sender, text)
            except Exception:
                logger.exception("Error in channel message handler")

    async def _on_dm(self, event) -> None:
        payload = event.payload
        pubkey_prefix = payload.get("pubkey_prefix", "")
        text = payload.get("text", "")

        # Resolve sender name from contacts
        sender_name = "unknown"
        contact = self.find_contact_by_key(pubkey_prefix)
        if contact:
            sender_name = contact.get("adv_name", "unknown")

        logger.info("DM from %s (%s): %s", sender_name, pubkey_prefix[:8], text[:80])

        if self._dm_handler:
            try:
                await self._dm_handler(pubkey_prefix, sender_name, text)
            except Exception:
                logger.exception("Error in DM handler")

    async def _on_advert(self, event) -> None:
        """Handle an incoming advertisement from another node."""
        # Refresh contacts to pick up the new node
        try:
            await self._mc.ensure_contacts(follow=True)
        except Exception:
            pass

        if not self._advert_handler:
            return

        # Try to identify who just adverted
        # The event marks contacts dirty; after ensure_contacts we can check
        # We don't get the name directly from the event, but we can check
        # pending contacts
        pending = self._mc._pending_contacts
        for key, contact in list(pending.items()):
            name = contact.get("adv_name", "unknown")
            prefix = key[:12].lower()
            logger.info("New advert from %s (%s)", name, prefix)
            try:
                await self._advert_handler(name, prefix)
            except Exception:
                logger.exception("Error in advert handler")

    # -- Periodic tasks --

    async def _send_advert(self) -> None:
        """Advertise ourselves so other nodes can discover and DM us."""
        try:
            await self._mc.commands.send_advert(flood=True)
            logger.info("Sent advertisement (flood)")
        except Exception:
            logger.exception("Failed to send advert")

    async def _advert_loop(self) -> None:
        while self._running:
            await asyncio.sleep(ADVERT_INTERVAL)
            await self._send_advert()
            # Refresh contacts right after advert to pick up new peers
            try:
                await self._mc.ensure_contacts(follow=True)
            except Exception:
                pass

    async def _contacts_loop(self) -> None:
        while self._running:
            await asyncio.sleep(CONTACTS_REFRESH)
            try:
                await self._mc.ensure_contacts(follow=True)
            except Exception:
                logger.debug("Failed to refresh contacts")

    @property
    def channel_idx(self) -> int | None:
        return self._channel_idx

    @property
    def data_channel_idx(self) -> int | None:
        return self._data_channel_idx

    @property
    def discover_channel_idx(self) -> int | None:
        return self._discover_channel_idx
