"""Tests for ChainMonitor — mempool.space wrapper.

Includes offline unit tests for the script-type normalizer and live
integration tests that hit mempool.space using well-known Satoshi-era
UTXOs. The live tests will fail if mempool.space is unreachable, or
(less likely) if those famous coins ever move. To skip the live tests
locally:

    pytest tests/test_chain_monitor.py -k 'not satoshi and not hal_finney'
"""

import os
import sys
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import httpx
import respx

from src.chain_monitor import ChainMonitor, _normalize_script_type
from src.vsize import VsizeCalculator


# Block 1 coinbase: 50 BTC p2pk to Satoshi's pubkey, never spent.
SATOSHI_BLOCK1_TXID = "0e3e2357e806b6cdb1f70b54c3a3a17b6714ee1f0e68bebb44a74b1efd512098"

# Block 9 coinbase: 50 BTC, spent on 2009-01-12 in the Hal Finney transaction
# (f4184fc596403b9d638783cf57adfe4c75c605f6356fbc91338530e9831e9e16) — the
# first Bitcoin transaction between two people.
HAL_FINNEY_PREV_TXID = "0437cd7f8525ceed2324359c2d0ba26006d92d856a9c20fa0241106ee5a597c9"

# A recent (block 949689) spent UTXO. The output at
# 5a2f7320c38c2da96c16b0ee06dd9aee5b587816910a0b0b749942ba48469fca:1 — a
# 150,000-sat p2wpkh to bc1qp6ywl60yfva7z62h3qe7jmepc0y246dl0arfk4 — was
# consumed by tx f4361b0fbe9a376f487fa42ec6d8ef20b4b05cba7f0db4c1d108f1f9e6538aa8.
RECENT_SPENT_TXID = "5a2f7320c38c2da96c16b0ee06dd9aee5b587816910a0b0b749942ba48469fca"
RECENT_SPENT_VOUT = 1
RECENT_SPENT_ADDRESS = "bc1qp6ywl60yfva7z62h3qe7jmepc0y246dl0arfk4"

# A confirmed transaction (block 949683). Used for is_confirmed coverage.
CONFIRMED_TXID = "088da5e483176f89b73848bd709a135684587e889a41fe120e5846a1ae82167d"


class TestNormalizeScriptType:
    def test_segwit_v0_p2wpkh(self):
        assert _normalize_script_type("v0_p2wpkh") == "p2wpkh"

    def test_segwit_v0_p2wsh(self):
        assert _normalize_script_type("v0_p2wsh") == "p2wsh"

    def test_taproot_v1(self):
        assert _normalize_script_type("v1_p2tr") == "p2tr"

    def test_legacy_passthrough(self):
        assert _normalize_script_type("p2pkh") == "p2pkh"
        assert _normalize_script_type("p2sh") == "p2sh"

    def test_unknown_passthrough(self):
        # p2pk isn't in the project's vocab but should not be coerced; the
        # vsize lookup falls back to p2wpkh defaults if the type is unknown.
        assert _normalize_script_type("p2pk") == "p2pk"

    def test_empty_falls_back_to_p2wpkh(self):
        assert _normalize_script_type("") == "p2wpkh"


