"""Nostr DM handler — wraps nostrbot-sdk for NIP-17 DMs, NIP-57 zaps, daily announcements."""

from __future__ import annotations

import asyncio
import signal
from typing import Optional, Callable, Awaitable, List, Dict, Any
from collections.abc import AsyncIterator

from nostrbot_sdk import (
    NostrBot,
    NostrBotConfig,
    SenderContext,
    ValidatedZap,
    build_note_tags,
    send_note,
)
from nostr_sdk import Keys, Tag

from .config import BotConfig


# Type aliases for clarity
OnDmCallback = Callable[[SenderContext, str], Awaitable[None]]
OnZapCallback = Callable[[ValidatedZap, SenderContext], Awaitable[None]]


class NostrHandler:
    """Handles all Nostr transport: DMs, zaps, announcements.

    Delegates command dispatch to a Coordinator callback.
    """

    def __init__(self, config: BotConfig):
        self._cfg = config
        self._bot: Optional[NostrBot] = None
        self._dm_callback: Optional[OnDmCallback] = None
        self._zap_callback: Optional[OnZapCallback] = None
        self._heartbeat_callback: Optional[Callable] = None
        self._on_ready: Optional[Callable] = None  # called when bot is ready
        self._shutdown_event: Optional[asyncio.Event] = None  # set by run_forever on signal

    def set_dm_handler(self, cb: OnDmCallback):
        self._dm_callback = cb

    def set_zap_handler(self, cb: OnZapCallback):
        self._zap_callback = cb

    def set_heartbeat_handler(self, cb) -> None:
        self._heartbeat_callback = cb

    def set_on_ready(self, cb) -> None:
        """Register a callback for when the bot is connected and ready."""
        self._on_ready = cb

    def build_config(self) -> NostrBotConfig:
        """Build a NostrBotConfig from our BotConfig."""
        cfg = self._cfg
        return NostrBotConfig(
            nsec=cfg.NOSTR_PRIVATE_KEY_NPUB,
            relays=cfg.NOSTR_RELAYS,
            profile=cfg.profile,
            zap_provider_pubkey=cfg.ZAP_PROVIDER_PUBKEY_HEX,
        )

    @property
    def bot(self) -> Optional[NostrBot]:
        return self._bot

    @property
    def keys(self) -> Optional[Keys]:
        """Get the bot's Nostr keys, if started."""
        if self._bot:
            return self._bot.keys
        return None

    @property
    def pubkey_hex(self) -> Optional[str]:
        """Get the bot's pubkey hex, if started."""
        if self._bot:
            return self._bot.pubkey_hex
        return None

    async def start(self):
        """Connect to relays, register handlers, start the bot runtime."""
        ncfg = self.build_config()
        self._bot = NostrBot(ncfg)

        # Register DM handler
        if self._dm_callback:
            @self._bot.on_dm
            async def dm_handler(ctx: SenderContext, text: str):
                await self._dm_callback(ctx, text)

        # Register zap handler
        if self._zap_callback:
            @self._bot.on_zap
            async def zap_handler(zap: ValidatedZap, ctx: SenderContext):
                await self._zap_callback(zap, ctx)

        # Register heartbeat
        if self._heartbeat_callback:
            @self._bot.on_heartbeat(interval=300)
            async def hb_handler(uptime_s: int):
                await self._heartbeat_callback(uptime_s)

        # Start the bot
        await self._bot.start()

        # Fire on_ready callback (used by coordinator to wire up keys)
        if self._on_ready:
            await self._on_ready(self)

    async def stop(self):
        """Disconnect gracefully."""
        if self._bot:
            await self._bot.stop()

    async def run_forever(self):
        """Block until a SIGINT/SIGTERM shutdown signal, then return.

        The bot is ALREADY live once start() has run — start() connects,
        subscribes, and spawns the SDK's notification/maintenance/heartbeat
        tasks. We must NOT call bot.run() here: run() begins with another
        bot.start(), which raises RuntimeError("NostrBot already started")
        and crashes the process at boot. Instead we install our own signal
        handlers and wait, mirroring the SDK's run() loop minus the re-start.
        Shutdown/cleanup (bot.stop) is the coordinator's responsibility via
        its own stop().
        """
        if not self._bot:
            return
        self._shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: List[int] = []
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._shutdown_event.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):
                # Windows or a non-main thread: handler unavailable. The
                # coordinator's stop() (e.g. from KeyboardInterrupt) is then
                # the only shutdown path.
                pass
        try:
            await self._shutdown_event.wait()
        finally:
            for sig in installed:
                try:
                    loop.remove_signal_handler(sig)
                except (NotImplementedError, RuntimeError):
                    pass

    async def send_dm(self, recipient_hex: str, message: str):
        """Send a NIP-17 DM to a participant."""
        if self._bot:
            await self._bot.send_dm(recipient_hex, message)

    async def post_note(self, text: str, hashtags: Optional[List[str]] = None,
                        reply_to: Optional[str] = None,
                        mention_pubkeys: Optional[List[str]] = None) -> Any:
        """Publish a kind 1 note to configured relays."""
        if self._bot:
            return await self._bot.post_note(
                text, hashtags=hashtags, reply_to=reply_to,
                mention_pubkeys=mention_pubkeys,
            )

    async def post_announcement(self, text: str) -> Optional[str]:
        """Post a mix announcement. Returns event_id hex on success."""
        result = await self.post_note(text, hashtags=["nostrmix", "coinjoin"])
        if result and result.ok:
            return result.event_id
        return None

    async def get_identity(self, pubkey_hex: str) -> Optional[Dict]:
        """Fetch kind 0 identity for a pubkey (used for lud16 discovery).

        Returns a dict with name/lud16/picture, or None only when the bot
        isn't started. The SDK's IdentityResolver.resolve() ALWAYS returns an
        Identity (never None) — an unknown/empty profile comes back with only
        pubkey_hex set and the rest None — so a missing lud16 surfaces here as
        "" (no refund address on file), not as a None dict. (The method is
        resolve(), not fetch() — fetch() does not exist on the SDK and calling
        it raised AttributeError on every /join.)
        """
        if self._bot:
            resolver = self._bot.identity
            identity = await resolver.resolve(pubkey_hex)
            if identity:
                return {
                    "name": identity.name or "",
                    "lud16": identity.lud16 or "",
                    "picture": identity.picture or "",
                }
        return None
