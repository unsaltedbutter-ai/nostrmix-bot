"""Mixing Coordinator — state machines, event loop, tie everything together."""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)


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
            logger.error(f"DM handler error for {npub_hex}: {e}", exc_info=True)
            try:
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

        # Check MAX_PENDING_MIXES
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
            await self.nostr.send_dm(npub_hex, "You haven't joined any open mixes. Try /list or /join <mix_name>")
            return

        pid = active[0]["id"]
        mix_id = active[0]["mix_id"]

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

            amount = txout.get("value", 0)
            script_type = txout.get("scriptpubkey_type", "p2wpkh")

            # Verify address type matches mix
            mix = await self.db.get_mix(mix_id)
            # For now we only accept p2wpkh

            # Add UTXO to database
            await self.db.add_utxo(pid, txid, vout, amount, script_type)
            valid_utxos.append({"txid": txid, "vout": vout, "amount": amount, "script_type": script_type})
            total_sats += amount

        if not valid_utxos:
            await self.nostr.send_dm(npub_hex, "No valid UTXOs registered.")
            return

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

        # Find active participant
        participants = await self.db.get_participants_by_npub(npub_hex)
        active = [p for p in participants if p["state"] in ("committed",)]

        if not active:
            await self.nostr.send_dm(npub_hex, "You haven't committed UTXOs yet. Start with /commit")
            return

        pid = active[0]["id"]
        mix_id = active[0]["mix_id"]
        mix = await self.db.get_mix(mix_id)

        if not mix:
            await self.nostr.send_dm(npub_hex, f"Mix {mix_id} not found.")
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

        # Store addresses in database
        # Save them in order, marking change
        for i, addr in enumerate(addrs):
            is_change = i >= num_equal  # indices >= num_equal are change outputs
            # Actually we need to be smarter: we use addresses in order
            # equal outputs use first N addresses, then change uses next
            amount = output_size if i < num_equal else (chg_amt if i == num_equal else 0)
            if amount > 0:
                await self.db.add_output(pid, addr, amount, is_change=(i >= num_equal))

        # Calculate final service fee
        final_service_fee = self.fee_engine.calculate_service_fee(num_inputs, num_used_outputs)

        await self.nostr.send_dm(
            npub_hex,
            f"{num_equal} outputs @ {eq_amt / 1e8:.4f} BTC each."
            + (f" + {chg_amt / 1e8:.4f} BTC change." if num_change and chg_amt > 0 else "")
            + f"\nPay {final_service_fee} sats (service fee) via zap to {self.cfg.BOT_LUD16}."
        )

        # Update participant state
        await self.db.update_participant(pid, state="committed", change_amount=chg_amt)
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

        pid = signing[0]["id"]
        mix_id = signing[0]["mix_id"]

        # Get the sent PSBT round
        round_data = await self.db.get_psbt_round(mix_id, pid, 1)
        if not round_data:
            await self.nostr.send_dm(npub_hex, "No PSBT record found. Please wait for the signing request.")
            return

        # Validate the returned PSBT
        skeleton_hex = round_data["psbt_sent"]
        if not skeleton_hex:
            await self.nostr.send_dm(npub_hex, "Internal error: No skeleton PSBT recorded.")
            return

        participant_utxos = await self.db.get_utxos_by_participant(pid)
        input_count = len(participant_utxos)

        expected_addrs = await self.db.get_outputs_by_participant(pid)
        expected_addr_list = [o["address"] for o in expected_addrs]

        is_valid, reason = self.psbt_mgr.validate_returned(
            skeleton_hex, psbt_hex, input_count, expected_addr_list
        )

        if not is_valid:
            await self.nostr.send_dm(npub_hex, f"PSBT rejected: {reason}. Please re-check and re-submit.")
            return

        # Store the returned PSBT
        await self.db.update_psbt_round(
            round_data["id"],
            psbt_returned=psbt_hex,
            psbt_returned_at_unix=int(time.time()),
            psbt_valid=True,
        )

        # Update participant state
        await self.db.update_participant(pid, state="signed")

        await self.nostr.send_dm(npub_hex, "PSBT accepted. Waiting for all participants to sign.")

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
                # No match — list the mixes they're in
                names = [p["mix_id"] for p in active]
                await self.nostr.send_dm(
                    npub_hex,
                    f"You are a part of {len(active)} mixes: {' & '.join(names)}. "
                    f"Say /cancel {name} to exit one."
                )
                return
        else:
            # Multiple mixes but no mix_id given
            names = [p["mix_id"] for p in active]
            await self.nostr.send_dm(
                npub_hex,
                f"You are a part of {len(active)} mixes: {' & '.join(names)}. "
                f"Say /cancel {name} to exit one."
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

        # Remove participant from mix
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
            # No pending fee — maybe a donation, ignore
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
                logger.error(f"Event loop tick error: {e}", exc_info=True)
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
                logger.error(f"Error processing mix {mix['id']}: {e}")

        # Stale chunk cleanup — discard chunk sets that started >1h ago
        stale_keys = [
            k for k, rec in self._psbt_chunks.items()
            if now - rec.get("started", 0) > self.STALE_CHUNK_TIMEOUT
        ]
        for key in stale_keys:
            logger.info(f"Cleaning up stale PSBT chunks for {key}")
            del self._psbt_chunks[key]

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
                # Check deadline
                deadline = mix.get("deadline_unix")
                if deadline and now >= deadline:
                    # Timeout
                    if len(active) < 2:
                        await self._cancel_and_refund(mix, "not enough participants")
                    elif len(active) < mix.get("min_participants", self.cfg.MIN_PARTICIPANTS_DEFAULT):
                        await self._cancel_and_refund(mix, "not enough participants")
                    else:
                        await self._proceed_to_assembling(mix, active)

            case "assembling":
                await self._assemble_psbt(mix, active)

            case "signing":
                await self._handle_signing(mix, active, now)

            case "broadcast":
                await self._handle_broadcast(mix)

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

    async def _assemble_psbt(self, mix: Dict, active: List[Dict]):
        """Build the PSBT skeleton and send to all paid participants."""
        mix_id = mix["id"]
        output_size = mix["output_size"]
        fee_rate = mix.get("fee_rate", 30)

        # Gather all inputs and outputs
        all_inputs = []
        all_outputs = []
        participant_details = []

        for p in active:
            pid = p["id"]
            utxos = await self.db.get_utxos_by_participant(pid)
            outputs = await self.db.get_outputs_by_participant(pid)

            for u in utxos:
                all_inputs.append({
                    "txid": u["txid"],
                    "vout": u["vout"],
                    "amount": u["amount"],
                    "script_type": u.get("script_type", "p2wpkh"),
                })

            for o in outputs:
                if o["amount"] > 0:
                    all_outputs.append({
                        "address": o["address"],
                        "amount": o["amount"],
                    })

            # Count inputs by script type for this participant
            ibt: Dict[str, int] = {}
            for u in utxos:
                st = u.get("script_type", "p2wpkh")
                ibt[st] = ibt.get(st, 0) + 1

            # Count outputs by script type (inferred from addresses)
            obt: Dict[str, int] = {}
            for o in outputs:
                if o["amount"] > 0:
                    addr_type = self.psbt_mgr._address_type(o["address"])
                    obt[addr_type] = obt.get(addr_type, 0) + 1

            participant_details.append({
                "pid": pid,
                "num_inputs": len(utxos),
                "total_sats": sum(u["amount"] for u in utxos),
                "num_addresses": len([o for o in outputs if o["amount"] > 0]),
                "inputs_by_type": ibt,
                "outputs_by_type": obt if obt else {"p2wpkh": len([o for o in outputs if o["amount"] > 0])},
            })

        # Aggregate all input/output types for total vsize
        agg_inputs: Dict[str, int] = {}
        agg_outputs: Dict[str, int] = {}
        for pd in participant_details:
            for k, v in pd.get("inputs_by_type", {"p2wpkh": pd["num_inputs"]}).items():
                agg_inputs[k] = agg_inputs.get(k, 0) + v
            for k, v in pd.get("outputs_by_type", {"p2wpkh": pd["num_addresses"]}).items():
                agg_outputs[k] = agg_outputs.get(k, 0) + v

        total_vsize = self.psbt_mgr.estimate_vsize(agg_inputs, agg_outputs)
        total_miner_fee = int(total_vsize * fee_rate)

        if total_miner_fee <= 0:
            await self._cancel_and_refund(mix, "invalid fee calculation")
            return

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

        # Record PSBT rounds for each participant
        now_ts = int(time.time())
        for p in active:
            pid = p["id"]
            round_id = await self.db.add_psbt_round(mix_id, pid, round_num=1)
            await self.db.update_psbt_round(round_id, psbt_sent=psbt_hex, psbt_sent_at_unix=now_ts)

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

            # Check ghosting
            if time_since > deadline_seconds:
                # Ghosted
                await self.db.update_participant(p["id"], state="ghosted")
                # Add to blacklist
                await self.db.add_to_blacklist(p["npub_hex"], reason="ghosting")
                ghosted_any = True
                logger.info(f"Participant {p['npub_hex']} ghosted mix {mix_id}")

            elif time_since > deadline_seconds // 2:
                # Final warning
                if p.get("reminder_count", 0) <= 1:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"FINAL WARNING: Sign the PSBT for {mix_id} within "
                        f"{int((deadline_seconds - time_since) / 3600)} hours or lose your fee."
                    )
                    await self.db.update_participant(p["id"], reminder_count=2)

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

                # Notify remaining of ghosting
                for p in remaining:
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        self.parser.format_ghost_warning(mix_id),
                    )
                    # Move back to paid state
                    await self.db.update_participant(p["id"], state="paid")

                # Move mix back to collecting
                await self.db.update_mix(mix_id, state="collecting")

    async def _combine_and_broadcast(self, mix: Dict, signed: List[Dict]):
        """Combine all signed PSBTs and broadcast."""
        mix_id = mix["id"]

        # Collect signed PSBTs
        psbt_hexes = []
        for p in signed:
            rounds = await self.db.get_psbt_round(mix_id, p["id"], 1)
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

        # Broadcast
        txid = await self.chain.broadcast_tx(raw_tx_hex)
        if txid:
            await self.db.update_mix(mix_id, state="broadcast", broadcast_txid=txid)
            # Notify participants
            for p in signed:
                await self.nostr.send_dm(p["npub_hex"], f"Transaction broadcast: {txid}")
        else:
            await self._cancel_and_refund(mix, "broadcast failed")

    async def _handle_broadcast(self, mix: Dict):
        """Check confirmation of broadcast transaction."""
        mix_id = mix["id"]
        txid = mix.get("broadcast_txid")

        if not txid:
            return

        # Check confirmation
        if await self.chain.is_confirmed(txid):
            await self.db.update_mix(mix_id, state="completed")
            # Notify participants
            participants = await self.db.get_participants_by_mix(mix_id)
            for p in participants:
                if p["state"] in ("signed", "broadcast"):
                    await self.nostr.send_dm(
                        p["npub_hex"],
                        f"Mix {mix_id} confirmed on-chain: {txid}",
                    )
            # Clean up is handled by completed state
        else:
            # Re-broadcast if not confirmed within 1 hour
            # We just log for now — re-broadcast can be added
            logger.info(f"Mix {mix_id} not yet confirmed, txid={txid}")

    # --- Announcement Scheduler ---

    async def _announcement_task_loop(self):
        """Post daily announcements for open mixes."""
        while self._running:
            await asyncio.sleep(86400)  # Once per day
            await self._post_daily_announcement()

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

        for m in available:
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
            else:
                await self.db.update_participant(p["id"], state="cancelled")
                await self.nostr.send_dm(p["npub_hex"], f"Mix {mix_id} cancelled: {reason}.")

        await self.db.update_mix(mix_id, state="cancelled")
        # Clean up associated records
        await self.db.delete_outputs_for_mix(mix_id)

    # --- Lifecycle ---

    async def _on_nostr_ready(self, nostr_handler: NostrHandler):
        """Called when NostrHandler has started and keys are available.

        Initializes the LnurlPayer with the bot's Nostr keys so refunds work.
        """
        keys = nostr_handler.keys
        if keys:
            await self.lightning.init_payer_with_keys(keys)
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