class TestLiveMempoolSpace:
    """Live tests against https://mempool.space/api.

    These will break if mempool.space is unreachable or if Satoshi ever
    spends his block-1 coins. We accept that risk.
    """

    @pytest.mark.asyncio
    async def test_lookup_txout_satoshi_block1(self):
        cm = ChainMonitor()
        try:
            txout = await cm.lookup_txout(SATOSHI_BLOCK1_TXID, 0)
        finally:
            await cm.close()

        assert txout is not None, "Block 1 coinbase should be findable"
        assert txout["txid"] == SATOSHI_BLOCK1_TXID
        assert txout["vout"] == 0
        assert txout["value"] == 5_000_000_000  # 50 BTC in sats
        assert txout["status"] is True  # parent tx confirmed
        # Block 1 coinbase is a p2pk script — passthrough, no normalization.
        assert txout["scriptpubkey_type"] == "p2pk"
        assert txout["scriptpubkey"]  # non-empty hex

    @pytest.mark.asyncio
    async def test_lookup_txout_nonexistent_returns_none(self):
        cm = ChainMonitor()
        try:
            # All-zero txid will never exist.
            txout = await cm.lookup_txout("0" * 64, 0)
        finally:
            await cm.close()
        assert txout is None

    @pytest.mark.asyncio
    async def test_lookup_txout_vout_out_of_range_returns_none(self):
        cm = ChainMonitor()
        try:
            # Block 1 coinbase has exactly one output.
            txout = await cm.lookup_txout(SATOSHI_BLOCK1_TXID, 999)
        finally:
            await cm.close()
        assert txout is None

    @pytest.mark.asyncio
    async def test_is_utxo_spent_satoshi_block1_still_unspent(self):
        cm = ChainMonitor()
        try:
            spent = await cm.is_utxo_spent(SATOSHI_BLOCK1_TXID, 0)
        finally:
            await cm.close()
        assert spent is False, "Block 1 coinbase has never been spent"

    @pytest.mark.asyncio
    async def test_is_utxo_spent_block9_coinbase_spent_by_hal_finney_tx(self):
        cm = ChainMonitor()
        try:
            spent = await cm.is_utxo_spent(HAL_FINNEY_PREV_TXID, 0)
        finally:
            await cm.close()
        assert spent is True, "Block 9 coinbase was spent in the Hal Finney tx"

    @pytest.mark.asyncio
    async def test_is_confirmed_block1_coinbase(self):
        cm = ChainMonitor()
        try:
            confirmed = await cm.is_confirmed(SATOSHI_BLOCK1_TXID)
        finally:
            await cm.close()
        assert confirmed is True

    @pytest.mark.asyncio
    async def test_estimate_fee_rate_returns_positive(self):
        cm = ChainMonitor()
        try:
            rate = await cm.estimate_fee_rate()
        finally:
            await cm.close()
        # Bounded by the constructor's [min_fee_rate, max_fee_rate]; whatever
        # the mempool looks like right now, the clamp guarantees > 0.
        assert rate > 0

    # --- Recent spent UTXO (tx f4361b0f… input from a real address) ---

    @pytest.mark.asyncio
    async def test_lookup_txout_recent_spent_returns_prevout(self):
        """Prevout details are returned even though the output is now spent."""
        cm = ChainMonitor()
        try:
            txout = await cm.lookup_txout(RECENT_SPENT_TXID, RECENT_SPENT_VOUT)
        finally:
            await cm.close()

        assert txout is not None
        assert txout["value"] == 150_000
        assert txout["address"] == RECENT_SPENT_ADDRESS
        # v0_p2wpkh normalizes to the project's bare 'p2wpkh' vocab.
        assert txout["scriptpubkey_type"] == "p2wpkh"

    @pytest.mark.asyncio
    async def test_is_utxo_spent_recent_consumed_output(self):
        """5a2f7320:1 was consumed by f4361b0f at block 949689."""
        cm = ChainMonitor()
        try:
            spent = await cm.is_utxo_spent(RECENT_SPENT_TXID, RECENT_SPENT_VOUT)
        finally:
            await cm.close()
        assert spent is True

    # --- Confirmation check on a known-confirmed tx ---

    @pytest.mark.asyncio
    async def test_is_confirmed_known_confirmed_tx(self):
        """User-supplied tx 088da5e… confirmed at block 949683."""
        cm = ChainMonitor()
        try:
            confirmed = await cm.is_confirmed(CONFIRMED_TXID)
        finally:
            await cm.close()
        assert confirmed is True

    # --- Negative cases: malformed inputs should not throw ---

    @pytest.mark.asyncio
    async def test_lookup_txout_malformed_txid(self):
        """Non-hex / wrong-length txid must not raise; returns None."""
        cm = ChainMonitor()
        try:
            r1 = await cm.lookup_txout("not-a-real-txid", 0)
            r2 = await cm.lookup_txout("zzzz" * 16, 0)  # right length, non-hex
            r3 = await cm.lookup_txout("", 0)
        finally:
            await cm.close()
        assert r1 is None
        assert r2 is None
        assert r3 is None

    @pytest.mark.asyncio
    async def test_is_utxo_spent_malformed_txid_fail_open(self):
        """is_utxo_spent is fail-open: malformed/unknown returns False."""
        cm = ChainMonitor()
        try:
            r1 = await cm.is_utxo_spent("not-a-real-txid", 0)
            r2 = await cm.is_utxo_spent("0" * 64, 0)  # well-formed but never existed
            r3 = await cm.is_utxo_spent("", 0)
        finally:
            await cm.close()
        assert r1 is False
        assert r2 is False
        assert r3 is False

    @pytest.mark.asyncio
    async def test_is_utxo_spent_negative_vout_does_not_throw(self):
        """A negative vout would 404 the API; we should still return False."""
        cm = ChainMonitor()
        try:
            spent = await cm.is_utxo_spent(SATOSHI_BLOCK1_TXID, -1)
        finally:
            await cm.close()
        assert spent is False

    @pytest.mark.asyncio
    async def test_is_confirmed_unknown_txid_returns_false(self):
        """An all-zero txid is never confirmed."""
        cm = ChainMonitor()
        try:
            confirmed = await cm.is_confirmed("0" * 64)
        finally:
            await cm.close()
        assert confirmed is False


