"""Tests for PSBTManager — per-script-type vsize support."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.psbt_manager import PSBTManager


class TestPSBTManager:
    def setup_method(self):
        self.mgr = PSBTManager()

    def test_estimate_vsize_p2wpkh_only(self):
        """Test vsize for uniform p2wpkh inputs/outputs."""
        inp = {"p2wpkh": 5}
        out = {"p2wpkh": 10}
        vsize = self.mgr.estimate_vsize(inp, out)
        expected = 10 + (5 * 70) + (10 * 35)
        assert vsize == expected

    def test_estimate_vsize_mixed(self):
        """Test vsize for mixed script types."""
        inp = {"p2wpkh": 2, "p2tr": 1, "p2sh": 1}
        out = {"p2wpkh": 4, "p2sh": 2}
        vsize = self.mgr.estimate_vsize(inp, out)
        # 10 + 2*70 + 1*60 + 1*135 + 4*35 + 2*35 = 10+140+60+135+140+70 = 555
        assert vsize == 555

    def test_estimate_vsize_p2tr_outputs(self):
        """Test with p2tr outputs (45 vB each)."""
        inp = {"p2wpkh": 3}
        out = {"p2tr": 4}
        vsize = self.mgr.estimate_vsize(inp, out)
        expected = 10 + (3 * 70) + (4 * 45)
        assert vsize == expected

    def test_input_vsize_lookup(self):
        assert self.mgr.input_vsize("p2wpkh") == 70
        assert self.mgr.input_vsize("p2tr") == 60
        assert self.mgr.input_vsize("p2pkh") == 150
        assert self.mgr.input_vsize("p2sh") == 135
        assert self.mgr.input_vsize("p2wsh") == 100

    def test_output_vsize_lookup(self):
        assert self.mgr.output_vsize("p2wpkh") == 35
        assert self.mgr.output_vsize("p2tr") == 45
        assert self.mgr.output_vsize("p2wsh") == 45
        assert self.mgr.output_vsize("p2pkh") == 35

    def test_needs_chunking_small(self):
        small_hex = "abc" * 100
        assert not self.mgr.needs_chunking(small_hex)

    def test_needs_chunking_large(self):
        large_hex = "a" * 60000
        assert self.mgr.needs_chunking(large_hex)

    def test_chunk_psbt_small(self):
        small = "abc123"
        chunks = self.mgr.chunk_psbt(small)
        assert len(chunks) == 1
        assert chunks[0] == small

    def test_chunk_psbt_large(self):
        large = "a" * 150000
        chunks = self.mgr.chunk_psbt(large)
        assert len(chunks) >= 2
        reassembled = "".join(chunks)
        assert reassembled == large

    def test_max_psbt_size_constant(self):
        assert self.mgr.MAX_PSBT_HEX_SIZE == 50000

    # --- _address_type dispatch (covers the p2tr / p2wsh detection fix) ---

    def test_address_type_p2wpkh(self):
        assert self.mgr._address_type("bc1q4gyakdgygyc8qweh39qxamywc9vdvrt82jsrcj") == "p2wpkh"

    def test_address_type_p2pkh(self):
        assert self.mgr._address_type("12cgpFdJViXbwHbhrA3TuW1EGnL25Zqc3P") == "p2pkh"

    def test_address_type_p2sh(self):
        assert self.mgr._address_type("3Hqmaknw6rDZBFgUau6S2kSv2bzpMW4ThX") == "p2sh"

    def test_address_type_p2wsh(self):
        # 32-byte witness program v0 (different length from p2wpkh).
        assert self.mgr._address_type(
            "bc1qyfffyfy9ld0rwzgpwutjdafxfqtydttkrnlfqanpyu0lgp4seg4q9p0ww0"
        ) == "p2wsh"

    def test_address_type_p2tr(self):
        # bech32m — CBitcoinAddress raises on these; the parser must fall back.
        assert self.mgr._address_type("bc1p9j0rwcgpd28gnastlh2yweshq7sl2vxxvrpstdsx9w3m8axaxn0qg0vcg0") == "p2tr"

    def test_address_type_garbage_raises(self):
        import pytest
        with pytest.raises(Exception):
            self.mgr._address_type("not-an-address")

    # --- S7: validate_returned must reject input outpoint substitution ---

    def _build_skeleton_with_input(self, prev_txid_hex: str):
        """Build a tiny 1-in/1-out skeleton; the input's prevout is the
        passed-in txid. Used to forge a 'returned' PSBT with a different vin."""
        return self.mgr.build_skeleton(
            inputs=[{
                "txid": prev_txid_hex,
                "vout": 0,
                "amount": 100_000,
                "script_type": "p2wpkh",
                "scriptpubkey": "0014" + "00" * 20,
            }],
            outputs=[{
                "address": "bc1q4gyakdgygyc8qweh39qxamywc9vdvrt82jsrcj",
                "amount": 50_000,
            }],
        )

    def test_validate_returned_rejects_swapped_input_outpoint(self):
        """A malicious participant could return a PSBT whose vin points at a
        different prevout than the skeleton. Today's structural checks (input
        count, output amount/script) wouldn't notice. The strict check must
        compare vin outpoints too."""
        skel_a = self._build_skeleton_with_input("aa" * 32)
        skel_b = self._build_skeleton_with_input("bb" * 32)
        ok, reason = self.mgr.validate_returned(
            skel_a, skel_b,
            participant_input_count=1,
            expected_output_addresses=[],
        )
        assert not ok, "expected rejection on swapped input outpoint"
        assert "outpoint" in reason.lower() or "input" in reason.lower(), (
            f"reason should mention input/outpoint: {reason!r}"
        )

    # --- Real-signing tests (Wave 3) ---
    #
    # These use python-bitcointx's KeyStore to produce REAL ECDSA signatures
    # against a multi-input p2wpkh PSBT. Without these the fund-safety paths
    # (validate_returned strict mode, combine, finalize) had no end-to-end
    # coverage — every prior test mocked the validator or used hand-rolled
    # fake partial_sigs that bitcointx's serializer rejected.
    #
    # Requires: libsecp256k1 (system) + conftest.py's symbol-rename patch.

    def _multi_input_skeleton(self, n=3):
        """Build an N-input p2wpkh PSBT where input i belongs to key i.

        Returns (skeleton_hex, [key_0, key_1, ..., key_{n-1}]).
        """
        from bitcointx.core.key import CKey
        from bitcointx.wallet import P2WPKHBitcoinAddress
        # Deterministic keys: 32 bytes, first byte is the participant index.
        material = [bytes([i + 1]) + b"\x55" * 31 for i in range(n)]
        keys = [CKey(m) for m in material]
        inputs = []
        for i, k in enumerate(keys):
            addr = P2WPKHBitcoinAddress.from_pubkey(k.pub)
            inputs.append({
                "txid": (bytes([i + 1]) * 32).hex(),  # distinct dummy prevouts
                "vout": 0,
                "amount": 200_000,
                "script_type": "p2wpkh",
                "scriptpubkey": addr.to_scriptPubKey().hex(),
            })
        # A couple of output slots — keeps the test in the realm of a tiny
        # mixed transaction.
        outputs = [
            {"address": "bc1q4gyakdgygyc8qweh39qxamywc9vdvrt82jsrcj",
             "amount": 100_000},
            {"address": "bc1q670lslr8tlv9w5kk4zw7ckha74ll6lx48tnsks",
             "amount": 100_000},
        ]
        return self.mgr.build_skeleton(inputs, outputs), keys

    def _sign_with(self, skel_hex: str, key_indices: list, all_keys: list) -> str:
        """Return a serialized PSBT signed by the listed key indices."""
        from bitcointx.core import b2x
        from bitcointx.core.key import KeyStore
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
        psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(skel_hex))
        ks = KeyStore.from_iterable([all_keys[i] for i in key_indices])
        psbt.sign(ks)
        return b2x(psbt.serialize())

    def test_validate_returned_strict_accepts_correct_sign(self):
        """Participant signs only their own input (index 0). Strict mode
        with participant_input_indices=[0] must accept."""
        skel, keys = self._multi_input_skeleton(n=3)
        signed = self._sign_with(skel, [0], keys)
        ok, reason = self.mgr.validate_returned(
            skel, signed,
            participant_input_count=1,
            expected_output_addresses=[],
            participant_input_indices=[0],
        )
        assert ok, f"strict mode should accept the participant signing their input: {reason}"

    def test_validate_returned_strict_rejects_unsigned(self):
        """Participant returns the unmodified skeleton (no sigs added).
        Strict mode must reject — their input is unsigned."""
        skel, keys = self._multi_input_skeleton(n=3)
        ok, reason = self.mgr.validate_returned(
            skel, skel,
            participant_input_count=1,
            expected_output_addresses=[],
            participant_input_indices=[0],
        )
        assert not ok, "strict mode should reject when assigned input is unsigned"
        assert "not signed" in reason.lower() or "yours" in reason.lower(), (
            f"reason should mention the missing signature: {reason!r}"
        )

    def test_validate_returned_strict_rejects_signing_peer_input(self):
        """Participant 0 maliciously also signs participant 1's input.
        Strict mode with participant_input_indices=[0] must reject —
        this is the central anti-cheat defense."""
        skel, keys = self._multi_input_skeleton(n=3)
        # Sign with keys 0 AND 1, claim only [0] as our indices.
        signed = self._sign_with(skel, [0, 1], keys)
        ok, reason = self.mgr.validate_returned(
            skel, signed,
            participant_input_count=1,
            expected_output_addresses=[],
            participant_input_indices=[0],
        )
        assert not ok, "strict mode should reject signing a non-assigned input"
        assert "not yours" in reason.lower() or "refus" in reason.lower(), (
            f"reason should call out the unauthorized sig: {reason!r}"
        )

    def test_validate_returned_strict_with_multi_input_participant(self):
        """A participant with TWO inputs signs both correctly. Strict mode
        with indices=[0, 1] accepts."""
        skel, keys = self._multi_input_skeleton(n=3)
        signed = self._sign_with(skel, [0, 1], keys)
        ok, reason = self.mgr.validate_returned(
            skel, signed,
            participant_input_count=2,
            expected_output_addresses=[],
            participant_input_indices=[0, 1],
        )
        assert ok, f"strict mode should accept signing all of one's assigned inputs: {reason}"

    def test_validate_returned_strict_rejects_partial_self_sign(self):
        """Participant has 2 inputs but signs only 1. Strict mode rejects —
        we won't proceed with a half-signed participant."""
        skel, keys = self._multi_input_skeleton(n=3)
        signed = self._sign_with(skel, [0], keys)  # signs only input 0
        ok, reason = self.mgr.validate_returned(
            skel, signed,
            participant_input_count=2,
            expected_output_addresses=[],
            participant_input_indices=[0, 1],
        )
        assert not ok, "strict mode should reject when only some assigned inputs are signed"

    def test_combine_then_finalize_yields_broadcastable_tx(self):
        """End-to-end: build a 3-input skeleton, each participant signs
        their own input separately, combine the three returns, finalize
        the combined PSBT, and assert we get a structurally-valid signed
        Bitcoin transaction back. This is the entire user-funds-at-risk
        flow exercised against real ECDSA crypto."""
        from bitcointx.core import b2x, CTransaction

        skel, keys = self._multi_input_skeleton(n=3)
        signed_returns = [
            self._sign_with(skel, [i], keys) for i in range(3)
        ]

        combined = self.mgr.combine_psbts(signed_returns)
        assert combined, "combine_psbts returned empty"

        raw_tx_hex = self.mgr.finalize(combined)
        assert raw_tx_hex is not None, "finalize returned None — PSBT not fully signed?"

        tx = CTransaction.deserialize(bytes.fromhex(raw_tx_hex))
        assert len(tx.vin) == 3, f"expected 3 inputs, got {len(tx.vin)}"
        assert len(tx.vout) == 2, f"expected 2 outputs, got {len(tx.vout)}"
        # Each input must have a non-empty witness after finalize (p2wpkh =
        # 2-item witness: signature + pubkey).
        assert tx.wit is not None
        for i in range(3):
            wit_items = list(tx.wit.vtxinwit[i].scriptWitness)
            assert len(wit_items) == 2, (
                f"vin[{i}] witness should be [sig, pubkey], got {len(wit_items)} items"
            )
            # Sanity-check: sig is ~70-72 bytes, pubkey is 33 bytes.
            assert 70 <= len(wit_items[0]) <= 73, (
                f"vin[{i}] sig length odd: {len(wit_items[0])}"
            )
            assert len(wit_items[1]) == 33, (
                f"vin[{i}] pubkey length wrong: {len(wit_items[1])}"
            )
        # Txid is deterministic; just confirm we can compute it.
        txid = b2x(tx.GetTxid()[::-1])
        assert len(txid) == 64

    def test_combine_skips_when_one_participant_unsigned(self):
        """If one participant's PSBT has no signature on their input, the
        combine still works structurally but finalize must fail (or return
        None), reflecting that the tx isn't actually broadcastable yet."""
        skel, keys = self._multi_input_skeleton(n=3)
        # Two of three sign; participant 2 returns the unsigned skeleton.
        returns = [
            self._sign_with(skel, [0], keys),
            self._sign_with(skel, [1], keys),
            skel,  # participant 2 didn't sign
        ]
        combined = self.mgr.combine_psbts(returns)
        assert combined
        raw_tx_hex = self.mgr.finalize(combined)
        assert raw_tx_hex is None, (
            "finalize should not produce a tx hex for an only-partially-signed PSBT"
        )

    # --- S-C: tx-level field checks (nVersion / nLockTime / nSequence) ---

    def _modify_unsigned_tx(self, skel_hex: str, *, nVersion=None,
                             nLockTime=None, vin_index_to_seq=None):
        """Build a tampered PSBT by deserializing the skeleton, mutating
        the unsigned_tx, and reserializing."""
        from bitcointx.core import b2x
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
        psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(skel_hex))
        tx = psbt.unsigned_tx.to_mutable()
        if nVersion is not None:
            tx.nVersion = nVersion
        if nLockTime is not None:
            tx.nLockTime = nLockTime
        if vin_index_to_seq:
            for i, seq in vin_index_to_seq.items():
                tx.vin[i].nSequence = seq
        psbt.unsigned_tx = tx
        return b2x(psbt.serialize())

    def test_validate_returned_rejects_modified_nversion(self):
        """S-C: changing nVersion changes the sighash; the signatures the
        participant produced wouldn't be ours, and extract_transaction
        would fail late. Reject up front."""
        skel = self._build_skeleton_with_input("dd" * 32)
        tampered = self._modify_unsigned_tx(skel, nVersion=42)
        ok, reason = self.mgr.validate_returned(
            skel, tampered,
            participant_input_count=0,
            expected_output_addresses=[],
        )
        assert not ok
        assert "version" in reason.lower()

    def test_validate_returned_rejects_modified_nlocktime(self):
        """S-C: a participant could push nLockTime forward to delay the tx."""
        skel = self._build_skeleton_with_input("ee" * 32)
        tampered = self._modify_unsigned_tx(skel, nLockTime=900_000)
        ok, reason = self.mgr.validate_returned(
            skel, tampered,
            participant_input_count=0,
            expected_output_addresses=[],
        )
        assert not ok
        assert "locktime" in reason.lower()

    def test_validate_returned_rejects_modified_nsequence(self):
        """S-C: per-input nSequence is part of the sighash; modifying it
        can also signal RBF in some wallets' interpretation."""
        skel, keys = self._multi_input_skeleton(n=3)
        tampered = self._modify_unsigned_tx(skel, vin_index_to_seq={1: 0xfffffffd})
        ok, reason = self.mgr.validate_returned(
            skel, tampered,
            participant_input_count=1,
            expected_output_addresses=[],
            participant_input_indices=[0],
        )
        assert not ok
        assert "nsequence" in reason.lower()

    # --- C-E: finalize logs a useful diagnostic on failure ---

    def test_finalize_logs_when_psbt_incomplete(self, caplog):
        """C-E: if extract_transaction raises (typically because some input
        is unsigned), finalize logs the exception class so the operator
        can tell unsigned-input failures from real PSBT-structure bugs."""
        import logging
        skel, keys = self._multi_input_skeleton(n=3)
        # Combine 2 of 3 signed PSBTs — the third input has no signature.
        signed = [self._sign_with(skel, [i], keys) for i in range(2)]
        combined = self.mgr.combine_psbts(signed)
        with caplog.at_level(logging.WARNING, logger="src.psbt_manager"):
            result = self.mgr.finalize(combined)
        assert result is None
        # Log captures the exception class name.
        joined = " ".join(r.message for r in caplog.records)
        assert "PSBT finalize failed" in joined

    def test_validate_returned_accepts_identical_skeleton_round_trip(self):
        """Regression guard for the S7 fix: an unmodified-but-not-yet-signed
        round-trip of the skeleton should pass the structural checks
        (legacy mode without indices, since no sigs were added)."""
        skel = self._build_skeleton_with_input("cc" * 32)
        ok, reason = self.mgr.validate_returned(
            skel, skel,
            participant_input_count=0,  # no sigs expected, accept zero
            expected_output_addresses=[],
        )
        assert ok, f"identical round-trip should pass: {reason}"
