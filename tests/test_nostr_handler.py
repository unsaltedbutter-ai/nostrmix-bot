"""Tests for NostrHandler — the wrapper around nostrbot-sdk's NostrBot.

The SDK itself is hard to test against (it talks to real relays). Here we
mock NostrBot and verify the wrapper's own behavior: callback registration,
config translation, identity wrapping, and delegation. The point isn't to
test the SDK — it's to catch the wrapper's own integration bugs (the
audit's S10: ``.identity`` being SDK-dependent with no fallback)."""

import asyncio
import os
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.config import BotConfig
from src.nostr_handler import NostrHandler


def _cfg() -> BotConfig:
    return BotConfig("/nonexistent.env")


class TestConfigTranslation:
    def test_build_config_pulls_from_botconfig(self):
        cfg = _cfg()
        h = NostrHandler(cfg)
        ncfg = h.build_config()
        # NostrBotConfig is an SDK object; just verify our fields are on it.
        assert ncfg.nsec == cfg.NOSTR_PRIVATE_KEY_NPUB
        assert ncfg.relays == cfg.NOSTR_RELAYS
        assert ncfg.profile == cfg.profile
        assert ncfg.zap_provider_pubkey == cfg.ZAP_PROVIDER_PUBKEY_HEX


class TestCallbackRegistration:
    """The setters just remember the callbacks; the actual SDK wiring
    happens in start(). Verify the setters don't drop or transform."""

    def test_setters_store_callbacks(self):
        h = NostrHandler(_cfg())
        f_dm = AsyncMock()
        f_zap = AsyncMock()
        f_hb = AsyncMock()
        f_ready = AsyncMock()
        h.set_dm_handler(f_dm)
        h.set_zap_handler(f_zap)
        h.set_heartbeat_handler(f_hb)
        h.set_on_ready(f_ready)
        assert h._dm_callback is f_dm
        assert h._zap_callback is f_zap
        assert h._heartbeat_callback is f_hb
        assert h._on_ready is f_ready


class TestStartLifecycle:
    """start() builds a NostrBot, registers callbacks via the SDK's
    decorator API, awaits bot.start, and fires on_ready. We mock NostrBot
    entirely — the test is about the order and shape of the wrapper's calls."""

    @pytest.mark.asyncio
    async def test_start_registers_handlers_and_fires_on_ready(self):
        h = NostrHandler(_cfg())

        dm_cb = AsyncMock()
        zap_cb = AsyncMock()
        hb_cb = AsyncMock()
        ready_cb = AsyncMock()
        h.set_dm_handler(dm_cb)
        h.set_zap_handler(zap_cb)
        h.set_heartbeat_handler(hb_cb)
        h.set_on_ready(ready_cb)

        # Stub the NostrBot class so its decorator methods are no-ops and
        # start() returns immediately.
        fake_bot = MagicMock()
        fake_bot.start = AsyncMock()
        # The decorator-style registrations are property-accessed callables
        # that return a decorator. Configure them as MagicMocks.
        fake_bot.on_dm = MagicMock(side_effect=lambda fn: fn)
        fake_bot.on_zap = MagicMock(side_effect=lambda fn: fn)
        fake_bot.on_heartbeat = MagicMock(return_value=lambda fn: fn)

        with patch("src.nostr_handler.NostrBot", return_value=fake_bot):
            await h.start()

        # SDK constructed once with our config.
        assert h._bot is fake_bot
        # Each callback got registered via its decorator path.
        assert fake_bot.on_dm.called
        assert fake_bot.on_zap.called
        # on_heartbeat is invoked with the interval kwarg to PRODUCE the decorator
        fake_bot.on_heartbeat.assert_called_once_with(interval=300)
        # Bot was started and on_ready fired.
        fake_bot.start.assert_awaited_once()
        ready_cb.assert_awaited_once_with(h)

    @pytest.mark.asyncio
    async def test_start_skips_callbacks_that_were_not_set(self):
        """If no zap handler is registered, start() shouldn't try to wire
        zap. Defends against attribute errors on a bare bot mock."""
        h = NostrHandler(_cfg())
        h.set_dm_handler(AsyncMock())  # only DM registered

        fake_bot = MagicMock()
        fake_bot.start = AsyncMock()
        fake_bot.on_dm = MagicMock(side_effect=lambda fn: fn)
        # If start() touches on_zap / on_heartbeat, it'll create attributes
        # on the MagicMock — assert they were never called by checking the
        # spy explicitly.
        fake_bot.on_zap = MagicMock(side_effect=lambda fn: fn)
        fake_bot.on_heartbeat = MagicMock(return_value=lambda fn: fn)

        with patch("src.nostr_handler.NostrBot", return_value=fake_bot):
            await h.start()

        assert fake_bot.on_dm.called
        assert not fake_bot.on_zap.called
        assert not fake_bot.on_heartbeat.called