# Reference transactions for vsize accuracy. Each tuple is (txid, label).
# Picked to exercise different input types. Pulled from the address spend
# histories the user pointed at; actual vsize verified at the time of writing.
VSIZE_FIXTURES = [
    # 1 p2wpkh input, mixed outputs (actual vsize ~172)
    ("2d7c1953c072fc67e96b7acf25783f0807c939a85da9cdb0c8f6efabb88d91cb", "p2wpkh-input"),
    # 3 p2sh-p2wsh 2-of-3 inputs, 1 p2sh + 1 p2wpkh out (actual vsize 466)
    ("7b148aeb24a1b5491ae30873fca9d443c388723bb0b91e79d4c39412b1ed3d89", "p2sh-p2wsh-2of3"),
    # 1 p2wsh 2-of-2 input, 2 p2wpkh outputs (actual vsize 168)
    ("1bb3bc9f0ddf5bf888b4d9af743abda04d1a7234e2d69aa84836a3be0ecb641c", "p2wsh-2of2"),
]


class TestVsizeAccuracy:
    """Regression guard against future re-introduction of vsize typos.

    For each reference tx, ask mempool.space for its real on-chain vsize and
    its input/output script type breakdown, then ask our VsizeCalculator for
    an estimate. The estimate must be >= actual (we never want to under-pay
    miner fee) but no more than ~25% over (we'd be over-charging participants
    if it ballooned).
    """

    LOWER_BOUND_FACTOR = 1.00  # estimate must cover actual
    UPPER_BOUND_FACTOR = 1.25  # but not by more than 25%

    async def _fetch_tx(self, txid: str) -> dict:
        cm = ChainMonitor()
        try:
            tx = await cm.lookup_tx(txid)
        finally:
            await cm.close()
        assert tx is not None, f"could not fetch tx {txid}"
        return tx

    def _estimate_from_tx(self, tx: dict, vsize_calc: VsizeCalculator) -> int:
        """Build inputs_by_type/outputs_by_type from a real tx and estimate."""
        in_by_type: dict = {}
        for vin in tx["vin"]:
            t = _normalize_script_type(
                vin.get("prevout", {}).get("scriptpubkey_type", "")
            )
            in_by_type[t] = in_by_type.get(t, 0) + 1
        out_by_type: dict = {}
        for vout in tx["vout"]:
            t = _normalize_script_type(vout.get("scriptpubkey_type", ""))
            out_by_type[t] = out_by_type.get(t, 0) + 1
        return vsize_calc.estimate_total_vsize(in_by_type, out_by_type)

    def _actual_vsize(self, tx: dict) -> int:
        # mempool.space reports `size` (bytes) and `weight` (WU); vsize = ceil(weight/4).
        return (tx["weight"] + 3) // 4

    @pytest.mark.parametrize("txid,label", VSIZE_FIXTURES)
    @pytest.mark.asyncio
    async def test_estimate_covers_real_vsize_with_small_buffer(self, txid, label):
        tx = await self._fetch_tx(txid)
        actual = self._actual_vsize(tx)
        vsize_calc = VsizeCalculator()  # uses src/vsize.py DEFAULT_*_VSIZE
        estimate = self._estimate_from_tx(tx, vsize_calc)
        lower = int(actual * self.LOWER_BOUND_FACTOR)
        upper = int(actual * self.UPPER_BOUND_FACTOR)
        assert lower <= estimate <= upper, (
            f"{label}: estimate {estimate} not in [{lower}, {upper}] "
            f"for actual vsize {actual}"
        )

    @pytest.mark.asyncio
    async def test_p2tr_estimate_covers_real_vsize(self):
        """Live-hunt a recent block for a pure-p2tr tx and check it against
        our estimate. Bumped out of parametrize because we have to search
        for a tx with only p2tr inputs (not always present in a fixed tx)."""
        cm = ChainMonitor()
        try:
            tip_height_raw = (await cm._client.get(f"{cm._api_base}/blocks/tip/height")).text
            tip = int(tip_height_raw)
            found_tx = None
            for offset in range(0, 6):
                h = tip - offset
                bh = (await cm._client.get(f"{cm._api_base}/block-height/{h}")).text
                txs = (await cm._client.get(f"{cm._api_base}/block/{bh}/txs")).json()
                for stub in txs:
                    if not stub.get("vin") or stub["vin"][0].get("is_coinbase"):
                        continue
                    types = {
                        vi.get("prevout", {}).get("scriptpubkey_type")
                        for vi in stub["vin"] if vi.get("prevout")
                    }
                    if types == {"v1_p2tr"}:
                        found_tx = stub
                        break
                if found_tx:
                    break
        finally:
            await cm.close()

        if found_tx is None:
            pytest.skip("no pure-p2tr tx in last 6 blocks; mempool.space ran out")

        vsize_calc = VsizeCalculator()
        actual = self._actual_vsize(found_tx)
        estimate = self._estimate_from_tx(found_tx, vsize_calc)
        assert actual <= estimate <= int(actual * self.UPPER_BOUND_FACTOR), (
            f"p2tr {found_tx['txid']}: estimate {estimate} vs actual {actual}"
        )


