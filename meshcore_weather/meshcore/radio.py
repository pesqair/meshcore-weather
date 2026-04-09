"""Meshcore radio interface using the official meshcore Python library.

Uses meshcore_py to communicate with a Meshcore device over USB serial.
Listens for incoming channel messages and sends responses.
"""

import logging
from collections.abc import Callable, Coroutine
from typing import Any

from meshcore import MeshCore, EventType

from meshcore_weather.config import settings

logger = logging.getLogger(__name__)


class MeshcoreRadio:
    """Interface to a Meshcore radio device using the official library."""

    def __init__(self):
        self._mc: MeshCore | None = None
        self._running = False
        self._channel_idx: int | None = None  # Resolved at startup
        self._message_handler: Callable[[str, str, str], Coroutine[Any, Any, None]] | None = None

    def on_message(self, handler: Callable[[str, str, str], Coroutine[Any, Any, None]]) -> None:
        """Register a handler for incoming channel messages.

        Handler signature: async def handler(channel: str, sender: str, text: str)
        """
        self._message_handler = handler

    async def start(self) -> None:
        """Connect to Meshcore radio via serial or TCP."""
        port = settings.serial_port
        baud = settings.serial_baud

        if port.startswith("tcp://"):
            # TCP connection (for Docker: socat bridges serial on host)
            # Format: tcp://host:port
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

        # Subscribe to channel messages
        self._mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)

        # Start auto-fetching messages from the device
        await self._mc.start_auto_message_fetching()

        # Load contacts so we can resolve sender names
        await self._mc.ensure_contacts()

        logger.info("Meshcore radio connected. Node: %s", self._mc.self_info.get("adv_name", "?"))

    async def _resolve_channel(self, channel_ref: str) -> int:
        """Resolve a channel name or index string to a channel index.

        Accepts:
            - A channel name like "#digitaino-wx-bot"
            - A numeric index like "3" or "0"
        """
        # If it's a plain number, use it directly
        try:
            return int(channel_ref)
        except ValueError:
            pass

        # Search channels by name
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
        """Disconnect from the radio."""
        self._running = False
        if self._mc:
            await self._mc.disconnect()
        logger.info("Meshcore radio disconnected")

    async def send_channel_message(self, channel: int, text: str) -> None:
        """Send a single message on a channel.

        Messages must fit in one frame (293 bytes text max). The caller
        is responsible for keeping messages within the limit.
        """
        if not self._mc:
            logger.error("Cannot send - not connected")
            return

        # Never transmit on channel 0 (public) or any channel other than ours
        if channel == 0 or channel != self._channel_idx:
            logger.warning("Blocked send on ch %d (our ch is %d)", channel, self._channel_idx)
            return

        try:
            await self._mc.commands.send_chan_msg(channel, text)
            logger.debug("Sent to ch %d: %s", channel, text[:60])
        except Exception:
            logger.exception("Failed to send channel message")

    async def _on_channel_msg(self, event) -> None:
        """Handle incoming channel message from the meshcore library."""
        payload = event.payload
        channel_idx = payload.get("channel_idx", 0)
        text = payload.get("text", "")

        # Channel messages have sender name prefixed as "SenderName: message"
        # Strip the sender prefix to get the actual command
        sender = "unknown"
        if ": " in text:
            sender, text = text.split(": ", 1)

        logger.info("Channel msg from %s on ch %d: %s", sender, channel_idx, text[:80])

        if self._message_handler:
            try:
                await self._message_handler(str(channel_idx), sender, text)
            except Exception:
                logger.exception("Error in message handler")

    @property
    def channel_idx(self) -> int | None:
        return self._channel_idx
