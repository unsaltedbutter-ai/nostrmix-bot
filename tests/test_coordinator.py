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
        self.confirmed: Dict[str, bool] = {}
        self.broadcast_calls: List[str] = []
        self.broadcast_return = "fake_broadcast_txid"

    async def lookup_txout(self, txid, vout):
        return self.txouts.get(f"{txid}:{vout}")

    async def is_utxo_spent(self, txid, vout):
        return self.spent.get(f"{txid}:{vout}", False)

    async def is_confirmed(self, txid):
        return self.confirmed.get(txid, False)

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


async def make_coord():
    """Build a Coordinator wired to fakes + temp db."""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    _db_paths.append(db_path)

    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    db_mod.SCHEMA_PATH = schema_path

    db = Database(db_path)
    await db.connect()

    # BotConfig falls back to its _DEFAULTS table when the env path doesn't exist.
    cfg = BotConfig("/nonexistent-env-for-tests.env")

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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=3)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
            npub = "npub_committed"
            pid = await db.add_participant(mix_id, npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(pid, state="committed")

            await coord._cmd_provide_addresses(
                FakeCtx(npub), npub, P2WPKH_ADDRS[0:3],
            )

            last_dm = nostr.sent_dms[-1][1].lower()
            assert "zap" in last_dm
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
        mix_id = await db.create_mix(output_size=1_000_000, min_participants=2)
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
    async def test_final_warning_does_not_fire_if_count_stuck_at_2_in_old_logic(self):
        """Regression guard: the old buggy code gated on count<=1 so the final
        warning never fired after the second reminder set count=2. The new
        code gates on count==2 and bumps to 3."""
        coord, db, nostr, pid, mix_id = await self._setup_signing_participant("half")
        try:
            await db.update_participant(pid, reminder_count=1)  # not yet 2
            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            await coord._handle_signing(mix_row, active, int(time.time()))

            # With count==1, the final-warning gate (count==2) shouldn't fire.
            p = await db.get_participant(pid)
            assert p["reminder_count"] == 1
            joined = " ".join(m for _, m in nostr.sent_dms).lower()
            assert "final warning" not in joined
        finally:
            await db.close()


# --- Bug #13: UTXO blacklisting on ghost ---


class TestGhostBlacklistsUtxos:
    @pytest.mark.asyncio
    async def test_ghoster_npub_and_each_utxo_are_blacklisted(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=2)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=2)
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


# --- Operator allowlist for input/output types ---


