"""Mixing Coordinator — state machines, event loop, tie everything together."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import logging
from decimal import Decimal, InvalidOperation
from typing import Optional, Dict, List, Any, Callable, Tuple

from nostrbot_sdk import SenderContext, ValidatedZap, NostrBot

from .config import BotConfig
from .database import Database
from .nostr_handler import NostrHandler
from .chain_monitor import ChainMonitor
from .psbt_manager import PSBTManager
from .fee_engine import FeeEngine, FeeResult
from .lightning_handler import LightningHandler
from .command_parser import CommandParser, ParsedCommand
from .privacy import PrivacyCheck
from .log_tokens import tokens

logger = logging.getLogger(__name__)


# Logging discipline (privacy gate):
#
# This bot's job is to break the on-chain link between participants. Any
# log line that pairs (npub OR lud16) with (mix_id OR txid OR address OR
# UTXO) reconstructs the link an outside observer can't otherwise derive.
# Rules followed throughout this file:
#
#   1. Never log raw npub / lud16. Use tokens.p() / tokens.l().
#   2. Never log mix_id and txid in the same line. The txid is public
#      on-chain; the mix_id is internal. Pairing them in a log file
#      lets anyone reading the log map every participant onto the
#      public coinjoin transaction.
#   3. Never log full output addresses or UTXO txid:vout. Tokens only.
#   4. Never use ``exc_info=True`` or include ``{e}`` text in logs
#      triggered by user input — tracebacks dump frame locals
#      (UTXOs, addresses, PSBT hex). Use ``type(e).__name__`` instead.


class Coordinator:
    """State machines, event loop, ties everything together."""

    def __init__(self, config: BotConfig, db: Database):
        self.cfg = config
        self.db = db

        # Components (set during init)
        self.nostr: Optional[NostrHandler] = None
        self.chain: Optional[ChainMonitor] = None
        self.psbt_mgr: Optional[PSBTManager] = None
        self.fee_engine: Optional[FeeEngine] = None
        self.lightning: Optional[LightningHandler] = None
        self.parser = CommandParser(bot_name=config.BOT_NAME)
        self.privacy = PrivacyCheck()

        # Runtime state
        self._running = False
        self._announcement_task: Optional[asyncio.Task] = None
        self._event_loop_task: Optional[asyncio.Task] = None
        self._session_ctx: Optional[Any] = None  # SenderContext reference from NostrBot

        # For chunked PSBT reassembly — keyed per (npub_hex, mix_id) so same
        # participant in multiple mixes doesn't collide chunks.
        # Value is tuple: (chunk_total, dict[int,str], timestamp_of_first_chunk)
        self._psbt_chunks: Dict[str, dict] = {}  # "<npub_hex>:<mix_id>" -> {"chunks": {idx: hex}, "total": int, "started": float}

    async def init(self, nostr: NostrHandler, chain: ChainMonitor,
                   psbt_mgr: PSBTManager, fee_engine: FeeEngine,
                   lightning: LightningHandler):
        """Wire components together."""
        self.nostr = nostr
        self.chain = chain
        self.psbt_mgr = psbt_mgr
        self.fee_engine = fee_engine
        self.lightning = lightning

        # Register handlers
        self.nostr.set_dm_handler(self._on_dm)
        self.nostr.set_zap_handler(self._on_zap)
        self.nostr.set_heartbeat_handler(self._on_heartbeat)

        # Register on_ready callback to wire up keys after bot connects
        self.nostr.set_on_ready(self._on_nostr_ready)

    # --- DM Handler (routes commands) ---

    async def _on_dm(self, ctx: SenderContext, text: str):
        """Handle an incoming DM from a participant."""
        parsed = self.parser.parse(text)
        npub_hex = ctx.sender_hex

        try:
            # Privacy: do NOT pass user input to logger here. The match
            # arms below log their own context; the outer except logs
            # only the command verb + exception class, never the npub
            # or the parsed args.
            match parsed.command:
                case "list_mixes":
                    await self._cmd_list_mixes(ctx)

                case "help":
                    await self._cmd_help(ctx)

                case "join_mix":
                    mix_id = parsed.args[0] if parsed.args else None
                    # parsed.args[1] is the "<word1>-<word2>" fallback for a
                    # name typed with a space instead of a hyphen.
                    alt = parsed.args[1] if len(parsed.args) > 1 else None
                    # parsed.args[2] is the BTC amount for "/join <amount>"
                    # (None for a name-join).
                    amount = parsed.args[2] if len(parsed.args) > 2 else None
                    await self._cmd_join_mix(ctx, mix_id, alt, amount)

                case "commit_utxos":
                    utxos = parsed.args[0] if parsed.args else []
                    await self._cmd_commit_utxos(ctx, npub_hex, utxos)

                case "provide_addresses":
                    addrs = parsed.args[0] if parsed.args else []
                    await self._cmd_provide_addresses(ctx, npub_hex, addrs)

                case "accept_psbt":
                    psbt_hex = parsed.args[0] if parsed.args else ""
                    await self._cmd_accept_psbt(ctx, npub_hex, psbt_hex)

                case "accept_psbt_chunk":
                    if len(parsed.args) >= 3:
                        chunk_idx = parsed.args[0]
                        chunk_total = parsed.args[1]
                        chunk_hex = parsed.args[2]
                        await self._cmd_accept_psbt_chunk(ctx, npub_hex, chunk_idx, chunk_total, chunk_hex)

                case "exit_mix":
                    mix_id = parsed.args[0] if parsed.args else None
                    await self._cmd_exit_mix(ctx, npub_hex, mix_id)

                case _:
                    # Tune the suggested commands to where this user is (e.g.
                    # don't suggest /psbt_accept while they're still gathering).
                    cmds = await self._relevant_commands(npub_hex)
                    await self.nostr.send_dm(
                        npub_hex,
                        "Unknown command.\n" + self.parser.format_help(cmds),
                    )
        except Exception as e:
            # exc_info would dump frame locals (UTXOs, addresses, PSBT hex)
            # into the log. Just record the participant token + command
            # verb + exception class — enough to triage, nothing to leak.
            logger.error(
                "DM handler error: participant=%s command=%s err=%s",
                tokens.p(npub_hex), parsed.command, type(e).__name__,
            )
            try:
                # S-G: never put str(e) in the DM. Inner exceptions from
                # SQLite / bitcointx / httpx sometimes embed other-user
                # data in their string form (SQL fragments, hex). Users
                # could probe for leaks by sending malformed commands.
                # Give them a generic message; the operator gets the
                # diagnostic via the logger.error above.
                await self.nostr.send_dm(
                    npub_hex,
                    "Error processing your message. Check the command format "
                    "and try again — send /help for the commands available to you.",
                )
            except Exception:
                pass

    # --- Command Implementations ---

    # M2: cap to avoid spamming a user (or eating relay budget) when
    # someone pastes a giant blob. First N rejections are detailed; the
    # rest collapse to a count.
    _MAX_REJECTION_LINES = 8

    async def _send_rejection_summary(self, npub_hex: str,
                                       rejections: List[Tuple[str, str]]):
        """One DM listing up to _MAX_REJECTION_LINES rejected outpoints
        with their reasons; collapse the rest into a "+N more rejected"
        tail. Replaces the old one-DM-per-bad-UTXO behaviour (M2)."""
        if not rejections:
            return
        head = rejections[: self._MAX_REJECTION_LINES]
        rest = len(rejections) - len(head)
        lines = [f"Rejected {len(rejections)} UTXO(s):"]
        for outpoint, reason in head:
            lines.append(f"  • {outpoint} — {reason}")
        if rest > 0:
            lines.append(f"  • …and {rest} more rejected.")
        try:
            await self.nostr.send_dm(npub_hex, "\n".join(lines))
        except Exception:
            pass

    async def _create_default_mix(self) -> str:
        """Create a fresh mix with DEFAULT_* settings, set it collecting, and
        return its id. input_type is left NULL — it locks at the first /commit.
        Shared by /list, the /commit auto-create, and the daily announcement so
        "open a default mix when none exist" lives in exactly one place."""
        deadline_unix = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
        mid = await self.db.create_mix(
            output_size=self.cfg.DEFAULT_OUTPUT_SIZE,
            max_participants=self.cfg.MAX_PARTICIPANTS_DEFAULT,
            fee_per_element=self.cfg.FEE_PER_ELEMENT,
            deadline_unix=deadline_unix,
            required_nonconforming=self.cfg.DEFAULT_REQUIRED_NONCONFORMING,
            max_conforming_utxos=self.cfg.MAX_CONFORMING_UTXOS,
        )
        await self.db.update_mix(mid, state="collecting")
        return mid

    async def _cmd_list_mixes(self, ctx: SenderContext):
        """Handle /list — show open mixes. If none are open, open a default one
        so the user always has something to join right now rather than being
        told to come back later."""
        active = await self.db.get_active_mixes()
        available = [m for m in active if m["state"] in ("announced", "collecting")]
        if not available:
            mid = await self._create_default_mix()
            logger.info("Auto-created mix %s on /list (no open mixes)",
                        tokens.m(mid))
            # Re-fetch so the new mix (and anything created concurrently) shows.
            active = await self.db.get_active_mixes()
            available = [m for m in active
                         if m["state"] in ("announced", "collecting")]
        msg = self.parser.format_list_response(available)
        await self.nostr.send_dm(ctx.sender_hex, msg)

    # Participant states that count as "actively in a mix" for help/stage
    # purposes (terminal/exited states — cancelled/ghosted/refunded/etc. — and
    # the post-broadcast states don't shape what the user should do next).
    _ACTIVE_PARTICIPANT_STATES = ("interested", "committed", "paid",
                                  "signing", "signed")

    async def _relevant_commands(self, npub_hex: str) -> List[str]:
        """Return the command keys worth *suggesting* to this user, tuned to
        where they are across their active mixes. /list is always relevant;
        the rest appear only when they're an actual next step. This shapes only
        the help text — every command still works regardless of what's listed.

        - Not in any mix → list, join
        - Joined / committed (still assembling their entry) → commit, addresses
        - Signing (PSBT sent) → psbt_accept
        - join is offered again only when nothing is half-finished and the user
          is under MAX_PENDING_MIXES; cancel whenever they're in a mix.
        """
        parts = await self.db.get_participants_by_npub(npub_hex)
        stages = {p["state"] for p in parts
                  if p["state"] in self._ACTIVE_PARTICIPANT_STATES}
        cmds = ["list"]
        if not stages:
            cmds.append("join")
            return cmds
        assembling = bool({"interested", "committed"} & stages)
        if assembling:
            cmds += ["commit", "addresses"]
        if "signing" in stages:
            cmds.append("psbt_accept")
        if not assembling:
            active_count = await self.db.count_active_participant_mixes(npub_hex)
            if active_count < self.cfg.MAX_PENDING_MIXES:
                cmds.append("join")
        cmds.append("cancel")
        return cmds

    async def _cmd_help(self, ctx: SenderContext):
        """Handle /help — send the command list tuned to the user's stage."""
        cmds = await self._relevant_commands(ctx.sender_hex)
        await self.nostr.send_dm(ctx.sender_hex, self.parser.format_help(cmds))

    async def _cmd_join_mix(self, ctx: SenderContext, mix_id: Optional[str],
                            alt_mix_id: Optional[str] = None,
                            amount_btc: Optional[str] = None):
        """Handle /join. Two forms:

        - ``/join <mix_name>`` — join an existing mix by name. ``alt_mix_id`` is
          the "<word1>-<word2>" fallback used when the name was typed with a
          space instead of a hyphen ("/join silver cupcake").
        - ``/join <amount>`` — join-or-create a mix of that BTC output size
          (``amount_btc`` is the raw BTC string). Joins the closest-to-full open
          mix of that exact size, else creates a fresh one.
        """
        npub_hex = ctx.sender_hex

        if not mix_id and not amount_btc:
            await self.nostr.send_dm(
                npub_hex,
                "Usage: /join <mix_name>  or  /join <amount_btc>  (e.g. /join 0.01)",
            )
            return

        # Check blacklist
        if await self.db.is_blacklisted(npub_hex):
            await self.nostr.send_dm(npub_hex, "You have been blacklisted from this bot.")
            return

        # One-at-a-time pre-paid mix per npub. The plan permits being in
        # multiple PAID mixes simultaneously, but the pre-payment phase
        # (interested/committed) must be finished before starting another —
        # otherwise /commit and /addresses pick the wrong mix.
        participants_for_npub = await self.db.get_participants_by_npub(npub_hex)
        unfinished = [p for p in participants_for_npub if p["state"] in ("interested", "committed")]
        if unfinished:
            blocking = unfinished[0]
            if blocking["state"] == "interested":
                msg = (f"Finish sending /commit and /addresses for "
                       f"{blocking['mix_id']} before joining another mix.")
            else:  # committed
                msg = (f"Zap the service fee for {blocking['mix_id']} "
                       f"before joining another mix.")
            await self.nostr.send_dm(npub_hex, msg)
            return

        # Check MAX_PENDING_MIXES (counts paid+ mixes the user is in)
        active_count = await self.db.count_active_participant_mixes(npub_hex)
        if active_count >= self.cfg.MAX_PENDING_MIXES:
            await self.nostr.send_dm(npub_hex, self.parser.format_max_mixes(self.cfg.MAX_PENDING_MIXES))
            return

        created = False
        if amount_btc is not None:
            # --- Amount form: join-or-create a mix of this BTC size. ---
            sats = self._parse_btc_to_sats(amount_btc)
            if sats is None:
                await self.nostr.send_dm(
                    npub_hex, "Couldn't read that amount — try e.g. /join 0.01")
                return
            if sats < self.cfg.MINIMUM_UTXO_SIZE:
                await self.nostr.send_dm(
                    npub_hex,
                    f"Minimum mix size is {self.cfg.MINIMUM_UTXO_SIZE / 1e8:.8f} BTC.",
                )
                return
            # The amount is the per-output mix size, in BTC, and must be a
            # fraction of a bitcoin (0 < amount < 1). This both guards against a
            # sats-vs-BTC typo (e.g. "/join 100000" meaning 0.001 BTC would
            # otherwise read as 100000 BTC) and keeps output sizes sane.
            if sats >= 1 * 100_000_000:
                await self.nostr.send_dm(
                    npub_hex,
                    "The mix size must be less than 1 BTC — amounts are in BTC, "
                    "so use a decimal like /join 0.01 (not sats).",
                )
                return
            mix_id, created = await self._find_or_create_mix_by_size(sats)
            if mix_id is None:
                await self.nostr.send_dm(
                    npub_hex,
                    f"Too many open mixes right now (max {self.cfg.MAX_OPEN_MIXES}). "
                    f"Try /list and /join an existing one.",
                )
                return
            mix = await self.db.get_mix(mix_id)
        else:
            # --- Name form: verify the mix exists and is open. If the first
            # token didn't match, fall back to the hyphen-joined two-word form
            # (handles "/join silver cupcake").
            mix = await self.db.get_mix(mix_id)
            if not mix and alt_mix_id:
                alt = await self.db.get_mix(alt_mix_id)
                if alt:
                    mix, mix_id = alt, alt_mix_id
            if not mix:
                await self.nostr.send_dm(
                    npub_hex, f"No mix named '{alt_mix_id or mix_id}' found.")
                return
            if mix["state"] not in ("announced", "collecting"):
                await self.nostr.send_dm(npub_hex, f"Mix '{mix_id}' is already in progress or completed.")
                return

        # Look up kind 0 for lud16
        # We handle that later through the SDK
        identity = await self.nostr.get_identity(npub_hex)
        lud16 = identity["lud16"] if identity else ""

        # Add participant as 'interested'
        pid = await self.db.add_participant(mix_id, npub_hex, lud16)

        # Reply asking for UTXOs and addresses. For an amount-join, lead with
        # the created/joined size so a mistyped amount is visible before funds.
        lead = f"Registered interest in {mix_id}."
        if amount_btc is not None:
            output_btc = mix["output_size"] / 1e8
            verb = "Created mix" if created else "Joined mix"
            lead = f"{verb} {mix_id}: {output_btc:.8f} BTC outputs."
        await self.nostr.send_dm(
            npub_hex,
            f"{lead}\n"
            f"Send me txid(s) and vout(s) and your output addresses:\n"
            f"/commit <txid:vout> ...\n"
            f"/addresses <addr1> <addr2> ..."
        )

    @staticmethod
    def _parse_btc_to_sats(s: str) -> Optional[int]:
        """Convert a BTC amount string to integer sats, or None if it isn't a
        valid whole number of sats. Decimal avoids the float rounding error
        ``float("0.00125") * 1e8`` would introduce."""
        try:
            sats = Decimal(s) * 100_000_000
        except (InvalidOperation, ValueError):
            return None
        if sats <= 0 or sats != sats.to_integral_value():
            return None
        return int(sats)

    async def _find_or_create_mix_by_size(
        self, output_size: int
    ) -> Tuple[Optional[str], bool]:
        """Find an open (announced/collecting) mix with this exact output_size
        that still has room, preferring the closest-to-full so participants
        coalesce. Otherwise create a fresh mix with DEFAULT_* settings — unless
        MAX_OPEN_MIXES is reached, in which case return (None, False).

        Returns (mix_id, created)."""
        open_mixes = await self.db.get_mixes_by_state("announced", "collecting")
        best_id: Optional[str] = None
        best_count = -1
        for m in open_mixes:
            if m["output_size"] != output_size:
                continue
            cap = m.get("max_participants") or self.cfg.MAX_PARTICIPANTS_DEFAULT
            cnt = await self.db.count_participants_by_mix(
                m["id"], exclude_states=["cancelled", "ghosted", "refunding", "refunded", "refund_failed"],
            )
            if cnt >= cap:
                continue
            if cnt > best_count:
                best_count, best_id = cnt, m["id"]
        if best_id is not None:
            return best_id, False
        # None compatible — spin up a fresh mix unless we're at the open cap.
        if len(open_mixes) >= self.cfg.MAX_OPEN_MIXES:
            return None, False
        deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
        mid = await self.db.create_mix(
            output_size=output_size,
            max_participants=self.cfg.MAX_PARTICIPANTS_DEFAULT,
            fee_per_element=self.cfg.FEE_PER_ELEMENT,
            deadline_unix=deadline,
            required_nonconforming=self.cfg.DEFAULT_REQUIRED_NONCONFORMING,
            max_conforming_utxos=self.cfg.MAX_CONFORMING_UTXOS,
        )
        # Leave input_type NULL — it locks at the first /commit, same as the
        # name-join flow.
        await self.db.update_mix(mid, state="collecting")
        logger.info("Created mix %s by amount (output_size=%d)", tokens.m(mid), output_size)
        return mid, True

    async def _find_or_create_mix_for(self, input_type: str) -> Optional[str]:
        """Find an open mix matching input_type (or unlocked) with capacity,
        creating one with DEFAULT_* settings if none exists. Used by the
        auto-mix-on-commit flow (plan section 3g)."""
        open_mixes = await self.db.get_mixes_by_state("announced", "collecting")
        for m in open_mixes:
            locked = m.get("input_type")
            if locked and locked != input_type:
                continue
            cap = m.get("max_participants") or self.cfg.MAX_PARTICIPANTS_DEFAULT
            cnt = await self.db.count_participants_by_mix(
                m["id"], exclude_states=["cancelled", "ghosted", "refunding", "refunded", "refund_failed"],
            )
            if cnt >= cap:
                continue
            return m["id"]
        # None compatible — spin up a fresh default mix unless we're at the
        # open-mix cap (then the caller tells the user to /list and /join).
        if len(open_mixes) >= self.cfg.MAX_OPEN_MIXES:
            return None
        deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
        mid = await self.db.create_mix(
            output_size=self.cfg.DEFAULT_OUTPUT_SIZE,
            max_participants=self.cfg.MAX_PARTICIPANTS_DEFAULT,
            fee_per_element=self.cfg.FEE_PER_ELEMENT,
            deadline_unix=deadline,
            required_nonconforming=self.cfg.DEFAULT_REQUIRED_NONCONFORMING,
            max_conforming_utxos=self.cfg.MAX_CONFORMING_UTXOS,
        )
        await self.db.update_mix(mid, state="collecting", input_type=input_type)
        logger.info("Auto-created mix %s for input_type=%s", tokens.m(mid), input_type)
        return mid

    async def _cmd_commit_utxos(self, ctx: SenderContext, npub_hex: str, utxos: List[Dict]):
        """Handle /commit <txid:vout> ... — register UTXOs."""
        if not utxos:
            await self.nostr.send_dm(npub_hex, "No UTXOs found. Format: /commit <txid:vout> <txid:vout> ...")
            return

        # Find the participant's record
        participants = await self.db.get_participants_by_npub(npub_hex)
        # Filter to unpaid, un-cancelled
        active = [p for p in participants if p["state"] in ("interested", "committed")]

        if not active:
            # Auto-mix-on-commit (plan section 3g): peek at the first UTXO to
            # learn the user's input type, then find or create a compatible
            # open mix and add the user as 'interested'.
            first = utxos[0]
            peek = await self.chain.lookup_txout(first["txid"], first["vout"])
            if peek is None:
                await self.nostr.send_dm(
                    npub_hex,
                    f"Could not look up {first['txid']}:{first['vout']}. "
                    f"Try /list to see open mixes, or /join one.",
                )
                return
            peek_type = peek.get("scriptpubkey_type", "p2wpkh")
            if peek_type not in self.cfg.ACCEPTED_INPUT_TYPES:
                accepted = ", ".join(sorted(self.cfg.ACCEPTED_INPUT_TYPES))
                await self.nostr.send_dm(
                    npub_hex,
                    f"Your UTXO is {peek_type}; we only accept {accepted} inputs right now.",
                )
                return
            if await self.db.is_blacklisted(npub_hex):
                await self.nostr.send_dm(npub_hex, "You have been blacklisted from this bot.")
                return

            # S1 race guard: a concurrent /commit (or this same npub's
            # second DM) may have inserted an 'interested' / 'committed'
            # row while we were awaiting the chain RPC above. If so, do
            # NOT spin up a fresh participant — let the main path attach
            # the UTXOs to the existing row instead.
            race_participants = await self.db.get_participants_by_npub(npub_hex)
            race_active = [p for p in race_participants
                           if p["state"] in ("interested", "committed")]
            if race_active:
                active = race_active
            else:
                chosen_mix = await self._find_or_create_mix_for(peek_type)
                if chosen_mix is None:
                    await self.nostr.send_dm(
                        npub_hex, "No compatible mix available — try /list and /join.",
                    )
                    return
                identity = await self.nostr.get_identity(npub_hex)
                lud16 = identity["lud16"] if identity else ""
                await self.db.add_participant(chosen_mix, npub_hex, lud16)
                await self.nostr.send_dm(
                    npub_hex,
                    f"Added you to mix {chosen_mix} ({peek_type}). Processing your UTXOs...",
                )
                participants = await self.db.get_participants_by_npub(npub_hex)
                active = [p for p in participants if p["state"] in ("interested", "committed")]
                if not active:
                    return  # defensive

        pid = active[0]["id"]
        mix_id = active[0]["mix_id"]
        mix = await self.db.get_mix(mix_id)
        # Mix-level type lock. None means "first commit sets it"; once set,
        # later UTXOs must match.
        locked_input_type: Optional[str] = mix.get("input_type") if mix else None
        candidate_lock_type: Optional[str] = locked_input_type

        # Conforming / non-conforming caps. A conforming UTXO (amount ==
        # output_size) is a 1->1 free pass-through; the mix absorbs at most
        # max_conforming_utxos of them (a miner-fee burden the non-conforming
        # participants subsidise). A participant may bring at most
        # MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT non-conforming UTXOs.
        output_size = mix["output_size"] if mix else self.cfg.DEFAULT_OUTPUT_SIZE
        max_conforming = mix.get("max_conforming_utxos") if mix else None
        if max_conforming is None:
            max_conforming = self.cfg.MAX_CONFORMING_UTXOS
        max_nc_per_p = self.cfg.MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT
        mix_conforming_existing = sum(
            1 for u in await self.db.get_utxos_for_mix(mix_id)
            if u["amount"] == output_size
        )
        pid_nc_existing = sum(
            1 for u in await self.db.get_utxos_by_participant(pid)
            if u["amount"] != output_size
        )
        accepted_conforming = 0
        accepted_nc = 0

        # M2: batch rejections into a single summary DM instead of spamming
        # one DM per bad UTXO. A user pasting 50 outpoints (or a griefer
        # pasting nonsense) used to get one DM per row; that exhausts the
        # SDK's relay budget and is genuinely user-hostile.
        # rejections: list of (txid:vout, short reason). Same outpoint can
        # appear under multiple buckets if multiple checks would fail; we
        # only report the first one we hit per UTXO.
        rejections: List[Tuple[str, str]] = []

        # Validate UTXOs on chain
        total_sats = 0
        valid_utxos = []
        import sqlite3
        for utxo_data in utxos:
            txid = utxo_data["txid"]
            vout = utxo_data["vout"]
            outpoint = f"{txid}:{vout}"

            if await self.db.is_blacklisted(npub_hex, outpoint):
                rejections.append((outpoint, "blacklisted"))
                continue

            if await self.db.is_utxo_used(txid, vout):
                rejections.append((outpoint, "already used in another mix"))
                continue

            txout = await self.chain.lookup_txout(txid, vout)
            if txout is None:
                rejections.append((outpoint, "not found on chain"))
                continue

            # Require the funding (parent) tx to be confirmed. An unconfirmed
            # parent can be replaced (RBF) out from under us AFTER everyone has
            # signed, failing the whole coinjoin late — and the griefer isn't
            # even ghosted (this isn't a signing-deadline miss). lookup_txout's
            # "status" is the parent-confirmed flag.
            if not txout.get("status", False):
                rejections.append(
                    (outpoint, "unconfirmed — wait for at least 1 confirmation"))
                continue

            # S-B: None = couldn't verify; True = spent; False = unspent.
            spent = await self.chain.is_utxo_spent(txid, vout)
            if spent is None:
                rejections.append((outpoint, "chain unreachable — try again later"))
                continue
            if spent:
                rejections.append((outpoint, "already spent on-chain"))
                continue

            amount = txout.get("value", 0)
            script_type = txout.get("scriptpubkey_type", "p2wpkh")
            scriptpubkey = txout.get("scriptpubkey", "")

            if script_type not in self.cfg.ACCEPTED_INPUT_TYPES:
                accepted = ", ".join(sorted(self.cfg.ACCEPTED_INPUT_TYPES))
                rejections.append(
                    (outpoint, f"is {script_type}, we only accept {accepted}"),
                )
                continue

            if candidate_lock_type is None:
                candidate_lock_type = script_type
            elif script_type != candidate_lock_type:
                rejections.append(
                    (outpoint,
                     f"is {script_type}, but this mix is locked to {candidate_lock_type}"),
                )
                continue

            if amount < self.cfg.MINIMUM_UTXO_SIZE:
                rejections.append(
                    (outpoint,
                     f"{amount} sats < {self.cfg.MINIMUM_UTXO_SIZE}-sat minimum"),
                )
                continue

            # Conforming/non-conforming cap enforcement.
            is_conforming = (amount == output_size)
            if is_conforming:
                if mix_conforming_existing + accepted_conforming + 1 > max_conforming:
                    rejections.append(
                        (outpoint,
                         f"conforming-UTXO cap ({max_conforming}) for this mix is full"),
                    )
                    continue
            else:
                if pid_nc_existing + accepted_nc + 1 > max_nc_per_p:
                    rejections.append(
                        (outpoint,
                         f"over the {max_nc_per_p} non-conforming UTXO limit per participant"),
                    )
                    continue

            try:
                await self.db.add_utxo(pid, txid, vout, amount, script_type, scriptpubkey)
            except sqlite3.IntegrityError:
                rejections.append(
                    (outpoint, "claimed by another commit during processing — retry"),
                )
                continue
            await self.db.mark_utxo_used(pid, txid, vout)
            if is_conforming:
                accepted_conforming += 1
            else:
                accepted_nc += 1
            valid_utxos.append({"txid": txid, "vout": vout, "amount": amount, "script_type": script_type, "scriptpubkey": scriptpubkey})
            total_sats += amount

        # Send the batched rejection summary now — before the "no valid
        # UTXOs" early-return and before the success DM so the user sees
        # both pieces in order.
        if rejections:
            await self._send_rejection_summary(npub_hex, rejections)

        if not valid_utxos:
            await self.nostr.send_dm(npub_hex, "No valid UTXOs registered.")
            return

        # Persist the mix-level input type lock if this commit set it.
        if locked_input_type is None and candidate_lock_type is not None:
            await self.db.update_mix(mix_id, input_type=candidate_lock_type)

        # Update participant state
        await self.db.update_participant(pid, state="committed")

        # Address requirement depends on the conforming/non-conforming split of
        # ALL the participant's committed UTXOs (this commit + any prior ones).
        all_utxos = await self.db.get_utxos_by_participant(pid)
        conforming_total = sum(1 for u in all_utxos if u["amount"] == output_size)
        has_nc = any(u["amount"] != output_size for u in all_utxos)
        # Required floor: one fresh address per conforming UTXO, plus at least
        # one for a non-conforming participant's equal output. We RECOMMEND one
        # more for change so leftover sats aren't donated unintentionally.
        min_addrs = (conforming_total + 1) if has_nc else max(conforming_total, 1)
        recommended = (conforming_total + 2) if has_nc else max(conforming_total, 1)
        guidance = (
            f"Provide at least {min_addrs} output address(es) with /addresses "
            f"<addr1> <addr2> ..."
        )
        if has_nc:
            guidance += (
                f"\nTip: send one address per mixed output PLUS one for change "
                f"(≈{recommended} total). Without a change address, any above-dust "
                f"leftover is donated."
            )
        await self.nostr.send_dm(
            npub_hex,
            f"{len(valid_utxos)} UTXO(s) registered, total {total_sats / 1e8:.4f} BTC.\n"
            + guidance
        )

    async def _cmd_provide_addresses(self, ctx: SenderContext, npub_hex: str, addrs: List[str]):
        """Handle /addresses <addr> ... — register output addresses."""
        if not addrs:
            await self.nostr.send_dm(npub_hex, "Send me at least one output address.")
            return

        # Operator allowlist for output types. Reject the whole batch if any
        # address is the wrong type (or doesn't parse) — easier for the user to
        # fix a complete set than to track which ones we accepted.
        disallowed = []
        for addr in addrs:
            try:
                t = self.psbt_mgr._address_type(addr)
            except Exception:
                disallowed.append((addr, "unparseable"))
                continue
            if t not in self.cfg.ACCEPTED_OUTPUT_TYPES:
                disallowed.append((addr, t))
        if disallowed:
            accepted = ", ".join(sorted(self.cfg.ACCEPTED_OUTPUT_TYPES))
            sample = ", ".join(a for a, _ in disallowed[:3])
            await self.nostr.send_dm(
                npub_hex,
                f"For this mix we're only accepting {accepted} addresses. "
                f"These don't match: {sample}",
            )
            return

        # Find the mix this /addresses applies to. Eligible candidates:
        #   - state='committed' (the normal flow: UTXOs registered, no addresses yet)
        #   - state='paid' AND no stored outputs (ghost-recovery resubmission)
        # The one-at-a-time rule in /join means there's at most one 'committed'
        # row per npub, so the only multi-candidate case is multiple paid mixes
        # in simultaneous ghost-recovery.
        participants = await self.db.get_participants_by_npub(npub_hex)
        candidates = []
        for p in participants:
            if p["state"] == "committed":
                candidates.append(p)
            elif p["state"] == "paid":
                outs = await self.db.get_outputs_by_participant(p["id"])
                if not outs:
                    candidates.append(p)

        if not candidates:
            await self.nostr.send_dm(npub_hex, "You haven't committed UTXOs yet. Start with /commit")
            return
        if len(candidates) > 1:
            names = ", ".join(c["mix_id"] for c in candidates)
            await self.nostr.send_dm(
                npub_hex,
                f"You're awaiting addresses in multiple mixes: {names}. "
                f"Please /cancel one before resubmitting addresses for the other.",
            )
            return

        pid = candidates[0]["id"]
        mix_id = candidates[0]["mix_id"]
        already_paid = candidates[0]["state"] == "paid"
        mix = await self.db.get_mix(mix_id)

        if not mix:
            await self.nostr.send_dm(npub_hex, f"Mix {mix_id} not found.")
            return

        # Per-mix output type lock. First /addresses sets it; subsequent must
        # match. Combined with the allowlist gate above, this prevents mixed-type
        # outputs from fragmenting the anonymity set.
        locked_output_type: Optional[str] = mix.get("output_type")
        if locked_output_type:
            mismatched = []
            for addr in addrs:
                try:
                    t = self.psbt_mgr._address_type(addr)
                except Exception:
                    mismatched.append((addr, "unparseable"))
                    continue
                if t != locked_output_type:
                    mismatched.append((addr, t))
            if mismatched:
                sample = ", ".join(a for a, _ in mismatched[:3])
                await self.nostr.send_dm(
                    npub_hex,
                    f"This mix is locked to {locked_output_type} addresses. "
                    f"These don't match: {sample}",
                )
                return

        # Reject duplicate output addresses. Reusing an address (within this
        # batch, or one already pledged by another participant in the same mix)
        # collides two outputs into one on-chain — wrecking both the per-output
        # accounting and the privacy of an equal-output set. Say so plainly
        # rather than silently de-duping.
        seen_in_batch = set()
        dupes_in_batch = set()
        for a in addrs:
            if a in seen_in_batch:
                dupes_in_batch.add(a)
            seen_in_batch.add(a)
        if dupes_in_batch:
            sample = ", ".join(sorted(dupes_in_batch)[:3])
            await self.nostr.send_dm(
                npub_hex,
                f"Each output address must be unique. You repeated: {sample}. "
                f"Re-send /addresses with distinct, fresh addresses.",
            )
            return

        # Addresses already claimed by OTHER participants in this mix. (We're
        # about to replace THIS participant's own outputs, so theirs don't count.)
        others_addrs = set()
        for other in await self.db.get_participants_by_mix(mix_id):
            if other["id"] == pid:
                continue
            for o in await self.db.get_outputs_by_participant(other["id"]):
                others_addrs.add(o["address"])
        clash = [a for a in addrs if a in others_addrs]
        if clash:
            sample = ", ".join(sorted(set(clash))[:3])
            await self.nostr.send_dm(
                npub_hex,
                f"These addresses are already in use by someone else in {mix_id}: "
                f"{sample}. Send fresh addresses that you control.",
            )
            return

        # Get participant's UTXOs and classify against the mix's output size.
        output_size = mix["output_size"]
        utxos = await self.db.get_utxos_by_participant(pid)
        total_sats = sum(u["amount"] for u in utxos)
        conforming_count = sum(1 for u in utxos if u["amount"] == output_size)
        num_nc_inputs = sum(1 for u in utxos if u["amount"] != output_size)
        is_nc = num_nc_inputs > 0
        nc_total = sum(u["amount"] for u in utxos if u["amount"] != output_size)

        # Address-count rule: one fresh address per conforming UTXO, plus at
        # least one for a non-conforming participant's equal output. A change
        # address is OPTIONAL — if omitted, any above-dust leftover is donated
        # (and the user is warned) rather than blocking the join.
        min_addrs = (conforming_count + 1) if is_nc else max(conforming_count, 1)
        if len(addrs) < min_addrs:
            await self.nostr.send_dm(
                npub_hex,
                f"This commit needs at least {min_addrs} output address(es) "
                f"({conforming_count} conforming UTXO(s)"
                + (" + an equal output" if is_nc else "")
                + f"). You sent {len(addrs)}.",
            )
            return

        # Non-conforming participants must bring enough to fund at least one
        # full equal output (Q4: total inputs >= output_size).
        if is_nc and total_sats < output_size:
            await self.nostr.send_dm(
                npub_hex,
                f"Your inputs total {total_sats} sats, below one {output_size}-sat "
                f"output. Commit more before joining as a mixer.",
            )
            return

        # Preliminary non-conforming output layout (real miner fee is unknown
        # until assembly, so estimate with fee_share=0 — the maximum equal-output
        # count; assembly only ever trims it). When addresses are the binding
        # constraint, nc_output_plan gives back the last equal slot so an
        # above-dust leftover becomes change rather than being burnt/donated
        # (needs >=2 addresses).
        addrs_for_nc = max(0, len(addrs) - conforming_count)
        num_equal, num_change, chg_amt = self.fee_engine.nc_output_plan(
            nc_total, output_size, addrs_for_nc, 0,
        )
        total_equal = conforming_count + num_equal
        # Spare address for the above-dust leftover? If not, it gets donated.
        spare_change_addr = addrs_for_nc > num_equal
        will_donate = bool(num_change) and chg_amt > 0 and not spare_change_addr

        if total_equal == 0:
            await self.nostr.send_dm(npub_hex, "Your inputs are insufficient for even one output.")
            return

        # Service fee — charged ONLY on non-conforming inputs and their derived
        # outputs. Conforming pass-throughs are always free. With
        # FEE_PER_ELEMENT=0 this is 0 and no zap is requested.
        service_fee = self.fee_engine.calculate_service_fee(
            num_nc_inputs, num_equal + num_change,
            fee_per_element=mix.get("fee_per_element"),
        )

        # /addresses is replace-not-append: clear any outputs already on file
        # for this participant before storing the new set. This covers BOTH a
        # ghost-recovery resubmission (state 'paid') AND a fee-charged
        # participant who is still 'committed' (they have stored outputs from a
        # prior /addresses while awaiting their zap) re-sending a new list — e.g.
        # after we prompted them to add a change address. Without this the
        # outputs doubled, inflating the expected zap fee past what we quoted so
        # the user could never pay it. (C3)
        await self.db.delete_outputs_by_participant(pid)

        # Lay out in order: conforming pass-throughs, then NC equal outputs, then
        # a change output ONLY if the participant supplied a spare address. The
        # donation case (no spare address) is NOT stored here — it's added to the
        # tx at assembly, paid to DONATION_ADDRESS (or folded into the fee).
        idx = 0
        for _ in range(conforming_count):
            if idx < len(addrs):
                await self.db.add_output(pid, addrs[idx], output_size, is_change=False)
                idx += 1
        for _ in range(num_equal):
            if idx < len(addrs):
                await self.db.add_output(pid, addrs[idx], output_size, is_change=False)
                idx += 1
        if num_change > 0 and chg_amt > 0 and spare_change_addr and idx < len(addrs):
            await self.db.add_output(pid, addrs[idx], chg_amt, is_change=True)
            idx += 1

        # State transition + messaging. 'paid' is the universal ready state.
        summary = (
            f"{total_equal} output(s) @ {output_size / 1e8:.4f} BTC each"
            + (f" + {chg_amt / 1e8:.4f} BTC change" if num_change and chg_amt > 0 and spare_change_addr else "")
            + "."
        )
        # Warn about an above-dust leftover that will be donated for lack of a
        # change address. Approximate (real fee is applied at assembly).
        donation_note = ""
        if will_donate:
            donation_note = (
                f"\n⚠️ ~{chg_amt} sats of change will be DONATED — you didn't include "
                f"a change address. Re-send /addresses with {len(addrs) + 1} addresses "
                f"(one extra) to keep it."
            )
        # Warn when the no-burn rule had to sacrifice a mixed output because the
        # participant is address-constrained: they have a spare-address change
        # that EXCEEDS output_size (a funds-bound change is always < output_size,
        # so chg_amt >= output_size with a spare address uniquely identifies the
        # sacrifice). Nudge them to add addresses so they mix more and shrink
        # this distinctive (toxic) change. Mutually exclusive with donation_note.
        undermix_note = ""
        if (is_nc and spare_change_addr and num_change
                and chg_amt >= output_size and addrs_for_nc >= 2):
            funds_max_equal = nc_total // output_size
            potential_mixed = conforming_count + funds_max_equal
            ideal_addrs = potential_mixed + 1  # one per mixed output + one change
            potential_change = nc_total - funds_max_equal * output_size
            undermix_note = (
                f"\n⚠️ You sent {len(addrs)} address(es), so you'll mix {total_equal} "
                f"output(s) and receive {chg_amt} sats of change — larger than the "
                f"{output_size}-sat mix size, and easy to trace. Re-send /addresses "
                f"with {ideal_addrs} ({ideal_addrs - len(addrs)} more) to mix "
                f"{potential_mixed} output(s) and shrink your change to ~{potential_change} sats."
            )
        notes = donation_note + undermix_note
        if already_paid:
            new_state = "paid"
            await self.nostr.send_dm(
                npub_hex,
                summary + notes + "\nYou're already paid up; waiting for the mix to refill.",
            )
        elif service_fee <= 0:
            new_state = "paid"
            await self.nostr.send_dm(
                npub_hex, summary + notes + f"\nNo service fee — you're all set for {mix_id}.",
            )
        else:
            new_state = "committed"
            await self.nostr.send_dm(
                npub_hex,
                summary + notes + f"\nPay {service_fee} sats (service fee) via zap to {self.cfg.BOT_LUD16}.",
            )
        await self.db.update_participant(pid, state=new_state, change_amount=chg_amt)

        # Set the mix-level output lock if this was the first /addresses.
        if not locked_output_type and addrs:
            try:
                first_type = self.psbt_mgr._address_type(addrs[0])
                await self.db.update_mix(mix_id, output_type=first_type)
            except Exception:
                pass  # allowlist already validated parseability; best-effort
        # Move mix to collecting if not already; set deadline if unset
        if mix["state"] == "announced":
            deadline = mix.get("deadline_unix")
            if not deadline:
                deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
                await self.db.update_mix(mix_id, state="collecting", deadline_unix=deadline)
            else:
                await self.db.update_mix(mix_id, state="collecting")

    async def _cmd_accept_psbt(self, ctx: SenderContext, npub_hex: str, psbt_hex: str):
        """Handle /psbt_accept <hex> — participant returns signed PSBT."""
        if not psbt_hex:
            await self.nostr.send_dm(npub_hex, "No PSBT hex received.")
            return

        # Find participant in signing state
        participants = await self.db.get_participants_by_npub(npub_hex)
        signing = [p for p in participants if p["state"] == "signing"]

        if not signing:
            await self.nostr.send_dm(npub_hex, "No signing request pending for you.")
            return

        # Try matching the returned PSBT against each signing-state mix's
        # skeleton (#11). The participant's stored input_indices nail down
        # which inputs they were supposed to sign (#12); whichever skeleton's
        # signature pattern matches identifies the mix this PSBT belongs to.
        last_reason = "no signing requests found"
        chosen: Optional[Tuple[Dict, Dict, Dict, List[int]]] = None
        for cand in signing:
            cand_mix_id = cand["mix_id"]
            cand_mix = await self.db.get_mix(cand_mix_id)
            if not cand_mix:
                continue
            cand_round_num = cand_mix.get("ghost_retries", 0) + 1
            cand_round = await self.db.get_psbt_round(cand_mix_id, cand["id"], cand_round_num)
            if not cand_round or not cand_round.get("psbt_sent"):
                continue
            try:
                indices = json.loads(cand_round.get("input_indices") or "[]")
            except Exception:
                indices = []
            expected_addr_rows = await self.db.get_outputs_by_participant(cand["id"])
            expected_addr_list = [o["address"] for o in expected_addr_rows]
            ok, reason = self.psbt_mgr.validate_returned(
                cand_round["psbt_sent"], psbt_hex,
                participant_input_count=len(indices) if indices else 0,
                expected_output_addresses=expected_addr_list,
                participant_input_indices=indices if indices else None,
            )
            if ok:
                chosen = (cand, cand_mix, cand_round, indices)
                break
            last_reason = reason

        if chosen is None:
            await self.nostr.send_dm(
                npub_hex,
                f"PSBT didn't match any pending signing request ({last_reason}). "
                f"Please re-check and re-submit.",
            )
            return

        matched_p, matched_mix, matched_round, _ = chosen
        pid = matched_p["id"]
        mix_id = matched_p["mix_id"]

        await self.db.update_psbt_round(
            matched_round["id"],
            psbt_returned=psbt_hex,
            psbt_returned_at_unix=int(time.time()),
            psbt_valid=True,
        )
        await self.db.update_participant(pid, state="signed")
        await self.nostr.send_dm(
            npub_hex, f"PSBT for {mix_id} accepted. Waiting for all participants to sign.",
        )

    async def _cmd_accept_psbt_chunk(self, ctx: SenderContext, npub_hex: str,
                                      chunk_idx: int, chunk_total: int, chunk_hex: str):
        """Handle chunked PSBT reassembly — keyed per (npub_hex, mix_id)."""
        # Find the participant's active signing mix so we can key correctly
        participants = await self.db.get_participants_by_npub(npub_hex)
        signing = [p for p in participants if p["state"] == "signing"]
        if not signing:
            await self.nostr.send_dm(npub_hex, "No signing request pending for you.")
            return
        mix_id = signing[0]["mix_id"]

        # Key the chunk storage per (npub_hex, mix_id) to avoid collision when
        # the same npub is in multiple mixes simultaneously.
        key = f"{npub_hex}:{mix_id}"
        if key not in self._psbt_chunks:
            self._psbt_chunks[key] = {"chunks": {}, "total": chunk_total, "started": time.time()}

        record = self._psbt_chunks[key]
        record["chunks"][chunk_idx] = chunk_hex

        # Check if all chunks received
        chunks_dict = record["chunks"]
        if len(chunks_dict) == chunk_total:
            # Reassemble
            reassembled = ""
            for idx in sorted(chunks_dict.keys()):
                reassembled += chunks_dict[idx]
            # Clean up chunks
            del self._psbt_chunks[key]
            # Forward to accept_psbt handler
            await self._cmd_accept_psbt(ctx, npub_hex, reassembled)
        else:
            await self.nostr.send_dm(npub_hex, f"Chunk {chunk_idx}/{chunk_total} received. Waiting for remaining chunks.")

    async def _cmd_exit_mix(self, ctx: SenderContext, npub_hex: str, mix_id: Optional[str]):
        """Handle /cancel or /exit [mix_id] — remove from mix."""
        participants = await self.db.get_participants_by_npub(npub_hex)

        if not participants:
            await self.nostr.send_dm(npub_hex, "Done.")
            return

        # Filter to non-cancelled participants
        active = [p for p in participants if p["state"] not in ("cancelled", "ghosted", "completed",
                                                          "refunding", "refunded", "refund_failed")]

        if not active:
            await self.nostr.send_dm(npub_hex, "Done.")
            return

        if len(active) == 1:
            target_p = active[0]
            pid = target_p["id"]
            actual_mix_id = target_p["mix_id"]
            mix = await self.db.get_mix(actual_mix_id)
        elif mix_id:
            # Find matching mix
            for p in active:
                m = await self.db.get_mix(p["mix_id"])
                if m and m["id"].lower() == mix_id.lower():
                    target_p = p
                    pid = p["id"]
                    actual_mix_id = p["mix_id"]
                    mix = m
                    break
            else:
                # No match — list the mixes they're in. Use the first as the
                # example mix-id in the prompt so the user has something
                # concrete to copy/paste.
                names = [p["mix_id"] for p in active]
                await self.nostr.send_dm(
                    npub_hex,
                    f"You are a part of {len(active)} mixes: {' & '.join(names)}. "
                    f"Say /cancel {names[0]} (or another mix name) to exit one."
                )
                return
        else:
            # Multiple mixes but no mix_id given
            names = [p["mix_id"] for p in active]
            await self.nostr.send_dm(
                npub_hex,
                f"You are a part of {len(active)} mixes: {' & '.join(names)}. "
                f"Say /cancel {names[0]} (or another mix name) to exit one."
            )
            return

        # Once a mix has left collecting it is being (or about to be) turned
        # into a signed transaction that already commits this participant's
        # inputs. Letting them /cancel now would delete their inputs from under
        # the skeleton everyone else is signing — finalize would then fail and
        # the WHOLE mix would be cancelled, letting one party grief every honest
        # participant for the price of their own keep amount. We gate on BOTH
        # the participant state (signing/signed) AND the mix state (assembling/
        # signing): during the assembling window the participant is still 'paid'
        # but the skeleton is being built, so a state-only check missed it. If
        # they genuinely can't sign, the signing-deadline ghost-recovery path
        # handles their departure cleanly (and blacklists them). (H1)
        mix_state = mix.get("state") if mix else None
        if target_p["state"] in ("signing", "signed") or mix_state in ("assembling", "signing"):
            await self.nostr.send_dm(
                npub_hex,
                f"{actual_mix_id} is already assembling/signing, so it's too "
                f"late to cancel — backing out now would force everyone else to "
                f"start over. If you can't sign, just don't: after the signing "
                f"deadline the mix automatically re-forms without you.",
            )
            return

        # Release UTXOs back to the outpoint pool BEFORE the wallet call so
        # they're freed even if we crash mid-refund. UNIQUE(txid, vout)
        # would otherwise block the same outpoint forever.
        await self.db.delete_utxos_by_participant(pid)
        await self.db.delete_outputs_by_participant(pid)

        # Refund fee — goes through _safe_refund for idempotency (C-B).
        fee_paid = int(target_p.get("fee_paid") or 0)
        lud16 = target_p.get("lightning_addr") or ""
        if fee_paid > 0 and lud16:
            refund_sats = self._refund_keep_math(fee_paid)
            new_state = await self._safe_refund(
                target_p, actual_mix_id, refund_sats, reason="voluntary_exit",
            )
            if new_state == "refunded":
                msg = self.parser.format_refund(refund_sats, "voluntary exit")
            else:
                msg = (f"Sorry to see you go. We tried to refund {refund_sats} sats "
                       f"but our Lightning backend rejected it — please contact "
                       f"the operator.")
        elif fee_paid > 0:
            # Stuck without a lud16: log + DM, leave state cancelled.
            logger.error(
                "Cannot refund voluntary exit for participant %s in mix %s: "
                "fee_paid=%d but no lightning_addr.",
                tokens.p(npub_hex), tokens.m(actual_mix_id), fee_paid,
            )
            await self.db.update_participant(pid, state="cancelled")
            msg = (f"Sorry to see you go. We can't refund automatically "
                   f"(no Lightning address on file) — please contact the operator "
                   f"to reclaim your {fee_paid} sats.")
        else:
            await self.db.update_participant(pid, state="cancelled")
            msg = "Sorry to see you go."

        await self.nostr.send_dm(npub_hex, msg)

    # --- Zap Handler ---

    async def _on_zap(self, zap: ValidatedZap, ctx: SenderContext):
        """Handle a zap receipt — match sender npub + amount to pending participant."""
        npub_hex = zap.sender_hex
        amount_sats = zap.amount_sats

        # Find participants waiting for payment
        participants = await self.db.get_participants_by_npub(npub_hex)
        awaiting = [p for p in participants if p["state"] == "committed"]

        if not awaiting:
            # No 'committed' participant is waiting on this zap. Three cases:
            #
            #  1. The sender already paid and their mix is in flight ('paid' /
            #     'signing' / 'signed' / 'broadcast'). This is a duplicate or an
            #     overpayment, NOT a refundable orphan — log for revenue audit
            #     and keep it (a successful mix's fee is non-refundable).
            #  2. The sender's slot TIMED OUT ('cancelled') before this zap
            #     landed (LN/relay latency, or they paid right at the deadline).
            #     Their money arrived too late to join — record a refund debt so
            #     the operator returns it, and tell the user, rather than
            #     silently pocketing a payment they tried to make.
            #  3. Genuinely unknown sender / donation — log and keep.
            #
            # Sender is always an opaque token in logs so we never link an npub
            # to a payment trail.
            in_flight = [p for p in participants
                         if p["state"] in ("paid", "signing", "signed", "broadcast")]
            if in_flight:
                logger.info(
                    "Extra zap from already-paid sender=%s amount=%d sats — "
                    "duplicate/overpayment, kept",
                    tokens.p(npub_hex), amount_sats,
                )
                return

            # Most recent timed-out slot with a Lightning address on file.
            timed_out = [p for p in participants
                         if p["state"] == "cancelled" and (p.get("lightning_addr") or "").strip()]
            if timed_out:
                target = max(timed_out, key=lambda p: int(p.get("updated_at_unix") or 0))
                await self.db.add_refund_owed(
                    target["id"], target["lightning_addr"].strip(), amount_sats,
                    reason="late_zap_after_timeout",
                )
                logger.info(
                    "Late zap recorded as refund owed: sender=%s mix=%s amount=%d sats",
                    tokens.p(npub_hex), tokens.m(target["mix_id"]), amount_sats,
                )
                await self.nostr.send_dm(
                    npub_hex,
                    f"Your {amount_sats}-sat payment arrived after your slot in "
                    f"{target['mix_id']} expired, so it couldn't join the mix. "
                    f"The operator will refund it to your Lightning address.",
                )
                return

            logger.info(
                "Unmatched zap: sender=%s amount=%d sats — no committed participant; ignored",
                tokens.p(npub_hex), amount_sats,
            )
            return

        pid = awaiting[0]["id"]
        mix_id = awaiting[0]["mix_id"]
        mix = await self.db.get_mix(mix_id)

        if not mix:
            return

        # Calculate expected service fee. Only NON-conforming inputs and their
        # derived outputs are charged; conforming UTXOs (amount == output_size)
        # are free pass-throughs. Conforming outputs and NC-equal outputs both
        # have amount == output_size, so we can't tell them apart by value —
        # subtract the conforming UTXO count from the total used outputs to get
        # the NC-derived output count.
        utxos = await self.db.get_utxos_by_participant(pid)
        outputs = await self.db.get_outputs_by_participant(pid)
        output_size = mix["output_size"]
        nc_inputs = sum(1 for u in utxos if u["amount"] != output_size)
        conforming_count = sum(1 for u in utxos if u["amount"] == output_size)
        total_used = sum(1 for o in outputs if o["amount"] > 0)
        nc_used = max(0, total_used - conforming_count)

        expected_fee = self.fee_engine.calculate_service_fee(
            nc_inputs, nc_used, fee_per_element=mix.get("fee_per_element"),
        )

        # Validate payment — partial payments are the same as no payment
        if amount_sats >= expected_fee:
            # Note overpayments so the operator can audit revenue against
            # expected service fees. The user gets the same accept DM either
            # way (it includes the actual amount they sent). On cancellation,
            # the full fee_paid is what gets refunded (modulo keep_percent),
            # so the bot's only net gain on overpayment is on a successful
            # mix — the operator should be able to see that in the books.
            if amount_sats > expected_fee:
                logger.info(
                    "Overpayment: participant=%s mix=%s received=%d expected=%d (+%d)",
                    tokens.p(npub_hex), tokens.m(mix_id),
                    amount_sats, expected_fee, amount_sats - expected_fee,
                )
            await self.db.update_participant(pid, fee_paid=amount_sats, state="paid")
            await self.nostr.send_dm(npub_hex, f"Payment of {amount_sats} sats accepted for {mix_id}.")
            # The collecting-state tick checks readiness (_classify_ready) and
            # advances the mix to assembling once the non-conforming target is
            # met — no need to duplicate that decision here.
        else:
            await self.nostr.send_dm(
                npub_hex,
                f"Payment of {amount_sats} sats insufficient. Expected at least {expected_fee} sats."
            )

    # --- Heartbeat ---

    async def _on_heartbeat(self, uptime_s: int):
        """Heartbeat callback — called every 300s by the NostrBot runtime."""
        logger.info(f"Bot heartbeat: uptime={uptime_s}s")

    # --- Event Loop ---

    async def run_event_loop(self):
        """Main event loop — check mix states, advance state machines."""
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                # exc_info would carry user-data-bearing frame locals into
                # the log. Class name only.
                logger.error("Event loop tick error: %s", type(e).__name__)
            await asyncio.sleep(60)  # Check every 60 seconds

    STALE_CHUNK_TIMEOUT = 3600  # 1 hour — discard incomplete chunk sets

    async def _tick(self):
        """One tick of the state machine."""
        now = time.time()
        active_mixes = await self.db.get_active_mixes()

        for mix in active_mixes:
            try:
                await self._process_mix(mix, now)
            except Exception as e:
                # mix token + exception class only — `e` may include
                # UTXO or address content from inner SQL / PSBT errors.
                logger.error(
                    "Error processing mix %s: %s",
                    tokens.m(mix["id"]), type(e).__name__,
                )

        # Stale chunk cleanup — discard chunk sets that started >1h ago
        stale_keys = [
            k for k, rec in self._psbt_chunks.items()
            if now - rec.get("started", 0) > self.STALE_CHUNK_TIMEOUT
        ]
        if stale_keys:
            # Don't log the keys — they're "<npub_hex>:<mix_id>" strings.
            # The count is all the operator needs for hygiene.
            logger.info("Cleaning up %d stale PSBT chunk set(s)", len(stale_keys))
        for key in stale_keys:
            del self._psbt_chunks[key]

        # Broadcast sweep — checks confirmed mixes on an N-hour interval
        # rather than polling every 60s.
        await self._broadcast_sweep(now)

    async def _process_mix(self, mix: Dict, now: int):
        """Process a single mix's state."""
        mix_id = mix["id"]
        state = mix["state"]
        participants = await self.db.get_participants_by_mix(mix_id)
        # Filter active participants
        active = [p for p in participants if p["state"] not in ("cancelled", "ghosted", "completed",
                                                          "refunding", "refunded", "refund_failed")]

        match state:
            case "announced":
                # Nothing to process — waiting for participants
                pass

            case "collecting":
                # Per-participant pay-deadline: any 'committed' participant
                # who hasn't paid within PAY_DEADLINE_HOURS is removed. They
                # paid nothing on-chain, so no refund — just clean up their
                # UTXO/output rows and DM them.
                pay_seconds = self.cfg.PAY_DEADLINE_HOURS * 3600
                for p in list(active):
                    if p["state"] != "committed":
                        continue
                    age = now - int(p.get("updated_at_unix") or 0)
                    if age <= pay_seconds:
                        continue
                    logger.info(
                        "Participant %s never paid mix %s after %dh — removing",
                        tokens.p(p["npub_hex"]), tokens.m(mix_id),
                        self.cfg.PAY_DEADLINE_HOURS,
                    )
                    await self.db.delete_utxos_by_participant(p["id"])
                    await self.db.delete_outputs_by_participant(p["id"])
                    await self.db.update_participant(p["id"], state="cancelled")
                    p["state"] = "cancelled"  # sync in-memory for filters below
                    try:
                        await self.nostr.send_dm(
                            p["npub_hex"],
                            f"Your slot in {mix_id} expired (no payment within "
                            f"{self.cfg.PAY_DEADLINE_HOURS}h). Use /join {mix_id} to retry.",
                        )
                    except Exception:
                        pass

                # Re-filter active after the pay-timeout sweep.
                active = [p for p in active if p["state"] not in ("cancelled", "ghosted", "completed",
                                                          "refunding", "refunded", "refund_failed")]

                # Proceed as soon as the non-conforming target is met — we do
                # NOT wait for conforming UTXOs (unless a solo-NC mix, which
                # needs >=1 conforming so there are >=2 equal outputs). 'paid'
                # is the universal ready state: with a service fee it means the
                # zap arrived; with FEE_PER_ELEMENT=0 it's set right after
                # /addresses (no zap needed).
                ready = [p for p in active if p["state"] == "paid"]
                proceed, _nc, _conf = await self._classify_ready(mix, ready)
                if proceed:
                    await self._proceed_to_assembling(mix, ready)
                else:
                    deadline = mix.get("deadline_unix")
                    if deadline and now >= deadline:
                        # We wait for the EXACT non-conforming target; if the
                        # deadline arrives before we reach it, the deterministic
                        # fee split can't be honoured — cancel and refund.
                        await self._cancel_and_refund(
                            mix,
                            "deadline passed before the non-conforming target was met",
                        )

            case "assembling":
                await self._assemble_psbt(mix, active)

            case "signing":
                await self._handle_signing(mix, active, now)

            case "broadcast":
                # No per-tick processing — handled by _broadcast_sweep
                # on an N-hour interval instead.
                pass

            case "completed":
                # Clean up old completed mixes
                pass

    async def _classify_ready(self, mix: Dict, ready: List[Dict]) -> Tuple[bool, int, bool]:
        """Decide whether a collecting mix can advance to assembling.

        ``ready`` is the list of 'paid' participants (the universal
        ready-to-assemble state). Returns (proceed, nc_count, conforming_present):

        - nc_count: how many ready participants brought >=1 non-conforming UTXO
        - conforming_present: whether any conforming UTXO is present among ready
        - proceed: nc_count >= required_nonconforming, AND (required >= 2 OR a
          conforming UTXO is present) — the solo-NC guard guarantees >=2 equal
          outputs from distinct parties before we ever sign.
        """
        output_size = mix["output_size"]
        required = mix.get("required_nonconforming") or self.cfg.DEFAULT_REQUIRED_NONCONFORMING
        nc_count = 0
        conforming_present = False
        for p in ready:
            # A participant only counts toward the target once they have output
            # addresses on file. After ghost recovery every survivor is reset to
            # 'paid' with their addresses deleted; without this guard the mix
            # would re-advance to assembling on the very next tick — before
            # anyone resubmits /addresses — and assemble with empty address
            # lists (burning conforming UTXOs to the miner fee). (C2/H1)
            outputs = await self.db.get_outputs_by_participant(p["id"])
            if not outputs:
                continue
            utxos = await self.db.get_utxos_by_participant(p["id"])
            if any(u["amount"] != output_size for u in utxos):
                nc_count += 1
            if any(u["amount"] == output_size for u in utxos):
                conforming_present = True
        solo_ok = required >= 2 or conforming_present
        return (nc_count >= required and solo_ok), nc_count, conforming_present

    async def _cleanup_interested(self, mix_id: str):
        """Drop participants still in 'interested' (joined via /join but never
        successfully /commit-ed) when the mix leaves the collecting phase.

        An 'interested' row has no UTXOs or outputs (those are created at
        /commit, which also moves the row to 'committed'), so there's nothing
        on-chain to release — we just delete the row. Leaving it would (a) keep
        counting against the user's MAX_PENDING_MIXES / one-at-a-time gate and
        (b) leave their npub on disk for a mix they aren't part of. Idempotent:
        a second call finds no 'interested' rows. End-of-life phases handle
        their own cleanup (cancel scrubs all rows; confirmation destroys them).
        """
        participants = await self.db.get_participants_by_mix(mix_id)
        for p in participants:
            if p["state"] != "interested":
                continue
            try:
                await self.nostr.send_dm(
                    p["npub_hex"],
                    f"Mix {mix_id} has started without you — you never sent "
                    f"/commit. Use /list to find another open mix.",
                )
            except Exception:
                pass
            await self.db.delete_participant(p["id"])

    async def _proceed_to_assembling(self, mix: Dict, active: List[Dict]):
        """Move mix from collecting to assembling."""
        mix_id = mix["id"]
        await self.db.update_mix(mix_id, state="assembling")

        # Drop never-committed 'interested' stragglers now that the mix is
        # leaving the collecting phase.
        await self._cleanup_interested(mix_id)

        # Notify participants
        for p in active:
            await self.nostr.send_dm(
                p["npub_hex"],
                self.parser.format_psbt_request(mix_id, self.cfg.SIGNING_DEADLINE_HOURS),
            )

    async def _gather_assembly_data(self, active: List[Dict], output_size: int) -> Tuple[
            List[Dict], List[Dict], Dict[str, List[str]], Dict[str, List[int]]]:
        """Build the four parallel structures _assemble_psbt needs:

        - all_inputs: positional list fed to build_skeleton
        - participants_data: list fed to fee_engine.calculate_all_fees
        - addrs_by_pid: each participant's addresses (used to lay out outputs)
        - input_indices_by_pid: which vin indices each participant must sign
          (used by /psbt_accept's strict per-input check)

        Each participant's UTXOs are classified against ``output_size``:
        conforming (amount == output_size, free 1->1 pass-through) vs
        non-conforming (carved into equal outputs + change; pays the miner fee).

        Kept as a helper so the under-funded-drop retry path (C2) can re-build
        these for the surviving participants without duplicating logic.
        """
        all_inputs: List[Dict] = []
        participants_data: List[Dict] = []
        addrs_by_pid: Dict[str, List[str]] = {}
        input_indices_by_pid: Dict[str, List[int]] = {}

        for p in active:
            pid = p["id"]
            utxos = await self.db.get_utxos_by_participant(pid)
            stored_outputs = await self.db.get_outputs_by_participant(pid)
            addrs_in_order = [o["address"] for o in stored_outputs]
            total_sats = sum(u["amount"] for u in utxos)

            start_idx = len(all_inputs)
            for u in utxos:
                all_inputs.append({
                    "txid": u["txid"],
                    "vout": u["vout"],
                    "amount": u["amount"],
                    "script_type": u.get("script_type", "p2wpkh"),
                    "scriptpubkey": u.get("scriptpubkey", ""),
                    "pid": pid,  # used to re-derive input_indices after sorting
                })
            # Provisional indices (pre-sort). _assemble_psbt re-derives these
            # against the final sorted input order before sending the skeleton.
            input_indices_by_pid[pid] = list(range(start_idx, start_idx + len(utxos)))

            # Classify: conforming (== output_size) vs non-conforming.
            conforming = [u for u in utxos if u["amount"] == output_size]
            nonconf = [u for u in utxos if u["amount"] != output_size]
            nc_total = sum(u["amount"] for u in nonconf)

            nc_ibt: Dict[str, int] = {}
            for u in nonconf:
                st = u.get("script_type", "p2wpkh")
                nc_ibt[st] = nc_ibt.get(st, 0) + 1

            # Output script type — all of a participant's addresses are the same
            # type (per-mix output lock), so peek at the first; fall back p2wpkh.
            if addrs_in_order:
                try:
                    output_type = self.psbt_mgr._address_type(addrs_in_order[0])
                except Exception:
                    output_type = "p2wpkh"
            else:
                output_type = "p2wpkh"

            participants_data.append({
                "pid": pid,
                "npub_hex": p["npub_hex"],
                "total_sats": total_sats,
                "num_addresses": len(addrs_in_order),
                "conforming_count": len(conforming),
                "nonconforming_total_sats": nc_total,
                "nonconforming_inputs_by_type": nc_ibt,
                "output_type": output_type,
                "is_nonconforming": len(nonconf) > 0,
            })
            addrs_by_pid[pid] = addrs_in_order

        return all_inputs, participants_data, addrs_by_pid, input_indices_by_pid

    # Terminal-for-refund-purposes states. Any participant in one of these
    # has already had their refund decision made; calling _safe_refund again
    # on them is a no-op. Critical for crash-recovery idempotency (C-B).
    # States that _safe_refund must NOT pay out. 'ghosted' is included
    # deliberately: a participant who paid the service fee but let the signing
    # deadline lapse FORFEITS that fee (the FINAL WARNING DM says as much) and
    # is blacklisted — refunding them would both contradict that promise and pay
    # out money on every "max ghost retries exceeded" cancellation.
    _REFUND_TERMINAL_STATES = frozenset({
        "refunding", "refunded", "refund_failed", "cancelled", "completed",
        "ghosted",
    })

    async def _safe_refund(self, p: Dict, mix_id: str, refund_sats: int,
                            reason: str) -> str:
        """Idempotent refund. Sets state='refunding' BEFORE calling the LN
        wallet, then 'refunded' or 'refund_failed' after. If a prior call
        already moved the participant past 'paid', returns immediately
        without touching the wallet — this is the crash-resume defence
        against double payouts.

        Returns the new state. Callers should DM the user based on that.
        """
        pid = p["id"]
        # Re-read state from DB. The `p` dict the caller passed may be
        # stale if anything else updated this participant in the meantime
        # (e.g. event loop crash + restart between the caller's read and
        # this call). The DB is the source of truth.
        fresh = await self.db.get_participant(pid)
        if fresh and fresh.get("state") in self._REFUND_TERMINAL_STATES:
            logger.info(
                "Skipping refund for participant %s in mix %s — already in state %s",
                tokens.p(p["npub_hex"]), tokens.m(mix_id), fresh.get("state"),
            )
            return fresh.get("state") or "cancelled"

        # Commit the intent BEFORE the network call. If the bot crashes
        # between this UPDATE and the LN call, the participant will be in
        # 'refunding' on resume — _REFUND_TERMINAL_STATES treats that as
        # done, so we don't pay twice. The trade-off: an actual LN failure
        # mid-call also looks like 'refunding'. The operator scans for
        # stuck 'refunding' rows at startup.
        await self.db.update_participant(pid, state="refunding")

        lud16 = p.get("lightning_addr", "")
        result = await self.lightning.send_refund(lud16, refund_sats, reason=reason)
        if result is not None:
            await self.db.update_participant(pid, state="refunded")
            return "refunded"
        # LN refused both backends. Don't go back to 'paid' (would invite
        # another retry). Park in 'refund_failed' so the operator can
        # reconcile by hand without us hammering the wallet on every tick.
        await self.db.update_participant(pid, state="refund_failed")
        logger.error(
            "Refund FAILED for participant %s in mix %s (sats=%d, reason=%s) — "
            "operator must reconcile",
            tokens.p(p["npub_hex"]), tokens.m(mix_id), refund_sats, reason,
        )
        return "refund_failed"

    def _refund_keep_math(self, fee_paid: int) -> int:
        """Apply REFUND_KEEP_PERCENT / REFUND_KEEP_MIN_SATS to fee_paid."""
        return max(
            fee_paid * (100 - self.cfg.REFUND_KEEP_PERCENT) // 100,
            max(fee_paid - self.cfg.REFUND_KEEP_MIN_SATS, 0),
        )

    async def _drop_underfunded(self, p: Dict, mix_id: str):
        """Refund + DM a participant whose allocation collapsed to 0 equal
        outputs once the real miner fee was applied. C2 fix — the old code
        cancelled the whole mix in this case.

        Idempotent via _safe_refund — safe to call twice on the same pid
        across a crash boundary (C-B fix)."""
        # If already past 'paid' (e.g. a prior _drop_underfunded call landed
        # before a crash), skip the whole thing — UTXOs are gone, refund
        # decision is recorded.
        fresh = await self.db.get_participant(p["id"])
        if fresh and fresh.get("state") in self._REFUND_TERMINAL_STATES:
            return

        # Release the dropped participant's UTXOs back to the pool — the
        # UNIQUE(txid, vout) constraint would otherwise block these
        # outpoints from being committed to a future mix. Idempotent
        # (DELETE on already-empty set is a no-op).
        await self.db.delete_utxos_by_participant(p["id"])
        await self.db.delete_outputs_by_participant(p["id"])

        fee_paid = int(p.get("fee_paid") or 0)
        lud16 = p.get("lightning_addr", "")
        npub = p["npub_hex"]

        if fee_paid > 0 and lud16:
            refund_sats = self._refund_keep_math(fee_paid)
            new_state = await self._safe_refund(
                p, mix_id, refund_sats, reason="underfunded_dropped",
            )
            if new_state == "refunded":
                await self.nostr.send_dm(
                    npub,
                    f"Dropped from mix {mix_id}: your inputs couldn't cover one equal "
                    f"output plus your share of the miner fee. Refunded {refund_sats} sats.",
                )
            else:  # refund_failed
                await self.nostr.send_dm(
                    npub,
                    f"Dropped from mix {mix_id}: your inputs couldn't cover one equal "
                    f"output plus miner fees. We tried to refund {refund_sats} sats but "
                    f"our Lightning backend rejected it — please contact the operator.",
                )
        elif fee_paid > 0:
            logger.error(
                "Cannot refund dropped participant %s in mix %s: fee_paid=%d but "
                "no lightning_addr. Sats stranded; operator must reconcile.",
                tokens.p(npub), tokens.m(mix_id), fee_paid,
            )
            await self.db.update_participant(p["id"], state="cancelled")
            await self.nostr.send_dm(
                npub,
                f"Dropped from mix {mix_id}: your inputs couldn't cover one equal "
                f"output plus miner fees. We can't refund automatically (no Lightning "
                f"address on file) — contact the operator to reclaim your {fee_paid} sats.",
            )
        else:
            await self.db.update_participant(p["id"], state="cancelled")
            await self.nostr.send_dm(
                npub,
                f"Dropped from mix {mix_id}: your inputs couldn't cover one equal "
                f"output plus miner fees.",
            )

    async def _drop_address_starved(self, p: Dict, mix_id: str):
        """Drop a participant who reached assembly without enough output
        addresses to receive the outputs they committed (most often a survivor
        whose addresses were cleared by ghost recovery and not resubmitted).
        Releases their UTXOs and refunds any service fee, with an accurate
        message telling them to resubmit /addresses.

        Idempotent via _safe_refund (C-B): safe to call twice across a crash."""
        fresh = await self.db.get_participant(p["id"])
        if fresh and fresh.get("state") in self._REFUND_TERMINAL_STATES:
            return

        await self.db.delete_utxos_by_participant(p["id"])
        await self.db.delete_outputs_by_participant(p["id"])

        fee_paid = int(p.get("fee_paid") or 0)
        lud16 = p.get("lightning_addr", "")
        npub = p["npub_hex"]
        msg = (
            f"Dropped from mix {mix_id}: we didn't have enough output addresses "
            f"on file to pay you when the mix assembled. Re-join and send "
            f"/addresses to try again."
        )

        if fee_paid > 0 and lud16:
            refund_sats = self._refund_keep_math(fee_paid)
            new_state = await self._safe_refund(
                p, mix_id, refund_sats, reason="address_starved_dropped",
            )
            if new_state == "refunded":
                await self.nostr.send_dm(
                    npub, msg + f" Refunded {refund_sats} sats.")
            else:
                await self.nostr.send_dm(
                    npub, msg + f" We tried to refund {refund_sats} sats but our "
                    f"Lightning backend rejected it — please contact the operator.")
        elif fee_paid > 0:
            logger.error(
                "Cannot refund address-starved participant %s in mix %s: "
                "fee_paid=%d but no lightning_addr. Operator must reconcile.",
                tokens.p(npub), tokens.m(mix_id), fee_paid,
            )
            await self.db.update_participant(p["id"], state="cancelled")
            await self.nostr.send_dm(npub, msg)
        else:
            await self.db.update_participant(p["id"], state="cancelled")
            await self.nostr.send_dm(npub, msg)

    async def _assemble_psbt(self, mix: Dict, active: List[Dict]):
        """Build the PSBT skeleton and send to all paid participants.

        Each participant's outputs are sized as:
            equal_outputs:  num_equal * output_size  (size set by the mix)
            change_output:  total_inputs - num_equal*output_size - fee_share

        fee_share is each participant's proportional slice of the total miner
        fee, computed from their input+output vsize contribution. The miner
        fee is what makes (sum of inputs) > (sum of outputs); it must be left
        on the table, not handed back as change.

        Under-funded participants (those whose proportional fee_share would
        push them below one equal output) are dropped + refunded, and the
        fee math is re-run with the survivors. If too many drops would push
        the mix below its required non-conforming count (or below 2 total),
        fall back to cancelling the whole mix.
        """
        mix_id = mix["id"]
        output_size = mix["output_size"]

        # Live fee-rate estimate at assembly time. If the chain monitor can
        # determine a recent-blocks rate, use it; otherwise fall back to the
        # mix's stored rate (set on a prior assembly attempt during crash
        # resume), and only then to the schema default. The estimate is
        # already clamped to [MIN_FEE_RATE, MAX_FEE_RATE] inside
        # ChainMonitor.estimate_fee_rate.
        live_rate: Optional[float] = None
        try:
            live_rate = await self.chain.estimate_fee_rate()
        except Exception as e:
            logger.warning(
                "estimate_fee_rate failed for mix %s: %s — falling back to stored rate",
                tokens.m(mix_id), type(e).__name__,
            )
        if live_rate and live_rate > 0:
            fee_rate = live_rate
        else:
            fee_rate = mix.get("fee_rate") or 30

        # Defensive: only assemble paid (or already-signing/signed, for
        # crash-resume) participants. The caller's `active` filter is loose
        # ("not cancelled, not ghosted") — without this guard, a participant
        # whose pay-timeout hasn't fired yet would be included with no zap on
        # file. 'signed' is included so a fast signer who returned a PSBT
        # against the OLD skeleton before a crash is re-added to the new round
        # (add_psbt_round resets their stale psbt_returned to NULL and they are
        # re-sent the fresh skeleton) rather than being silently excluded and
        # leaving a stale signature behind to poison the combine. (stale-sig)
        active = [p for p in active if p["state"] in ("paid", "signing", "signed")]

        # Conforming-model fee inputs. The miner fee is computed from the ACTUAL
        # conforming UTXOs in this frozen participant set (the cap,
        # MAX_CONFORMING_UTXOS, only bounded intake during collecting), so we
        # target the correct fee instead of over-collecting for unfilled slots.
        # The conforming burden is split evenly across the non-conforming
        # participants; conforming input/output vbytes use the mix's locked
        # types (fallback p2wpkh).
        conf_in_type = mix.get("input_type") or "p2wpkh"
        conf_out_type = mix.get("output_type") or "p2wpkh"

        def _calc(pdata):
            return self.fee_engine.calculate_all_fees(
                pdata, output_size, fee_rate,
                conf_input_type=conf_in_type, conf_output_type=conf_out_type,
            )

        # First pass: gather + fee math. (The S-A output-trim iteration now
        # lives inside calculate_all_fees, which sizes NC-derived outputs at
        # the real fee share rather than the declared address count.)
        all_inputs, participants_data, addrs_by_pid, input_indices_by_pid = \
            await self._gather_assembly_data(active, output_size)

        required_nc = mix.get("required_nonconforming") or self.cfg.DEFAULT_REQUIRED_NONCONFORMING

        # Defence-in-depth (C2/H1): a participant must have at least enough
        # addresses to receive the conforming pass-throughs they committed plus,
        # if they brought non-conforming inputs, one equal output. /addresses
        # enforces this at intake, but ghost recovery deletes everyone's
        # addresses and resets them to 'paid'. If such a participant reached
        # assembly before resubmitting, the output-building loop would SILENTLY
        # skip their funded outputs and burn those sats to the miner fee. Drop
        # them from this round (releasing UTXOs + refunding any fee) so funds are
        # never burned. _classify_ready already withholds the mix from advancing
        # for this reason, so in practice this is a backstop.
        def _min_addrs(rec: Dict) -> int:
            return rec.get("conforming_count", 0) + (1 if rec.get("is_nonconforming") else 0)

        starved_pids = {
            rec["pid"] for rec in participants_data
            if rec.get("num_addresses", 0) < _min_addrs(rec)
        }
        if starved_pids:
            survivors = []
            for p in active:
                if p["id"] in starved_pids:
                    await self._drop_address_starved(p, mix_id)
                else:
                    survivors.append(p)
            active = survivors
            all_inputs, participants_data, addrs_by_pid, input_indices_by_pid = \
                await self._gather_assembly_data(active, output_size)

            nc_survivors = sum(1 for rec in participants_data if rec.get("is_nonconforming"))
            if len(active) < 2 or nc_survivors < required_nc:
                await self._cancel_and_refund(
                    mix, "not enough participants after dropping address-starved",
                )
                return

        total_vsize, total_miner_fee, fee_results = _calc(participants_data)

        if total_miner_fee <= 0:
            await self._cancel_and_refund(mix, "invalid fee calculation")
            return

        # C2: a non-conforming participant whose NC inputs can't fund one equal
        # output after their fee share (num_equal_outputs == 0) is dropped +
        # refunded, then the fee math re-runs with the survivors. Conforming-only
        # participants are never under-funded (their outputs are free
        # pass-throughs). After dropping, the mix must still have at least the
        # required number of non-conforming participants (the exact target it
        # advanced on) AND >=2 participants overall; otherwise cancel.
        # (required_nc computed above, before the address-starvation guard.)
        underfunded_pids = {
            rec["pid"] for rec, fr in zip(participants_data, fee_results)
            if fr.is_nonconforming and fr.num_equal_outputs == 0
        }
        if underfunded_pids:
            survivors_active = []
            for p in active:
                if p["id"] in underfunded_pids:
                    await self._drop_underfunded(p, mix_id)
                else:
                    survivors_active.append(p)

            # Rebuild with survivors and re-run the fee math.
            active = survivors_active
            all_inputs, participants_data, addrs_by_pid, input_indices_by_pid = \
                await self._gather_assembly_data(active, output_size)
            total_vsize, total_miner_fee, fee_results = _calc(participants_data)

            nc_survivors = sum(1 for fr in fee_results if fr.is_nonconforming)
            still_underfunded = any(
                fr.is_nonconforming and fr.num_equal_outputs == 0
                for fr in fee_results
            )
            if (len(survivors_active) < 2 or nc_survivors < required_nc
                    or total_miner_fee <= 0 or still_underfunded):
                # Can't honour the required non-conforming target after the
                # drop — fall back to cancelling the whole mix and refunding.
                await self._cancel_and_refund(
                    mix, "not enough participants after dropping under-funded",
                )
                return

        # Build all_outputs with the corrected per-participant amounts. Each
        # participant's change is reduced by their fee_share; if change drops
        # below MINIMUM_UTXO_SIZE it's dropped entirely (those sats become
        # additional miner fee, per the plan).
        donation_address = (self.cfg.DONATION_ADDRESS or "").strip()
        all_outputs: List[Dict] = []
        # Track sats deliberately folded into the miner fee (above-dust leftovers
        # with no change address and no DONATION_ADDRESS). Used to bound the
        # pre-broadcast fee invariant without false-positives on legitimate folds.
        folded_above_dust = 0
        for rec, fr in zip(participants_data, fee_results):
            addrs = addrs_by_pid[rec["pid"]]
            idx = 0
            # 1) conforming pass-throughs: one output_size output per conforming
            #    UTXO, using the participant's first addresses.
            for _ in range(fr.conforming_count):
                if idx < len(addrs):
                    all_outputs.append({"address": addrs[idx], "amount": output_size})
                    idx += 1
            # 2) equal outputs carved from non-conforming inputs.
            for _ in range(fr.num_equal_outputs):
                if idx < len(addrs):
                    all_outputs.append({"address": addrs[idx], "amount": output_size})
                    idx += 1
            # 3) the above-dust leftover (num_change=1). If the participant
            #    supplied a spare address, it's their change. If not, it's
            #    donated: to DONATION_ADDRESS if configured, otherwise folded
            #    into the miner fee (output omitted). Sub-dust leftovers never
            #    reach here (num_change=0) — they always fold into the fee.
            if fr.num_change_outputs > 0 and fr.change_sats > 0:
                if idx < len(addrs):
                    all_outputs.append({"address": addrs[idx], "amount": fr.change_sats})
                    idx += 1
                elif donation_address:
                    all_outputs.append({"address": donation_address, "amount": fr.change_sats})
                    logger.info(
                        "Mix %s: participant %s donated %d sats (no change address)",
                        tokens.m(mix_id), tokens.p(rec["npub_hex"]), fr.change_sats,
                    )
                else:
                    # No donation address configured → the leftover stays in the
                    # tx as additional miner fee (output omitted).
                    folded_above_dust += fr.change_sats
                    logger.info(
                        "Mix %s: participant %s left %d above-dust sats to the "
                        "miner fee (no change address, no DONATION_ADDRESS)",
                        tokens.m(mix_id), tokens.p(rec["npub_hex"]), fr.change_sats,
                    )
            # Persist the final accounting for transparency / debugging.
            await self.db.update_participant(
                rec["pid"],
                fee_share=fr.fee_share_sats,
                change_amount=fr.change_sats,
            )

        # Store the fee rate as a float (not int()). A calm-mempool rate like
        # 1.5 sat/vB truncated to 1 would, on a crash-resume with the chain API
        # down, fall back below MIN_FEE_RATE_SATS and trip the pre-broadcast
        # invariant — cancelling the mix and costing users their refund keep for
        # a pure rounding bug. The column is REAL; SQLite stores the fraction.
        await self.db.update_mix(mix_id, fee_rate=round(float(fee_rate), 3))

        # S-E: defensive invariant. The PSBT we're about to send must pay
        # the miner more than the minimum relay fee. A future bug in the
        # fee math (wrong sign, off-by-one, etc.) could otherwise produce
        # a tx with sum(outputs) >= sum(inputs), and we'd push it to
        # broadcast where it would either silently fail or — worse —
        # actually relay at a negative effective fee depending on the
        # node's relay policy. Better to cancel here loudly than to ship
        # a bad tx.
        sum_inputs = sum(int(i["amount"]) for i in all_inputs)
        sum_outputs = sum(int(o["amount"]) for o in all_outputs)
        actual_miner_fee = sum_inputs - sum_outputs
        min_required_fee = int(total_vsize * self.cfg.MIN_FEE_RATE_SATS)
        if actual_miner_fee < min_required_fee:
            logger.error(
                "Mix %s: pre-broadcast sum invariant failed. "
                "sum_inputs=%d sum_outputs=%d miner_fee=%d min_required=%d "
                "(vsize=%d × MIN_FEE_RATE=%s). Cancelling rather than sending "
                "a tx that won't relay.",
                tokens.m(mix_id), sum_inputs, sum_outputs, actual_miner_fee,
                min_required_fee, total_vsize, self.cfg.MIN_FEE_RATE_SATS,
            )
            await self._cancel_and_refund(
                mix, "fee math produced an undercollecting tx",
            )
            return

        # Upper-bound guard (symmetric to the lower bound above). The actual
        # miner fee should be the engine's target plus only the sats we
        # KNOWINGLY folded: above-dust leftovers with nowhere to go
        # (folded_above_dust), and sub-dust leftovers (< MINIMUM_UTXO_SIZE each,
        # at most one per participant). A regression that silently dropped a
        # funded output would push actual_miner_fee far past this ceiling — far
        # better to cancel loudly than to burn a participant's output to miners.
        max_expected_fee = (
            total_miner_fee
            + folded_above_dust
            + len(participants_data) * self.cfg.MINIMUM_UTXO_SIZE
        )
        if actual_miner_fee > max_expected_fee:
            logger.error(
                "Mix %s: pre-broadcast fee ceiling exceeded. miner_fee=%d "
                "target=%d folded_above_dust=%d ceiling=%d — a funded output may "
                "have been dropped. Cancelling rather than burning coins.",
                tokens.m(mix_id), actual_miner_fee, total_miner_fee,
                folded_above_dust, max_expected_fee,
            )
            await self._cancel_and_refund(
                mix, "fee math produced an overcollecting tx",
            )
            return

        # Privacy: order inputs and outputs deterministically (alphabetically by
        # outpoint / address) so a participant's inputs and outputs aren't
        # grouped together by position on-chain — an observer can't read mix
        # membership off the transaction's ordering. Done AFTER the fee math so
        # amounts are final, and we re-derive each participant's signing indices
        # against the sorted input order (the skeleton everyone signs).
        all_inputs.sort(key=lambda x: (x["txid"], x["vout"]))
        all_outputs.sort(key=lambda o: (o["address"], o["amount"]))
        input_indices_by_pid = {}
        for idx, inp in enumerate(all_inputs):
            input_indices_by_pid.setdefault(inp["pid"], []).append(idx)

        # Build the PSBT
        psbt_hex = self.psbt_mgr.build_skeleton(all_inputs, all_outputs)
        if not psbt_hex:
            await self._cancel_and_refund(mix, "failed to build skeleton PSBT")
            return

        # Privacy check — the bar is >=2 equal-size outputs from >=2 inputs
        # (NC + conforming combined). Floor at max(2, required_nonconforming):
        # each NC participant contributes >=1 equal output, conforming UTXOs add
        # more, and the solo (required==1) case is still held to the >=2 minimum.
        # Non-authoritative; users seeking more anonymity re-mix in later rounds.
        required_nc = mix.get("required_nonconforming") or self.cfg.DEFAULT_REQUIRED_NONCONFORMING
        privacy_floor = max(2, required_nc)
        privacy_pass, privacy_msg = self.privacy.check_psbt(psbt_hex, privacy_floor)
        if not privacy_pass:
            logger.warning(f"Privacy check failed for {mix_id}: {privacy_msg}")
            # Continue anyway — the plan says non-authoritative

        # Record PSBT rounds for each participant. round_num tracks ghost
        # recovery passes; the schema's UNIQUE(mix_id, pid, round_num) means
        # a second pass with round_num=1 would collide.
        round_num = mix.get("ghost_retries", 0) + 1
        now_ts = int(time.time())
        # Per-participant accounting, keyed by pid, for the fee-disclosure DM.
        # fee_share_sats is each participant's personal slice of the miner fee
        # (their own NC portion + an even share of the conforming burden);
        # conforming-only participants are 0.
        fee_by_pid = {
            rec["pid"]: fr.fee_share_sats
            for rec, fr in zip(participants_data, fee_results)
        }
        for p in active:
            pid = p["id"]
            round_id = await self.db.add_psbt_round(mix_id, pid, round_num=round_num)
            await self.db.update_psbt_round(
                round_id,
                psbt_sent=psbt_hex,
                psbt_sent_at_unix=now_ts,
                input_indices=json.dumps(input_indices_by_pid.get(pid, [])),
            )

            # Tell the participant their personal miner-fee share before the
            # machine-readable PSBT, so they can see what they're paying (not
            # just the whole-tx fee). 0 sats for a conforming-only participant.
            share = fee_by_pid.get(pid, 0)
            await self.nostr.send_dm(
                p["npub_hex"],
                f"Your share of the miner fee: {share} sats "
                f"(~{share / 1e8:.8f} BTC). "
                + ("Conforming UTXOs pass through free." if share == 0
                   else "The PSBT to review and sign follows."),
            )

            # Send PSBT to participant
            # Check if chunking needed
            if self.psbt_mgr.needs_chunking(psbt_hex):
                chunks = self.psbt_mgr.chunk_psbt(psbt_hex)
                for idx, chunk in enumerate(chunks, 1):
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"/psbt_chunk {idx}/{len(chunks)} {chunk}",
                    )
            else:
                await self.nostr.send_dm(
                    p["npub_hex"],
                    f"/psbt_accept {psbt_hex}",
                )

            # Move participant to signing
            await self.db.update_participant(pid, state="signing", psbt_sent_at_unix=now_ts)

        # Move mix to signing
        await self.db.update_mix(mix_id, state="signing")

    async def _handle_signing(self, mix: Dict, active: List[Dict], now: int):
        """Handle signing phase — check deadlines, ghost detection."""
        mix_id = mix["id"]
        deadline_hours = self.cfg.SIGNING_DEADLINE_HOURS
        deadline_seconds = deadline_hours * 3600

        for p in active:
            if p["state"] not in ("signing", "signed", "ghosted"):
                continue

            psbt_sent = p.get("psbt_sent_at_unix", 0)
            time_since = now - psbt_sent

            if p["state"] == "signed":
                continue  # Already signed

            # Reminder progression: count is the number of DMs sent so far.
            # Time bands (low → high): deadline/8 → /4 → /2 → deadline.
            # Each band gates on the prior band's count so we only DM once.
            if time_since > deadline_seconds:
                # Ghosted — blacklist the npub AND each of their UTXOs by
                # txid:vout, so a ghoster can't re-commit the same UTXOs from
                # a fresh npub.
                await self.db.update_participant(p["id"], state="ghosted")
                # Keep the in-memory dict in sync: later filters (remaining,
                # ghost_participants) read p["state"] and would otherwise see
                # the pre-update value.
                p["state"] = "ghosted"
                await self.db.add_to_blacklist(p["npub_hex"], reason="ghosting")
                ghost_utxos = await self.db.get_utxos_by_participant(p["id"])
                for gu in ghost_utxos:
                    await self.db.add_to_blacklist(
                        p["npub_hex"],
                        utxo_txid_vout=f"{gu['txid']}:{gu['vout']}",
                        reason="ghosting",
                    )
                logger.info(
                    "Participant %s ghosted mix %s",
                    tokens.p(p["npub_hex"]), tokens.m(mix_id),
                )

            else:
                # S-D: compute expected reminder level from time_since alone,
                # not from prior reminder_count. The old code gated each
                # band on the previous band's count having advanced first;
                # if the bot was down across a band boundary the participant
                # could be ghosted with zero DMs sent. The fix: figure out
                # which level we OUGHT to be at given the time elapsed, and
                # if the participant's stored count is behind, fire the
                # highest-numbered DM we haven't fired yet (so they always
                # get at least one warning before the deadline).
                if time_since > deadline_seconds // 2:
                    expected_level = 3  # final warning
                elif time_since > deadline_seconds // 4:
                    expected_level = 2  # second reminder
                elif time_since > deadline_seconds // 8:
                    expected_level = 1  # first reminder
                else:
                    expected_level = 0

                current_level = p.get("reminder_count", 0) or 0
                if expected_level > current_level:
                    hours_remaining = max(0, int((deadline_seconds - time_since) / 3600))
                    if expected_level == 3:
                        text = (
                            f"FINAL WARNING: Sign the PSBT for {mix_id} within "
                            f"{hours_remaining} hours or lose your fee."
                        )
                    elif expected_level == 2:
                        text = (
                            f"Reminder: Sign the PSBT for {mix_id}. "
                            f"{hours_remaining} hours remaining."
                        )
                    else:  # 1
                        text = (
                            f"Reminder: Sign the PSBT for {mix_id}. You have "
                            f"{hours_remaining} hours from receipt."
                        )
                    await self.nostr.send_dm(p["npub_hex"], text)
                    await self.db.update_participant(p["id"], reminder_count=expected_level)

        # Decide ghost-recovery vs broadcast from the DATABASE, scoped to the
        # current assembled round — NOT from a tick-local flag. The active list
        # passed in by _process_mix has already filtered out 'ghosted' rows, so
        # a ghost persisted by an EARLIER tick that crashed (or whose recovery
        # DM raised) mid-recovery would be invisible to a tick-local check:
        # ghosted_any would read False, the cooperative signers would look
        # "all signed", and the mix would wrongly broadcast/cancel with the
        # ghost's input still unsigned. Re-reading from the DB makes recovery
        # resumable. Round membership is the psbt_rounds row for this round_num,
        # so a ghost from a PRIOR round (still 'ghosted' but reset out of this
        # round) doesn't re-trigger recovery. (HIGH ghost-recovery)
        round_num = mix.get("ghost_retries", 0) + 1
        all_parts = await self.db.get_participants_by_mix(mix_id)
        remaining = []        # current-round participants who can still sign
        round_ghosts = []     # current-round participants who ghosted
        for p in all_parts:
            if p["state"] in ("signing", "signed"):
                remaining.append(p)
            elif p["state"] == "ghosted":
                rnd = await self.db.get_psbt_round(mix_id, p["id"], round_num)
                if rnd is not None:
                    round_ghosts.append(p)
        ghosted_any = bool(round_ghosts)
        all_signed = bool(remaining) and all(p["state"] == "signed" for p in remaining)

        # Ghost recovery takes precedence over broadcast. The common ghost
        # pattern is "everyone cooperative signs early, one lets the deadline
        # lapse": the others are all 'signed', so all_signed would be true — but
        # the skeleton still has the ghost's input unsigned, so finalize would
        # fail and _combine_and_broadcast would cancel the whole mix instead of
        # restarting the round. Checking ghosted_any FIRST routes that case to
        # ghost recovery. (H2)
        if ghosted_any:
            ghost_retries = mix.get("ghost_retries", 0)
            max_ghost = mix.get("max_ghost_retries", self.cfg.MAX_GHOST_RETRIES)

            if ghost_retries >= max_ghost:
                await self._cancel_and_refund(mix, "max ghost retries exceeded")
            else:
                # Crash-safe ordering: do the idempotent cleanup (drop the
                # ghost's inputs/outputs, reset survivors to 'paid' with
                # addresses cleared) FIRST, then bump ghost_retries and flip to
                # 'collecting' in a SINGLE update as the very last step. If we
                # crash before that final update, the mix stays 'signing' with
                # the SAME round_num, so the ghost is still found next tick and
                # recovery re-runs harmlessly. Incrementing ghost_retries early
                # would change round_num and HIDE the ghost on resume — exactly
                # the bug we're fixing.
                for gp in round_ghosts:
                    await self.db.delete_utxos_by_participant(gp["id"])
                    await self.db.delete_outputs_by_participant(gp["id"])

                # Notify remaining and actually clear their addresses so the
                # ghost-warning DM ("we've thrown out your addresses") tells the
                # truth. Their UTXOs and service-fee payment are kept; 'paid'
                # with empty outputs is what _cmd_provide_addresses needs to
                # accept a re-submission without re-charging. The DM is guarded:
                # a relay hiccup must NOT abort recovery (that would strand the
                # mix mid-recovery and let the next tick wrongly cancel it).
                for p in remaining:
                    await self.db.delete_outputs_by_participant(p["id"])
                    await self.db.update_participant(p["id"], state="paid", reminder_count=0)
                    try:
                        await self.nostr.send_dm(
                            p["npub_hex"], self.parser.format_ghost_warning(mix_id),
                        )
                    except Exception:
                        logger.warning(
                            "Mix %s: ghost-warning DM to participant %s failed "
                            "(continuing recovery)",
                            tokens.m(mix_id), tokens.p(p["npub_hex"]),
                        )

                # Final, single atomic step: bump retries, return to collecting,
                # extend the deadline so survivors have time to re-submit.
                new_deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
                await self.db.update_mix(
                    mix_id, ghost_retries=ghost_retries + 1,
                    state="collecting", deadline_unix=new_deadline,
                )

        elif all_signed:
            await self._combine_and_broadcast(mix, remaining)

    async def _combine_and_broadcast(self, mix: Dict, signed: List[Dict]):
        """Combine all signed PSBTs and broadcast."""
        mix_id = mix["id"]
        round_num = mix.get("ghost_retries", 0) + 1

        # Collect signed PSBTs from the active round. Guard against a STALE
        # return poisoning the combine: a returned PSBT must have the same
        # unsigned-tx txid as this round's skeleton. A crash between a fast
        # signer's per-participant update and the round-level flip could leave a
        # signature made against an OLD skeleton; merging it would corrupt every
        # combine attempt. We compare each return's unsigned txid against the
        # skeleton we actually sent for this round. (stale-sig)
        psbt_hexes = []
        skeleton_txid: Optional[str] = None
        stale = 0
        for p in signed:
            rounds = await self.db.get_psbt_round(mix_id, p["id"], round_num)
            if not rounds or not rounds.get("psbt_returned"):
                continue
            if skeleton_txid is None and rounds.get("psbt_sent"):
                skeleton_txid = self.psbt_mgr.unsigned_txid(rounds["psbt_sent"])
            ret_txid = self.psbt_mgr.unsigned_txid(rounds["psbt_returned"])
            if skeleton_txid is not None and ret_txid != skeleton_txid:
                stale += 1
                logger.warning(
                    "Mix %s: discarding a returned PSBT whose unsigned tx "
                    "doesn't match this round's skeleton (stale signature).",
                    tokens.m(mix_id),
                )
                continue
            psbt_hexes.append(rounds["psbt_returned"])

        if len(psbt_hexes) < 2:
            # If we dropped stale returns, this isn't a real "everyone signed"
            # state — let recovery/the deadline handle it rather than broadcast
            # a half-signed tx.
            await self._cancel_and_refund(mix, "not enough signed PSBTs")
            return

        # Combine
        combined_hex = self.psbt_mgr.combine_psbts(psbt_hexes)
        if not combined_hex:
            await self._cancel_and_refund(mix, "failed to combine PSBTs")
            return

        # Finalize
        raw_tx_hex = self.psbt_mgr.finalize(combined_hex)
        if not raw_tx_hex:
            await self._cancel_and_refund(mix, "failed to finalize transaction")
            return

        # Broadcast. Save the raw hex alongside the txid so _broadcast_sweep
        # can re-push the tx if it falls out of the mempool before confirming.
        txid = await self.chain.broadcast_tx(raw_tx_hex)
        if txid:
            await self.db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid=txid, broadcast_tx_hex=raw_tx_hex,
            )
            # Notify participants
            for p in signed:
                await self.nostr.send_dm(p["npub_hex"], f"Transaction broadcast: {txid}")
            return

        # C-D: broadcast_tx returned None. That can mean (a) genuine
        # rejection, OR (b) all endpoints had transient failures while
        # the tx may actually have entered the mempool. Compute the local
        # txid from our raw hex and ask the chain whether anyone has seen
        # it. Refunding without this check would double-pay anyone whose
        # on-chain output later confirms.
        local_txid: Optional[str] = None
        try:
            from bitcointx.core import CTransaction, b2x as _b2x
            local_txid = _b2x(
                CTransaction.deserialize(bytes.fromhex(raw_tx_hex)).GetTxid()[::-1]
            )
        except Exception:
            local_txid = None

        known: Optional[bool] = None
        if local_txid:
            try:
                known = await self.chain.tx_known(local_txid)
            except Exception as e:
                logger.warning(
                    "tx_known check failed for mix %s: %s",
                    tokens.m(mix_id), type(e).__name__,
                )
                known = None

        if known is True:
            # Tx is out there. Park the mix in broadcast state and let
            # _broadcast_sweep take it from here. Don't refund.
            logger.warning(
                "Mix %s: broadcast_tx returned None but tx is known on chain "
                "(%s) — parking in broadcast state instead of refunding.",
                tokens.m(mix_id), tokens.tx(local_txid),
            )
            await self.db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid=local_txid, broadcast_tx_hex=raw_tx_hex,
            )
            for p in signed:
                await self.nostr.send_dm(
                    p["npub_hex"],
                    f"Transaction broadcast (confirmed via fallback check): {local_txid}",
                )
            return

        if known is None:
            # We couldn't reach any chain endpoint. Don't refund — that
            # might double-pay if the tx actually got out. Park in
            # broadcast state; the sweep will re-check later.
            logger.error(
                "Mix %s: broadcast_tx returned None AND chain endpoints "
                "unreachable — cannot tell whether tx (%s) is in mempool. "
                "Parking in broadcast state; the sweep will recheck.",
                tokens.m(mix_id), tokens.tx(local_txid) if local_txid else "<unparseable>",
            )
            if local_txid:
                await self.db.update_mix(
                    mix_id, state="broadcast",
                    broadcast_txid=local_txid, broadcast_tx_hex=raw_tx_hex,
                )
                for p in signed:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Broadcast uncertain (chain unreachable). We will "
                        f"keep retrying. Reference: {local_txid}",
                    )
            else:
                # We can't even compute a local txid — give up safely.
                await self._cancel_and_refund(
                    mix, "broadcast failed (and tx hex unparseable)",
                )
            return

        # known is False — every endpoint that answered said the tx is
        # nowhere to be found. Genuine broadcast failure; safe to refund.
        await self._cancel_and_refund(mix, "broadcast failed")

    async def _tx_double_spent(self, raw_tx_hex: str, txid: str) -> Optional[bool]:
        """Has this broadcast tx been replaced out by a conflicting spend?

        Returns True only when (a) the tx is NOT known to any chain endpoint
        (not in mempool, not confirmed) AND (b) at least one of its inputs is
        now spent — which, since our tx isn't the spender, means a DIFFERENT tx
        took that outpoint. That coinjoin can never confirm, so the sweep must
        stop re-broadcasting it forever. Returns False when the tx is still
        around or its inputs are free (a normal "fell out of mempool, re-push"
        case), and None when we can't tell (chain unreachable / unparseable) —
        the caller treats None as "keep trying", never as a conflict.
        """
        try:
            known = await self.chain.tx_known(txid)
        except Exception:
            known = None
        if known is None:
            return None          # can't reach the chain — inconclusive
        if known:
            return False         # still in mempool or confirmed — not conflicted
        # Our tx is nowhere on-chain. Probe its inputs for a conflicting spend.
        try:
            from bitcointx.core import CTransaction, b2x as _b2x
            tx = CTransaction.deserialize(bytes.fromhex(raw_tx_hex))
            outpoints = [
                (_b2x(vin.prevout.hash[::-1]), vin.prevout.n) for vin in tx.vin
            ]
        except Exception:
            return None
        for in_txid, vout in outpoints:
            spent = await self.chain.is_utxo_spent(in_txid, vout)
            if spent is True:
                return True      # a different tx spent our input → conflict
        return False             # inputs free (or unreachable) — keep retrying

    async def _broadcast_sweep(self, now: float):
        """Sweep all broadcast-pending mixes and check confirmation.

        Runs on an N-hour interval (BROADCAST_CHECK_INTERVAL_HOURS from env)
        rather than polling every 60s. Tracks last-check timestamp in the
        settings table so you can force a manual check with:
          sqlite3 bot.db "UPDATE settings SET value='0' WHERE key='last_broadcast_check_unix'"
        """
        interval_hours = self.cfg.BROADCAST_CHECK_INTERVAL_HOURS
        interval_seconds = interval_hours * 3600

        raw = await self.db.get_setting("last_broadcast_check_unix", "0")
        last_check_str = raw if raw is not None else "0"
        try:
            last_check = int(last_check_str)
        except (ValueError, TypeError):
            last_check = 0

        if now - last_check < interval_seconds:
            return  # Not yet time for the next sweep

        # Mark sweep as done (even if it fails — retry next interval)
        await self.db.set_setting("last_broadcast_check_unix", str(int(now)))

        # Find all mixes in broadcast state
        broadcast_mixes = await self.db.get_mixes_by_state("broadcast")
        if not broadcast_mixes:
            return

        for mix in broadcast_mixes:
            mix_id = mix["id"]
            txid = mix.get("broadcast_txid")
            if not txid:
                await self.db.update_mix(mix_id, state="cancelled")
                continue

            try:
                confirmed = await self.chain.is_confirmed(txid)
            except Exception:
                confirmed = False

            if not confirmed:
                # Re-push the tx in case it fell out of mempool. Cheap to do
                # on the sweep cadence; harmless if the tx is still in mempool.
                raw_tx_hex = mix.get("broadcast_tx_hex")
                # Privacy: NEVER log mix_id and txid together. The txid
                # is public on-chain, the mix_id is internal — joining
                # them in a log file lets anyone reconstruct mix
                # membership from the public coinjoin. Use the mix
                # token only; surface the txid in a separate line.
                mtoken = tokens.m(mix_id)
                if raw_tx_hex:
                    # Before re-pushing, check whether the tx was double-spent
                    # out from under us. If a conflicting tx took one of our
                    # inputs, ours can NEVER confirm — stop the infinite
                    # re-broadcast loop (which also retains all participant data
                    # forever) and tear the mix down, refunding service fees.
                    conflicted = await self._tx_double_spent(raw_tx_hex, txid)
                    if conflicted:
                        logger.warning(
                            "Mix %s: broadcast tx conflicted — an input was "
                            "double-spent by another tx that won. Cancelling "
                            "(cannot confirm).", mtoken,
                        )
                        await self._cancel_and_refund(
                            mix,
                            "broadcast transaction was double-spent (a conflicting "
                            "transaction confirmed first)",
                        )
                        continue
                    rebroadcast = await self.chain.re_broadcast(raw_tx_hex)
                    if rebroadcast:
                        logger.info("Mix %s: re-broadcast attempted; next check in %dh", mtoken, interval_hours)
                    else:
                        logger.warning("Mix %s: re-broadcast failed", mtoken)
                else:
                    logger.info("Mix %s: broadcast not yet confirmed; no raw hex saved, cannot re-broadcast", mtoken)
                continue

            # Confirmed! Notify remaining participants, then destroy all trace.
            participants = await self.db.get_participants_by_mix(mix_id)
            for p in participants:
                if p["state"] in ("signed", "broadcast"):
                    try:
                        await self.nostr.send_dm(
                            p["npub_hex"],
                            f"Mix {mix_id} confirmed on-chain: {txid}",
                        )
                    except Exception:
                        pass

            await self._destroy_mix(mix_id, "completed")
            # Privacy: mix token only — pairing it with the public txid
            # would let an observer map every participant in the local
            # logs onto the on-chain coinjoin.
            logger.info("Mix %s confirmed and all data destroyed", tokens.m(mix_id))

    # --- Announcement Scheduler ---

    def _seconds_until_next_announcement(self) -> float:
        """Wall-clock seconds until the next ANNOUNCEMENT_HOUR_UTC boundary.

        Always returns > 0 — if we're already past today's hour we roll to
        tomorrow. Pulled out for testability.
        """
        now = dt.datetime.now(dt.timezone.utc)
        target_hour = max(0, min(23, self.cfg.ANNOUNCEMENT_HOUR_UTC))
        target = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target = target + dt.timedelta(days=1)
        return (target - now).total_seconds()

    async def _announcement_task_loop(self):
        """Post one announcement per UTC day at ANNOUNCEMENT_HOUR_UTC.

        Replaces the older naive `sleep(86400)` loop which drifted with
        bot uptime — announcements would fire at whatever hour the bot
        happened to start.
        """
        while self._running:
            wait_s = self._seconds_until_next_announcement()
            try:
                await asyncio.sleep(wait_s)
            except asyncio.CancelledError:
                return
            if self._running:
                try:
                    await self._post_daily_announcement()
                except Exception as e:
                    logger.error("Daily announcement failed: %s", type(e).__name__)

    async def _post_daily_announcement(self):
        """Post a daily announcement of open mixes."""
        active = await self.db.get_active_mixes()
        available = [m for m in active if m["state"] in ("announced", "collecting")]

        if not available:
            # No open mixes — create one using defaults (shared helper).
            mid = await self._create_default_mix()
            msg = self.parser.format_list_response([{"id": mid, "output_size": self.cfg.DEFAULT_OUTPUT_SIZE, "state": "collecting"}])

        else:
            msg = self.parser.format_list_response(available)

        full_text = f"🌟 Open Mixes:\n\n{msg}\n\nUse /join <mix_name> to participate."
        event_id = await self.nostr.post_announcement(full_text)

        # If we just auto-created a mix above, `available` was empty when we
        # snapshotted it — make sure we record an announcement row for it too,
        # otherwise the audit trail loses the very first announcement.
        recorded_mixes = list(available)
        if not recorded_mixes:
            fresh = await self.db.get_mixes_by_state("collecting")
            recorded_mixes = fresh

        for m in recorded_mixes:
            await self.db.add_announcement(m["id"], event_id)

    # --- Cancel and Refund ---

    async def _destroy_mix(self, mix_id: str, reason: str):
        """Wipe every trace of a terminal mix (confirmed OR failed), preserving
        ONLY a minimal debt record for any participant whose service-fee refund
        the Lightning backend rejected. Used by both terminal paths so no
        failed-refund debt is lost when the mix is destroyed.

        Idempotent: add_refund_owed is INSERT OR IGNORE (keyed on the opaque
        participant id) and destroy_mix_data on an already-gone mix is a no-op,
        so a crash-resume that re-enters here does no harm.
        """
        participants = await self.db.get_participants_by_mix(mix_id)
        for p in participants:
            state = p.get("state")
            if state == "refund_failed":
                lud16 = (p.get("lightning_addr") or "").strip()
                fee_paid = int(p.get("fee_paid") or 0)
                owed = self._refund_keep_math(fee_paid) if fee_paid > 0 else 0
                if lud16 and owed > 0:
                    await self.db.add_refund_owed(p["id"], lud16, owed, reason)
                else:
                    # No address (or nothing owed) — we can't record a payable
                    # debt. Log it (tokenised) before the row is destroyed so the
                    # operator at least knows a reconciliation is outstanding.
                    logger.error(
                        "Mix %s: participant %s was refund_failed but has no "
                        "Lightning address to owe to (fee_paid=%d); cannot record debt",
                        tokens.m(mix_id), tokens.p(p["npub_hex"]), fee_paid,
                    )
            elif state == "refunding":
                # In-flight refund whose outcome we never confirmed (a crash
                # mid-payout). It MIGHT have been paid, so we can't safely record
                # it as owed (double-pay risk). Log loudly before destroying.
                logger.error(
                    "Mix %s: participant %s left in 'refunding' (in-flight refund, "
                    "fee_paid=%d) when destroyed — operator must verify whether the "
                    "Lightning payout settled",
                    tokens.m(mix_id), tokens.p(p["npub_hex"]),
                    int(p.get("fee_paid") or 0),
                )
        await self.db.destroy_mix_data(mix_id)

    async def _cancel_and_refund(self, mix: Dict, reason: str):
        """Cancel a mix and refund all non-blacklisted participants.

        Idempotent: refund decisions for each participant go through
        _safe_refund, which is no-op on participants already in
        _REFUND_TERMINAL_STATES. This is the C-B crash-recovery defence —
        if we crash mid-loop, the next tick re-enters and only the
        participants we hadn't processed yet are touched.
        """
        mix_id = mix["id"]
        participants = await self.db.get_participants_by_mix(mix_id)

        for p in participants:
            if p["state"] in self._REFUND_TERMINAL_STATES:
                continue

            fee_paid = int(p.get("fee_paid") or 0)
            lud16 = p.get("lightning_addr") or ""

            if fee_paid > 0 and lud16:
                refund_sats = self._refund_keep_math(fee_paid)
                new_state = await self._safe_refund(p, mix_id, refund_sats, reason=reason)
                if new_state == "refunded":
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Mix {mix_id} cancelled ({reason}). Refunded {refund_sats} sats.",
                    )
                else:  # refund_failed
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Mix {mix_id} cancelled ({reason}). We tried to refund "
                        f"{refund_sats} sats but our Lightning backend rejected it — "
                        f"please contact the operator.",
                    )
            elif fee_paid > 0:
                # Paid the service fee but we have no lud16 to refund to.
                logger.error(
                    "Cannot refund participant %s for mix %s: fee_paid=%d but "
                    "no lightning_addr on record. Sats are stranded; operator "
                    "must reconcile manually.",
                    tokens.p(p["npub_hex"]), tokens.m(mix_id), fee_paid,
                )
                await self.db.update_participant(p["id"], state="cancelled")
                await self.nostr.send_dm(
                    p["npub_hex"],
                    f"Mix {mix_id} cancelled: {reason}. We can't refund "
                    f"automatically because we don't have a Lightning address "
                    f"for you. Please contact the operator to reclaim your "
                    f"{fee_paid} sats.",
                )
            else:
                await self.db.update_participant(p["id"], state="cancelled")
                await self.nostr.send_dm(p["npub_hex"], f"Mix {mix_id} cancelled: {reason}.")

        # Destroy ALL mix data (the requirement: leave no trace once a mix is
        # confirmed OR failed). This subsumes the old scrub-and-keep — utxos,
        # outputs, psbt rounds, participant identifiers, and the mix row all go.
        # The only thing preserved is a minimal debt for any refund_failed
        # participant (recorded inside _destroy_mix). Blacklist entries for
        # ghosters live in a separate table and are untouched.
        await self._destroy_mix(mix_id, f"cancelled: {reason}")

    # --- Lifecycle ---

    async def _on_nostr_ready(self, nostr_handler: NostrHandler):
        """Called when NostrHandler has started and keys are available.

        Initializes the LnurlPayer with the bot's Nostr keys so refunds work.
        """
        keys = nostr_handler.keys
        if keys:
            await self.lightning.init_payer_with_keys(keys)
            # The bot's own pubkey is a public identity — not a privacy
            # concern. The operator needs to know which key the running
            # bot is signing as.
            logger.info(
                "LNURL payer initialized with bot keys: %s",
                keys.public_key().to_bech32(),
            )

    async def start(self):
        """Start the coordinator."""
        self._running = True

        # Resume unfinished work (crash recovery)
        unfinished = await self.db.resume_unfinished()
        logger.info(f"Resuming {len(unfinished)} unfinished mixes")

        # C-B: surface participants stuck in 'refunding' across a restart.
        # That state means we set the intent before the LN call but never
        # observed its completion — either the LN backend went down, the
        # bot crashed mid-call, or the SDK ate the response. Operator must
        # verify with the wallet whether the payout actually left.
        stuck = await self.db.participants_in_state("refunding")
        if stuck:
            logger.error(
                "Found %d participant(s) stuck in 'refunding' at startup — "
                "operator must verify each LN payout manually before forcing "
                "them to 'refunded' or 'refund_failed'.",
                len(stuck),
            )
            for p in stuck:
                logger.error(
                    "  stuck refund: participant=%s mix=%s fee_paid=%s",
                    tokens.p(p.get("npub_hex", "")), tokens.m(p.get("mix_id", "")),
                    p.get("fee_paid"),
                )

        # Start event loop
        self._event_loop_task = asyncio.create_task(self.run_event_loop())
        # Start announcement scheduler
        self._announcement_task = asyncio.create_task(self._announcement_task_loop())

        # Start Nostr handler
        await self.nostr.start()

    async def run_forever(self):
        """Run the coordinator until interrupted."""
        # Start before running
        await self.start()
        # The nostr handler's run_forever blocks until a shutdown signal. It
        # does NOT re-start the bot (start() already did). When it returns,
        # tear everything down cleanly — cancel the event-loop / announcement
        # tasks, stop the bot, close the chain client and DB.
        try:
            await self.nostr.run_forever()
        finally:
            await self.stop()

    async def stop(self):
        """Graceful shutdown."""
        self._running = False
        if self._event_loop_task:
            self._event_loop_task.cancel()
        if self._announcement_task:
            self._announcement_task.cancel()
        await self.nostr.stop()
        # Close async HTTP client — prevents hanging connections on exit
        if self.chain:
            await self.chain.close()
        await self.db.close()