# Use a deliberately unreachable hostname so the live tests are guaranteed
# offline. respx will intercept before httpx makes a real request.
_OFFLINE_API = "https://test-mempool-offline.invalid/api"
_OFFLINE_BACKUP = "https://test-mempool-backup-offline.invalid/api"


def _offline_monitor() -> ChainMonitor:
    return ChainMonitor(api_base=_OFFLINE_API, api_backup=_OFFLINE_BACKUP)


class TestBroadcastErrorPaths:
    """Mocked broadcast paths — covers the audit's #16 coverage gap.

    The bot's _combine_and_broadcast gates on broadcast_tx returning a non-None
    txid. If we ever flip its semantics by accident, real failures would look
    like successes and the mix would advance to 'broadcast' state with a junk
    txid. These tests pin down all the failure modes.
    """

    @pytest.mark.asyncio
    @respx.mock
    async def test_400_non_final_returns_none(self):
        respx.post(f"{_OFFLINE_API}/tx").mock(
            return_value=httpx.Response(400, text="non-final")
        )
        respx.post(f"{_OFFLINE_BACKUP}/tx").mock(
            return_value=httpx.Response(400, text="non-final")
        )
        cm = _offline_monitor()
        try:
            result = await cm.broadcast_tx("aabbccdd")
        finally:
            await cm.close()
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_409_mempool_conflict_returns_none(self):
        respx.post(f"{_OFFLINE_API}/tx").mock(
            return_value=httpx.Response(409, text="txn-mempool-conflict")
        )
        respx.post(f"{_OFFLINE_BACKUP}/tx").mock(
            return_value=httpx.Response(409, text="txn-mempool-conflict")
        )
        cm = _offline_monitor()
        try:
            result = await cm.broadcast_tx("aabbccdd")
        finally:
            await cm.close()
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_on_primary_falls_through_to_backup(self):
        """Primary 5xx → try backup. If backup succeeds, we return its txid."""
        respx.post(f"{_OFFLINE_API}/tx").mock(
            return_value=httpx.Response(500, text="upstream error")
        )
        respx.post(f"{_OFFLINE_BACKUP}/tx").mock(
            return_value=httpx.Response(200, text="abc123txid")
        )
        cm = _offline_monitor()
        try:
            result = await cm.broadcast_tx("aabbccdd")
        finally:
            await cm.close()
        assert result == "abc123txid"

    @pytest.mark.asyncio
    @respx.mock
    async def test_timeout_falls_through_to_backup(self):
        respx.post(f"{_OFFLINE_API}/tx").mock(
            side_effect=httpx.ReadTimeout("timed out")
        )
        respx.post(f"{_OFFLINE_BACKUP}/tx").mock(
            return_value=httpx.Response(200, text="backup_txid_ok")
        )
        cm = _offline_monitor()
        try:
            result = await cm.broadcast_tx("aabbccdd")
        finally:
            await cm.close()
        assert result == "backup_txid_ok"

    @pytest.mark.asyncio
    @respx.mock
    async def test_both_endpoints_unreachable_returns_none(self):
        respx.post(f"{_OFFLINE_API}/tx").mock(
            side_effect=httpx.ConnectError("dns")
        )
        respx.post(f"{_OFFLINE_BACKUP}/tx").mock(
            side_effect=httpx.ConnectError("dns")
        )
        cm = _offline_monitor()
        try:
            result = await cm.broadcast_tx("aabbccdd")
        finally:
            await cm.close()
        assert result is None

    @pytest.mark.asyncio
    @respx.mock
    async def test_lookup_txout_falls_back_to_secondary(self):
        respx.get(f"{_OFFLINE_API}/tx/abc").mock(
            side_effect=httpx.ConnectError("dns")
        )
        respx.get(f"{_OFFLINE_BACKUP}/tx/abc").mock(
            return_value=httpx.Response(200, json={
                "vout": [{
                    "value": 500_000,
                    "scriptpubkey": "0014" + "00" * 20,
                    "scriptpubkey_type": "v0_p2wpkh",
                    "scriptpubkey_address": "bc1q...",
                }],
                "status": {"confirmed": True},
            })
        )
        cm = _offline_monitor()
        try:
            result = await cm.lookup_txout("abc", 0)
        finally:
            await cm.close()
        assert result is not None
        assert result["value"] == 500_000
        assert result["scriptpubkey_type"] == "p2wpkh"