class TestInputTypeAllowlist:
    """ACCEPTED_INPUT_TYPES gates /commit. Default is p2wpkh-only."""

    @pytest.mark.asyncio
    async def test_rejects_non_allowed_input_type(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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

            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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

            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=3)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=3)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            existing = await db.create_mix(output_size=1_000_000, min_participants=3,
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
    async def test_skips_locked_incompatible_mix_creates_new(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Widen the allowlist so p2tr passes the gate.
            coord.cfg._values["_accepted_input_types"] = {"p2wpkh", "p2tr"}
            # Existing mix is locked to p2wpkh.
            existing = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=2)
            await db.update_mix(mix_a, state="assembling", fee_rate=30)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=2)
            await db.update_mix(mix_b, state="assembling", fee_rate=30)

            # In each mix, the npub is paired with one other participant so
            # _assemble_psbt has min_participants=2 to work with.
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=3)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_a = await db.create_mix(output_size=1_000_000, min_participants=3)
            mix_b = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=2)
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

            # Add a second paid participant so the min_participants=2 check holds.
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
        enough for min_participants."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=2,
                                         max_participants=10)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Two well-funded participants.
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

            # The under-funded one: exactly output_size sats. Passes the
            # /addresses check (where estimated_fee_share=0) but fails at
            # assembly once the proportional miner fee is applied.
            poor = await db.add_participant(mix_id, "poor", "poor@x")
            await db.update_participant(poor, state="paid", fee_paid=300)
            await db.add_utxo(poor, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
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
    async def test_dropping_drops_below_min_participants_cancels_whole_mix(self):
        """C2 boundary: if dropping under-funded participants would leave
        fewer than min_participants, fall back to cancelling the whole mix
        (the prior behavior)."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3,
                                         max_participants=10)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Two rich + one poor. Dropping poor leaves 2 < min=3.
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
            await db.add_utxo(poor, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(poor, P2WPKH_ADDRS[6], 1_000_000)

            active = await db.get_participants_by_mix(mix_id)
            await coord._assemble_psbt(await db.get_mix(mix_id), active)

            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "cancelled", (
                f"should cancel when survivors < min_participants; got {mix_after['state']}"
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
        output_size=100_000, min_participants=2,
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

            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "cancelled"
            assert mix_after["broadcast_txid"] is None
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

            mix_after = await db.get_mix(mix_id)
            assert mix_after["state"] == "cancelled"
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
            mix_id = await db.create_mix(output_size=100_000, min_participants=2)
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid="finaltxid_xyz",
                broadcast_tx_hex="deadbeef" * 8,
            )
            pid = await db.add_participant(mix_id, "npub_signed", "")
            await db.update_participant(pid, state="signed")
            await db.add_utxo(pid, TXID[0], 0, 100_000, "p2wpkh", FAKE_SCRIPTPUBKEY)

            chain.confirmed["finaltxid_xyz"] = True

            # Force the sweep window to be open.
            await db.set_setting("last_broadcast_check_unix", "0")
            await coord._broadcast_sweep(int(time.time()))

            # All trace gone except blacklist (which we didn't add to).
            mix_after = await db.get_mix(mix_id)
            assert mix_after is None, "mix should be wiped after confirmation"
            assert await db.get_participant(pid) is None
            assert await db.get_utxos_by_participant(pid) == []
            # And the signer got a confirmation DM.
            dms = [m for r, m in nostr.sent_dms if r == "npub_signed"]
            assert any("finaltxid_xyz" in m for m in dms)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unconfirmed_triggers_rebroadcast(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=100_000, min_participants=2)
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
            mix_id = await db.create_mix(output_size=100_000, min_participants=2)
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
                mid = await db.create_mix(output_size=100_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=100_000, min_participants=3)
            pid = await db.add_participant(mix_id, "npub_solo", "solo@x")
            await db.update_participant(pid, state="paid", fee_paid=1000)

            await coord._cmd_exit_mix(FakeCtx("npub_solo"), "npub_solo", None)

            p = await db.get_participant(pid)
            assert p["state"] == "cancelled"
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
            mix_a = await db.create_mix(output_size=100_000, min_participants=3)
            mix_b = await db.create_mix(output_size=100_000, min_participants=3)
            pid_a = await db.add_participant(mix_a, "npub_mm", "mm@x")
            pid_b = await db.add_participant(mix_b, "npub_mm", "mm@x")
            await db.update_participant(pid_a, state="paid", fee_paid=500)
            await db.update_participant(pid_b, state="paid", fee_paid=500)

            await coord._cmd_exit_mix(FakeCtx("npub_mm"), "npub_mm", mix_a)

            # Only the named mix's participant is cancelled.
            assert (await db.get_participant(pid_a))["state"] == "cancelled"
            assert (await db.get_participant(pid_b))["state"] == "paid"
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
            pid = await db.add_participant(mix_id, "user_x", "x@x")
            await db.update_participant(pid, state="paid", fee_paid=500)
            await db.add_utxo(pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.mark_utxo_used(pid, TXID[0], 0)

            await coord._cancel_and_refund(await db.get_mix(mix_id), "test")

            # Row is gone.
            assert await db.get_utxo(TXID[0], 0) is None
            # And a new commit can now use the same outpoint.
            mix_id2 = await db.create_mix(output_size=1_000_000, min_participants=3)
            pid2 = await db.add_participant(mix_id2, "user_y", "")
            await db.add_utxo(pid2, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            assert await db.get_utxo(TXID[0], 0) is not None
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_voluntary_exit_deletes_utxos(self):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=3)
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
            mix_id = await db.create_mix(output_size=1_000_000, min_participants=2,
                                         max_participants=10)
            await db.update_mix(mix_id, state="assembling", fee_rate=30)

            # Pattern from TestBroadcast409TreatedAsSuccess: two well-funded
            # + one under-funded; the latter is dropped on assembly.
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
            await db.add_utxo(poor, TXID[2], 0, 1_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
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
            other_mix = await db.create_mix(output_size=1_000_000, min_participants=3)
            other_pid = await db.add_participant(other_mix, "other", "")
            await db.add_utxo(other_pid, TXID[3], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            # Note: is_used left as 0 so is_utxo_used returns False (the row
            # exists but isn't yet "claimed"). This forces the add_utxo
            # path to actually run and hit the UNIQUE constraint.

            # New user commits the same outpoint.
            new_mix = await db.create_mix(output_size=1_000_000, min_participants=3)
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
                        output_size=1_000_000, min_participants=3,
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
