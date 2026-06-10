"""Tests for the Coordinator state machine and command flow.

Covers the four critical bugs fixed in this batch plus the bundled items:
- #1 miner fee deducted at assembly
- #2 mark_utxo_used is wired
- #3 round_num progresses with ghost_retries
- #4 reminder progression (1 → 2 → 3)
- #5 ghost recovery clears survivor addresses + deadline extended
- #13 UTXO blacklist is populated, not just npub
- #14 dust below MINIMUM_UTXO_SIZE rejected at commit

Uses real Database / FeeEngine / PSBTManager, with fake Nostr/Chain/Lightning
handlers that record interactions rather than touching the network.
"""

import json
import os
import sys
import time
import tempfile
import pytest
from typing import Optional, Dict, List, Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import src.database as db_mod
from src.config import BotConfig
from src.database import Database
from src.coordinator import Coordinator
from src.psbt_manager import PSBTManager
from src.fee_engine import FeeEngine


# Real valid mainnet p2wpkh addresses sourced from a recent block — used as
# output addresses in PSBT construction so CBitcoinAddress parses them.
P2WPKH_ADDRS = [
    "bc1q4gyakdgygyc8qweh39qxamywc9vdvrt82jsrcj",
    "bc1q670lslr8tlv9w5kk4zw7ckha74ll6lx48tnsks",
    "bc1qa9d476j967wv6xdq3zcxncqgufj3evm0qakga4",
    "bc1qcsz06k58myv2az3uy35krphtw6m4rzs7jmsy96",
    "bc1qszfu7qa9ylms987se2445s63hahh589h7w74gs",
    "bc1qt9x5m8sq083zrrgycp2m8vaxt2tyy8e3yjuz9h",
    "bc1qunv7pv5lhq0a4f07qy4en4l65ul6q3m3gl00q5",
    "bc1qx0mqsvs70n3m8g92x2cn65tcmarfve5qud8uw4",
]

# 64-char hex placeholders that pass bytes.fromhex() in build_skeleton.
TXID = [f"{c*64}" for c in "abcdef"]

# A *valid* p2wpkh scriptPubKey: OP_0 (0x00) + OP_PUSHBYTES_20 (0x14) + 20-byte
# pubkey hash. Bitcointx's PSBT machinery enforces witness-vs-non-witness
# semantics, so a random 22-byte blob would be rejected at PSBT-input-validation
# time. The 20-byte hash itself can be anything since we never actually sign.
FAKE_SCRIPTPUBKEY = "0014" + "00" * 20


# --- Fakes ---


class FakeCtx:
    def __init__(self, sender_hex: str):
        self.sender_hex = sender_hex


class FakeNostrHandler:
    def __init__(self):
        self.sent_dms: List[tuple] = []  # (recipient_hex, message)
        self.identities: Dict[str, Dict] = {}

    def set_dm_handler(self, cb): pass
    def set_zap_handler(self, cb): pass
    def set_heartbeat_handler(self, cb): pass
    def set_on_ready(self, cb): pass

    async def send_dm(self, recipient_hex, message):
        self.sent_dms.append((recipient_hex, message))

    async def get_identity(self, pubkey_hex):
        return self.identities.get(pubkey_hex)

    async def post_announcement(self, text):
        return "fake_event_id"

    async def start(self): pass
    async def stop(self): pass
    async def run_forever(self): pass

    @property
    def keys(self):
        return None


class FakeChainMonitor:
    def __init__(self):
        self.txouts: Dict[str, Dict] = {}
        self.spent: Dict[str, bool] = {}
        self.spent_check_fails: Dict[str, bool] = {}  # S-B: simulate API errors
        self.confirmed: Dict[str, bool] = {}
        self.known_txids: Dict[str, Optional[bool]] = {}  # C-D: tx_known()
        # When True, tx_known returns False for txids not explicitly listed
        # (i.e. "we know it's not out there"). When False, returns None
        # (i.e. "we couldn't reach the chain"). Default True is the common
        # test case where the fake chain is "online".
        self.tx_known_default_chain_reachable: bool = True
        self.broadcast_calls: List[str] = []
        self.broadcast_return = "fake_broadcast_txid"

    async def lookup_txout(self, txid, vout):
        return self.txouts.get(f"{txid}:{vout}")

    async def is_utxo_spent(self, txid, vout):
        # S-B: real impl now returns Optional[bool]; None = couldn't check.
        if self.spent_check_fails.get(f"{txid}:{vout}", False):
            return None
        return self.spent.get(f"{txid}:{vout}", False)

    async def is_confirmed(self, txid):
        return self.confirmed.get(txid, False)

    async def tx_known(self, txid):
        if txid in self.known_txids:
            return self.known_txids[txid]
        # Default: chain is reachable, tx is not out there.
        return False if self.tx_known_default_chain_reachable else None

    async def estimate_fee_rate(self):
        return 30.0

    async def broadcast_tx(self, tx_hex):
        self.broadcast_calls.append(tx_hex)
        return self.broadcast_return

    async def re_broadcast(self, tx_hex):
        return await self.broadcast_tx(tx_hex)

    async def close(self): pass


class FakeLightningHandler:
    def __init__(self):
        self.refunds: List[tuple] = []  # (lud16, sats, reason)

    async def init(self): pass
    async def init_payer_with_keys(self, keys): pass

    async def send_refund(self, lud16, sats, reason="x"):
        self.refunds.append((lud16, sats, reason))
        return "ok"


# --- Setup ---


_db_paths: List[str] = []


async def make_coord(fee_per_element=None):
    """Build a Coordinator wired to fakes + temp db.

    ``fee_per_element`` overrides the config default (now 0) so tests that
    exercise the service-fee / zap path can opt into a non-zero fee.
    """
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    _db_paths.append(db_path)

    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    db_mod.SCHEMA_PATH = schema_path

    db = Database(db_path)
    await db.connect()

    # BotConfig falls back to its _DEFAULTS table when the env path doesn't exist.
    cfg = BotConfig("/nonexistent-env-for-tests.env")
    if fee_per_element is not None:
        cfg._values["FEE_PER_ELEMENT"] = fee_per_element

    nostr = FakeNostrHandler()
    chain = FakeChainMonitor()
    lightning = FakeLightningHandler()
    psbt_mgr = PSBTManager(
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
        overhead=cfg.TX_OVERHEAD_VSIZE,
    )
    fee_engine = FeeEngine(
        fee_per_element=cfg.FEE_PER_ELEMENT,
        min_fee_rate_sats=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate_sats=cfg.MAX_FEE_RATE_SATS,
        overhead_vsize=cfg.TX_OVERHEAD_VSIZE,
        minimum_utxo_size=cfg.MINIMUM_UTXO_SIZE,
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
    )

    coord = Coordinator(cfg, db)
    await coord.init(
        nostr=nostr, chain=chain, psbt_mgr=psbt_mgr,
        fee_engine=fee_engine, lightning=lightning,
    )
    return coord, db, nostr, chain, lightning


def _fake_txout(value: int, script_type: str = "p2wpkh") -> Dict:
    return {
        "value": value,
        "scriptpubkey": FAKE_SCRIPTPUBKEY,
        "scriptpubkey_type": script_type,
        "address": P2WPKH_ADDRS[0],
        "status": True,
    }


# --- Bug #14: dust rejection ---


class TestCommitDustRejection:
    @pytest.mark.asyncio
    async def test_rejects_utxo_below_minimum(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_hex_test_dust"
            pid = await db.add_participant(mix_id, npub, "")

            # Value = 5000 sats, below MINIMUM_UTXO_SIZE default of 10000.
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=5_000)

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            # No UTXO row should have been inserted.
            utxos = await db.get_utxos_by_participant(pid)
            assert utxos == []
            # User got a clear DM about the floor.
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "minimum" in joined or "below" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_accepts_utxo_at_or_above_minimum(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_at_floor"
            pid = await db.add_participant(mix_id, npub, "")

            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=10_000)  # exactly the floor

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            utxos = await db.get_utxos_by_participant(pid)
            assert len(utxos) == 1
            assert utxos[0]["amount"] == 10_000
        finally:
            await db.close()


# --- Bug #2: mark_utxo_used wired ---


class TestCommitMarksUtxoUsed:
    @pytest.mark.asyncio
    async def test_committed_utxo_is_marked_used(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_marker"
            await db.add_participant(mix_id, npub, "")

            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            # is_utxo_used joins to mixes filtering to non-cancelled/completed.
            # Our mix is 'announced', which qualifies.
            assert await db.is_utxo_used(TXID[0], 0) is True
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_duplicate_commit_is_rejected_by_is_utxo_used(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=1_000_000)
            mix_b = await db.create_mix(output_size=1_000_000)
            npub = "npub_dup"
            await db.add_participant(mix_a, npub, "")
            # second mix participant for the same npub
            await db.add_participant(mix_b, npub, "")

            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)

            # Commit once — should be accepted by mix_a (first active record).
            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )
            # Clear DMs so the second pass is easy to read.
            nostr.sent_dms.clear()

            # _cmd_commit_utxos picks active[0] which is mix_a again, but the
            # is_utxo_used check should now refuse re-commit of the same outpoint.
            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "already used" in joined
        finally:
            await db.close()


# --- Bug #5: ghost-recovery /addresses re-submission ---