class TestDelegations:
    """send_dm / post_note / post_announcement / stop / run_forever just
    forward to the bot. The tests pin down that we're forwarding (a) the
    right method, (b) the right args, and (c) await-vs-return semantics."""

    @pytest.mark.asyncio
    async def test_send_dm_delegates(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        h._bot.send_dm = AsyncMock()
        await h.send_dm("npubhex", "hi")
        h._bot.send_dm.assert_awaited_once_with("npubhex", "hi")

    @pytest.mark.asyncio
    async def test_send_dm_is_no_op_when_bot_not_started(self):
        """The wrapper guards against being called before start() — we
        don't want a NoneType error to surface to the coordinator."""
        h = NostrHandler(_cfg())
        h._bot = None
        # Should NOT raise.
        await h.send_dm("anyone", "anything")

    @pytest.mark.asyncio
    async def test_post_announcement_returns_event_id_on_success(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        ok_result = MagicMock(ok=True, event_id="abc123event")
        h._bot.post_note = AsyncMock(return_value=ok_result)
        result = await h.post_announcement("hello world")
        assert result == "abc123event"
        # And the post_note was called with the project's hashtags.
        h._bot.post_note.assert_awaited_once()
        _args, kwargs = h._bot.post_note.call_args
        assert kwargs.get("hashtags") == ["nostrmix", "coinjoin"]

    @pytest.mark.asyncio
    async def test_post_announcement_returns_none_on_failure(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        failed = MagicMock(ok=False)
        h._bot.post_note = AsyncMock(return_value=failed)
        assert await h.post_announcement("x") is None

    @pytest.mark.asyncio
    async def test_stop_delegates_when_started(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        h._bot.stop = AsyncMock()
        await h.stop()
        h._bot.stop.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_stop_is_safe_when_never_started(self):
        h = NostrHandler(_cfg())
        h._bot = None
        await h.stop()  # must not raise


class TestGetIdentity:
    """get_identity reads lud16 off the SDK via .identity.resolve(). The
    method name matters: the SDK exposes resolve() (NOT fetch()), and
    resolve() ALWAYS returns an Identity — never None — with unset fields as
    None. These tests pin that contract; TestSdkInterfacePinning below pins
    the method name against the real SDK so a future rename fails loudly."""

    @pytest.mark.asyncio
    async def test_returns_dict_with_name_lud16_picture(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        identity_obj = MagicMock()
        identity_obj.name = "alice"
        identity_obj.lud16 = "alice@x"
        identity_obj.picture = "https://x/p.png"
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=identity_obj)
        h._bot.identity = resolver

        result = await h.get_identity("npub_hex_abc")
        assert result == {
            "name": "alice",
            "lud16": "alice@x",
            "picture": "https://x/p.png",
        }
        resolver.resolve.assert_awaited_once_with("npub_hex_abc")

    @pytest.mark.asyncio
    async def test_empty_profile_returns_dict_with_blank_lud16(self):
        """resolve() returns an Identity with only pubkey_hex set for an
        unknown/empty profile (never None). get_identity must surface that as
        a dict with blank fields — NOT crash, and NOT a None lud16 that would
        flow into the DB. '' means 'no refund address on file'."""
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        identity_obj = MagicMock()
        identity_obj.name = None
        identity_obj.lud16 = None
        identity_obj.picture = None
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=identity_obj)
        h._bot.identity = resolver
        result = await h.get_identity("unknown_npub")
        assert result == {"name": "", "lud16": "", "picture": ""}

    @pytest.mark.asyncio
    async def test_returns_none_when_bot_not_started(self):
        h = NostrHandler(_cfg())
        h._bot = None
        assert await h.get_identity("anyone") is None


class TestSdkInterfacePinning:
    """Pin the NostrBot/IdentityResolver API we depend on against the REAL
    installed SDK (no network). The prior bug — calling .identity.fetch() and
    re-calling bot.run() after bot.start() — passed every mocked test while
    breaking the live bot. These assertions fail loudly if the SDK renames or
    removes the surface NostrHandler relies on."""

    def test_identity_resolver_exposes_resolve_not_fetch(self):
        from nostrbot_sdk.identity import IdentityResolver, Identity
        assert hasattr(IdentityResolver, "resolve"), "SDK dropped resolve()"
        assert not hasattr(IdentityResolver, "fetch"), (
            "SDK now has fetch(); get_identity may need updating"
        )
        for field in ("name", "lud16", "picture"):
            assert field in Identity.__dataclass_fields__

    def test_bot_run_restarts_so_run_forever_must_not_call_it(self):
        """run() begins with start(), and start() raises if already started.
        This is WHY NostrHandler.run_forever must not call bot.run() after
        start(). If the SDK ever stops re-starting in run(), this test flags
        that the workaround can be simplified."""
        import inspect
        from nostrbot_sdk.bot import NostrBot
        run_src = inspect.getsource(NostrBot.run)
        start_src = inspect.getsource(NostrBot.start)
        assert "self.start()" in run_src, "run() no longer calls start()"
        assert "already started" in start_src, "start() no longer guards re-entry"
        assert hasattr(NostrBot, "stop")


class TestRunForeverDoesNotRestart:
    """Regression for the boot crash: start() already started the bot, so
    run_forever() must just wait for a shutdown signal — never call bot.run()
    or bot.start() again (which would raise 'NostrBot already started')."""

    @pytest.mark.asyncio
    async def test_run_forever_waits_without_restarting_bot(self):
        h = NostrHandler(_cfg())
        h._bot = MagicMock()
        h._bot.run = AsyncMock()
        h._bot.start = AsyncMock()

        task = asyncio.create_task(h.run_forever())
        # Let run_forever create its shutdown event and start waiting.
        await asyncio.sleep(0)
        assert h._shutdown_event is not None, "run_forever did not arm a waiter"
        assert not task.done(), "run_forever returned without waiting for shutdown"

        # Trip the shutdown the way a SIGINT handler would.
        h._shutdown_event.set()
        await asyncio.wait_for(task, timeout=1)

        h._bot.run.assert_not_called()
        h._bot.start.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_forever_no_op_when_bot_absent(self):
        h = NostrHandler(_cfg())
        h._bot = None
        await asyncio.wait_for(h.run_forever(), timeout=1)  # returns immediately


class TestKeysAndPubkeyProperties:
    def test_keys_property_pre_start_is_none(self):
        h = NostrHandler(_cfg())
        assert h.keys is None
        assert h.pubkey_hex is None

    def test_keys_property_after_start_delegates(self):
        h = NostrHandler(_cfg())
        bot = MagicMock()
        bot.keys = "fake_keys_obj"
        bot.pubkey_hex = "deadbeef" * 8
        h._bot = bot
        assert h.keys == "fake_keys_obj"
        assert h.pubkey_hex == "deadbeef" * 8
