"""Mixing Coordinator — state machines, event loop, tie everything together."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import time
import logging
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

                case "join_mix":
                    mix_id = parsed.args[0] if parsed.args else None
                    # num_outputs from parsed.args[1] if provided
                    await self._cmd_join_mix(ctx, mix_id)

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
                    await self.nostr.send_dm(npub_hex, "Unknown command. Try: /list, /join <mix_id>, /commit, /addresses, /psbt_accept, /cancel")
        except Exception as e:
            # exc_info would dump frame locals (UTXOs, addresses, PSBT hex)
            # into the log. Just record the participant token + command
            # verb + exception class — enough to triage, nothing to leak.
            logger.error(
                "DM handler error: participant=%s command=%s err=%s",
                tokens.p(npub_hex), parsed.command, type(e).__name__,
            )
            try:
                # The DM back to the user can carry the exception text
                # safely — it goes only to them. (We could trim if we
                # ever logged DM contents, but we don't.)
                await self.nostr.send_dm(npub_hex, f"Error processing your message: {str(e)}")
            except Exception:
                pass

    # --- Command Implementations ---

    async def _cmd_list_mixes(self, ctx: SenderContext):
        """Handle /list — show open mixes."""
        active = await self.db.get_active_mixes()
        # Filter to only collecting/announced
        available = [m for m in active if m["state"] in ("announced", "collecting")]
        msg = self.parser.format_list_response(available)
        await self.nostr.send_dm(ctx.sender_hex, msg)

    async def _cmd_join_mix(self, ctx: SenderContext, mix_id: Optional[str]):
        """Handle /join <mix_id> [num_outputs]."""
        npub_hex = ctx.sender_hex

        if not mix_id:
            await self.nostr.send_dm(npub_hex, "Usage: /join <mix_name>")
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

        # Verify the mix exists and is in collecting state
        mix = await self.db.get_mix(mix_id)
        if not mix:
            await self.nostr.send_dm(npub_hex, f"No mix named '{mix_id}' found.")
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

        # Reply asking for UTXOs and addresses
        await self.nostr.send_dm(
            npub_hex,
            f"Registered interest in {mix_id}.\n"
            f"Send me txid(s) and vout(s) and your output addresses:\n"
            f"/commit <txid:vout> ...\n"
            f"/addresses <addr1> <addr2> ..."
        )

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
                m["id"], exclude_states=["cancelled", "ghosted"],
            )
            if cnt >= cap:
                continue
            return m["id"]
        # None compatible — spin up a fresh DEFAULT_MIX_USER_COUNT mix.
        deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
        mid = await self.db.create_mix(
            output_size=self.cfg.DEFAULT_OUTPUT_SIZE,
            min_participants=self.cfg.DEFAULT_MIX_USER_COUNT,
            max_participants=self.cfg.MAX_PARTICIPANTS_DEFAULT,
            fee_per_element=self.cfg.FEE_PER_ELEMENT,
            deadline_unix=deadline,
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

        # Validate UTXOs on chain
        total_sats = 0
        valid_utxos = []
        for utxo_data in utxos:
            txid = utxo_data["txid"]
            vout = utxo_data["vout"]

            # Check blacklist
            if await self.db.is_blacklisted(npub_hex, f"{txid}:{vout}"):
                await self.nostr.send_dm(npub_hex, f"UTXO {txid}:{vout} is blacklisted.")
                continue

            # Check double-spend
            if await self.db.is_utxo_used(txid, vout):
                await self.nostr.send_dm(npub_hex, f"UTXO {txid}:{vout} already used in another mix.")
                continue

            # Look up on-chain
            txout = await self.chain.lookup_txout(txid, vout)
            if txout is None:
                await self.nostr.send_dm(npub_hex, f"Could not find UTXO {txid}:{vout} on chain.")
                continue

            # Reject UTXOs that have already been spent on-chain. Without this
            # check a participant could commit a stale output, the bot would
            # build a PSBT against it, and broadcast would fail late.
            if await self.chain.is_utxo_spent(txid, vout):
                await self.nostr.send_dm(npub_hex, f"UTXO {txid}:{vout} has already been spent on-chain.")
                continue

            amount = txout.get("value", 0)
            script_type = txout.get("scriptpubkey_type", "p2wpkh")
            scriptpubkey = txout.get("scriptpubkey", "")

            # Operator allowlist for input types. Anything off the list gets a
            # polite reject. Underlying vsize tables still support the rejected
            # types — only the policy gate is closed.
            if script_type not in self.cfg.ACCEPTED_INPUT_TYPES:
                accepted = ", ".join(sorted(self.cfg.ACCEPTED_INPUT_TYPES))
                await self.nostr.send_dm(
                    npub_hex,
                    f"UTXO {txid}:{vout} is {script_type}; we only accept {accepted} inputs right now.",
                )
                continue

            # Per-mix type lock: first valid UTXO sets the lock; later UTXOs
            # in this commit (and from other participants) must match it.
            if candidate_lock_type is None:
                candidate_lock_type = script_type
            elif script_type != candidate_lock_type:
                await self.nostr.send_dm(
                    npub_hex,
                    f"UTXO {txid}:{vout} is {script_type}, but this mix is locked to "
                    f"{candidate_lock_type}. Send only {candidate_lock_type} UTXOs.",
                )
                continue

            # Reject dust below MINIMUM_UTXO_SIZE — these can't realistically
            # carry an equal output through mixing and only inflate vsize.
            if amount < self.cfg.MINIMUM_UTXO_SIZE:
                await self.nostr.send_dm(
                    npub_hex,
                    f"UTXO {txid}:{vout} is {amount} sats, below the {self.cfg.MINIMUM_UTXO_SIZE}-sat minimum.",
                )
                continue

            # Add UTXO to database — include the actual prevout script hex
            # so build_skeleton can create a valid CTxOut for the PSBT input.
            # The schema's UNIQUE(txid, vout) is the defense of last resort
            # against the race window between is_utxo_used (which we checked
            # above) and this insert. A parallel /commit handler could have
            # claimed the same outpoint while we were awaiting chain calls;
            # SQLite raises IntegrityError, we DM the user and move on.
            import sqlite3
            try:
                await self.db.add_utxo(pid, txid, vout, amount, script_type, scriptpubkey)
            except sqlite3.IntegrityError:
                await self.nostr.send_dm(
                    npub_hex,
                    f"UTXO {txid}:{vout} was claimed by another commit while we "
                    f"were processing yours. Please retry.",
                )
                continue
            # Reserve the UTXO so a concurrent commit (from a second mix or
            # a re-/commit) can't claim the same outpoint.
            await self.db.mark_utxo_used(pid, txid, vout)
            valid_utxos.append({"txid": txid, "vout": vout, "amount": amount, "script_type": script_type, "scriptpubkey": scriptpubkey})
            total_sats += amount

        if not valid_utxos:
            await self.nostr.send_dm(npub_hex, "No valid UTXOs registered.")
            return

        # Persist the mix-level input type lock if this commit set it.
        if locked_input_type is None and candidate_lock_type is not None:
            await self.db.update_mix(mix_id, input_type=candidate_lock_type)

        # Update participant state
        await self.db.update_participant(pid, state="committed")

        # Tell them we need addresses now
        await self.nostr.send_dm(
            npub_hex,
            f"{len(valid_utxos)} UTXO(s) registered, total {total_sats / 1e8:.4f} BTC.\n"
            f"Provide {len(valid_utxos) + 1}+ output addresses with /addresses <addr1> <addr2> ..."
        )

    async def _cmd_provide_addresses(self, ctx: SenderContext, npub_hex: str, addrs: List[str]):
        """Handle /addresses <addr> ... — register output addresses."""
        if len(addrs) < 2:
            await self.nostr.send_dm(npub_hex, "You need to send us at least 2 addresses.")
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

        # Get participant's UTXOs to calculate fee
        utxos = await self.db.get_utxos_by_participant(pid)
        num_inputs = len(utxos)
        total_sats = sum(u["amount"] for u in utxos)

        output_size = mix["output_size"]
        fee_per_element = mix["fee_per_element"]

        # Calculate service fee using the FeeEngine
        service_fee = self.fee_engine.calculate_service_fee(num_inputs, len(addrs))

        # Determine outputs
        num_equal, num_change, eq_amt, chg_amt = self.fee_engine.determine_outputs(
            total_sats, output_size, len(addrs),
            0,  # estimated miner fee (to be finalized later)
            service_fee,
        )

        num_used_outputs = num_equal + num_change

        if num_used_outputs == 0:
            await self.nostr.send_dm(npub_hex, "Your inputs are insufficient for even one output.")
            return

        # Store addresses. If this is a ghost-recovery resubmission, clear the
        # stale outputs first so we don't double-count.
        if already_paid:
            await self.db.delete_outputs_by_participant(pid)

        # Save them in order: equal outputs use first num_equal addresses, then
        # one change output at index num_equal (if change is large enough).
        for i, addr in enumerate(addrs):
            amount = output_size if i < num_equal else (chg_amt if i == num_equal else 0)
            if amount > 0:
                await self.db.add_output(pid, addr, amount, is_change=(i >= num_equal))

        # Calculate final service fee
        final_service_fee = self.fee_engine.calculate_service_fee(num_inputs, num_used_outputs)

        if already_paid:
            # No zap prompt — they've already paid in a prior round.
            await self.nostr.send_dm(
                npub_hex,
                f"{num_equal} outputs @ {eq_amt / 1e8:.4f} BTC each."
                + (f" + {chg_amt / 1e8:.4f} BTC change." if num_change and chg_amt > 0 else "")
                + "\nYou're already paid up; waiting for the mix to refill."
            )
        else:
            await self.nostr.send_dm(
                npub_hex,
                f"{num_equal} outputs @ {eq_amt / 1e8:.4f} BTC each."
                + (f" + {chg_amt / 1e8:.4f} BTC change." if num_change and chg_amt > 0 else "")
                + f"\nPay {final_service_fee} sats (service fee) via zap to {self.cfg.BOT_LUD16}."
            )

        # Preserve 'paid' across resubmission; only move 'committed' rows forward.
        new_state = "paid" if already_paid else "committed"
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
        active = [p for p in participants if p["state"] not in ("cancelled", "ghosted", "completed")]

        if not active:
            await self.nostr.send_dm(npub_hex, "Done.")
            return

        if len(active) == 1:
            pid = active[0]["id"]
            actual_mix_id = active[0]["mix_id"]
            mix = await self.db.get_mix(actual_mix_id)
        elif mix_id:
            # Find matching mix
            for p in active:
                m = await self.db.get_mix(p["mix_id"])
                if m and m["id"].lower() == mix_id.lower():
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

        # Refund fee
        fee_paid = active[0].get("fee_paid", 0)
        if fee_paid > 0:
            refund_sats = max(fee_paid * (100 - self.cfg.REFUND_KEEP_PERCENT) // 100,
                              fee_paid - self.cfg.REFUND_KEEP_MIN_SATS)
            await self.lightning.send_refund(
                active[0].get("lightning_addr", ""),
                refund_sats,
                reason="voluntary_exit",
            )
            msg = self.parser.format_refund(refund_sats, "voluntary exit")
        else:
            msg = "Sorry to see you go."

        await self.nostr.send_dm(npub_hex, msg)

        # Remove participant from mix. Also release their UTXOs back to
        # the outpoint pool so they can re-commit them to a future mix —
        # UNIQUE(txid, vout) would otherwise block the same outpoint
        # forever.
        await self.db.delete_utxos_by_participant(pid)
        await self.db.delete_outputs_by_participant(pid)
        await self.db.update_participant(pid, state="cancelled")

    # --- Zap Handler ---

    async def _on_zap(self, zap: ValidatedZap, ctx: SenderContext):
        """Handle a zap receipt — match sender npub + amount to pending participant."""
        npub_hex = zap.sender_hex
        amount_sats = zap.amount_sats

        # Find participants waiting for payment
        participants = await self.db.get_participants_by_npub(npub_hex)
        awaiting = [p for p in participants if p["state"] == "committed"]

        if not awaiting:
            # No pending fee — could be a donation, a late zap after we already
            # marked the participant paid, or a zap from an npub we've never
            # met. Log so the operator can audit the bot's Lightning inflows
            # against expected service fees. Sender becomes an opaque token
            # so the log doesn't link an npub to a payment trail.
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

        # Calculate expected fee
        utxos = await self.db.get_utxos_by_participant(pid)
        outputs = await self.db.get_outputs_by_participant(pid)
        num_inputs = len(utxos)

        # Count used outputs (those with positive amount)
        num_used = sum(1 for o in outputs if o["amount"] > 0)

        expected_fee = self.fee_engine.calculate_service_fee(num_inputs, num_used)

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

            # Check if mix is now full
            count = await self.db.count_participants_by_mix(mix_id, exclude_states=["cancelled", "ghosted"])
            max_part = mix.get("max_participants") or self.cfg.MAX_PARTICIPANTS_DEFAULT
            min_part = mix.get("min_participants", self.cfg.MIN_PARTICIPANTS_DEFAULT)

            if count >= max_part or count >= min_part:
                # Proceed to assembly if already enough
                pass  # coordinator will handle in event loop
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
        active = [p for p in participants if p["state"] not in ("cancelled", "ghosted", "completed")]

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
                active = [p for p in active if p["state"] not in ("cancelled", "ghosted", "completed")]

                # Mix-level deadline
                deadline = mix.get("deadline_unix")
                if deadline and now >= deadline:
                    # Only paid participants count toward the proceed-or-cancel decision.
                    paid = [p for p in active if p["state"] == "paid"]
                    if len(paid) < 2:
                        await self._cancel_and_refund(mix, "not enough participants")
                    elif len(paid) < mix.get("min_participants", self.cfg.MIN_PARTICIPANTS_DEFAULT):
                        await self._cancel_and_refund(mix, "not enough participants")
                    else:
                        await self._proceed_to_assembling(mix, paid)

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

    async def _proceed_to_assembling(self, mix: Dict, active: List[Dict]):
        """Move mix from collecting to assembling."""
        mix_id = mix["id"]
        await self.db.update_mix(mix_id, state="assembling")

        # Notify participants
        for p in active:
            await self.nostr.send_dm(
                p["npub_hex"],
                self.parser.format_psbt_request(mix_id, self.cfg.SIGNING_DEADLINE_HOURS),
            )

    async def _gather_assembly_data(self, active: List[Dict]) -> Tuple[
            List[Dict], List[Dict], Dict[str, List[str]], Dict[str, List[int]]]:
        """Build the four parallel structures _assemble_psbt needs:

        - all_inputs: positional list fed to build_skeleton
        - participants_data: list fed to fee_engine.calculate_all_fees
        - addrs_by_pid: each participant's addresses (used to lay out outputs)
        - input_indices_by_pid: which vin indices each participant must sign
          (used by /psbt_accept's strict per-input check)

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
                })
            input_indices_by_pid[pid] = list(range(start_idx, start_idx + len(utxos)))

            ibt: Dict[str, int] = {}
            for u in utxos:
                st = u.get("script_type", "p2wpkh")
                ibt[st] = ibt.get(st, 0) + 1

            obt: Dict[str, int] = {}
            for addr in addrs_in_order:
                addr_type = self.psbt_mgr._address_type(addr)
                obt[addr_type] = obt.get(addr_type, 0) + 1

            participants_data.append({
                "pid": pid,
                "npub_hex": p["npub_hex"],
                "num_inputs": len(utxos),
                "total_sats": total_sats,
                "num_addresses": len(addrs_in_order),
                "inputs_by_type": ibt,
                "outputs_by_type": obt if obt else {"p2wpkh": 0},
            })
            addrs_by_pid[pid] = addrs_in_order

        return all_inputs, participants_data, addrs_by_pid, input_indices_by_pid

    async def _drop_underfunded(self, p: Dict, mix_id: str):
        """Refund + DM a participant whose allocation collapsed to 0 equal
        outputs once the real miner fee was applied. C2 fix — the old code
        cancelled the whole mix in this case."""
        # Release the dropped participant's UTXOs back to the pool — the
        # UNIQUE(txid, vout) constraint would otherwise block these
        # outpoints from being committed to a future mix.
        await self.db.delete_utxos_by_participant(p["id"])
        await self.db.delete_outputs_by_participant(p["id"])

        fee_paid = int(p.get("fee_paid") or 0)
        lud16 = p.get("lightning_addr", "")
        npub = p["npub_hex"]
        if fee_paid > 0 and lud16:
            refund_sats = max(
                fee_paid * (100 - self.cfg.REFUND_KEEP_PERCENT) // 100,
                max(fee_paid - self.cfg.REFUND_KEEP_MIN_SATS, 0),
            )
            await self.lightning.send_refund(lud16, refund_sats, reason="underfunded_dropped")
            await self.db.update_participant(p["id"], state="refunded")
            await self.nostr.send_dm(
                npub,
                f"Dropped from mix {mix_id}: your inputs couldn't cover one equal "
                f"output plus your share of the miner fee. Refunded {refund_sats} sats.",
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
        the mix below min_participants, fall back to cancelling the whole mix.
        """
        mix_id = mix["id"]
        output_size = mix["output_size"]
        fee_rate = mix.get("fee_rate") or 30

        # Defensive: only assemble paid (or already-signing, for crash-resume)
        # participants. The caller's `active` filter is loose ("not cancelled,
        # not ghosted") — without this guard, a participant whose pay-timeout
        # hasn't fired yet would be included with no zap on file.
        active = [p for p in active if p["state"] in ("paid", "signing")]
        min_part = mix.get("min_participants", self.cfg.MIN_PARTICIPANTS_DEFAULT)

        # First pass: gather + fee math.
        all_inputs, participants_data, addrs_by_pid, input_indices_by_pid = \
            await self._gather_assembly_data(active)
        total_vsize, total_miner_fee, fee_results = self.fee_engine.calculate_all_fees(
            participants_data, output_size, fee_rate,
        )

        if total_miner_fee <= 0:
            await self._cancel_and_refund(mix, "invalid fee calculation")
            return

        # C2: identify participants whose share would zero out their outputs.
        # Drop them, refund, and retry with the survivors. One retry only —
        # if survivors are STILL under-funded the situation is pathological
        # (or the mix was misconfigured) and we cancel everyone.
        underfunded_pids = {
            rec["pid"] for rec, fr in zip(participants_data, fee_results)
            if fr.num_equal_outputs == 0
        }
        if underfunded_pids:
            survivors_active = []
            for p in active:
                if p["id"] in underfunded_pids:
                    await self._drop_underfunded(p, mix_id)
                else:
                    survivors_active.append(p)

            if len(survivors_active) < min_part:
                # The whole mix can't proceed — fall back to the existing
                # cancel-and-refund path for the survivors.
                await self._cancel_and_refund(
                    mix, "not enough participants after dropping under-funded",
                )
                return

            # Rebuild with survivors and re-run the fee math.
            active = survivors_active
            all_inputs, participants_data, addrs_by_pid, input_indices_by_pid = \
                await self._gather_assembly_data(active)
            total_vsize, total_miner_fee, fee_results = self.fee_engine.calculate_all_fees(
                participants_data, output_size, fee_rate,
            )

            if total_miner_fee <= 0 or any(fr.num_equal_outputs == 0 for fr in fee_results):
                # Still bad after one drop pass — give up to avoid a
                # potentially-infinite cascade. Operator can dig into logs.
                await self._cancel_and_refund(
                    mix, "still under-funded after pruning",
                )
                return

        # Build all_outputs with the corrected per-participant amounts. Each
        # participant's change is reduced by their fee_share; if change drops
        # below MINIMUM_UTXO_SIZE it's dropped entirely (those sats become
        # additional miner fee, per the plan).
        all_outputs: List[Dict] = []
        for rec, fr in zip(participants_data, fee_results):
            addrs = addrs_by_pid[rec["pid"]]
            for i in range(fr.num_equal_outputs):
                all_outputs.append({"address": addrs[i], "amount": output_size})
            if fr.num_change_outputs > 0 and fr.change_sats > 0:
                change_idx = fr.num_equal_outputs
                if change_idx < len(addrs):
                    all_outputs.append({"address": addrs[change_idx], "amount": fr.change_sats})
            # Persist the final accounting for transparency / debugging.
            await self.db.update_participant(
                rec["pid"],
                fee_share=fr.fee_share_sats,
                change_amount=fr.change_sats,
            )

        await self.db.update_mix(mix_id, fee_rate=int(fee_rate))

        # Build the PSBT
        psbt_hex = self.psbt_mgr.build_skeleton(all_inputs, all_outputs)
        if not psbt_hex:
            await self._cancel_and_refund(mix, "failed to build skeleton PSBT")
            return

        # Privacy check
        num_participants = len(active)
        privacy_pass, privacy_msg = self.privacy.check_psbt(psbt_hex, num_participants)
        if not privacy_pass:
            logger.warning(f"Privacy check failed for {mix_id}: {privacy_msg}")
            # Continue anyway — the plan says non-authoritative

        # Record PSBT rounds for each participant. round_num tracks ghost
        # recovery passes; the schema's UNIQUE(mix_id, pid, round_num) means
        # a second pass with round_num=1 would collide.
        round_num = mix.get("ghost_retries", 0) + 1
        now_ts = int(time.time())
        for p in active:
            pid = p["id"]
            round_id = await self.db.add_psbt_round(mix_id, pid, round_num=round_num)
            await self.db.update_psbt_round(
                round_id,
                psbt_sent=psbt_hex,
                psbt_sent_at_unix=now_ts,
                input_indices=json.dumps(input_indices_by_pid.get(pid, [])),
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

        ghosted_any = False
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
                ghosted_any = True
                logger.info(
                    "Participant %s ghosted mix %s",
                    tokens.p(p["npub_hex"]), tokens.m(mix_id),
                )

            elif time_since > deadline_seconds // 2:
                # Final warning — gates on count==2 (the second reminder must
                # have already fired). The old code gated on count<=1, which
                # was permanently false after the second reminder ran.
                if p.get("reminder_count", 0) == 2:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"FINAL WARNING: Sign the PSBT for {mix_id} within "
                        f"{int((deadline_seconds - time_since) / 3600)} hours or lose your fee."
                    )
                    await self.db.update_participant(p["id"], reminder_count=3)

            elif time_since > deadline_seconds // 4:
                # Second reminder
                if p.get("reminder_count", 0) == 1:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Reminder: Sign the PSBT for {mix_id}. "
                        f"{int((deadline_seconds - time_since) / 3600)} hours remaining."
                    )
                    await self.db.update_participant(p["id"], reminder_count=2)

            elif time_since > deadline_seconds // 8:
                # First reminder
                if p.get("reminder_count", 0) == 0:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Reminder: Sign the PSBT for {mix_id}. You have "
                        f"{int(deadline_hours)} hours from receipt."
                    )
                    await self.db.update_participant(p["id"], reminder_count=1)

        # Check if all remaining signed
        remaining = [p for p in active if p["state"] not in ("ghosted", "cancelled")]
        all_signed = all(p["state"] == "signed" for p in remaining)

        if all_signed and remaining:
            await self._combine_and_broadcast(mix, remaining)
        elif ghosted_any:
            # Check ghost retries
            ghost_retries = mix.get("ghost_retries", 0)
            max_ghost = mix.get("max_ghost_retries", self.cfg.MAX_GHOST_RETRIES)

            if ghost_retries >= max_ghost:
                await self._cancel_and_refund(mix, "max ghost retries exceeded")
            else:
                # Increment ghost retries
                await self.db.update_mix(mix_id, ghost_retries=ghost_retries + 1)
                # Remove ghost from mix
                ghost_participants = [p for p in active if p["state"] == "ghosted"]
                for gp in ghost_participants:
                    await self.db.delete_utxos_by_participant(gp["id"])
                    await self.db.delete_outputs_by_participant(gp["id"])

                # Notify remaining and actually clear their addresses so the
                # ghost-warning DM ("we've thrown out your addresses") tells
                # the truth. Their UTXOs and service-fee payment are kept; the
                # 'paid' state combined with empty outputs is what _cmd_provide_addresses
                # needs to accept a re-submission without re-charging.
                for p in remaining:
                    await self.db.delete_outputs_by_participant(p["id"])
                    await self.db.update_participant(p["id"], state="paid", reminder_count=0)
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        self.parser.format_ghost_warning(mix_id),
                    )

                # Move mix back to collecting and extend the deadline so the
                # survivors actually have time to re-submit addresses before
                # the next assembly attempt.
                new_deadline = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
                await self.db.update_mix(
                    mix_id, state="collecting", deadline_unix=new_deadline,
                )

    async def _combine_and_broadcast(self, mix: Dict, signed: List[Dict]):
        """Combine all signed PSBTs and broadcast."""
        mix_id = mix["id"]
        round_num = mix.get("ghost_retries", 0) + 1

        # Collect signed PSBTs from the active round
        psbt_hexes = []
        for p in signed:
            rounds = await self.db.get_psbt_round(mix_id, p["id"], round_num)
            if rounds and rounds.get("psbt_returned"):
                psbt_hexes.append(rounds["psbt_returned"])

        if len(psbt_hexes) < 2:
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
        else:
            await self._cancel_and_refund(mix, "broadcast failed")

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

            await self.db.destroy_mix_data(mix_id)
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
            # No open mixes — create one using defaults
            deadline_unix = int(time.time()) + self.cfg.PAY_DEADLINE_HOURS * 3600
            mid = await self.db.create_mix(
                output_size=self.cfg.DEFAULT_OUTPUT_SIZE,
                min_participants=self.cfg.DEFAULT_MIX_USER_COUNT,
                max_participants=self.cfg.MAX_PARTICIPANTS_DEFAULT,
                fee_per_element=self.cfg.FEE_PER_ELEMENT,
                deadline_unix=deadline_unix,
            )
            await self.db.update_mix(mid, state="collecting",
                                     deadline_unix=deadline_unix)
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

    async def _cancel_and_refund(self, mix: Dict, reason: str):
        """Cancel a mix and refund all non-blacklisted participants."""
        mix_id = mix["id"]
        participants = await self.db.get_participants_by_mix(mix_id)

        for p in participants:
            if p["state"] in ("cancelled", "completed"):
                continue

            # Refund fee minus keep percent
            fee_paid = p.get("fee_paid", 0)
            if fee_paid > 0 and p.get("lightning_addr"):
                refund_sats = max(
                    fee_paid * (100 - self.cfg.REFUND_KEEP_PERCENT) // 100,
                    max(fee_paid - self.cfg.REFUND_KEEP_MIN_SATS, 0),
                )
                await self.lightning.send_refund(p["lightning_addr"], refund_sats, reason=reason)
                await self.db.update_participant(p["id"], state="refunded")
                await self.nostr.send_dm(p["npub_hex"], f"Mix {mix_id} cancelled ({reason}). Refunded {refund_sats} sats.")
            elif fee_paid > 0:
                # Paid the service fee but we have no lud16 to refund to.
                # Log loudly and DM the user so they know to contact us
                # rather than silently writing off their sats.
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

        await self.db.update_mix(mix_id, state="cancelled")
        # Clean up associated records. utxos must go too: now that the
        # schema enforces UNIQUE(txid, vout), leaving rows around would
        # permanently block the same outpoints from being committed to
        # any future mix.
        await self.db.delete_outputs_for_mix(mix_id)
        await self.db.delete_utxos_for_mix(mix_id)

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
        # The nostr handler's run_forever blocks
        await self.nostr.run_forever()

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