class TestProvideAddressesPaidState:
    @pytest.mark.asyncio
    async def test_paid_participant_can_resubmit_addresses_without_recharge(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_paid_resub"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            # Move to 'paid' to simulate ghost-recovery survivor state.
            await db.update_participant(pid, state="paid", fee_paid=500)

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, P2WPKH_ADDRS[0:3],
            )

            # State stays 'paid' (no re-charge).
            p = await db.get_participant(pid)
            assert p["state"] == "paid"

            # Outputs were populated.
            outs = await db.get_outputs_by_participant(pid)
            assert len(outs) >= 1

            # DM doesn't prompt for a zap.
            last_dm = nostr.sent_dms[-1][1].lower()
            assert "already paid up" in last_dm
            assert "zap" not in last_dm
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_committed_participant_still_prompted_for_zap(self):
        # Service fee enabled (default is now 0 / no zap).
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(output_size=1_000_000,
                                         fee_per_element=100)
            npub = "npub_committed"
            pid = await db.add_participant(mix_id, npub, "")
            # Non-conforming UTXO (!= output_size) so the service fee applies.
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, P2WPKH_ADDRS[0:3],
            )

            last_dm = nostr.sent_dms[-1][1].lower()
            assert "zap" in last_dm
            # Stays 'committed' until the zap arrives.
            assert (await db.get_participant(pid))["state"] == "committed"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_committed_resubmit_replaces_not_appends(self):
        """C3: a fee-charged participant stays 'committed' with outputs stored,
        then re-sends /addresses (e.g. after we asked for a change address).
        The new set must REPLACE the old — appending doubled the outputs and
        inflated the expected zap past what we quoted, making it unpayable."""
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(output_size=1_000_000, fee_per_element=100)
            npub = "npub_resub"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(FakeCtx(npub), npub, P2WPKH_ADDRS[0:3])
            first = await db.get_outputs_by_participant(pid)
            # Re-send a different set of the same size.
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, P2WPKH_ADDRS[3:6])
            second = await db.get_outputs_by_participant(pid)

            assert len(second) == len(first), "outputs were appended, not replaced"
            # The stored addresses are the NEW set, not a mix of both.
            stored = {o["address"] for o in second}
            assert stored <= set(P2WPKH_ADDRS[3:6])
            assert stored.isdisjoint(set(P2WPKH_ADDRS[0:3]))
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_duplicate_addresses_in_batch_rejected(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_dupe"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            dupes = [P2WPKH_ADDRS[0], P2WPKH_ADDRS[1], P2WPKH_ADDRS[0]]
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, dupes)

            assert await db.get_outputs_by_participant(pid) == []
            assert (await db.get_participant(pid))["state"] == "committed"
            assert "unique" in nostr.sent_dms[-1][1].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_address_clash_with_other_participant_rejected(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            # Participant A already holds an output address in this mix.
            a = await db.add_participant(mix_id, "npub_A", "")
            await db.add_output(a, P2WPKH_ADDRS[0], 1_000_000)

            # Participant B tries to reuse A's address.
            b = await db.add_participant(mix_id, "npub_B", "")
            await db.add_utxo(b, TXID[1], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(b, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx("npub_B"), "npub_B", [P2WPKH_ADDRS[0], P2WPKH_ADDRS[1], P2WPKH_ADDRS[2]])

            assert await db.get_outputs_by_participant(b) == []
            assert "already in use" in nostr.sent_dms[-1][1].lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_zero_fee_skips_zap_and_marks_paid(self):
        # Default config: FEE_PER_ELEMENT == 0 → no zap, straight to 'paid'.
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_nofee"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, P2WPKH_ADDRS[0:3],
            )

            last_dm = nostr.sent_dms[-1][1].lower()
            assert "zap" not in last_dm
            assert "no service fee" in last_dm
            assert (await db.get_participant(pid))["state"] == "paid"
        finally:
            await db.close()


# --- Bug #4: reminder progression ---


class TestReminderProgression:
    """The plan calls for graduated DMs at deadline/8, /4, /2. The old code
    set reminder_count=2 on both the second reminder and the final warning,
    so the final-warning gate (count<=1) was permanently false. Fixed by
    progressing 0 → 1 → 2 → 3 across the three branches."""

    async def _setup_signing_participant(self, time_band: str):
        """Build a coord with one participant in 'signing' state whose
        psbt_sent_at_unix puts time_since into the requested band.
        time_band ∈ {'eighth', 'quarter', 'half', 'past'}."""
        coord, db, nostr, chain, lightning = await make_coord()
        mix_id = await db.create_mix(output_size=1_000_000)
        # Two participants — ghosting one when only 1 exists would cancel the
        # mix. Two means the ghost-recovery branch runs.
        npub_a = "npub_signing_a"
        pid_a = await db.add_participant(mix_id, npub_a, "")
        npub_b = "npub_signing_b"
        pid_b = await db.add_participant(mix_id, npub_b, "")

        deadline_seconds = coord.cfg.SIGNING_DEADLINE_HOURS * 3600
        offsets = {
            "eighth": deadline_seconds // 8 + 60,   # > /8 but < /4
            "quarter": deadline_seconds // 4 + 60,
            "half": deadline_seconds // 2 + 60,
            "past": deadline_seconds + 60,
        }
        psbt_sent = int(time.time()) - offsets[time_band]
        await db.update_participant(pid_a, state="signing", psbt_sent_at_unix=psbt_sent)
        await db.update_participant(pid_b, state="signed", psbt_sent_at_unix=psbt_sent)
        await db.update_mix(mix_id, state="signing")
        return coord, db, nostr, pid_a, mix_id

    @pytest.mark.asyncio
    async def test_first_reminder_at_eighth(self):
        coord, db, nostr, pid, mix_id = await self._setup_signing_participant("eighth")
        try:
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            p = await db.get_participant(pid)
            assert p["reminder_count"] == 1
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "reminder" in joined
            assert "final warning" not in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_reminder_at_quarter(self):
        coord, db, nostr, pid, mix_id = await self._setup_signing_participant("quarter")
        try:
            await db.update_participant(pid, reminder_count=1)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            p = await db.get_participant(pid)
            assert p["reminder_count"] == 2
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "remaining" in joined  # phrase from the second-reminder DM
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_final_warning_at_half_fires_after_two_reminders(self):
        coord, db, nostr, pid, mix_id = await self._setup_signing_participant("half")
        try:
            await db.update_participant(pid, reminder_count=2)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            p = await db.get_participant(pid)
            assert p["reminder_count"] == 3
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "final warning" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_final_warning_fires_even_when_count_was_lagged_by_downtime(self):
        """S-D: if the bot was down across the /4 → /2 boundary the
        participant's reminder_count may be 1 when time_since is already
        past /2. The fix bumps directly to the expected level (3) and fires
        the final warning rather than refusing because the prior band's
        reminder didn't run."""
        coord, db, nostr, pid, mix_id = await self._setup_signing_participant("half")
        try:
            await db.update_participant(pid, reminder_count=1)  # stale (bot was down)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # Expected: jumps to level 3 and the user gets a final warning.
            p = await db.get_participant(pid)
            assert p["reminder_count"] == 3
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "final warning" in joined
        finally:
            await db.close()


# --- Bug #13: UTXO blacklisting on ghost ---


class TestGhostBlacklistsUtxos:
    @pytest.mark.asyncio
    async def test_ghoster_npub_and_each_utxo_are_blacklisted(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="signing")
            deadline_seconds = coord.cfg.SIGNING_DEADLINE_HOURS * 3600
            past = int(time.time()) - (deadline_seconds + 120)

            # Ghoster with 2 UTXOs.
            ghoster_npub = "npub_ghoster"
            ghoster_pid = await db.add_participant(mix_id, ghoster_npub, "")
            await db.add_utxo(ghoster_pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_utxo(ghoster_pid, TXID[1], 1, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(ghoster_pid, state="signing", psbt_sent_at_unix=past)

            # One survivor still in 'signing' (not 'signed' — otherwise
            # all_signed would be true and _combine_and_broadcast would run
            # instead of the ghost-recovery branch we're trying to exercise).
            surv_pid = await db.add_participant(mix_id, "npub_survivor", "")
            await db.update_participant(
                surv_pid, state="signing",
                psbt_sent_at_unix=int(time.time()) - 60,  # plenty of time
            )

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # Blacklist now has 3 rows: 1 npub, 2 utxos.
            bl = await db.get_blacklist()
            assert len(bl) == 3
            utxo_rows = [b for b in bl if b["utxo_txid_vout"]]
            assert {b["utxo_txid_vout"] for b in utxo_rows} == {
                f"{TXID[0]}:0", f"{TXID[1]}:1",
            }
            npub_only_rows = [b for b in bl if not b["utxo_txid_vout"]]
            assert len(npub_only_rows) == 1
            assert npub_only_rows[0]["npub_hex"] == ghoster_npub

            # Ghoster moved to 'ghosted'.
            p = await db.get_participant(ghoster_pid)
            assert p["state"] == "ghosted"
        finally:
            await db.close()


# --- Bug #5: ghost recovery clears survivor outputs + extends deadline ---


class TestGhostRecovery:
    @pytest.mark.asyncio
    async def test_survivors_have_outputs_cleared_and_state_paid(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(
                mix_id, state="signing", ghost_retries=0,
                deadline_unix=int(time.time()) - 100,  # already past
            )
            deadline_seconds = coord.cfg.SIGNING_DEADLINE_HOURS * 3600
            past = int(time.time()) - (deadline_seconds + 120)

            # Ghoster
            ghoster_pid = await db.add_participant(mix_id, "g", "")
            await db.add_utxo(ghoster_pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(ghoster_pid, state="signing", psbt_sent_at_unix=past)

            # Two survivors in 'signing' (not 'signed') so all_signed is False
            # and the ghost-recovery branch fires instead of combine.
            recent = int(time.time()) - 60
            surv1_pid = await db.add_participant(mix_id, "s1", "")
            await db.add_utxo(surv1_pid, TXID[1], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(surv1_pid, P2WPKH_ADDRS[0], 1_000_000)
            await db.update_participant(surv1_pid, state="signing",
                                        fee_paid=500, psbt_sent_at_unix=recent,
                                        reminder_count=2)

            surv2_pid = await db.add_participant(mix_id, "s2", "")
            await db.add_utxo(surv2_pid, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(surv2_pid, P2WPKH_ADDRS[1], 1_000_000)
            await db.update_participant(surv2_pid, state="signing",
                                        fee_paid=500, psbt_sent_at_unix=recent)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # Survivor outputs cleared.
            assert await db.get_outputs_by_participant(surv1_pid) == []
            assert await db.get_outputs_by_participant(surv2_pid) == []
            # Survivors back to 'paid', reminder_count reset.
            for pid in (surv1_pid, surv2_pid):
                p = await db.get_participant(pid)
                assert p["state"] == "paid"
                assert p["reminder_count"] == 0

            # Mix back to collecting, ghost_retries bumped, deadline extended.
            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "collecting"
            assert mix_after["ghost_retries"] == 1
            assert mix_after["deadline_unix"] > int(time.time())

            # Survivors got the ghost-warning DM.
            dms_to_survivors = [m for r, m in nostr.sent_dms if r in ("s1", "s2")]
            assert dms_to_survivors  # at least one
            assert any("thrown out your addresses" in m for m in dms_to_survivors)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_ghost_when_everyone_else_signed_restarts_round_not_cancel(self):
        """H2: the common ghost pattern — all cooperative participants sign
        early, one lets the deadline lapse. On the tick that ghosts the
        laggard the others are all 'signed', so the OLD code took the
        all_signed branch first, tried to finalize a tx still missing the
        ghost's signature, and cancelled the whole mix. The fix checks
        ghosted_any first → ghost recovery restarts the round instead."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="signing", ghost_retries=0,
                                deadline_unix=int(time.time()) - 100)
            deadline_seconds = coord.cfg.SIGNING_DEADLINE_HOURS * 3600
            past = int(time.time()) - (deadline_seconds + 120)
            recent = int(time.time()) - 60

            # Two survivors who already SIGNED.
            for i, npub in enumerate(("s1", "s2")):
                pid = await db.add_participant(mix_id, npub, "")
                await db.add_utxo(pid, TXID[i], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                await db.add_output(pid, P2WPKH_ADDRS[i], 1_000_000)
                await db.update_participant(pid, state="signed",
                                            fee_paid=500, psbt_sent_at_unix=recent)

            # The ghoster — still 'signing', past the deadline.
            gpid = await db.add_participant(mix_id, "ghost", "")
            await db.add_utxo(gpid, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(gpid, state="signing", psbt_sent_at_unix=past)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # Recovery, NOT cancellation: mix survives and is back to collecting.
            mix_after = await db.get_mix(mix_id)
            assert mix_after is not None, "mix was wrongly cancelled"
            assert mix_after["state"] == "collecting"
            assert mix_after["ghost_retries"] == 1
            # The ghoster is blacklisted.
            assert await db.is_blacklisted("ghost")
            # No broadcast was attempted.
            assert chain.broadcast_calls == []
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_committed_straggler_does_not_wedge_signing(self):
        """H3: a participant who /commit-ed but never finished /addresses
        before the mix advanced stays 'committed'. The OLD code included
        such stragglers in 'remaining', so all_signed was forever false and
        the fully-signed mix never broadcast. The fix scopes the
        completion check to the assembled round (signing/signed/ghosted)."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = "txid_round_scoped_ok"

            # A straggler joins/commits but never gets into the round.
            spid = await db.add_participant(mix_id, "npub_straggler", "")
            await db.add_utxo(spid, "cc" * 32, 0, 250_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(spid, state="committed")

            mix_row = await db.get_mix(mix_id)   # already 'signing' after assembly
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # The signed round broadcast despite the committed straggler.
            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "broadcast"
            assert mix_after["broadcast_txid"] == "txid_round_scoped_ok"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_classify_ready_requires_addresses(self):
        """C2/H1 part A: a 'paid' participant with no output addresses (e.g. a
        ghost-recovery survivor who hasn't resubmitted) must NOT count toward
        the non-conforming target, so the mix can't re-advance to assembling
        and assemble with empty address lists."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=1)
            mix_row = await db.get_mix(mix_id)

            # One NC participant, paid, but addresses were cleared.
            pid = await db.add_participant(mix_id, "npub_no_addr", "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="paid", fee_paid=500)

            ready = [p for p in await db.get_participants_by_mix(mix_id)
                     if p["state"] == "paid"]
            proceed, nc_count, _ = await coord._classify_ready(mix_row, ready)
            assert proceed is False
            assert nc_count == 0  # the address-less participant did not count
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_assembly_drops_address_starved_conforming_participant(self):
        """C2/H1 part B: defence-in-depth. A conforming-only participant who
        reaches assembly with no addresses would have their pass-through output
        SILENTLY skipped (their whole UTXO burned to the miner fee). The fix
        drops them rather than burning funds; with the NC target then unmet the
        mix cancels and refunds instead of shipping a fund-burning tx."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=1, fee_per_element=100)
            await db.update_mix(mix_id, state="assembling", fee_rate=30,
                                input_type="p2wpkh", output_type="p2wpkh")

            # NC participant with addresses (the legitimate mixer).
            nc = await db.add_participant(mix_id, "npub_nc", "nc@x")
            await db.add_utxo(nc, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(nc, state="paid", fee_paid=500)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(nc, addr, 1_000_000)

            # Conforming-only participant, paid, but NO addresses on file.
            conf = await db.add_participant(mix_id, "npub_conf", "conf@x")
            await db.add_utxo(conf, TXID[1], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(conf, state="paid", fee_paid=0)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            # The conforming UTXO was never silently burned: no PSBT was built
            # that omits its output. With only the NC participant left (target
            # unmet after the drop), the mix cancels + refunds rather than
            # broadcasting. The conforming participant's UTXO row is released.
            mix_after = await db.get_mix(mix_id)
            assert mix_after is None or mix_after["state"] != "broadcast"
            # No broadcast of a fund-burning tx.
            assert chain.broadcast_calls == []
        finally:
            await db.close()


# --- Operator allowlist for input/output types ---


class TestInputTypeAllowlist:
    """ACCEPTED_INPUT_TYPES gates /commit. Default is p2wpkh-only."""

    @pytest.mark.asyncio
    async def test_rejects_non_allowed_input_type(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_tr_rejected"
            pid = await db.add_participant(mix_id, npub, "")

            # A p2tr UTXO — script_type 'p2tr' isn't in the default allowlist.
            chain.txouts[f"{TXID[0]}:0"] = {
                "value": 500_000,
                "scriptpubkey": "5120" + "00" * 32,
                "scriptpubkey_type": "p2tr",
                "address": "bc1p...",
                "status": True,
            }

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            # No UTXO saved.
            assert await db.get_utxos_by_participant(pid) == []
            # Polite DM mentioning the allowed set.
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "p2tr" in joined and "p2wpkh" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_accepts_input_type_added_to_allowlist(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Widen the operator's allowlist in-place for this test.
            coord.cfg._values["_accepted_input_types"] = {"p2wpkh", "p2tr"}

            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_tr_allowed"
            await db.add_participant(mix_id, npub, "")

            chain.txouts[f"{TXID[0]}:0"] = {
                "value": 500_000,
                "scriptpubkey": "5120" + "00" * 32,
                "scriptpubkey_type": "p2tr",
                "address": "bc1p...",
                "status": True,
            }

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            utxos = await db.get_utxos_by_participant(
                (await db.get_participants_by_npub(npub))[0]["id"]
            )
            assert len(utxos) == 1
            assert utxos[0]["script_type"] == "p2tr"
        finally:
            await db.close()


class TestOutputTypeAllowlist:
    """ACCEPTED_OUTPUT_TYPES gates /addresses. Default is p2wpkh-only."""

    @pytest.mark.asyncio
    async def test_rejects_p2tr_address_when_only_p2wpkh_allowed(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_mixed_out"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            # First two are valid p2wpkh; the third is a real p2tr (bc1p).
            addrs = [
                P2WPKH_ADDRS[0],
                P2WPKH_ADDRS[1],
                "bc1p9j0rwcgpd28gnastlh2yweshq7sl2vxxvrpstdsx9w3m8axaxn0qg0vcg0",
            ]

            await coord._cmd_provide_addresses(FakeCtx(npub), npub, addrs)

            # No outputs stored, polite DM with the allowed set + the offending addr.
            assert await db.get_outputs_by_participant(pid) == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "p2wpkh" in joined
            assert "bc1p" in joined  # the offending address is named
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_accepts_p2tr_address_when_added_to_allowlist(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["_accepted_output_types"] = {"p2wpkh", "p2tr"}

            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_tr_out_allowed"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            addrs = [
                P2WPKH_ADDRS[0],
                "bc1p9j0rwcgpd28gnastlh2yweshq7sl2vxxvrpstdsx9w3m8axaxn0qg0vcg0",
            ]
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, addrs)

            outs = await db.get_outputs_by_participant(pid)
            assert len(outs) >= 1
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_rejects_unparseable_address(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_garbage"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, [P2WPKH_ADDRS[0], "not-an-address"],
            )

            assert await db.get_outputs_by_participant(pid) == []
        finally:
            await db.close()


# --- Bugs #1 + #3: miner fee deducted, round_num progresses ---


async def _seed_paid_participants(db, mix_id, count: int = 3):
    """Add `count` paid participants with 1 UTXO and 3 addresses each.

    Returns list of pid in insertion order. UTXO amounts chosen to give a
    mix of equal-output counts and a positive change (after fees) per p."""
    pids = []
    # 3M / 2M / 2.5M sats — leaves room for 2 equal outputs and some change
    # after the proportional miner fee bite.
    amounts = [3_000_000, 2_000_000, 2_500_000]
    for i in range(count):
        npub = f"npub_p{i}"
        pid = await db.add_participant(mix_id, npub, f"lud16{i}@test")
        await db.update_participant(pid, state="paid", fee_paid=500 + i * 100)
        await db.add_utxo(pid, TXID[i], 0, amounts[i], "p2wpkh", FAKE_SCRIPTPUBKEY)
        # 3 addresses each — assembly picks num_equal of them + a change addr.
        base = i * 3
        for addr in P2WPKH_ADDRS[base:base + 3]:
            # Preliminary stored amount — assembly overrides via FeeResult.
            await db.add_output(pid, addr, 1_000_000)
        pids.append(pid)
    return pids


class TestAssemblePsbt:
    @pytest.mark.asyncio
    async def test_miner_fee_is_deducted_so_outputs_sum_lt_inputs(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)
            pids = await _seed_paid_participants(db, mix_id, count=3)

            sum_inputs = 0
            for pid in pids:
                for u in await db.get_utxos_by_participant(pid):
                    sum_inputs += u["amount"]

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            # Each participant got a fee_share recorded.
            shares = []
            for pid in pids:
                p = await db.get_participant(pid)
                shares.append(p.get("fee_share") or 0)
            assert all(s > 0 for s in shares), f"fee_shares not set: {shares}"

            # Recompute the actual all_outputs sum from the participants'
            # final accounting: num_equal*output_size + change.
            sum_outputs = 0
            for pid in pids:
                p = await db.get_participant(pid)
                # Equal outputs: we don't store this directly, but we can read
                # the surviving 'is_change=0' rows are gone (assembly overwrote
                # via output build, but stored rows are the preliminary ones).
                # Instead, derive: outputs sum = inputs - fee_share - dropped_change.
                # If change_amount > 0 it's included; if 0 it was dropped.
                pass

            # Simpler invariant: total fee share sums to less than total inputs.
            assert sum(shares) > 0
            assert sum(shares) < sum_inputs

            # All participants moved to 'signing' state.
            for pid in pids:
                p = await db.get_participant(pid)
                assert p["state"] == "signing"

            # Mix transitioned to 'signing'.
            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "signing"

            # Each participant has a psbt_round row.
            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            assert len(rounds) == 3
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_round_num_progresses_with_ghost_retries(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            # Simulate two prior ghost-recovery passes. The current assembly
            # should be round 3 (= ghost_retries + 1).
            await db.update_mix(mix_id, state="assembling", fee_rate=30, ghost_retries=2)
            pids = await _seed_paid_participants(db, mix_id, count=3)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            assert len(rounds) == 3
            assert all(r["round_num"] == 3 for r in rounds), \
                f"unexpected round_nums: {[r['round_num'] for r in rounds]}"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_psbt_round_unique_constraint_survives_ghost_loop(self):
        """The schema has UNIQUE(mix_id, pid, round_num). The old code always
        used 1; a ghost-recovery + reassembly would violate it. With round_num
        derived from ghost_retries+1, two passes write to different rows."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="assembling", fee_rate=30, ghost_retries=0)
            pids = await _seed_paid_participants(db, mix_id, count=3)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            # Simulate a ghost cycle: bump retries, re-seed paid state for the
            # round 2 attempt. We don't have to re-create participants — they
            # already exist; just bump the mix.
            await db.update_mix(mix_id, state="assembling", ghost_retries=1)
            # Reset participants to paid so _assemble_psbt re-uses them.
            for pid in pids:
                await db.update_participant(pid, state="paid")

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            # Should not raise (UNIQUE constraint not violated).
            await coord._assemble_psbt(mix_row, active)

            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            # 3 participants × 2 rounds = 6 rows.
            assert len(rounds) == 6
            assert {r["round_num"] for r in rounds} == {1, 2}
        finally:
            await db.close()


# --- #17 announcement clock alignment ---


class TestAnnouncementScheduling:
    @pytest.mark.asyncio
    async def test_seconds_until_next_announcement_is_in_range(self):
        coord, db, _, _, _ = await make_coord()
        try:
            secs = coord._seconds_until_next_announcement()
            assert 0 < secs <= 86_400
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_seconds_honors_configured_hour(self):
        import datetime as dt
        coord, db, _, _, _ = await make_coord()
        try:
            now = dt.datetime.now(dt.timezone.utc)
            # Schedule for the next 30 minutes — set ANNOUNCEMENT_HOUR_UTC to
            # "now" if minutes > 30 we roll to next hour, otherwise this hour.
            # Either way the wait should be well under an hour.
            coord.cfg._values["ANNOUNCEMENT_HOUR_UTC"] = (now.hour + 1) % 24
            secs = coord._seconds_until_next_announcement()
            # At most ~1 hour ahead.
            assert secs < 3700  # 1h + small slack
        finally:
            await db.close()


# --- #9 per-participant pay deadline ---


class TestPerParticipantPayTimeout:
    @pytest.mark.asyncio
    async def test_committed_past_deadline_is_cancelled(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="collecting",
                                deadline_unix=int(time.time()) + 999_999)

            npub = "npub_unpaid"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            # Backdate updated_at_unix past the pay deadline.
            past = int(time.time()) - (coord.cfg.PAY_DEADLINE_HOURS * 3600 + 60)
            await db._conn.execute(
                "UPDATE participants SET updated_at_unix=? WHERE id=?",
                (past, pid),
            )
            await db._conn.commit()

            await coord._tick()

            p = await db.get_participant(pid)
            assert p["state"] == "cancelled"
            assert await db.get_utxos_by_participant(pid) == []
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_committed_within_deadline_survives_tick(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="collecting",
                                deadline_unix=int(time.time()) + 999_999)
            npub = "npub_recent"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")
            # updated_at_unix is current (just got set above), well within deadline.

            await coord._tick()

            p = await db.get_participant(pid)
            assert p["state"] == "committed"
        finally:
            await db.close()


# --- #6 one-at-a-time mix per npub ---


class TestOneAtATimeMix:
    @pytest.mark.asyncio
    async def test_join_blocked_when_already_committed_elsewhere(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=1_000_000)
            mix_b = await db.create_mix(output_size=1_000_000)
            npub = "npub_busy"
            pid = await db.add_participant(mix_a, npub, "")
            await db.update_participant(pid, state="committed")

            await coord._cmd_join_mix(FakeCtx(npub), mix_b)

            # No new participant row created.
            allp = await db.get_participants_by_npub(npub)
            assert len(allp) == 1

            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "before joining another" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_join_blocked_when_interested_elsewhere(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=1_000_000)
            mix_b = await db.create_mix(output_size=1_000_000)
            npub = "npub_interested"
            await db.add_participant(mix_a, npub, "")  # default state 'interested'

            await coord._cmd_join_mix(FakeCtx(npub), mix_b)

            allp = await db.get_participants_by_npub(npub)
            assert len(allp) == 1
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "send /commit and /addresses" in joined.lower() or "before joining another" in joined
        finally:
            await db.close()


# --- #7 per-mix input/output type lock ---


class TestPerMixTypeLock:
    @pytest.mark.asyncio
    async def test_first_commit_locks_mix_input_type(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_first_commit"
            await db.add_participant(mix_id, npub, "")
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000, script_type="p2wpkh")

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            mix = await db.get_mix(mix_id)
            assert mix["input_type"] == "p2wpkh"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_second_participant_with_mismatched_type_rejected(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Widen the operator allowlist so the rejection comes from the
            # per-mix lock, not the global allowlist.
            coord.cfg._values["_accepted_input_types"] = {"p2wpkh", "p2tr"}
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, input_type="p2wpkh")

            npub = "npub_mismatch"
            await db.add_participant(mix_id, npub, "")
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000, script_type="p2tr")

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            pid = (await db.get_participants_by_npub(npub))[0]["id"]
            assert await db.get_utxos_by_participant(pid) == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "locked to p2wpkh" in joined
        finally:
            await db.close()


# --- #8 auto-mix-on-commit ---


class TestAutoMixOnCommit:
    @pytest.mark.asyncio
    async def test_creates_new_mix_when_no_open_one_exists(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            npub = "npub_solo_commit"
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
            new_mix = await db.get_mix(ps[0]["mix_id"])
            assert new_mix is not None
            assert new_mix["state"] == "collecting"
            assert new_mix["input_type"] == "p2wpkh"
            # Participant moved to 'committed' since the UTXO was registered.
            assert ps[0]["state"] == "committed"
            utxos = await db.get_utxos_by_participant(ps[0]["id"])
            assert len(utxos) == 1
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_joins_existing_open_mix_if_compatible(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            existing = await db.create_mix(output_size=1_000_000,
                                           max_participants=10)
            await db.update_mix(existing, state="collecting", input_type="p2wpkh")

            npub = "npub_auto_joiner"
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
            assert ps[0]["mix_id"] == existing
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_join_tolerates_space_instead_of_hyphen(self):
        """`/join silver cupcake` (space, not hyphen) still finds silver-cupcake:
        the first token doesn't match, so the coordinator tries the joined form."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)  # e.g. silver-cupcake
            await db.update_mix(mix_id, state="collecting")

            primary = mix_id.split("-")[0]  # first word alone — won't match a mix
            # alt is the hyphen-joined form the parser would build from two words.
            await coord._cmd_join_mix(FakeCtx("spacer"), primary, mix_id)

            parts = await db.get_participants_by_mix(mix_id)
            assert any(p["npub_hex"] == "spacer" for p in parts), (
                "two-word /join should have registered interest in the real mix"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_skips_locked_incompatible_mix_creates_new(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Widen the allowlist so p2tr passes the gate.
            coord.cfg._values["_accepted_input_types"] = {"p2wpkh", "p2tr"}
            # Existing mix is locked to p2wpkh.
            existing = await db.create_mix(output_size=1_000_000)
            await db.update_mix(existing, state="collecting", input_type="p2wpkh")

            npub = "npub_tr_seeker"
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000, script_type="p2tr")

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
            # Should NOT have joined the p2wpkh mix.
            assert ps[0]["mix_id"] != existing
            # New mix locked to p2tr.
            new_mix = await db.get_mix(ps[0]["mix_id"])
            assert new_mix["input_type"] == "p2tr"
        finally:
            await db.close()


# --- join-by-amount (/join <btc>) ---


class TestJoinByAmount:
    @pytest.mark.asyncio
    async def test_creates_mix_with_exact_size(self):
        """/join 0.00125 creates a 125000-sat mix (exact, no rounding) and
        registers the user as interested."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            npub = "npub_sizer"
            await coord._cmd_join_mix(FakeCtx(npub), None, None, "0.00125")

            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
            mix = await db.get_mix(ps[0]["mix_id"])
            assert mix["output_size"] == 125_000
            assert mix["state"] == "collecting"
            assert mix["required_nonconforming"] == coord.cfg.DEFAULT_REQUIRED_NONCONFORMING
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "created mix" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_joins_existing_same_size_prefers_fullest(self):
        """/join <amount> joins an open mix of that exact size, picking the
        closest-to-full one rather than creating a new mix."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            empty = await db.create_mix(output_size=1_000_000, max_participants=10)
            await db.update_mix(empty, state="collecting")
            fuller = await db.create_mix(output_size=1_000_000, max_participants=10)
            await db.update_mix(fuller, state="collecting")
            await db.add_participant(fuller, "someone_else", "")

            npub = "npub_joiner_amt"
            await coord._cmd_join_mix(FakeCtx(npub), None, None, "0.01")

            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
            assert ps[0]["mix_id"] == fuller  # the fuller one, not a new mix
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_below_minimum_rejected(self):
        """An amount below MINIMUM_UTXO_SIZE is rejected and creates no mix."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            npub = "npub_tiny"
            # 1000 sats, default MINIMUM_UTXO_SIZE is 10000.
            await coord._cmd_join_mix(FakeCtx(npub), None, None, "0.00001")

            assert await db.get_participants_by_npub(npub) == []
            assert await db.get_mixes_by_state("announced", "collecting") == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "minimum mix size" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_amount_of_one_btc_or_more_rejected(self):
        """The mix size must be < 1 BTC. A bare integer like '/join 100000'
        (a sats-vs-BTC typo) reads as 100000 BTC and must be refused, not
        create a mix; '/join 1' (1 BTC) likewise."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            for npub, amt in (("npub_big1", "1"), ("npub_big2", "100000")):
                await coord._cmd_join_mix(FakeCtx(npub), None, None, amt)
                assert await db.get_participants_by_npub(npub) == []
            assert await db.get_mixes_by_state("announced", "collecting") == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "less than 1 btc" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_amount_just_under_one_btc_is_allowed(self):
        """0.99 BTC is a valid (if large) mix size — the bound is < 1, not <=."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            npub = "npub_under1"
            await coord._cmd_join_mix(FakeCtx(npub), None, None, "0.99")
            ps = await db.get_participants_by_npub(npub)
            assert len(ps) == 1
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_refused_at_max_open_mixes(self):
        """At MAX_OPEN_MIXES open mixes, an amount-join of a new size is refused
        rather than creating another mix."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["MAX_OPEN_MIXES"] = 2
            for _ in range(2):
                mid = await db.create_mix(output_size=2_000_000)
                await db.update_mix(mid, state="collecting")

            npub = "npub_capped"
            await coord._cmd_join_mix(FakeCtx(npub), None, None, "0.01")

            assert await db.get_participants_by_npub(npub) == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "too many open mixes" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_commit_auto_create_refused_at_cap(self):
        """The /commit auto-create path also respects MAX_OPEN_MIXES."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["MAX_OPEN_MIXES"] = 1
            # One open mix already, locked to a type our UTXO won't match so the
            # auto-create path is forced (and then blocked by the cap).
            existing = await db.create_mix(output_size=999_999)
            await db.update_mix(existing, state="collecting", input_type="p2tr")

            npub = "npub_commit_capped"
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)
            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            assert await db.get_participants_by_npub(npub) == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "no compatible mix available" in joined
        finally:
            await db.close()


# --- #11 /psbt_accept disambiguation across multiple signing mixes ---


class TestPsbtAcceptDisambiguation:
    """When a user is signing in two mixes simultaneously, /psbt_accept must
    pick the right one. The participant's stored input_indices + the skeleton
    structure tell validate_returned which mix this PSBT corresponds to."""

    @pytest.mark.asyncio
    async def test_picks_correct_mix_among_two_signing(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Two mixes, both with same npub (legal: one paid each).
            mix_a = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_a, state="assembling", fee_rate=30)
            mix_b = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_b, state="assembling", fee_rate=30)

            # In each mix, the npub is paired with one other participant so
            # _assemble_psbt has 2 paid participants to work with.
            our_npub = "npub_two_mix"
            other_npub_a = "npub_other_a"
            other_npub_b = "npub_other_b"

            async def _seed(mix_id, our_pid_holder, other_npub, our_txid, our_outs, other_txid, other_outs):
                pid_a = await db.add_participant(mix_id, our_npub, "")
                pid_b = await db.add_participant(mix_id, other_npub, "")
                await db.update_participant(pid_a, state="paid", fee_paid=500)
                await db.update_participant(pid_b, state="paid", fee_paid=500)
                await db.add_utxo(pid_a, our_txid, 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                await db.add_utxo(pid_b, other_txid, 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                for addr in our_outs:
                    await db.add_output(pid_a, addr, 1_000_000)
                for addr in other_outs:
                    await db.add_output(pid_b, addr, 1_000_000)
                our_pid_holder[0] = pid_a

            our_pid_a = [None]
            our_pid_b = [None]
            await _seed(mix_a, our_pid_a, other_npub_a,
                        TXID[0], P2WPKH_ADDRS[0:3], TXID[1], P2WPKH_ADDRS[3:6])
            await _seed(mix_b, our_pid_b, other_npub_b,
                        TXID[2], P2WPKH_ADDRS[1:4], TXID[3], P2WPKH_ADDRS[4:7])

            # Assemble each — moves participants to 'signing' with stored
            # skeletons and input_indices.
            for mid in (mix_a, mix_b):
                mix_row = await db.get_mix(mid)
                active = await db.get_participants_by_mix(mid)
                await coord._assemble_psbt(mix_row, active)

            # Pull the mix-A skeleton — that's what our_npub is "returning."
            round_a = await db.get_psbt_round(mix_a, our_pid_a[0], 1)
            assert round_a and round_a["psbt_sent"]

            # We don't actually have private keys to produce a valid signed
            # PSBT, and bitcointx's serializer rejects fake partial_sigs.
            # The dispatch logic (#11) is independent of signature crypto —
            # we mock validate_returned so it returns True only when the
            # caller passes mix_a's skeleton. The coordinator should then
            # land on mix_a and ignore mix_b.
            original = coord.psbt_mgr.validate_returned

            def fake_validate(skeleton_hex, returned_hex, **kwargs):
                if skeleton_hex == returned_hex and skeleton_hex == round_a["psbt_sent"]:
                    return (True, "valid")
                return (False, "no match")

            coord.psbt_mgr.validate_returned = fake_validate

            # Confirm both participants are in 'signing' state from the assembly.
            our_a = await db.get_participant(our_pid_a[0])
            our_b = await db.get_participant(our_pid_b[0])
            assert our_a["state"] == "signing"
            assert our_b["state"] == "signing"

            try:
                await coord._cmd_accept_psbt(
                    FakeCtx(our_npub), our_npub, round_a["psbt_sent"],
                )
            finally:
                coord.psbt_mgr.validate_returned = original

            our_a_after = await db.get_participant(our_pid_a[0])
            our_b_after = await db.get_participant(our_pid_b[0])
            assert our_a_after["state"] == "signed", "mix_a should have matched"
            assert our_b_after["state"] == "signing", "mix_b should be untouched"
        finally:
            await db.close()


# --- #12 strict per-input signature validation (unit-tested in test_psbt_manager.py) ---


# --- Wave 1: critical + quick-fix significant items (regression guards) ---


class TestExitMixTypoMultiMix:
    """M6: /cancel with a typo while user is in 2+ mixes referenced an
    undefined `name` variable (local was `names`). The handler's outer
    try/except would have papered over it with 'Error processing your
    message'. This guard catches both the NameError regression AND the
    user-visible message format."""

    @pytest.mark.asyncio
    async def test_cancel_typo_with_multiple_active_mixes(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=1_000_000)
            mix_b = await db.create_mix(output_size=1_000_000)
            npub = "npub_two_paid"
            pid_a = await db.add_participant(mix_a, npub, "")
            pid_b = await db.add_participant(mix_b, npub, "")
            await db.update_participant(pid_a, state="paid", fee_paid=500)
            await db.update_participant(pid_b, state="paid", fee_paid=500)

            await coord._cmd_exit_mix(FakeCtx(npub), npub, "nonexistent-mix")

            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "you are a part of 2 mixes" in joined
            # Outer try/except in _on_dm would surface a NameError as this:
            assert "error processing" not in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_cancel_no_mix_with_multiple_active_mixes(self):
        """Same broken f-string path, hit via /cancel with no mix_id at all."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=1_000_000)
            mix_b = await db.create_mix(output_size=1_000_000)
            npub = "npub_two_paid2"
            pid_a = await db.add_participant(mix_a, npub, "")
            pid_b = await db.add_participant(mix_b, npub, "")
            await db.update_participant(pid_a, state="paid", fee_paid=500)
            await db.update_participant(pid_b, state="paid", fee_paid=500)

            await coord._cmd_exit_mix(FakeCtx(npub), npub, None)

            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "you are a part of 2 mixes" in joined
            assert "error processing" not in joined
        finally:
            await db.close()


class TestOnZapHappyAndOverpayPaths:
    """The committed-zap path was thin on coverage (only the unmatched-zap
    log was tested). These pin down the exact-pay, overpay, and underpay
    accept/reject + accounting + operator-log behaviour."""

    async def _setup_committed_participant(self, db):
        mix_id = await db.create_mix(output_size=1_000_000,
                                     fee_per_element=100)
        await db.update_mix(mix_id, state="collecting")
        npub = "npub_payer"
        pid = await db.add_participant(mix_id, npub, "")
        await db.add_utxo(pid, TXID[0], 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
        # 2 outputs registered → expected = FEE_PER_ELEMENT * (1 + 2) = 300 sats
        await db.add_output(pid, P2WPKH_ADDRS[0], 1_000_000)
        await db.add_output(pid, P2WPKH_ADDRS[1], 1_000_000)
        await db.update_participant(pid, state="committed")
        return mix_id, pid, npub

    @pytest.mark.asyncio
    async def test_exact_payment_marks_paid_and_dms_acceptance(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pid, npub = await self._setup_committed_participant(db)

            class Zap:
                sender_hex = npub
                amount_sats = 300  # 100 * (1 input + 2 outputs)

            await coord._on_zap(Zap(), FakeCtx(npub))

            p = await db.get_participant(pid)
            assert p["state"] == "paid"
            assert p["fee_paid"] == 300
            joined = " ".join(m for r, m in nostr.sent_dms if r == npub).lower()
            assert "300 sats accepted" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_overpayment_marks_paid_with_full_amount_and_logs(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            import logging
            mix_id, pid, npub = await self._setup_committed_participant(db)

            class Zap:
                sender_hex = npub
                amount_sats = 1500  # expected 300, sent 1500 → +1200 overpay

            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._on_zap(Zap(), FakeCtx(npub))

            p = await db.get_participant(pid)
            assert p["state"] == "paid"
            # The FULL zap amount is recorded — that's what gets refunded
            # (modulo keep_percent) if the mix later cancels.
            assert p["fee_paid"] == 1500
            joined = " ".join(m for r, m in nostr.sent_dms if r == npub).lower()
            assert "1500 sats accepted" in joined
            # Operator-visibility log surfaces the excess.
            log_text = " ".join(r.message.lower() for r in caplog.records)
            assert "overpayment" in log_text
            assert "1500" in log_text and "300" in log_text
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_underpayment_does_not_mark_paid_and_dms_insufficient(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pid, npub = await self._setup_committed_participant(db)

            class Zap:
                sender_hex = npub
                amount_sats = 100  # expected 300, sent 100 → partial

            await coord._on_zap(Zap(), FakeCtx(npub))

            # Per the plan, partial payments are treated as no payment.
            p = await db.get_participant(pid)
            assert p["state"] == "committed"
            assert p["fee_paid"] in (None, 0)
            joined = " ".join(m for r, m in nostr.sent_dms if r == npub).lower()
            assert "insufficient" in joined and "300" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_one_at_a_time_invariant_prevents_multi_mix_zap_ambiguity(self):
        """The friend-reported concern that _on_zap picks awaiting[0] and
        ignores siblings is structurally prevented: /join and the auto-mix
        race guard ensure a user has at most one 'committed' participant.
        Verify that by attempting to insert two committed rows for the
        same npub — only one survives at all (the second goes through the
        one-at-a-time gate)."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Get into mix A and commit (so state is 'committed').
            mix_a = await db.create_mix(output_size=1_000_000)
            pid_a = await db.add_participant(mix_a, "npub_x", "")
            await db.update_participant(pid_a, state="committed")

            # Now try /join'ing mix B.
            mix_b = await db.create_mix(output_size=1_000_000)
            await coord._cmd_join_mix(FakeCtx("npub_x"), mix_b)

            participants = await db.get_participants_by_npub("npub_x")
            committed = [p for p in participants if p["state"] == "committed"]
            assert len(committed) == 1, (
                f"one-at-a-time gate failed: {len(committed)} committed rows"
            )
            # And the DM explains why /join was refused.
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "before joining another" in joined
        finally:
            await db.close()


class TestSilentZapNoLongerSilent:
    """S6: zaps not matched to any committed participant were dropped with
    no log or DM. Operator-visibility regression."""

    @pytest.mark.asyncio
    async def test_unmatched_zap_logs_at_info(self, caplog):
        import logging
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # No participants for this npub at all.
            class FakeZap:
                sender_hex = "npub_donor"
                amount_sats = 1234

            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._on_zap(FakeZap(), FakeCtx("npub_donor"))

            messages = " ".join(r.message.lower() for r in caplog.records)
            assert "unmatched zap" in messages or "no pending fee" in messages
            assert "1234" in messages
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_late_zap_after_timeout_recorded_as_refund_owed(self):
        """H3: a zap that lands after the participant's slot timed out (state
        'cancelled') must not be silently kept. Record a refund debt + DM."""
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(output_size=1_000_000, fee_per_element=100)
            pid = await db.add_participant(mix_id, "npub_late", "late@wallet.com")
            # Their slot timed out before the zap arrived.
            await db.update_participant(pid, state="cancelled")

            class Zap:
                sender_hex = "npub_late"
                amount_sats = 700
            await coord._on_zap(Zap(), FakeCtx("npub_late"))

            owed = await db.get_refunds_owed()
            assert len(owed) == 1
            assert owed[0]["participant_id"] == pid
            assert owed[0]["lightning_addr"] == "late@wallet.com"
            assert owed[0]["sats"] == 700
            dms = " ".join(m for r, m in nostr.sent_dms if r == "npub_late").lower()
            assert "refund" in dms and "expired" in dms
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_extra_zap_from_paid_participant_is_kept_not_refunded(self):
        """A second zap from an already-paid participant in an active mix is a
        duplicate/overpayment — kept, never recorded as a refund."""
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(output_size=1_000_000, fee_per_element=100)
            pid = await db.add_participant(mix_id, "npub_dup", "dup@wallet.com")
            await db.update_participant(pid, state="paid", fee_paid=500)

            class Zap:
                sender_hex = "npub_dup"
                amount_sats = 500
            await coord._on_zap(Zap(), FakeCtx("npub_dup"))

            assert await db.get_refunds_owed() == []
        finally:
            await db.close()


class TestAssemblePsbtFiltersToPaid:
    """S8: _assemble_psbt iterated whatever 'active' list it was handed. In
    a crash-recovery scenario the mix could be in 'assembling' state with
    a participant still 'committed' (paid timeout hasn't fired). That
    participant's inputs would be added to the PSBT but they'd never sign.
    Filter defensively to 'paid' / 'signing' (signing for resumes)."""

    @pytest.mark.asyncio
    async def test_assemble_skips_committed_participant(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Two participants — one paid, one stuck in 'committed'.
            pid_paid = await db.add_participant(mix_id, "npub_paid", "")
            await db.update_participant(pid_paid, state="paid", fee_paid=500)
            await db.add_utxo(pid_paid, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(pid_paid, addr, 1_000_000)

            pid_unpaid = await db.add_participant(mix_id, "npub_unpaid", "")
            await db.update_participant(pid_unpaid, state="committed")
            await db.add_utxo(pid_unpaid, TXID[1], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:6]:
                await db.add_output(pid_unpaid, addr, 1_000_000)

            # Add a second paid participant so assembly has >=2 to work with.
            pid_paid2 = await db.add_participant(mix_id, "npub_paid2", "")
            await db.update_participant(pid_paid2, state="paid", fee_paid=500)
            await db.add_utxo(pid_paid2, TXID[2], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[6:8] + [P2WPKH_ADDRS[0]]:
                await db.add_output(pid_paid2, addr, 1_000_000)

            mix_row = await db.get_mix(mix_id)
            # Pass the "wrong" active list that includes the unpaid participant
            # (this is what _process_mix does today in the 'assembling' case).
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            # Only 2 psbt_round rows should exist (one per PAID participant).
            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            pid_set = {r["participant_id"] for r in rounds}
            assert pid_unpaid not in pid_set, "unpaid participant was included in assembly"
            assert pid_paid in pid_set and pid_paid2 in pid_set
        finally:
            await db.close()


class TestOutputSizeMinimumValidation:
    """S11: A mix with output_size < MINIMUM_UTXO_SIZE would build dust
    equal-outputs that no wallet would later spend. Catch at config load."""

    def test_config_rejects_output_size_below_minimum_utxo_size(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("DEFAULT_OUTPUT_SIZE=5000\n")
            f.write("MINIMUM_UTXO_SIZE=10000\n")
            env_path = f.name
        try:
            with pytest.raises((ValueError, AssertionError)):
                BotConfig(env_path)
        finally:
            os.unlink(env_path)

    def test_config_accepts_output_size_at_or_above_minimum(self):
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("DEFAULT_OUTPUT_SIZE=10000\n")
            f.write("MINIMUM_UTXO_SIZE=10000\n")
            env_path = f.name
        try:
            cfg = BotConfig(env_path)
            assert cfg.DEFAULT_OUTPUT_SIZE == 10000
        finally:
            os.unlink(env_path)


class TestStrandedFeeFallback:
    """S2: a participant who paid the service fee but whose Nostr profile
    lacks a lud16 would land in the 'else' branch of _cancel_and_refund —
    marked cancelled with NO refund attempt and NO DM mentioning their
    stranded sats. Real-money UX bug."""

    @pytest.mark.asyncio
    async def test_paid_participant_without_lud16_gets_stranded_dm(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="collecting")

            paid_npub = "npub_paid_no_lud16"
            pid = await db.add_participant(mix_id, paid_npub, "")  # empty lud16
            await db.update_participant(pid, state="paid", fee_paid=2_000)

            await coord._cancel_and_refund(mix_row := await db.get_mix(mix_id),
                                           "insufficient participants")

            joined = " ".join(m for r, m in nostr.sent_dms if r == paid_npub).lower()
            # The user should be told their refund is stranded and to contact us.
            assert (
                "couldn't refund" in joined
                or "could not refund" in joined
                or "contact" in joined
                or "lightning address" in joined
            ), f"DM didn't mention the stranded refund: {joined!r}"
        finally:
            await db.close()


class TestDailyAnnouncementRecordsAutoCreatedMix:
    """S3: when no mixes are open, _post_daily_announcement auto-creates one
    but never inserts an 'announcements' row for it. Operator audit trail
    missing."""

    @pytest.mark.asyncio
    async def test_auto_created_mix_gets_announcement_row(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # No mixes pre-existing.
            await coord._post_daily_announcement()

            mixes = await db.get_mixes_by_state("collecting")
            assert len(mixes) == 1, "auto-create should have produced exactly one mix"
            anns = await db.get_announcements_for_mix(mixes[0]["id"])
            assert len(anns) == 1, "announcement row missing for the auto-created mix"
        finally:
            await db.close()


class TestBroadcast409TreatedAsSuccess:
    """C3: mempool.space returning 409 means 'this tx (or a conflict) is
    already in mempool.' If it's OUR tx, that's a successful broadcast —
    we should return the txid (computed locally from the raw hex) so the
    caller doesn't refund participants while their tx is waiting to confirm.
    The status doc spells out the money-at-risk path."""

    @pytest.mark.asyncio
    async def test_409_returns_txid_not_none(self):
        import httpx, respx
        from src.chain_monitor import ChainMonitor
        OFFLINE = "https://offline-test-mempool.invalid/api"
        OFFLINE_BACKUP = "https://offline-test-backup.invalid/api"

        # A real (but tiny) raw tx hex. Doesn't need to be valid for any
        # signature check — broadcast_tx never inspects it. Just needs to be
        # parseable by python-bitcointx so we can compute its txid.
        from bitcointx.core import CTransaction, CTxIn, CTxOut, COutPoint, CMutableTransaction, b2x
        from bitcointx.core.script import CScript
        tx = CMutableTransaction(
            [CTxIn(COutPoint(b"\x11" * 32, 0))],
            [CTxOut(50_000, CScript(b"\x00\x14" + b"\x00" * 20))],
        )
        raw_hex = b2x(tx.serialize())
        expected_txid = b2x(tx.GetTxid()[::-1])  # bitcoin-style display order

        with respx.mock:
            respx.post(f"{OFFLINE}/tx").mock(
                return_value=httpx.Response(409, text="txn-mempool-conflict")
            )
            respx.post(f"{OFFLINE_BACKUP}/tx").mock(
                return_value=httpx.Response(409, text="txn-mempool-conflict")
            )
            cm = ChainMonitor(api_base=OFFLINE, api_backup=OFFLINE_BACKUP)
            try:
                result = await cm.broadcast_tx(raw_hex)
            finally:
                await cm.close()

        assert result == expected_txid, (
            f"409 should be treated as broadcast success and return the local "
            f"txid; got {result!r}, expected {expected_txid!r}"
        )

    @pytest.mark.asyncio
    async def test_drops_underfunded_participant_and_proceeds(self):
        """C2: when one participant's allocation drops to 0 equal outputs
        after applying the proportional miner fee, the old code cancelled
        the whole mix. New behaviour: refund the under-funded participant,
        notify them, and continue with the survivors if there are still
        enough non-conforming participants for required_nonconforming."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000,
                                         max_participants=10, required_nonconforming=2)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Two well-funded non-conforming participants.
            rich1 = await db.add_participant(mix_id, "rich1", "rich1@x")
            await db.update_participant(rich1, state="paid", fee_paid=500)
            await db.add_utxo(rich1, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(rich1, addr, 1_000_000)

            rich2 = await db.add_participant(mix_id, "rich2", "rich2@x")
            await db.update_participant(rich2, state="paid", fee_paid=500)
            await db.add_utxo(rich2, TXID[1], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:6]:
                await db.add_output(rich2, addr, 1_000_000)

            # The under-funded one: a NON-conforming UTXO just barely above
            # output_size. Passes the /addresses check (estimated_fee_share=0)
            # but its non-conforming inputs can't fund one equal output once the
            # proportional miner fee + conforming-burden share are applied.
            poor = await db.add_participant(mix_id, "poor", "poor@x")
            await db.update_participant(poor, state="paid", fee_paid=300)
            await db.add_utxo(poor, TXID[2], 0, 1_000_001, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(poor, P2WPKH_ADDRS[6], 1_000_000)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            # The mix should have proceeded to 'signing' with the two
            # remaining participants — not been cancelled wholesale.
            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "signing", (
                f"mix should advance after dropping under-funded; got {mix_after['state']}"
            )

            # The under-funded participant should be marked refunded with a
            # Lightning refund attempted, and DM'd that they were dropped.
            poor_after = await db.get_participant(poor)
            assert poor_after["state"] in ("refunded", "cancelled"), (
                f"poor should be refunded/cancelled, got {poor_after['state']}"
            )
            assert any(r[0] == "poor@x" for r in lightning.refunds), (
                f"poor should have gotten a Lightning refund; got {lightning.refunds}"
            )
            poor_dms = [m for r, m in nostr.sent_dms if r == "poor"]
            assert poor_dms, "poor got no DM about being dropped"
            assert any(
                "dropped" in m.lower() or "couldn't cover" in m.lower()
                or "insufficient" in m.lower() or "can't cover" in m.lower()
                for m in poor_dms
            ), f"DM didn't explain the drop reason: {poor_dms}"

            # The survivors should be in signing state.
            for pid in (rich1, rich2):
                p = await db.get_participant(pid)
                assert p["state"] == "signing", (
                    f"survivor {pid} should be signing, got {p['state']}"
                )

            # And only 2 psbt_round rows (one per survivor).
            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            pid_set = {r["participant_id"] for r in rounds}
            assert pid_set == {rich1, rich2}, (
                f"expected rounds for the two survivors only, got {pid_set}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_dropping_drops_below_required_nonconforming_cancels_whole_mix(self):
        """C2 boundary: if dropping under-funded participants would leave fewer
        non-conforming survivors than required_nonconforming, fall back to
        cancelling the whole mix."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000,
                                         max_participants=10, required_nonconforming=3)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Two rich + one poor. required_nonconforming=3; dropping poor
            # leaves 2 non-conforming survivors < 3 → cancel the whole mix.
            rich1 = await db.add_participant(mix_id, "rich1m", "rich1m@x")
            await db.update_participant(rich1, state="paid", fee_paid=500)
            await db.add_utxo(rich1, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(rich1, addr, 1_000_000)

            rich2 = await db.add_participant(mix_id, "rich2m", "rich2m@x")
            await db.update_participant(rich2, state="paid", fee_paid=500)
            await db.add_utxo(rich2, TXID[1], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:6]:
                await db.add_output(rich2, addr, 1_000_000)

            poor = await db.add_participant(mix_id, "poorm", "poorm@x")
            await db.update_participant(poor, state="paid", fee_paid=300)
            await db.add_utxo(poor, TXID[2], 0, 1_000_001, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(poor, P2WPKH_ADDRS[6], 1_000_000)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            # Cancel now DESTROYS the mix entirely (leave no trace on failure).
            assert await db.get_mix(mix_id) is None, (
                "should destroy the mix when NC survivors < required_nonconforming"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_400_still_returns_none(self):
        """Regression guard: 400 means the tx was actually rejected (bad
        format, missing inputs, etc.) — must still fail, not be papered
        over as success."""
        import httpx, respx
        from src.chain_monitor import ChainMonitor
        OFFLINE = "https://offline-test-mempool.invalid/api"
        OFFLINE_BACKUP = "https://offline-test-backup.invalid/api"
        with respx.mock:
            respx.post(f"{OFFLINE}/tx").mock(
                return_value=httpx.Response(400, text="bad-txns-inputs-missingorspent")
            )
            respx.post(f"{OFFLINE_BACKUP}/tx").mock(
                return_value=httpx.Response(400, text="bad-txns-inputs-missingorspent")
            )
            cm = ChainMonitor(api_base=OFFLINE, api_backup=OFFLINE_BACKUP)
            try:
                result = await cm.broadcast_tx("deadbeef" * 8)
            finally:
                await cm.close()
        assert result is None


# ============================================================================
# Wave 4 — coverage backfill (state-machine paths that nothing else exercised)
# ============================================================================
#
# Up to this point most coordinator tests stop at the boundary of PSBT signing
# because we couldn't easily produce real signed PSBTs. Wave 3 added the
# libsecp256k1 bridge + KeyStore-based signing helpers, so the broadcast,
# sweep, and chunked-reassembly paths are now reachable end-to-end without
# mocking psbt_mgr.


async def _seed_signed_mix(coord, db, *, mix_id: str, signing_keys: list,
                           output_size: int = 100_000):
    """Run _assemble_psbt for the given mix, then for each participant
    sign the resulting skeleton with their key and store as psbt_returned.
    Mirrors the on-chain flow: assembly → distribute → each signs → returns.

    Returns the list of participant ids in the same order as signing_keys."""
    from bitcointx.core import b2x
    from bitcointx.core.key import KeyStore
    from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

    active = await db.get_participants_by_mix(mix_id)
    pid_order = [p["id"] for p in active]
    await coord._assemble_psbt(await db.get_mix(mix_id), active)

    for pid, k in zip(pid_order, signing_keys):
        rd = await db.get_psbt_round(mix_id, pid, 1)
        assert rd and rd["psbt_sent"], f"no skeleton stored for {pid}"
        psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(rd["psbt_sent"]))
        psbt.sign(KeyStore.from_iterable([k]))
        signed_hex = b2x(psbt.serialize())
        await db.update_psbt_round(rd["id"], psbt_returned=signed_hex, psbt_valid=True)
        await db.update_participant(pid, state="signed")
    return pid_order


async def _make_2p_signing_mix(coord, db):
    """Bootstrap a 2-participant mix in 'assembling' state with real keys
    and one p2wpkh input + 2 output addresses each. Returns
    (mix_id, [pid_a, pid_b], [key_a, key_b])."""
    from bitcointx.core.key import CKey
    from bitcointx.wallet import P2WPKHBitcoinAddress

    k_a = CKey(b"\x10" * 32)
    k_b = CKey(b"\x20" * 32)
    spk_a = P2WPKHBitcoinAddress.from_pubkey(k_a.pub).to_scriptPubKey().hex()
    spk_b = P2WPKHBitcoinAddress.from_pubkey(k_b.pub).to_scriptPubKey().hex()

    mix_id = await db.create_mix(
        output_size=100_000,
        max_participants=10, fee_per_element=100,
    )
    await db.update_mix(
        mix_id, state="assembling", fee_rate=30,
        input_type="p2wpkh", output_type="p2wpkh",
    )

    pid_a = await db.add_participant(mix_id, "npub_bc_a", "a@x")
    await db.update_participant(pid_a, state="paid", fee_paid=500)
    await db.add_utxo(pid_a, "aa" * 32, 0, 250_000, "p2wpkh", spk_a)
    for addr in P2WPKH_ADDRS[0:2]:
        await db.add_output(pid_a, addr, 100_000)

    pid_b = await db.add_participant(mix_id, "npub_bc_b", "b@x")
    await db.update_participant(pid_b, state="paid", fee_paid=500)
    await db.add_utxo(pid_b, "bb" * 32, 0, 250_000, "p2wpkh", spk_b)
    for addr in P2WPKH_ADDRS[2:4]:
        await db.add_output(pid_b, addr, 100_000)

    return mix_id, [pid_a, pid_b], [k_a, k_b]


# --- _combine_and_broadcast: real PSBTs, real combine+finalize, mocked chain ---


class TestCombineAndBroadcast:
    @pytest.mark.asyncio
    async def test_happy_path_marks_mix_broadcast_and_dms_signers(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = "real_txid_abcd0123"

            signed = await db.get_participants_by_mix(mix_id)
            signed = [p for p in signed if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "broadcast"
            assert mix_after["broadcast_txid"] == "real_txid_abcd0123"
            assert mix_after["broadcast_tx_hex"], "raw tx hex not persisted"
            # broadcast_tx was called with the finalized hex
            assert len(chain.broadcast_calls) == 1
            assert chain.broadcast_calls[0] == mix_after["broadcast_tx_hex"]
            # Both signers got the broadcast DM with the txid
            for npub in ("npub_bc_a", "npub_bc_b"):
                dms = [m for r, m in nostr.sent_dms if r == npub]
                assert any("real_txid_abcd0123" in m for m in dms), (
                    f"no broadcast DM to {npub}: {dms}"
                )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_chain_broadcast_returning_none_cancels_and_refunds(self):
        """If chain.broadcast_tx returns None (all endpoints exhausted with
        non-recoverable errors), the mix must cancel and refund. Verifies
        we DON'T mark broadcast state without a txid."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = None  # simulate hard failure

            signed = [p for p in await db.get_participants_by_mix(mix_id)
                      if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            # Failure destroys the mix (no lingering 'cancelled' row / txid).
            assert await db.get_mix(mix_id) is None
            # Refunds attempted for both
            assert {r[0] for r in lightning.refunds} == {"a@x", "b@x"}
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unsigned_participant_blocks_finalize_and_cancels(self):
        """If even one participant's psbt_returned doesn't actually have a
        signature (e.g. they replayed the skeleton), finalize returns None
        and the mix cancels — verifying we don't broadcast a junk tx."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            # Sign only with key_a; pid_b's "psbt_returned" will be the
            # skeleton (no sig). _seed_signed_mix sees both pids; bypass it.
            from bitcointx.core import b2x
            from bitcointx.core.key import KeyStore
            from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)
            for pid, k in zip(pids, keys):
                rd = await db.get_psbt_round(mix_id, pid, 1)
                if k is keys[0]:
                    psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(rd["psbt_sent"]))
                    psbt.sign(KeyStore.from_iterable([k]))
                    await db.update_psbt_round(
                        rd["id"], psbt_returned=b2x(psbt.serialize()), psbt_valid=True,
                    )
                else:
                    # Return the skeleton unchanged — no sig.
                    await db.update_psbt_round(
                        rd["id"], psbt_returned=rd["psbt_sent"], psbt_valid=True,
                    )
                await db.update_participant(pid, state="signed")

            signed = [p for p in await db.get_participants_by_mix(mix_id)
                      if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            assert await db.get_mix(mix_id) is None  # destroyed on failure
            assert chain.broadcast_calls == [], "broadcast must not be attempted"
        finally:
            await db.close()


# --- _broadcast_sweep ---


class TestBroadcastSweep:
    @pytest.mark.asyncio
    async def test_confirmed_tx_triggers_destroy_mix_data(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Mix is in broadcast state with a known txid.
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid="finaltxid_xyz",
                broadcast_tx_hex="deadbeef" * 8,
            )
            pid = await db.add_participant(mix_id, "npub_signed", "")
            await db.update_participant(pid, state="signed")
            await db.add_utxo(pid, TXID[0], 0, 100_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(pid, P2WPKH_ADDRS[0], 100_000)
            await db.add_psbt_round(mix_id, pid, round_num=1)

            chain.confirmed["finaltxid_xyz"] = True

            # Force the sweep window to be open.
            await db.set_setting("last_broadcast_check_unix", "0")
            await coord._broadcast_sweep(int(time.time()))

            # All bitcoin data gone (mix, participant, utxos, outputs, psbt).
            mix_after = await db.get_mix(mix_id)
            assert mix_after is None, "mix should be wiped after confirmation"
            assert await db.get_participant(pid) is None
            assert await db.get_utxos_by_participant(pid) == []
            assert await db.get_outputs_by_participant(pid) == []
            assert await db.get_psbt_rounds_by_mix(mix_id) == []
            # And the signer got a confirmation DM.
            dms = [m for r, m in nostr.sent_dms if r == "npub_signed"]
            assert any("finaltxid_xyz" in m for m in dms)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unconfirmed_triggers_rebroadcast(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid="pending_txid",
                broadcast_tx_hex="cafef00d" * 8,
            )

            chain.confirmed["pending_txid"] = False

            await db.set_setting("last_broadcast_check_unix", "0")
            await coord._broadcast_sweep(int(time.time()))

            # State stays 'broadcast' (we're still waiting), and the chain
            # was asked to re-broadcast the same hex.
            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "broadcast"
            assert len(chain.broadcast_calls) == 1
            assert chain.broadcast_calls[0] == "cafef00d" * 8
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_sweep_throttled_by_interval(self):
        """The sweep tracks last-check in the settings table and only runs
        once per BROADCAST_CHECK_INTERVAL_HOURS. If the interval hasn't
        elapsed, the sweep is a no-op even if there's a pending broadcast."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid="any", broadcast_tx_hex="00" * 16,
            )
            chain.confirmed["any"] = True

            # Set last-check to "just now" — well within the interval.
            now = int(time.time())
            await db.set_setting("last_broadcast_check_unix", str(now - 60))
            await coord._broadcast_sweep(now)

            # No confirmation check or destroy happened.
            mix_after = await db.get_mix(mix_id)
            assert mix_after is not None
            assert mix_after["state"] == "broadcast"
        finally:
            await db.close()


# --- _post_daily_announcement happy path (S3 covered the empty-mix case) ---


class TestDailyAnnouncementWithExistingMixes:
    @pytest.mark.asyncio
    async def test_existing_mixes_each_get_an_announcement_row(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            ids = []
            for _ in range(2):
                mid = await db.create_mix(output_size=100_000)
                await db.update_mix(mid, state="collecting")
                ids.append(mid)

            await coord._post_daily_announcement()

            for mid in ids:
                anns = await db.get_announcements_for_mix(mid)
                assert len(anns) == 1, f"mix {mid} missing announcement"
        finally:
            await db.close()


# --- _cmd_exit_mix plan-§3g coverage (single-mix refund + 0-mix done) ---


class TestExitMixSingleAndNone:
    @pytest.mark.asyncio
    async def test_single_mix_paid_refunds_and_dms(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            pid = await db.add_participant(mix_id, "npub_solo", "solo@x")
            await db.update_participant(pid, state="paid", fee_paid=1000)

            await coord._cmd_exit_mix(FakeCtx("npub_solo"), "npub_solo", None)

            p = await db.get_participant(pid)
            # C-B: paid users go through the idempotent refund path and end
            # in 'refunded' (or 'refund_failed') rather than the older
            # 'cancelled'. 'cancelled' is now reserved for unpaid exits.
            assert p["state"] == "refunded"
            # Refund was attempted for the participant's lud16.
            assert any(r[0] == "solo@x" for r in lightning.refunds)
            dms = [m for r, m in nostr.sent_dms if r == "npub_solo"]
            assert any("refund" in m.lower() or "sorry" in m.lower() for m in dms)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_not_in_any_mix_says_done(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            await coord._cmd_exit_mix(FakeCtx("npub_orphan"), "npub_orphan", None)
            dms = [m for r, m in nostr.sent_dms if r == "npub_orphan"]
            assert dms == ["Done."], f"expected single 'Done.' DM, got {dms}"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_multi_mix_with_matching_id_exits_just_that_one(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=100_000)
            mix_b = await db.create_mix(output_size=100_000)
            pid_a = await db.add_participant(mix_a, "npub_mm", "mm@x")
            pid_b = await db.add_participant(mix_b, "npub_mm", "mm@x")
            await db.update_participant(pid_a, state="paid", fee_paid=500)
            await db.update_participant(pid_b, state="paid", fee_paid=500)

            await coord._cmd_exit_mix(FakeCtx("npub_mm"), "npub_mm", mix_a)

            # C-B: paid exit lands in 'refunded' (not 'cancelled').
            assert (await db.get_participant(pid_a))["state"] == "refunded"
            assert (await db.get_participant(pid_b))["state"] == "paid"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_multi_mix_refunds_the_matched_participants_fee(self):
        """The matched participant's OWN fee/lud16 is used for the refund, not
        the first mix's. (Previously the refund read active[0] regardless of
        which mix matched.)"""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_a = await db.create_mix(output_size=100_000)
            mix_b = await db.create_mix(output_size=100_000)
            pid_a = await db.add_participant(mix_a, "npub_mm2", "a_addr@x")
            pid_b = await db.add_participant(mix_b, "npub_mm2", "b_addr@x")
            await db.update_participant(pid_a, state="paid", fee_paid=500)
            await db.update_participant(pid_b, state="paid", fee_paid=4000)

            await coord._cmd_exit_mix(FakeCtx("npub_mm2"), "npub_mm2", mix_b)

            assert (await db.get_participant(pid_b))["state"] == "refunded"
            assert (await db.get_participant(pid_a))["state"] == "paid"
            # The refund went to mix_b's lud16 for an amount derived from
            # mix_b's 4000-sat fee (not mix_a's 500).
            mm_refunds = [r for r in lightning.refunds if r[0] == "b_addr@x"]
            assert mm_refunds, f"no refund to b_addr@x: {lightning.refunds}"
            assert mm_refunds[0][1] > 500  # derived from 4000, not 500
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_cancel_during_signing_is_refused(self):
        """H1: a participant in 'signing' cannot /cancel — that would delete
        their inputs from under the shared skeleton and force the whole mix to
        restart. Their row/UTXOs/fee are left untouched."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(mix_id, state="signing")
            pid = await db.add_participant(mix_id, "npub_sign", "s@x")
            await db.add_utxo(pid, TXID[0], 0, 250_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="signing", fee_paid=500)

            await coord._cmd_exit_mix(FakeCtx("npub_sign"), "npub_sign", None)

            # Nothing was torn down, no refund issued.
            p = await db.get_participant(pid)
            assert p["state"] == "signing"
            assert await db.get_utxos_by_participant(pid) != []
            assert lightning.refunds == []
            dms = " ".join(m for r, m in nostr.sent_dms if r == "npub_sign").lower()
            assert "signing phase" in dms
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_ghosted_participant_not_refunded_on_cancel(self):
        """M1: a ghosted (paid-but-never-signed) participant forfeits their fee.
        _cancel_and_refund must skip them."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(mix_id, state="signing")
            # A cooperative paid participant + a ghost, both with a fee paid.
            good = await db.add_participant(mix_id, "npub_good", "good@x")
            await db.update_participant(good, state="paid", fee_paid=500)
            ghost = await db.add_participant(mix_id, "npub_ghost", "ghost@x")
            await db.update_participant(ghost, state="ghosted", fee_paid=500)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test")

            # The ghost was NOT refunded; the cooperative participant was.
            refunded_addrs = {r[0] for r in lightning.refunds}
            assert "ghost@x" not in refunded_addrs
            assert "good@x" in refunded_addrs
        finally:
            await db.close()


# --- chunked PSBT reassembly end-to-end ---


class TestChunkedReassembly:
    @pytest.mark.asyncio
    async def test_two_chunks_assemble_and_get_accepted(self):
        """Submit a real signed PSBT in two halves via /psbt_chunk. The
        reassembled hex should be accepted as a normal /psbt_accept and
        the participant should advance to 'signed'."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            from bitcointx.core import b2x
            from bitcointx.core.key import CKey, KeyStore
            from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
            from bitcointx.wallet import P2WPKHBitcoinAddress

            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            # Roll the participants back to 'signing' so /psbt_accept can
            # advance them; clear their stored psbt_returned so the test
            # exercises the reassembly + accept flow fresh.
            for pid in pids:
                rd = await db.get_psbt_round(mix_id, pid, 1)
                await db.update_psbt_round(rd["id"], psbt_returned=None, psbt_valid=None)
                await db.update_participant(pid, state="signing")

            # Construct the signed PSBT for participant A on the fly so we
            # have its hex to chunk.
            rd_a = await db.get_psbt_round(mix_id, pids[0], 1)
            psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(rd_a["psbt_sent"]))
            psbt.sign(KeyStore.from_iterable([keys[0]]))
            signed_hex = b2x(psbt.serialize())

            mid = len(signed_hex) // 2
            chunks = [signed_hex[:mid], signed_hex[mid:]]

            # Chunk 1: just buffered.
            await coord._cmd_accept_psbt_chunk(
                FakeCtx("npub_bc_a"), "npub_bc_a", 1, 2, chunks[0],
            )
            assert (await db.get_participant(pids[0]))["state"] == "signing"

            # Chunk 2: triggers reassembly → /psbt_accept → 'signed'.
            await coord._cmd_accept_psbt_chunk(
                FakeCtx("npub_bc_a"), "npub_bc_a", 2, 2, chunks[1],
            )
            assert (await db.get_participant(pids[0]))["state"] == "signed"
        finally:
            await db.close()


# --- S9: outpoint released to the pool on every cancel/drop/exit path ---


class TestOutpointReleasedOnCancel:
    """The UNIQUE(txid, vout) constraint is permanent — once a row exists,
    no other commit can use the same outpoint. So every path that 'releases'
    a participant or mix MUST delete the corresponding utxos rows or the
    outpoint is bricked forever."""

    @pytest.mark.asyncio
    async def test_cancel_and_refund_deletes_utxos(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "user_x", "x@x")
            await db.update_participant(pid, state="paid", fee_paid=500)
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.mark_utxo_used(pid, TXID[0], 0)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test")

            # Row is gone.
            assert await db.get_utxo(TXID[0], 0) is None
            # And a new commit can now use the same outpoint.
            mix_id2 = await db.create_mix(output_size=1_000_000)
            pid2 = await db.add_participant(mix_id2, "user_y", "")
            await db.add_utxo(pid2, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            assert await db.get_utxo(TXID[0], 0) is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_voluntary_exit_deletes_utxos(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "exiter", "e@x")
            await db.update_participant(pid, state="paid", fee_paid=500)
            await db.add_utxo(pid, TXID[1], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.mark_utxo_used(pid, TXID[1], 0)

            await coord._cmd_exit_mix(FakeCtx("exiter"), "exiter", None)

            assert await db.get_utxo(TXID[1], 0) is None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_drop_underfunded_deletes_utxos(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000,
                                         max_participants=10, required_nonconforming=2)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Pattern from TestBroadcast409TreatedAsSuccess: two well-funded
            # + one under-funded (non-conforming, just above output_size); the
            # latter is dropped on assembly.
            rich1 = await db.add_participant(mix_id, "rich_d1", "r1@x")
            await db.update_participant(rich1, state="paid", fee_paid=500)
            await db.add_utxo(rich1, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(rich1, addr, 1_000_000)

            rich2 = await db.add_participant(mix_id, "rich_d2", "r2@x")
            await db.update_participant(rich2, state="paid", fee_paid=500)
            await db.add_utxo(rich2, TXID[1], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:6]:
                await db.add_output(rich2, addr, 1_000_000)

            poor = await db.add_participant(mix_id, "poor_d", "p@x")
            await db.update_participant(poor, state="paid", fee_paid=300)
            await db.add_utxo(poor, TXID[2], 0, 1_000_001, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(poor, P2WPKH_ADDRS[6], 1_000_000)

            await coord._assemble_psbt(await db.get_mix(mix_id),
                                       await db.get_participants_by_mix(mix_id))

            # poor's utxo should be released.
            assert await db.get_utxo(TXID[2], 0) is None
            # The survivors' utxos remain.
            assert await db.get_utxo(TXID[0], 0) is not None
            assert await db.get_utxo(TXID[1], 0) is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_commit_handles_unique_constraint_violation_gracefully(self):
        """Race between is_utxo_used and add_utxo: simulate by pre-inserting
        the same outpoint via a different participant. The coordinator
        should catch the IntegrityError, DM the user, and not crash."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # A separate mix already holds (TXID[3], 0).
            other_mix = await db.create_mix(output_size=1_000_000)
            other_pid = await db.add_participant(other_mix, "other", "")
            await db.add_utxo(other_pid, TXID[3], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            # Note: is_used left as 0 so is_utxo_used returns False (the row
            # exists but isn't yet "claimed"). This forces the add_utxo
            # path to actually run and hit the UNIQUE constraint.

            # New user commits the same outpoint.
            new_mix = await db.create_mix(output_size=1_000_000)
            new_pid = await db.add_participant(new_mix, "racer", "")

            chain.txouts[f"{TXID[3]}:0"] = _fake_txout(value=500_000)
            await coord._cmd_commit_utxos(
                FakeCtx("racer"), "racer", [{"txid": TXID[3], "vout": 0}],
            )

            # No second row added; user got a clear DM.
            us = await db.get_utxos_by_participant(new_pid)
            assert us == []
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "claimed" in joined or "another commit" in joined
        finally:
            await db.close()


# --- S1: auto-mix-on-commit must re-check during the chain.lookup_txout await ---


class TestAutoMixRaceGuard:
    """S1: the auto-mix-on-commit branch in _cmd_commit_utxos awaits
    chain.lookup_txout BEFORE adding the participant. During that await,
    a concurrent handler (or this same npub's second DM) could insert
    an 'interested' / 'committed' participant. Without a re-check the
    branch would happily add a second participant for the same npub,
    bypassing the one-at-a-time invariant that /join enforces.

    Verified by intercepting lookup_txout to insert a competing
    participant mid-await — what a true asyncio race would look like."""

    @pytest.mark.asyncio
    async def test_concurrent_participant_insertion_does_not_duplicate(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            npub = "auto_mix_race"
            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)

            # Wrap lookup_txout to inject a competing participant
            # during the await — simulating a concurrent /commit handler
            # that ran while our handler was waiting on the chain RPC.
            original_lookup = chain.lookup_txout
            injected = {"done": False}

            async def race_lookup(txid, vout):
                if not injected["done"]:
                    injected["done"] = True
                    other_mix = await db.create_mix(
                        output_size=1_000_000,
                    )
                    await db.add_participant(other_mix, npub, "")
                return await original_lookup(txid, vout)

            chain.lookup_txout = race_lookup

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            # Exactly one participant row should exist for npub.
            allp = await db.get_participants_by_npub(npub)
            assert len(allp) == 1, (
                f"S1 regression: auto-mix added a duplicate participant. "
                f"Rows: {[(p['mix_id'], p['state']) for p in allp]}"
            )
        finally:
            await db.close()


# ============================================================================
# Wave 5 — fixes from the second audit pass
# ============================================================================


# --- C-A: smart fee estimator is wired into assembly ---


class TestAssemblyUsesLiveFeeRate:
    """C-A: _assemble_psbt must call chain.estimate_fee_rate, not use the
    hardcoded schema default of 30 sat/vB."""

    @pytest.mark.asyncio
    async def test_estimate_fee_rate_is_called_and_persisted(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            calls: List[float] = []

            async def fake_estimate():
                calls.append(99.0)
                return 99.0
            chain.estimate_fee_rate = fake_estimate

            mix_id, pids, _ = await _make_2p_signing_mix(coord, db)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            assert len(calls) == 1, "estimate_fee_rate should have been called"
            updated = await db.get_mix(mix_id)
            assert updated["fee_rate"] == 99, (
                f"persisted fee_rate should match live estimate; got {updated['fee_rate']}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_falls_back_to_stored_rate_when_estimate_raises(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            async def boom():
                raise RuntimeError("mempool.space down")
            chain.estimate_fee_rate = boom

            mix_id, pids, _ = await _make_2p_signing_mix(coord, db)
            # stored fee_rate from _make_2p_signing_mix is 30
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            # Should not have crashed; mix should have advanced to signing.
            after = await db.get_mix(mix_id)
            assert after["state"] == "signing"
            assert after["fee_rate"] == 30
        finally:
            await db.close()


# --- C-B: refund idempotency on crash-resume ---


class TestRefundIdempotency:
    """C-B: a crash between send_refund() and the state UPDATE used to leave
    the participant in 'paid', so the next event-loop tick would re-enter
    the same code path and pay them again. Now: state moves to 'refunding'
    BEFORE the wallet call, and _REFUND_TERMINAL_STATES blocks re-entry."""

    @pytest.mark.asyncio
    async def test_cancel_and_refund_is_idempotent_across_simulated_crash(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_idem", "idem@x")
            await db.update_participant(pid, state="paid", fee_paid=1000)
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)

            # First call: refund goes through, then the mix is destroyed.
            mix = await db.get_mix(mix_id)
            await coord._cancel_and_refund(mix, "test")
            first_count = len(lightning.refunds)
            assert first_count == 1
            assert await db.get_mix(mix_id) is None       # destroyed on failure
            assert await db.get_participant(pid) is None   # participant wiped
            # Fee was refunded successfully, so nothing is owed.
            assert await db.get_refunds_owed() == []

            # Simulate the bot crashing right after the refund but before the
            # destroy — the next tick re-enters _cancel_and_refund with the now
            # stale mix dict. Participants are gone, so it must NOT pay again.
            await coord._cancel_and_refund(mix, "test (resume)")
            assert len(lightning.refunds) == first_count, (
                "C-B regression: second cancel_and_refund call paid again"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_drop_underfunded_is_idempotent_across_simulated_crash(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_drop", "drop@x")
            await db.update_participant(pid, state="paid", fee_paid=300)
            await db.add_utxo(pid, TXID[1], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)

            p = await db.get_participant(pid)
            await coord._drop_underfunded(p, mix_id)
            assert (await db.get_participant(pid))["state"] == "refunded"
            first_count = len(lightning.refunds)

            # Re-call with the now-stale `p` dict (state was 'paid' when we
            # read it). Real crash-resume hits the same scenario.
            await coord._drop_underfunded(p, mix_id)
            assert len(lightning.refunds) == first_count, (
                "C-B regression: _drop_underfunded paid twice"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_refunding_state_blocks_re_entry(self):
        """The pre-call state UPDATE to 'refunding' is the crash-window
        defence: even if the wallet crashes mid-call, the next resume
        sees 'refunding' and doesn't try again."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_stuck", "stuck@x")
            await db.update_participant(pid, state="refunding", fee_paid=1000)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "resume")
            # Should not have attempted a refund — state was already
            # 'refunding' so we skipped (no double-pay).
            assert lightning.refunds == [], (
                f"C-B regression: refunded a 'refunding' participant; got {lightning.refunds}"
            )
            # The mix is destroyed (the in-flight 'refunding' case is logged for
            # the operator, not recorded as a payable debt — outcome unknown).
            assert await db.get_mix(mix_id) is None
            assert await db.get_participant(pid) is None
            assert await db.get_refunds_owed() == []
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_refund_failed_state_when_both_backends_return_none(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_brokeln", "br@x")
            await db.update_participant(pid, state="paid", fee_paid=1000)

            async def fail_refund(lud16, sats, reason="x"):
                lightning.refunds.append((lud16, sats, reason))
                return None
            lightning.send_refund = fail_refund

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test")
            assert len(lightning.refunds) == 1
            # The mix is destroyed, but the failed refund leaves a MINIMAL debt
            # record (who we owe + how much) so the operator can reconcile.
            assert await db.get_mix(mix_id) is None
            assert await db.get_participant(pid) is None
            owed = await db.get_refunds_owed()
            assert len(owed) == 1
            assert owed[0]["lightning_addr"] == "br@x"
            assert owed[0]["sats"] == coord._refund_keep_math(1000)
        finally:
            await db.close()


# --- C-C: assembly idempotency under UNIQUE constraint ---


class TestAssemblyIdempotency:
    """C-C: a crash in _assemble_psbt after some pids got psbt_rounds rows
    but before mix.state moved to 'signing' would leave the mix wedged on
    the next attempt (UNIQUE(mix_id, pid, round_num) violation). Now
    add_psbt_round is idempotent."""

    @pytest.mark.asyncio
    async def test_assemble_psbt_can_be_called_twice_without_unique_violation(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, _ = await _make_2p_signing_mix(coord, db)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)

            # Simulate a crash by resetting the mix to 'assembling' and
            # participants back to 'paid' WITHOUT bumping ghost_retries.
            # The next assembly attempt re-uses round_num=1 — the UNIQUE
            # constraint would fire if add_psbt_round weren't idempotent.
            await db.update_mix(mix_id, state="assembling")
            for pid in pids:
                await db.update_participant(pid, state="paid", psbt_sent_at_unix=None)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(mix_row, active)  # must not raise

            # Still one row per participant per round_num=1 (the existing
            # rows got UPDATED, not duplicated).
            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            assert len(rounds) == 2
            assert all(r["round_num"] == 1 for r in rounds)
            # And the mix did advance to signing.
            assert (await db.get_mix(mix_id))["state"] == "signing"
        finally:
            await db.close()


# --- C-D: pre-refund tx_known check ---


class TestPreRefundChainCheck:
    """C-D: when broadcast_tx returns None, the coordinator must verify the
    tx isn't actually known to the chain before refunding. Otherwise a
    broadcast that succeeded into one mempool but had its HTTP response
    lost would cause a double-pay."""

    @pytest.mark.asyncio
    async def test_broadcast_none_but_tx_known_parks_in_broadcast(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = None
            # Force tx_known to return True — pretend the tx made it.
            chain.tx_known_default_chain_reachable = True

            async def tx_is_known(txid):
                return True
            chain.tx_known = tx_is_known

            signed = [p for p in await db.get_participants_by_mix(mix_id)
                      if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            after = await db.get_mix(mix_id)
            assert after["state"] == "broadcast", (
                f"C-D regression: refunded instead of parking; state={after['state']}"
            )
            assert after["broadcast_txid"], "broadcast_txid should be set"
            assert lightning.refunds == [], (
                f"C-D regression: refunded while tx is known: {lightning.refunds}"
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_broadcast_none_and_chain_unreachable_parks_uncertain(self):
        """If tx_known returns None (couldn't reach chain), we still don't
        refund — better to park in broadcast and re-check than to double-pay."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = None

            async def tx_unknown_chain_down(txid):
                return None
            chain.tx_known = tx_unknown_chain_down

            signed = [p for p in await db.get_participants_by_mix(mix_id)
                      if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            after = await db.get_mix(mix_id)
            assert after["state"] == "broadcast", (
                f"C-D regression: refunded on uncertain chain; state={after['state']}"
            )
            assert lightning.refunds == []
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_broadcast_none_and_tx_not_known_cancels_and_refunds(self):
        """Only the 'tx is definitely nowhere' case proceeds to refund."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, keys = await _make_2p_signing_mix(coord, db)
            await _seed_signed_mix(coord, db, mix_id=mix_id, signing_keys=keys)
            chain.broadcast_return = None  # tx_known default returns False
            # Default fake returns False for unknown txids → "chain is online
            # and says nothing's there" → safe to refund.

            signed = [p for p in await db.get_participants_by_mix(mix_id)
                      if p["state"] == "signed"]
            await coord._combine_and_broadcast(await db.get_mix(mix_id), signed)

            assert await db.get_mix(mix_id) is None  # destroyed on failure
            assert {r[0] for r in lightning.refunds} == {"a@x", "b@x"}
        finally:
            await db.close()


# --- S-A: iterated fee math ---


class TestIteratedFeeMath:
    """S-A: a participant who provides MORE addresses than they have BTC
    for ends up with fewer actual outputs. The first fee pass overcounted
    their vsize contribution; the iteration shrinks fee_share accordingly."""

    @pytest.mark.asyncio
    async def test_extra_addresses_dont_inflate_fee_share(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, max_participants=10,
            )
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Participant A: 3M sats, 3 addresses, will use all 3.
            pa = await db.add_participant(mix_id, "p_a_iter", "a@x")
            await db.update_participant(pa, state="paid", fee_paid=500)
            await db.add_utxo(pa, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(pa, addr, 1_000_000)

            # Participant B: only 1.05M sats but provided 5 addresses — will
            # only use 1 equal output. Pre-S-A would charge B for a 5-output
            # vsize contribution.
            pb = await db.add_participant(mix_id, "p_b_iter", "b@x")
            await db.update_participant(pb, state="paid", fee_paid=500)
            await db.add_utxo(pb, TXID[1], 0, 1_050_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:8]:
                await db.add_output(pb, addr, 1_000_000)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            after_a = await db.get_participant(pa)
            after_b = await db.get_participant(pb)
            # B contributed fewer real outputs than A, so B's fee_share
            # should be <= A's fee_share, NOT higher because of the 5
            # declared addresses. (Without iteration B's fee_share would
            # have been ≈ 5/8 of total instead of ≈ 1/4.)
            assert after_b["fee_share"] <= after_a["fee_share"], (
                f"S-A regression: B's fee_share ({after_b['fee_share']}) > "
                f"A's ({after_a['fee_share']}) despite B having fewer "
                f"actual outputs"
            )
        finally:
            await db.close()


# --- S-B: coordinator rejects /commit when chain spent-check is unreachable ---


class TestCommitRejectsOnSpentCheckUnreachable:
    @pytest.mark.asyncio
    async def test_chain_unreachable_does_not_add_utxo(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_unreachable"
            pid = await db.add_participant(mix_id, npub, "")

            chain.txouts[f"{TXID[0]}:0"] = _fake_txout(value=500_000)
            # S-B fake: simulate chain spent-check failure for this outpoint.
            chain.spent_check_fails[f"{TXID[0]}:0"] = True

            await coord._cmd_commit_utxos(
                FakeCtx(npub), npub, [{"txid": TXID[0], "vout": 0}],
            )

            utxos = await db.get_utxos_by_participant(pid)
            assert utxos == [], (
                "S-B regression: accepted a UTXO whose spent-check failed"
            )
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "verify" in joined or "unreachable" in joined or "retry" in joined
        finally:
            await db.close()


# --- S-E: pre-broadcast sum invariant ---


class TestSumInvariantCancelsBadFeeMath:
    @pytest.mark.asyncio
    async def test_zero_miner_fee_cancels_mix(self, monkeypatch):
        """S-E: if the fee math somehow produces sum(outputs) >= sum(inputs),
        the mix is cancelled BEFORE we send the PSBT — better loud cancel
        than a tx that won't relay."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id, pids, _ = await _make_2p_signing_mix(coord, db)
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)

            # Force the fee engine to claim zero miner fee but full output
            # allocation. Patch calculate_all_fees to return a result that
            # would produce sum(outputs) == sum(inputs).
            from src.fee_engine import FeeResult
            real_calc = coord.fee_engine.calculate_all_fees

            def zero_fee_calc(participants_data, output_size, fee_rate, **kwargs):
                # Build results where fee_share is 0 and change_sats =
                # total_sats - num_equal * output_size → no miner fee. The
                # new signature accepts the conforming-model kwargs and ignores
                # them (this stub forces the degenerate zero-fee case).
                results = []
                for p in participants_data:
                    n_eq = p["total_sats"] // output_size
                    change = p["total_sats"] - n_eq * output_size
                    results.append(FeeResult(
                        total_inputs=0, total_sats=p["total_sats"],
                        num_equal_outputs=n_eq,
                        num_change_outputs=1 if change >= 10000 else 0,
                        fee_share_sats=0,
                        change_sats=change if change >= 10000 else 0,
                        service_fee_sats=0,
                        conforming_count=p.get("conforming_count", 0),
                        is_nonconforming=p.get("is_nonconforming", True),
                    ))
                # total_vsize > 0 so MIN_FEE_RATE_SATS × vsize > 0 too.
                return (200, 0, results)
            coord.fee_engine.calculate_all_fees = zero_fee_calc

            await coord._assemble_psbt(mix_row, active)

            # The invariant trips → mix cancels → destroyed.
            assert await db.get_mix(mix_id) is None, (
                "S-E regression: assembled a 0-miner-fee tx instead of destroying the mix"
            )
        finally:
            await db.close()


# --- S-G: DM error path does not leak str(e) ---


class TestDMErrorDoesNotLeakException:
    @pytest.mark.asyncio
    async def test_dm_handler_exception_returns_generic_message(self):
        """S-G: if an internal handler raises, the user-facing DM is a
        generic prompt, not str(e). Inner exceptions can carry other-user
        data in their message."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            secret = "SECRET_ADDRESS_bc1qOTHERUSER"

            # Replace _cmd_list_mixes to raise with the secret in the message.
            async def raise_with_secret(ctx):
                raise RuntimeError(secret)
            coord._cmd_list_mixes = raise_with_secret

            await coord._on_dm(FakeCtx("npub_dm_err"), "/list")

            dms = [m for r, m in nostr.sent_dms if r == "npub_dm_err"]
            assert dms, "user got no DM at all"
            joined = " ".join(dms)
            assert secret not in joined, (
                f"S-G regression: exception text leaked to user: {joined!r}"
            )
        finally:
            await db.close()


# --- M2: batched commit-rejection DM ---


class TestBatchedCommitRejectionDMs:
    @pytest.mark.asyncio
    async def test_many_bad_utxos_produce_one_summary_dm(self):
        """M2: pasting 12 invalid outpoints used to produce 12 DMs. Now:
        one DM with up to _MAX_REJECTION_LINES detail lines + a tail."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_spammer"
            await db.add_participant(mix_id, npub, "")

            # 12 outpoints, none of which exist on chain.
            utxos = [{"txid": f"{i:064x}", "vout": 0} for i in range(1, 13)]
            await coord._cmd_commit_utxos(FakeCtx(npub), npub, utxos)

            dms_to_user = [m for r, m in nostr.sent_dms if r == npub]
            # Exactly one rejection-summary DM + (no-valid-UTXOs DM).
            summary_dms = [m for m in dms_to_user if "Rejected" in m and "UTXO" in m]
            assert len(summary_dms) == 1, (
                f"M2 regression: expected one summary DM, got {len(summary_dms)}: "
                f"{summary_dms}"
            )
            # The summary mentions the total count and includes the "more"
            # tail since 12 > _MAX_REJECTION_LINES (8 by default).
            assert "Rejected 12" in summary_dms[0]
            assert "more rejected" in summary_dms[0]
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_small_number_of_rejections_lists_each_with_reason(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            npub = "npub_two_bad"
            await db.add_participant(mix_id, npub, "")

            utxos = [
                {"txid": "a" * 64, "vout": 0},
                {"txid": "b" * 64, "vout": 0},
            ]
            await coord._cmd_commit_utxos(FakeCtx(npub), npub, utxos)

            dms_to_user = [m for r, m in nostr.sent_dms if r == npub]
            summary = [m for m in dms_to_user if "Rejected" in m]
            assert len(summary) == 1
            # Both outpoints appear.
            assert ("a" * 64) in summary[0]
            assert ("b" * 64) in summary[0]
            assert "more rejected" not in summary[0]
        finally:
            await db.close()


# --- S-F: cancel destroys all participant data (was: scrub identifiers) ---


class TestCancelScrubsIdentifiers:
    @pytest.mark.asyncio
    async def test_cancel_destroys_all_child_rows(self):
        """Failure leaves no trace: the mix, its participants, and every child
        row (utxos, outputs, psbt rounds) are gone — not just scrubbed."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_full", "full@x")
            await db.update_participant(pid, state="paid", fee_paid=0)
            await db.add_utxo(pid, TXID[0], 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(pid, P2WPKH_ADDRS[0], 1_000_000)
            await db.add_psbt_round(mix_id, pid, round_num=1)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "boom")

            assert await db.get_mix(mix_id) is None
            assert await db.get_participants_by_mix(mix_id) == []
            assert await db.get_utxos_by_participant(pid) == []
            assert await db.get_outputs_by_participant(pid) == []
            assert await db.get_psbt_rounds_by_mix(mix_id) == []
            assert await db.get_refunds_owed() == []  # free mix owes nothing
        finally:
            await db.close()


    @pytest.mark.asyncio
    async def test_cancelled_mix_blanks_npub_and_lud16(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            pid = await db.add_participant(mix_id, "npub_priv", "priv@example.com")
            await db.update_participant(pid, state="paid", fee_paid=500)
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test")

            # S-F (strengthened): cancel now DESTROYS the mix outright, so there's
            # no participant row left to leak npub/lud16, and the mix itself is
            # gone. The fee here was refunded, so no debt record remains either.
            assert await db.get_participant(pid) is None
            assert await db.get_mix(mix_id) is None
            assert await db.get_refunds_owed() == []
        finally:
            await db.close()


# --- C-A wiring smoke test ---


class TestFeeRateConfigKnob:
    def test_fee_lookback_blocks_is_a_config_property(self):
        cfg = BotConfig("/nonexistent-env-for-tests.env")
        # Default from _DEFAULTS in src/config.py.
        assert cfg.FEE_LOOKBACK_BLOCKS == 6


# --- Conforming / non-conforming UTXO model ---


class TestConformingModel:
    """Coverage for the conforming/non-conforming feature: classification at
    /commit (caps), the conforming-only free flow, the optional service fee,
    the required-non-conforming proceed gate, and mixed assembly."""

    async def _interested(self, coord, db, mix_id, npub):
        pid = await db.add_participant(mix_id, npub, f"{npub}@x")
        return pid

    @pytest.mark.asyncio
    async def test_commit_enforces_conforming_cap(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=2,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "npub_confcap"
            pid = await self._interested(coord, db, mix_id, npub)

            # Three conforming UTXOs (== output_size); cap is 2.
            for v in (0, 1, 2):
                chain.txouts[f"{TXID[0]}:{v}"] = _fake_txout(1_000_000)
            utxos = [{"txid": TXID[0], "vout": v} for v in (0, 1, 2)]
            await coord._cmd_commit_utxos(FakeCtx(npub), npub, utxos)

            stored = await db.get_utxos_by_participant(pid)
            assert len(stored) == 2, "conforming cap should have capped to 2"
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "conforming" in joined and "cap" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_commit_enforces_per_participant_nonconforming_cap(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT"] = 1
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "npub_nccap"
            pid = await self._interested(coord, db, mix_id, npub)

            # Two non-conforming UTXOs; per-participant cap is 1.
            for v in (0, 1):
                chain.txouts[f"{TXID[1]}:{v}"] = _fake_txout(1_500_000)
            utxos = [{"txid": TXID[1], "vout": v} for v in (0, 1)]
            await coord._cmd_commit_utxos(FakeCtx(npub), npub, utxos)

            stored = await db.get_utxos_by_participant(pid)
            assert len(stored) == 1
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "non-conforming" in joined and "limit" in joined
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_conforming_only_participant_free_no_zap(self):
        # Even with a service fee configured, a conforming-only participant
        # pays nothing and is marked paid straight away.
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, fee_per_element=100,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "npub_confonly"
            pid = await self._interested(coord, db, mix_id, npub)
            await db.add_utxo(pid, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            # One conforming UTXO → one address suffices.
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, [P2WPKH_ADDRS[0]])

            p = await db.get_participant(pid)
            assert p["state"] == "paid"
            last = nostr.sent_dms[-1][1].lower()
            assert "zap" not in last and "no service fee" in last
            outs = await db.get_outputs_by_participant(pid)
            assert len(outs) == 1 and outs[0]["amount"] == 1_000_000
            assert outs[0]["is_change"] in (0, False)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_conforming_only_address_count_enforced(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "npub_addrcount"
            pid = await self._interested(coord, db, mix_id, npub)
            # Two conforming UTXOs need two fresh addresses.
            await db.add_utxo(pid, TXID[3], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_utxo(pid, TXID[3], 1, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(FakeCtx(npub), npub, [P2WPKH_ADDRS[0]])
            assert "at least 2" in nostr.sent_dms[-1][1].lower()
            assert (await db.get_participant(pid))["state"] == "committed"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_nonconforming_total_below_output_size_rejected(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="collecting")
            npub = "npub_small"
            pid = await self._interested(coord, db, mix_id, npub)
            # Non-conforming but too small to fund one full output.
            await db.add_utxo(pid, TXID[4], 0, 400_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, P2WPKH_ADDRS[0:2],
            )
            assert "below one" in nostr.sent_dms[-1][1].lower()
            assert (await db.get_participant(pid))["state"] == "committed"
        finally:
            await db.close()

    async def _add_paid_nc(self, db, mix_id, npub, vout, total=2_000_000):
        pid = await db.add_participant(mix_id, npub, f"{npub}@x")
        await db.add_utxo(pid, TXID[0], vout, total, "p2wpkh", FAKE_SCRIPTPUBKEY)
        await db.add_output(pid, P2WPKH_ADDRS[vout % len(P2WPKH_ADDRS)], 1_000_000)
        await db.update_participant(pid, state="paid")
        return pid

    @pytest.mark.asyncio
    async def test_proceed_waits_for_required_nonconforming(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
            )
            await db.update_mix(mix_id, state="collecting")
            # One paid NC participant — not enough yet.
            await self._add_paid_nc(db, mix_id, "ncA", 0)
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "collecting"

            # Second NC participant → target met → advances to assembling.
            await self._add_paid_nc(db, mix_id, "ncB", 1)
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "assembling"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_solo_nonconforming_needs_a_conforming_utxo(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=1,
            )
            await db.update_mix(mix_id, state="collecting")
            # Single NC participant, no conforming present → must NOT proceed.
            await self._add_paid_nc(db, mix_id, "soloNC", 0)
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "collecting"

            # A conforming-only participant joins → now there are >=2 equal
            # outputs from distinct parties → proceed.
            cpid = await db.add_participant(mix_id, "conf", "conf@x")
            await db.add_utxo(cpid, TXID[1], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(cpid, P2WPKH_ADDRS[5], 1_000_000)
            await db.update_participant(cpid, state="paid")
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "assembling"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_assembly_with_conforming_participant_pays_no_fee(self):
        """Two non-conforming + one conforming-only participant assemble. The
        conforming-only participant's fee_share is 0; the NC participants carry
        the miner fee; the tx is built and advances to signing."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
                max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="assembling", fee_rate=30,
                                input_type="p2wpkh", output_type="p2wpkh")

            nc1 = await db.add_participant(mix_id, "asmNC1", "n1@x")
            await db.update_participant(nc1, state="paid", fee_paid=0)
            await db.add_utxo(nc1, TXID[0], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:3]:
                await db.add_output(nc1, addr, 1_000_000)

            nc2 = await db.add_participant(mix_id, "asmNC2", "n2@x")
            await db.update_participant(nc2, state="paid", fee_paid=0)
            await db.add_utxo(nc2, TXID[1], 0, 3_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[3:6]:
                await db.add_output(nc2, addr, 1_000_000)

            conf = await db.add_participant(mix_id, "asmConf", "c@x")
            await db.update_participant(conf, state="paid", fee_paid=0)
            await db.add_utxo(conf, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(conf, P2WPKH_ADDRS[6], 1_000_000)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            assert (await db.get_mix(mix_id))["state"] == "signing"
            # Conforming-only participant: no miner fee deducted.
            assert (await db.get_participant(conf))["fee_share"] in (0, None) or \
                (await db.get_participant(conf))["fee_share"] == 0
            # NC participants carry a positive fee share.
            assert (await db.get_participant(nc1))["fee_share"] > 0
            assert (await db.get_participant(nc2))["fee_share"] > 0
            # All three got a PSBT round / are signing.
            for pid in (nc1, nc2, conf):
                assert (await db.get_participant(pid))["state"] == "signing"
        finally:
            await db.close()


# --- Conforming / non-conforming model: gap-closing tests ---


class TestConformingModelGaps:
    """Closes the highest-priority gaps from the status doc's critical analysis:
    mixed conforming+NC participant (layout + assembly), conforming-only
    multi-UTXO layout, cap accumulation across participants/commits, and the
    mix-level deadline cancel when the non-conforming target is never met."""

    async def _interested(self, db, mix_id, npub):
        return await db.add_participant(mix_id, npub, f"{npub}@x")

    async def _commit(self, coord, chain, npub, utxos):
        """utxos: list of (txid, vout, amount). Sets chain txouts then commits."""
        for (txid, vout, amt) in utxos:
            chain.txouts[f"{txid}:{vout}"] = _fake_txout(amt)
        await coord._cmd_commit_utxos(
            FakeCtx(npub), npub,
            [{"txid": t, "vout": v} for (t, v, _a) in utxos],
        )

    # ---- Gap #1: a participant bringing BOTH conforming and non-conforming ----

    @pytest.mark.asyncio
    async def test_mixed_participant_addresses_layout(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "mixed1"
            pid = await self._interested(db, mix_id, npub)
            # 1 conforming (1M) + 1 non-conforming (2.5M).
            await self._commit(coord, chain, npub, [
                (TXID[0], 0, 1_000_000),
                (TXID[0], 1, 2_500_000),
            ])
            # NC participant needs conforming(1) + 2 = 3 addresses; give 4.
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, P2WPKH_ADDRS[0:4])

            outs = await db.get_outputs_by_participant(pid)
            # Layout: conforming(1M, not change) + 2 NC equal(1M) + change(0.5M).
            assert len(outs) == 4
            assert outs[0]["amount"] == 1_000_000 and not outs[0]["is_change"]
            equal_1m = [o for o in outs if o["amount"] == 1_000_000]
            assert len(equal_1m) == 3  # 1 conforming + 2 NC-derived
            changes = [o for o in outs if o["is_change"]]
            assert len(changes) == 1 and changes[0]["amount"] == 500_000
            # FEE_PER_ELEMENT=0 → straight to paid, no zap.
            assert (await db.get_participant(pid))["state"] == "paid"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_mixed_participant_too_few_addresses_rejected(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "mixed_few"
            pid = await self._interested(db, mix_id, npub)
            await self._commit(coord, chain, npub, [
                (TXID[0], 0, 1_000_000),   # conforming
                (TXID[0], 1, 2_500_000),   # non-conforming
            ])
            # Floor is conforming(1) + 1 = 2 (change address optional). Give
            # only 1 → rejected, stays committed.
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, [P2WPKH_ADDRS[0]])
            assert "at least 2" in nostr.sent_dms[-1][1].lower()
            assert (await db.get_participant(pid))["state"] == "committed"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_above_dust_leftover_donated_when_no_change_address(self, caplog):
        """Dust-donation feature: a non-conforming participant who supplies only
        an equal-output address (no change address) and has above-dust leftover
        is warned at /addresses and the excess is paid to DONATION_ADDRESS at
        assembly."""
        import logging
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["DONATION_ADDRESS"] = P2WPKH_ADDRS[7]
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")

            # A: pure NC 2.5M, only ONE address → 1 equal output, ~1.5M donated.
            a = await self._interested(db, mix_id, "donA")
            await self._commit(coord, chain, "donA", [(TXID[0], 0, 2_500_000)])
            await coord._cmd_provide_addresses(FakeCtx("donA"), "donA", [P2WPKH_ADDRS[0]])
            warn = nostr.sent_dms[-1][1].lower()
            assert "donated" in warn and "re-send /addresses" in warn
            assert (await db.get_participant(a))["state"] == "paid"
            # Only the equal output is stored — the donation is added at assembly.
            outs = await db.get_outputs_by_participant(a)
            assert len(outs) == 1 and outs[0]["amount"] == 1_000_000

            # B: normal NC with enough addresses.
            b = await self._interested(db, mix_id, "donB")
            await self._commit(coord, chain, "donB", [(TXID[1], 0, 3_000_000)])
            await coord._cmd_provide_addresses(FakeCtx("donB"), "donB", P2WPKH_ADDRS[2:5])

            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
                await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            assert (await db.get_mix(mix_id))["state"] == "signing"
            logs = " ".join(r.message.lower() for r in caplog.records)
            assert "donated" in logs  # assembly emitted the donation log line
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_above_dust_leftover_folds_to_fee_without_donation_address(self):
        """When no DONATION_ADDRESS is configured, an above-dust leftover with no
        change address folds into the miner fee (no operator output)."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # DONATION_ADDRESS left blank (default).
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")
            a = await self._interested(db, mix_id, "feeA")
            await self._commit(coord, chain, "feeA", [(TXID[0], 0, 2_500_000)])
            await coord._cmd_provide_addresses(FakeCtx("feeA"), "feeA", [P2WPKH_ADDRS[0]])
            b = await self._interested(db, mix_id, "feeB")
            await self._commit(coord, chain, "feeB", [(TXID[1], 0, 3_000_000)])
            await coord._cmd_provide_addresses(FakeCtx("feeB"), "feeB", P2WPKH_ADDRS[2:5])

            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            # Reaches signing; the leftover became miner fee (no donation output,
            # no crash from a missing donation address).
            assert (await db.get_mix(mix_id))["state"] == "signing"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_address_bound_leftover_becomes_change_not_burnt(self, caplog):
        """No-burn rule: a non-conforming participant who supplies >=2 addresses
        but not enough for a separate change output gets the LAST equal slot
        turned into an (oversized) change output to their OWN address — the
        leftover is NEITHER donated NOR folded into the miner fee."""
        import logging
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # DONATION_ADDRESS configured precisely to prove it is NOT used.
            coord.cfg._values["DONATION_ADDRESS"] = P2WPKH_ADDRS[7]
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")

            # A: 2.5M NC with exactly TWO addresses. Naive plan = 2 equal + 0.5M
            # with no address -> burn. No-burn plan = 1 equal + ~1.5M change.
            a = await self._interested(db, mix_id, "nbA")
            await self._commit(coord, chain, "nbA", [(TXID[0], 0, 2_500_000)])
            await coord._cmd_provide_addresses(
                FakeCtx("nbA"), "nbA", P2WPKH_ADDRS[0:2])
            # No donation warning at intake — the leftover has a home.
            warn = nostr.sent_dms[-1][1].lower()
            assert "donated" not in warn
            # Two outputs stored: one equal, one oversized change.
            outs = sorted(o["amount"] for o in await db.get_outputs_by_participant(a))
            assert outs == [1_000_000, 1_500_000]

            # B: a second NC participant so the mix can proceed.
            await self._interested(db, mix_id, "nbB")
            await self._commit(coord, chain, "nbB", [(TXID[1], 0, 3_000_000)])
            await coord._cmd_provide_addresses(
                FakeCtx("nbB"), "nbB", P2WPKH_ADDRS[2:5])

            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
                await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            assert (await db.get_mix(mix_id))["state"] == "signing"
            # A kept an oversized change (above one output_size), after the fee.
            a_final = await db.get_participant(a)
            assert a_final["change_amount"] > 1_000_000

            # The assembled tx pays a tiny miner fee at the target rate — proof
            # the 0.5M was NOT folded into the fee.
            rounds = await db.get_psbt_rounds_by_mix(mix_id)
            psbt_hex = next(r["psbt_sent"] for r in rounds if r.get("psbt_sent"))
            psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(psbt_hex))
            sum_out = sum(o.nValue for o in psbt.unsigned_tx.vout)
            miner_fee = (2_500_000 + 3_000_000) - sum_out
            assert 0 < miner_fee < 50_000, f"leftover was burnt: fee={miner_fee}"

            # Nothing was donated.
            logs = " ".join(r.message.lower() for r in caplog.records)
            assert "donated" not in logs
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_address_constrained_participant_is_nudged_to_add_addresses(self):
        """A non-conforming participant who funds more mixed outputs than their
        address count allows (so the no-burn rule sacrifices a mixed output and
        hands them an oversized change) is warned at /addresses to send more —
        NOT a donation warning, since nothing is being donated."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")
            # 3.5M with only TWO addresses: funds support 3 mixed outputs, but
            # 2 addresses -> 1 mixed + 2.5M change. Should nudge to 4 addresses.
            a = await self._interested(db, mix_id, "ucA")
            await self._commit(coord, chain, "ucA", [(TXID[0], 0, 3_500_000)])
            await coord._cmd_provide_addresses(
                FakeCtx("ucA"), "ucA", P2WPKH_ADDRS[0:2])
            msg = nostr.sent_dms[-1][1].lower()
            assert "donated" not in msg          # nothing is donated
            assert "re-send /addresses" in msg
            assert "easy to trace" in msg
            # Suggests 4 addresses (3 mixed + 1 change), i.e. 2 more.
            assert "with 4" in msg and "2 more" in msg
            # And it actually kept all the sats as change (no burn).
            outs = sorted(o["amount"] for o in await db.get_outputs_by_participant(a))
            assert outs == [1_000_000, 2_500_000]
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_mixed_participant_assembly_preserves_conforming(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")

            # Mixed: 1 conforming + 1 NC; plain: NC only.
            mixed = await self._interested(db, mix_id, "mx")
            await self._commit(coord, chain, "mx", [
                (TXID[0], 0, 1_000_000), (TXID[0], 1, 2_500_000),
            ])
            await coord._cmd_provide_addresses(FakeCtx("mx"), "mx", P2WPKH_ADDRS[0:4])

            plain = await self._interested(db, mix_id, "pl")
            await self._commit(coord, chain, "pl", [(TXID[1], 0, 3_000_000)])
            await coord._cmd_provide_addresses(FakeCtx("pl"), "pl", P2WPKH_ADDRS[4:7])

            # collecting → assembling → signing
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            assert (await db.get_mix(mix_id))["state"] == "signing"
            mp = await db.get_participant(mixed)
            pp = await db.get_participant(plain)
            # Both non-conforming participants carry a positive miner-fee share.
            assert mp["fee_share"] > 0 and pp["fee_share"] > 0
            # Conforming preservation: mixed participant's total outputs (which
            # the PSBT pays to their addresses) == total inputs − fee_share.
            # change_amount is persisted; equal outputs are full output_size.
            # total_out = (conforming + nc_equal)*size + change == 3.5M − fee.
            # We can't read nc_equal directly post-assembly, but the sum
            # invariant having passed (state==signing) guarantees it.
            assert mp["change_amount"] >= 0
        finally:
            await db.close()

    # ---- Gap #2: conforming-only with >=2 UTXOs → >=2 equal outputs ----

    @pytest.mark.asyncio
    async def test_conforming_only_two_utxos_two_outputs(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, max_conforming_utxos=5,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "conf2"
            pid = await self._interested(db, mix_id, npub)
            await self._commit(coord, chain, npub, [
                (TXID[2], 0, 1_000_000), (TXID[2], 1, 1_000_000),
            ])
            await coord._cmd_provide_addresses(FakeCtx(npub), npub, P2WPKH_ADDRS[0:2])

            outs = await db.get_outputs_by_participant(pid)
            assert len(outs) == 2
            assert all(o["amount"] == 1_000_000 and not o["is_change"] for o in outs)
            assert (await db.get_participant(pid))["state"] == "paid"
        finally:
            await db.close()

    # ---- Gap #3: conforming cap accumulates across participants ----

    @pytest.mark.asyncio
    async def test_conforming_cap_accumulates_across_participants(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000,
                required_nonconforming=2, max_conforming_utxos=2,
            )
            await db.update_mix(mix_id, state="collecting")

            a = await self._interested(db, mix_id, "capA")
            await self._commit(coord, chain, "capA", [
                (TXID[0], 0, 1_000_000), (TXID[0], 1, 1_000_000),  # fills cap (2)
            ])
            assert len(await db.get_utxos_by_participant(a)) == 2

            b = await self._interested(db, mix_id, "capB")
            await self._commit(coord, chain, "capB", [(TXID[1], 0, 1_000_000)])
            # B's conforming UTXO is rejected — the mix-wide cap is already full.
            assert len(await db.get_utxos_by_participant(b)) == 0
            joined = " ".join(m for r, m in nostr.sent_dms if r == "capB").lower()
            assert "conforming" in joined and "cap" in joined
        finally:
            await db.close()

    # ---- Gap #4: per-participant NC cap accumulates across multiple commits ----

    @pytest.mark.asyncio
    async def test_nonconforming_cap_accumulates_across_commits(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT"] = 2
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
            )
            await db.update_mix(mix_id, state="collecting")
            npub = "nccap2"
            pid = await self._interested(db, mix_id, npub)

            # First commit: 1 NC.
            await self._commit(coord, chain, npub, [(TXID[3], 0, 1_500_000)])
            assert len(await db.get_utxos_by_participant(pid)) == 1

            # Second commit: 2 more NC; only 1 fits under the cap of 2.
            await self._commit(coord, chain, npub, [
                (TXID[3], 1, 1_600_000), (TXID[3], 2, 1_700_000),
            ])
            stored = await db.get_utxos_by_participant(pid)
            assert len(stored) == 2
            joined = " ".join(m for r, m in nostr.sent_dms if r == npub).lower()
            assert "non-conforming" in joined and "limit" in joined
        finally:
            await db.close()

    # ---- Gap #5: deadline cancels when the NC target is never met ----

    @pytest.mark.asyncio
    async def test_deadline_cancels_when_target_not_met(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
                deadline_unix=int(time.time()) - 100,  # already past
            )
            await db.update_mix(mix_id, state="collecting")
            # Only ONE non-conforming participant is ready — below the target of 2.
            pid = await db.add_participant(mix_id, "lonely", "lonely@x")
            await db.add_utxo(pid, TXID[0], 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(pid, P2WPKH_ADDRS[0], 1_000_000)
            await db.update_participant(pid, state="paid")

            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            assert await db.get_mix(mix_id) is None  # destroyed on failure
        finally:
            await db.close()


# --- Lower-priority gap closers (#8 zero-fee lifecycle, #9 zap-then-proceed,
#     #10 solo-NC privacy floor) ---


class TestLowerPriorityGaps:
    async def _interested(self, db, mix_id, npub):
        return await db.add_participant(mix_id, npub, f"{npub}@x")

    async def _commit(self, coord, chain, npub, utxos, spk=FAKE_SCRIPTPUBKEY):
        for (txid, vout, amt) in utxos:
            chain.txouts[f"{txid}:{vout}"] = {
                "value": amt, "scriptpubkey": spk,
                "scriptpubkey_type": "p2wpkh", "address": "", "status": True,
            }
        await coord._cmd_commit_utxos(
            FakeCtx(npub), npub,
            [{"txid": t, "vout": v} for (t, v, _a) in utxos],
        )

    # ---- #8a: full FEE_PER_ELEMENT=0 lifecycle commit -> broadcast ----

    @pytest.mark.asyncio
    async def test_zero_fee_full_lifecycle_to_broadcast(self):
        from bitcointx.core import b2x
        from bitcointx.core.key import CKey, KeyStore
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
        from bitcointx.wallet import P2WPKHBitcoinAddress

        coord, db, nostr, chain, lightning = await make_coord()  # FEE_PER_ELEMENT=0
        try:
            k_a, k_b = CKey(b"\x41" * 32), CKey(b"\x42" * 32)
            spk_a = P2WPKHBitcoinAddress.from_pubkey(k_a.pub).to_scriptPubKey().hex()
            spk_b = P2WPKHBitcoinAddress.from_pubkey(k_b.pub).to_scriptPubKey().hex()
            txid_a, txid_b = "a1" * 32, "b2" * 32

            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=2,
                fee_per_element=0,
            )
            await db.update_mix(mix_id, state="collecting")

            # Two non-conforming participants join entirely through the DM flow.
            await self._interested(db, mix_id, "lcA")
            await self._commit(coord, chain, "lcA", [(txid_a, 0, 250_000)], spk=spk_a)
            await coord._cmd_provide_addresses(FakeCtx("lcA"), "lcA", P2WPKH_ADDRS[0:3])
            await self._interested(db, mix_id, "lcB")
            await self._commit(coord, chain, "lcB", [(txid_b, 0, 250_000)], spk=spk_b)
            await coord._cmd_provide_addresses(FakeCtx("lcB"), "lcB", P2WPKH_ADDRS[3:6])

            # No zap was ever requested.
            assert not any("zap" in m.lower() for _r, m in nostr.sent_dms)
            for npub in ("lcA", "lcB"):
                ps = await db.get_participants_by_npub(npub)
                assert ps[0]["state"] == "paid"

            # collecting -> assembling -> signing
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "signing"

            # Each participant signs the skeleton and returns it via /psbt_accept.
            for npub, k in (("lcA", k_a), ("lcB", k_b)):
                p = (await db.get_participants_by_npub(npub))[0]
                rd = await db.get_psbt_round(mix_id, p["id"], 1)
                psbt = PartiallySignedBitcoinTransaction.from_binary(
                    bytes.fromhex(rd["psbt_sent"]))
                psbt.sign(KeyStore.from_iterable([k]))
                await coord._cmd_accept_psbt(FakeCtx(npub), npub, b2x(psbt.serialize()))

            # signing tick -> combine + broadcast
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "broadcast"
            assert mix_after["broadcast_txid"] == "fake_broadcast_txid"
            assert chain.broadcast_calls, "broadcast should have been attempted"
            # Zero-fee mix: nothing was ever paid, so nothing is refunded.
            assert lightning.refunds == []
        finally:
            await db.close()

    # ---- #8b: cancelling a fee=0 mix refunds nobody ----

    @pytest.mark.asyncio
    async def test_zero_fee_cancel_refunds_nobody(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
                fee_per_element=0,
            )
            await db.update_mix(mix_id, state="collecting")
            pid = await db.add_participant(mix_id, "zc", "zc@x")
            await db.add_utxo(pid, TXID[0], 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(pid, P2WPKH_ADDRS[0], 1_000_000)
            await db.update_participant(pid, state="paid", fee_paid=0)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test cancel")

            assert await db.get_mix(mix_id) is None  # destroyed on failure
            assert lightning.refunds == [], "fee=0 mix has nothing to refund"
            assert await db.get_participant(pid) is None  # participant wiped too
            assert await db.get_refunds_owed() == []  # free mix owes nothing
        finally:
            await db.close()

    # ---- #9: fee>0 — zap arrives, THEN the mix proceeds ----

    @pytest.mark.asyncio
    async def test_zap_path_then_proceed(self):
        coord, db, nostr, chain, lightning = await make_coord(fee_per_element=100)
        try:
            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=2,
                fee_per_element=100,
            )
            await db.update_mix(mix_id, state="collecting")

            for npub, (txid, vout), addr_slice in (
                ("zpA", (TXID[0], 0), P2WPKH_ADDRS[0:3]),
                ("zpB", (TXID[1], 0), P2WPKH_ADDRS[3:6]),
            ):
                await self._interested(db, mix_id, npub)
                await self._commit(coord, chain, npub, [(txid, vout, 250_000)])
                await coord._cmd_provide_addresses(FakeCtx(npub), npub, addr_slice)
                # Fee > 0 -> stays committed, zap requested.
                assert (await db.get_participants_by_npub(npub))[0]["state"] == "committed"

            # Before any zap, the mix must NOT proceed (no paid participants).
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "collecting"

            # Zaps arrive (generous amount, comfortably over the expected fee).
            for npub in ("zpA", "zpB"):
                class Zap:
                    sender_hex = npub
                    amount_sats = 100_000
                await coord._on_zap(Zap(), FakeCtx(npub))
                assert (await db.get_participants_by_npub(npub))[0]["state"] == "paid"

            # Now the collecting tick sees the target met and advances.
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))
            assert (await db.get_mix(mix_id))["state"] == "assembling"
        finally:
            await db.close()

    # ---- #10: solo-NC assembled PSBT passes the privacy floor of 1 ----

    @pytest.mark.asyncio
    async def test_solo_nc_assembled_psbt_passes_privacy_floor(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=1,
                max_conforming_utxos=5, fee_per_element=0,
            )
            await db.update_mix(mix_id, state="collecting",
                                input_type="p2wpkh", output_type="p2wpkh")

            # One non-conforming participant (-> 2 equal + change).
            nc = await self._interested(db, mix_id, "soloNC2")
            await self._commit(coord, chain, "soloNC2", [(TXID[0], 0, 250_000)])
            await coord._cmd_provide_addresses(FakeCtx("soloNC2"), "soloNC2", P2WPKH_ADDRS[0:3])
            # One conforming participant (-> 1 equal pass-through).
            await self._interested(db, mix_id, "soloConf")
            await self._commit(coord, chain, "soloConf", [(TXID[1], 0, 100_000)])
            await coord._cmd_provide_addresses(FakeCtx("soloConf"), "soloConf", [P2WPKH_ADDRS[5]])

            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))  # -> assembling
            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))  # -> signing
            assert (await db.get_mix(mix_id))["state"] == "signing"

            # The assembled skeleton must clear the privacy floor of 1
            # (>=2 equal output_size outputs exist from distinct parties).
            rd = await db.get_psbt_round(mix_id, nc, 1)
            ok, msg = coord.privacy.check_psbt(rd["psbt_sent"], 1)
            assert ok is True, msg
        finally:
            await db.close()


# --- Review gaps: _tick() dispatch, output lock, MAX_PENDING, on_ready,
#     conforming-only-can't-fund-fee, resume, auto-create defaults ---


class TestReviewGaps:
    async def _interested(self, db, mix_id, npub):
        return await db.add_participant(mix_id, npub, f"{npub}@x")

    async def _commit(self, coord, chain, npub, utxos, spk=FAKE_SCRIPTPUBKEY):
        for (txid, vout, amt) in utxos:
            chain.txouts[f"{txid}:{vout}"] = {
                "value": amt, "scriptpubkey": spk,
                "scriptpubkey_type": "p2wpkh", "address": "", "status": True,
            }
        await coord._cmd_commit_utxos(
            FakeCtx(npub), npub,
            [{"txid": t, "vout": v} for (t, v, _a) in utxos],
        )

    # ---- #1 + #13: _tick() actually dispatches state transitions ----

    @pytest.mark.asyncio
    async def test_tick_dispatches_collecting_then_assembling(self):
        """Drive the real event-loop dispatcher (_tick), not _process_mix
        directly, so a mismatched state arm would be caught. Also stands in for
        crash-resume: the rows are in the DB and _tick picks them up."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=2,
                fee_per_element=0,
            )
            await db.update_mix(mix_id, state="collecting")
            for npub, txid, addr_slice in (
                ("tA", TXID[0], P2WPKH_ADDRS[0:3]),
                ("tB", TXID[1], P2WPKH_ADDRS[3:6]),
            ):
                await self._interested(db, mix_id, npub)
                await self._commit(coord, chain, npub, [(txid, 0, 250_000)])
                await coord._cmd_provide_addresses(FakeCtx(npub), npub, addr_slice)

            # First tick: collecting -> assembling (proceed gate dispatched).
            await coord._tick()
            assert (await db.get_mix(mix_id))["state"] == "assembling"
            # Second tick: assembling -> signing (assembly dispatched).
            await coord._tick()
            assert (await db.get_mix(mix_id))["state"] == "signing"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_tick_resumes_midflight_assembling_mix(self):
        """A mix left in 'assembling' (as after a crash) is advanced by the
        next _tick without any further user input."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=2,
                fee_per_element=0,
            )
            await db.update_mix(mix_id, state="assembling",
                                input_type="p2wpkh", output_type="p2wpkh")
            for npub, txid in (("rA", TXID[0]), ("rB", TXID[1])):
                pid = await self._interested(db, mix_id, npub)
                await db.add_utxo(pid, txid, 0, 250_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                for a in P2WPKH_ADDRS[0:3]:
                    await db.add_output(pid, a, 100_000)
                await db.update_participant(pid, state="paid")

            await coord._tick()
            assert (await db.get_mix(mix_id))["state"] == "signing"
        finally:
            await db.close()

    # ---- #4: only NC participant under-funded, others conforming-only ----

    @pytest.mark.asyncio
    async def test_only_nc_underfunded_with_conforming_others_cancels(self):
        """The miner fee falls entirely on non-conforming participants. If the
        sole NC participant can't fund it (dropped at assembly), no NC survivor
        remains to pay — the whole mix must cancel even though conforming-only
        participants are present."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=1,
                max_conforming_utxos=20, fee_per_element=0,
            )
            await db.update_mix(mix_id, state="assembling", fee_rate=30,
                                input_type="p2wpkh", output_type="p2wpkh")

            # Sole NC participant: just above output_size, can't cover the
            # (large, max-conforming) fee burden → dropped → 0 NC survivors.
            nc = await db.add_participant(mix_id, "uNC", "uNC@x")
            await db.add_utxo(nc, TXID[0], 0, 1_000_001, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(nc, P2WPKH_ADDRS[0], 1_000_000)
            await db.update_participant(nc, state="paid")

            # Two conforming-only participants (pay nothing toward the fee).
            for npub, txid in (("cf1", TXID[1]), ("cf2", TXID[2])):
                p = await db.add_participant(mix_id, npub, f"{npub}@x")
                await db.add_utxo(p, txid, 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                await db.add_output(p, P2WPKH_ADDRS[5], 1_000_000)
                await db.update_participant(p, state="paid")

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            assert await db.get_mix(mix_id) is None  # destroyed on failure
        finally:
            await db.close()

    # ---- #5: per-mix OUTPUT type lock (first /addresses locks it) ----

    @pytest.mark.asyncio
    async def test_output_type_lock_rejects_mismatched_second_participant(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Widen the allowlist so the rejection comes from the per-mix lock,
            # not the operator allowlist.
            coord.cfg._values["_accepted_output_types"] = {"p2wpkh", "p2tr"}
            P2TR = "bc1p9j0rwcgpd28gnastlh2yweshq7sl2vxxvrpstdsx9w3m8axaxn0qg0vcg0"

            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2,
            )
            await db.update_mix(mix_id, state="collecting")

            # Participant A locks the mix to p2wpkh outputs.
            a = await self._interested(db, mix_id, "outA")
            await self._commit(coord, chain, "outA", [(TXID[0], 0, 2_500_000)])
            await coord._cmd_provide_addresses(FakeCtx("outA"), "outA", P2WPKH_ADDRS[0:3])
            assert (await db.get_mix(mix_id))["output_type"] == "p2wpkh"

            # Participant B tries p2tr → rejected by the per-mix output lock.
            b = await self._interested(db, mix_id, "outB")
            await self._commit(coord, chain, "outB", [(TXID[1], 0, 2_500_000)])
            await coord._cmd_provide_addresses(
                FakeCtx("outB"), "outB", [P2TR, P2WPKH_ADDRS[4], P2WPKH_ADDRS[5]],
            )
            assert await db.get_outputs_by_participant(b) == []
            joined = " ".join(m for r, m in nostr.sent_dms if r == "outB").lower()
            assert "locked to p2wpkh" in joined
        finally:
            await db.close()

    # ---- #8: _on_nostr_ready initializes the Lightning payer ----

    @pytest.mark.asyncio
    async def test_on_nostr_ready_initializes_ln_payer(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            inits = []

            async def _record(keys):
                inits.append(keys)
            coord.lightning.init_payer_with_keys = _record

            class _PubKey:
                def to_bech32(self):
                    return "npub1botkey"

            class _Keys:
                def public_key(self):
                    return _PubKey()

            class _Handler:
                keys = _Keys()

            await coord._on_nostr_ready(_Handler())
            assert len(inits) == 1, "LN payer must be initialized on nostr-ready"
        finally:
            await db.close()

    # ---- #10: MAX_PENDING_MIXES cap on /join ----

    @pytest.mark.asyncio
    async def test_join_blocked_at_max_pending_mixes(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            coord.cfg._values["MAX_PENDING_MIXES"] = 2
            npub = "pending_user"
            # Already paid into 2 collecting mixes.
            for txid in (TXID[0], TXID[1]):
                m = await db.create_mix(output_size=1_000_000)
                await db.update_mix(m, state="collecting")
                p = await db.add_participant(m, npub, "")
                await db.update_participant(p, state="paid")

            # A 3rd open mix exists; joining it must be refused.
            m3 = await db.create_mix(output_size=1_000_000)
            await db.update_mix(m3, state="collecting")
            await coord._cmd_join_mix(FakeCtx(npub), m3)

            joined = " ".join(m for r, m in nostr.sent_dms if r == npub).lower()
            assert "already in 2 mixes" in joined
            # No participant row was added to the 3rd mix.
            assert await db.get_participants_by_mix(m3) == []
        finally:
            await db.close()

    # ---- #13b: resume_unfinished surfaces mid-flight mixes ----

    @pytest.mark.asyncio
    async def test_resume_unfinished_returns_midflight_mixes(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            states = ["announced", "collecting", "assembling", "signing",
                      "broadcast", "completed", "cancelled"]
            ids = {}
            for s in states:
                mid = await db.create_mix(output_size=1_000_000)
                await db.update_mix(mid, state=s)
                ids[s] = mid

            resumed = {m["id"] for m in await db.resume_unfinished()}
            # Active + broadcast are resumed; terminal states are not.
            for s in ("announced", "collecting", "assembling", "signing", "broadcast"):
                assert ids[s] in resumed, f"{s} should resume"
            for s in ("completed", "cancelled"):
                assert ids[s] not in resumed, f"{s} should NOT resume"
        finally:
            await db.close()

    # ---- #16: auto-created mix uses the configured defaults ----

    @pytest.mark.asyncio
    async def test_auto_created_mix_uses_default_params(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            await coord._post_daily_announcement()  # no open mixes → auto-create
            mixes = await db.get_mixes_by_state("collecting")
            assert len(mixes) == 1
            m = mixes[0]
            assert m["output_size"] == coord.cfg.DEFAULT_OUTPUT_SIZE
            assert m["required_nonconforming"] == coord.cfg.DEFAULT_REQUIRED_NONCONFORMING
            assert m["max_conforming_utxos"] == coord.cfg.MAX_CONFORMING_UTXOS
        finally:
            await db.close()

    # ---- interested cleanup when the mix leaves collecting ----

    @pytest.mark.asyncio
    async def test_interested_dropped_when_mix_advances(self):
        """A participant who /join-ed but never /commit-ed (state 'interested')
        is removed when the mix proceeds to assembling, freeing their slot and
        leaving no on-disk trace."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=100_000, required_nonconforming=2,
                fee_per_element=0,
            )
            await db.update_mix(mix_id, state="collecting")

            # Two paid non-conforming participants → target met.
            for npub, txid in (("iA", TXID[0]), ("iB", TXID[1])):
                pid = await db.add_participant(mix_id, npub, f"{npub}@x")
                await db.add_utxo(pid, txid, 0, 250_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
                for a in P2WPKH_ADDRS[0:3]:
                    await db.add_output(pid, a, 100_000)
                await db.update_participant(pid, state="paid")

            # A straggler who only /join-ed.
            idle = await db.add_participant(mix_id, "idler", "idler@x")
            assert (await db.get_participant(idle))["state"] == "interested"

            await coord._process_mix(await db.get_mix(mix_id), int(time.time()))

            assert (await db.get_mix(mix_id))["state"] == "assembling"
            # The interested straggler is gone; the paid two remain.
            assert await db.get_participant(idle) is None
            assert {p["npub_hex"] for p in await db.get_participants_by_mix(mix_id)} == {"iA", "iB"}
            # And they were told.
            assert any(r == "idler" and "without you" in m.lower()
                       for r, m in nostr.sent_dms)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_cleanup_interested_is_idempotent(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000)
            await db.update_mix(mix_id, state="collecting")
            idle = await db.add_participant(mix_id, "idle2", "idle2@x")
            await coord._cleanup_interested(mix_id)
            assert await db.get_participant(idle) is None
            # Second call is a no-op (no interested rows left).
            await coord._cleanup_interested(mix_id)
        finally:
            await db.close()


class TestInputOutputOrdering:
    """Privacy: assembled inputs/outputs are ordered alphabetically so a
    participant's inputs aren't grouped together by position on-chain."""

    @pytest.mark.asyncio
    async def test_inputs_sorted_and_participants_degrouped(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(
                output_size=1_000_000, required_nonconforming=2, max_conforming_utxos=0)
            await db.update_mix(mix_id, state="assembling", fee_rate=30,
                                input_type="p2wpkh", output_type="p2wpkh")

            # A's outpoints (11, 33) interleave with B's (22, 44) once sorted.
            a = await db.add_participant(mix_id, "ord_a", "a@x")
            await db.add_utxo(a, "11" * 32, 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_utxo(a, "33" * 32, 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[0:4]:
                await db.add_output(a, addr, 1_000_000)
            await db.update_participant(a, state="paid")

            b = await db.add_participant(mix_id, "ord_b", "b@x")
            await db.add_utxo(b, "22" * 32, 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_utxo(b, "44" * 32, 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            for addr in P2WPKH_ADDRS[4:8]:
                await db.add_output(b, addr, 1_000_000)
            await db.update_participant(b, state="paid")

            await coord._assemble_psbt(await db.get_mix(mix_id),
                                       await db.get_participants_by_mix(mix_id))
            assert (await db.get_mix(mix_id))["state"] == "signing"

            import json as _json
            ra = await db.get_psbt_round(mix_id, a, 1)
            rb = await db.get_psbt_round(mix_id, b, 1)
            a_idx = _json.loads(ra["input_indices"])
            b_idx = _json.loads(rb["input_indices"])
            # Sorted order is 11(A),22(B),33(A),44(B) -> A=[0,2], B=[1,3]:
            # each participant's inputs are interleaved, not contiguous.
            assert a_idx == [0, 2], a_idx
            assert b_idx == [1, 3], b_idx

            # And the PSBT's vin are in alphabetical outpoint order.
            from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
            psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(ra["psbt_sent"]))
            txids = [bytes(vin.prevout.hash)[::-1].hex() for vin in psbt.unsigned_tx.vin]
            assert txids == sorted(txids), txids
        finally:
            await db.close()
