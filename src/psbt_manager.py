"""PSBT Manager — build, validate, combine PSBTs for coinjoin transactions.

Uses python-bitcointx for PSBT operations (BIP-174).
Per-script-type vbyte sizes for accurate vsize estimation.

CRITICAL ARCHITECTURE NOTES:
- build_skeleton creates the unsigned CTransaction with ALL inputs/outputs in one
  CMutableTransaction, then wraps each input in a PSBT_Input with its prevout UTXO
  (CTxOut) so that extract_transaction() can finalize the combine.
- Participants receive the skeleton, sign their OWN inputs, return a PSBT with
  partial_sigs. combine() merges partial_sigs per-input across all returned PSBTs.
- finalize() calls extract_transaction() which internally signs using the combined
  partial_sigs and returns a spendable CTransaction hex for broadcast.
"""

from __future__ import annotations

import logging
from typing import List, Dict, Optional, Tuple, Any
from collections import Counter

# validate_returned cryptographically verifies returned signatures, which is an
# EC operation — ensure the libsecp256k1 symbol-rename shim is applied before
# any verify() call loads the library.
from . import secp256k1_compat  # noqa: F401

logger = logging.getLogger(__name__)

from bitcointx.core import (
    b2x, CTxIn, CTxOut, COutPoint, CTransaction,
    CMutableTxIn, CMutableTxOut, CMutableTransaction,
)
from bitcointx.core.script import CScript
from bitcointx.wallet import (
    CBitcoinAddress, P2PKHBitcoinAddress, P2SHBitcoinAddress,
    P2WPKHBitcoinAddress, P2WSHBitcoinAddress, P2TRBitcoinAddress,
)

from .vsize import VsizeCalculator
from bitcointx.core.psbt import (
    PartiallySignedBitcoinTransaction,
    PSBT_Input, PSBT_Output,
)


class PSBTManager:
    """Build, validate, and combine PSBTs for coinjoin."""

    MAX_PSBT_HEX_SIZE = 50000  # 50KB relay concern

    def __init__(self, network: str = "mainnet",
                 input_vsize_map: Optional[Dict[str, int]] = None,
                 output_vsize_map: Optional[Dict[str, int]] = None,
                 overhead: int = 10):
        self._network = network
        self._vsize = VsizeCalculator(input_vsize_map, output_vsize_map, overhead)

    def _parse_address(self, address: str):
        """Parse a bitcoin address from string.

        CBitcoinAddress handles base58 (p2pkh, p2sh) and bech32 v0
        (p2wpkh, p2wsh), but raises on bech32m (p2tr). We fall back to
        P2TRBitcoinAddress for taproot.
        """
        try:
            return CBitcoinAddress(address)
        except Exception:
            return P2TRBitcoinAddress(address)

    def _address_type(self, address: str) -> str:
        """Determine the type of an address. Raises if the format is unknown.

        Checked in most-specific-first order: P2WPKHBitcoinAddress and
        P2WSHBitcoinAddress are both bech32 v0 but distinguished by length
        (20-byte vs 32-byte witness program), so their classes are distinct.
        """
        addr = self._parse_address(address)
        if isinstance(addr, P2TRBitcoinAddress):
            return "p2tr"
        if isinstance(addr, P2WPKHBitcoinAddress):
            return "p2wpkh"
        if isinstance(addr, P2WSHBitcoinAddress):
            return "p2wsh"
        if isinstance(addr, P2PKHBitcoinAddress):
            return "p2pkh"
        if isinstance(addr, P2SHBitcoinAddress):
            return "p2sh"
        raise ValueError(f"unrecognized address type: {address}")

    # --- Vsize estimation — delegates to VsizeCalculator ---

    def input_vsize(self, script_type: str) -> int:
        return self._vsize.input_vsize(script_type)

    def output_vsize(self, script_type: str) -> int:
        return self._vsize.output_vsize(script_type)

    def estimate_vsize(self, inputs_by_type: Dict[str, int],
                       outputs_by_type: Dict[str, int]) -> int:
        return self._vsize.estimate_total_vsize(inputs_by_type, outputs_by_type)

    # --- Build Skeleton PSBT ---

    def build_skeleton(self, inputs: List[Dict], outputs: List[Dict]) -> str:
        """Build a skeleton PSBT with prevout UTXO for each input.

        Args:
            inputs: list with keys: txid, vout, amount, script_type, scriptpubkey
                The scriptpubkey hex is the actual prevout script from chain
                lookup. Required so participants can sign (they need the
                prevout script for sighash) and finalize can extract.
            outputs: list with keys: address, amount

        Returns: hex-encoded PSBT.

        NOTE: The PSBT constructor creates PSBT_Input/PSBT_Output automatically
        from the unsigned_tx. We set UTXOs on inputs separately via set_utxo()
        so extract_transaction() can finalize the combined PSBT.
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        # 1. Build the unsigned transaction (CMutableTransaction).
        #    This is the transaction skeleton — all inputs (vin) with prevout
        #    references and outputs (vout) with amounts + recipient scripts.
        vin = []
        for inp in inputs:
            txid_bytes = bytes.fromhex(inp["txid"])[::-1]
            vin.append(CMutableTxIn(COutPoint(txid_bytes, inp["vout"])))

        vout = []
        for out in outputs:
            addr = self._parse_address(out["address"])
            vout.append(CMutableTxOut(out["amount"], addr.to_scriptPubKey()))

        unsigned_tx = CMutableTransaction(vin, vout)

        # 2. Build PSBT from unsigned_tx — constructor creates PSBT_Input and
        #    PSBT_Output objects for each vin/vout entry automatically.
        # version=0 explicitly: emit a BIP174 v0 PSBT (with the global
        # unsigned_tx) for the widest wallet compatibility. This is bitcointx's
        # default, but we pin it so a future library default (e.g. v2/BIP370)
        # can't silently change the format participants' wallets must parse.
        psbt = PartiallySignedBitcoinTransaction(unsigned_tx=unsigned_tx, version=0)

        # 3. Set the actual prevout UTXO on each input so that:
        #    - participants can sign (need prevout script to compute sighash)
        #    - finalize (extract_transaction) can convert partial_sigs to final
        #    The prevout script comes from the chain lookup at /commit time,
        #    stored in utxos.scriptpubkey via the coordinator.
        for i, inp_data in enumerate(inputs):
            scriptpubkey_hex = inp_data.get("scriptpubkey", "")
            if scriptpubkey_hex:
                prevout_script = CScript(bytes.fromhex(scriptpubkey_hex))
                psbt.inputs[i].set_utxo(
                    CTxOut(inp_data["amount"], prevout_script),
                    unsigned_tx,
                )
            psbt.inputs[i].sighash_type = 0x01

        return b2x(psbt.serialize())

    # --- Validate Returned PSBT ---

    def validate_returned(self, skeleton_hex: str, returned_hex: str,
                          participant_input_count: int,
                          expected_output_addresses: List[str],
                          participant_input_indices: Optional[List[int]] = None,
                          ) -> Tuple[bool, str]:
        """Validate a returned (signed) PSBT.

        Checks:
        - Input and output counts match the skeleton
        - No output amounts or scriptPubKeys were altered
        - The participant signed the inputs they were supposed to sign
          - If participant_input_indices is provided: those exact indices have
            partial_sigs AND nothing outside those indices has new partial_sigs
            (defends against a participant signing someone else's input)
          - If participant_input_indices is None: legacy fallback — only the
            count of signed inputs is verified

        Returns: (is_valid, reason)
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            skeleton_psbt = PartiallySignedBitcoinTransaction.from_binary(
                bytes.fromhex(skeleton_hex)
            )
            returned_psbt = PartiallySignedBitcoinTransaction.from_binary(
                bytes.fromhex(returned_hex)
            )

            # NOTE: bitcointx's PSBT_BitcoinInput / PSBT_BitcoinOutput objects
            # carry metadata (witness UTXOs, derivation paths) but NOT the
            # outpoints or output amounts — those live on the unsigned_tx.
            # Reading PSBT_Output.amount or .script_pubkey raises
            # AttributeError, which the outer try/except would mask as a
            # generic "Validation error". Use unsigned_tx.vin / vout.
            skel_tx = skeleton_psbt.unsigned_tx
            ret_tx = returned_psbt.unsigned_tx
            if skel_tx is None or ret_tx is None:
                return False, "Missing unsigned_tx on PSBT"

            # Check input/output counts match
            if len(ret_tx.vin) != len(skel_tx.vin):
                return False, f"Input count changed: {len(ret_tx.vin)} vs {len(skel_tx.vin)}"

            if len(ret_tx.vout) != len(skel_tx.vout):
                return False, f"Output count changed: {len(ret_tx.vout)} vs {len(skel_tx.vout)}"

            # S7: each input must point at the same outpoint as the skeleton.
            # A malicious participant could otherwise return a PSBT whose vin
            # references a different prevout — combine_psbts wouldn't notice
            # because it uses self's structure, but extract_transaction would
            # later fail with no actionable error. Catch it up front.
            for i, (skel_in, ret_in) in enumerate(zip(skel_tx.vin, ret_tx.vin)):
                if (ret_in.prevout.hash != skel_in.prevout.hash
                        or ret_in.prevout.n != skel_in.prevout.n):
                    return False, f"Input #{i} outpoint changed"

            # S-C: nVersion, nLockTime, and per-input nSequence are part of
            # the sighash. If a participant signed a modified version of any
            # of them, the resulting signatures would either fail to combine
            # cleanly or produce a tx with semantics we didn't intend (e.g.
            # nLockTime in the future to delay confirmation, nSequence that
            # signals RBF). In the happy path combine_psbts uses OUR
            # unsigned_tx so the broadcast carries our version/locktime,
            # but a malicious return whose signatures match a different
            # locktime would silently fail at extract_transaction, masking
            # the real cause. Reject up front.
            if ret_tx.nVersion != skel_tx.nVersion:
                return False, (
                    f"Transaction version changed: "
                    f"{ret_tx.nVersion} vs {skel_tx.nVersion}"
                )
            if ret_tx.nLockTime != skel_tx.nLockTime:
                return False, (
                    f"Transaction nLockTime changed: "
                    f"{ret_tx.nLockTime} vs {skel_tx.nLockTime}"
                )
            for i, (skel_in, ret_in) in enumerate(zip(skel_tx.vin, ret_tx.vin)):
                if ret_in.nSequence != skel_in.nSequence:
                    return False, (
                        f"Input #{i} nSequence changed: "
                        f"{ret_in.nSequence} vs {skel_in.nSequence}"
                    )

            # Check output amounts and scripts haven't been tampered with.
            for i, (skel_out, ret_out) in enumerate(zip(skel_tx.vout, ret_tx.vout)):
                if ret_out.nValue != skel_out.nValue:
                    return False, f"Output #{i} amount changed: {ret_out.nValue} vs {skel_out.nValue}"
                if ret_out.scriptPubKey != skel_out.scriptPubKey:
                    return False, f"Output #{i} address changed"

            # Check signatures. Treat an input as "signed" either when new
            # partial_sigs have appeared OR when the PSBT input has been
            # finalized (bitcointx auto-finalizes per-input once it has all
            # the sigs for that input — partial_sigs gets cleared and the
            # final_script_witness / final_script_sig is set instead).
            def _has_new_signature(skel_in, ret_in) -> bool:
                skel_sigs = set(skel_in.partial_sigs.keys()) if skel_in.partial_sigs else set()
                ret_sigs = set(ret_in.partial_sigs.keys()) if ret_in.partial_sigs else set()
                if ret_sigs - skel_sigs:
                    return True
                # Finalized inputs surface as final_script_witness (segwit) or
                # final_script_sig (legacy). Both are stronger than partial_sigs.
                if getattr(ret_in, "final_script_witness", None) and not getattr(skel_in, "final_script_witness", None):
                    return True
                if getattr(ret_in, "final_script_sig", None) and not getattr(skel_in, "final_script_sig", None):
                    return True
                return False

            if participant_input_indices is None:
                # Legacy: only count signed inputs (can be fooled by signing
                # a peer's input).
                signed_count = sum(
                    1 for skel_in, ret_in
                    in zip(skeleton_psbt.inputs, returned_psbt.inputs)
                    if _has_new_signature(skel_in, ret_in)
                )
                if signed_count < participant_input_count:
                    return False, f"Only {signed_count}/{participant_input_count} inputs signed"
            else:
                # Strict: each expected index must carry a new signature (or
                # be finalized); no other index may have a new signature added
                # by this participant.
                expected = set(participant_input_indices)
                for i, (skel_in, ret_in) in enumerate(zip(skeleton_psbt.inputs, returned_psbt.inputs)):
                    added = _has_new_signature(skel_in, ret_in)
                    if i in expected and not added:
                        return False, f"Input #{i} (yours) was not signed"
                    if i not in expected and added:
                        return False, f"Input #{i} (not yours) was signed — refusing"

            # Cryptographic verification: a PRESENT signature is not enough — a
            # troll can return a well-formed-but-wrong (or garbage) signature
            # that passes the presence/scope checks above, sails through
            # finalize(), and is only caught when the network rejects the
            # broadcast (wasting the whole signing round). Verify each
            # newly-signed input actually signs THIS input's BIP143 sighash with
            # a key that owns the input. p2wpkh only (the allowlist); other
            # types fall back to the presence checks. We verify against the
            # SKELETON's tx + witness UTXO (trusted) — never the returned
            # PSBT's, which could be tampered.
            for i, (skel_in, ret_in) in enumerate(
                    zip(skeleton_psbt.inputs, returned_psbt.inputs)):
                if not _has_new_signature(skel_in, ret_in):
                    continue
                utxo = getattr(skel_in, "utxo", None)
                if utxo is None:
                    return False, f"Input #{i}: skeleton has no witness UTXO to verify against"
                spk = bytes(utxo.scriptPubKey)
                is_p2wpkh = len(spk) == 22 and spk[0] == 0x00 and spk[1] == 0x14
                if not is_p2wpkh:
                    continue  # non-p2wpkh: presence-checked only
                extracted = self._extract_signature(ret_in)
                if extracted is None:
                    return False, f"Input #{i}: could not read signature for verification"
                pub_bytes, sig = extracted
                good, why = self._verify_p2wpkh_signature(skel_tx, i, utxo, pub_bytes, sig)
                if not good:
                    return False, f"Input #{i} signature invalid ({why})"

            return True, "valid"

        except Exception as e:
            return False, f"Validation error: {str(e)}"

    @staticmethod
    def _extract_signature(psbt_input) -> Optional[Tuple[bytes, bytes]]:
        """Recover (pubkey_bytes, sig_with_hashtype) from a participant's signed
        PSBT input — whether the signature is still a partial_sig or the input
        was auto-finalized into a p2wpkh witness ([sig, pubkey]). None if no
        recoverable signature is present."""
        if psbt_input.partial_sigs:
            pub, sig = next(iter(psbt_input.partial_sigs.items()))
            return bytes(pub), bytes(sig)
        fsw = getattr(psbt_input, "final_script_witness", None)
        if fsw:
            stack = [bytes(x) for x in (fsw.stack if hasattr(fsw, "stack") else fsw)]
            if len(stack) == 2:  # p2wpkh witness = [signature, pubkey]
                return stack[1], stack[0]
        return None

    @staticmethod
    def _verify_p2wpkh_signature(tx, i: int, utxo, pub_bytes: bytes,
                                 sig: bytes) -> Tuple[bool, str]:
        """Verify a p2wpkh signature over input i's BIP143 sighash.

        Checks (in order): the pubkey hashes to the input's witness program;
        the sig's hashtype byte is SIGHASH_ALL; the ECDSA signature verifies
        against the sighash. Returns (ok, reason)."""
        from bitcointx.core import Hash160
        from bitcointx.core.key import CPubKey
        from bitcointx.core.script import (
            CScript, SignatureHash, SIGHASH_ALL, SIGVERSION_WITNESS_V0,
            OP_DUP, OP_HASH160, OP_EQUALVERIFY, OP_CHECKSIG,
        )
        if not sig:
            return False, "empty signature"
        spk = bytes(utxo.scriptPubKey)
        keyhash = spk[2:22]
        if Hash160(pub_bytes) != keyhash:
            return False, "pubkey does not own the input"
        if sig[-1] != SIGHASH_ALL:
            return False, f"hashtype 0x{sig[-1]:02x} != SIGHASH_ALL"
        try:
            pub = CPubKey(pub_bytes)
            script_code = CScript(
                [OP_DUP, OP_HASH160, keyhash, OP_EQUALVERIFY, OP_CHECKSIG])
            sighash = SignatureHash(
                script_code, tx, i, SIGHASH_ALL,
                amount=utxo.nValue, sigversion=SIGVERSION_WITNESS_V0)
            if not pub.verify(sighash, sig[:-1]):
                return False, "ECDSA verification failed"
        except Exception as e:
            return False, f"verify error: {type(e).__name__}"
        return True, "ok"

    # --- Combine PSBTs ---

    def combine_psbts(self, psbt_hexes: List[str]) -> str:
        """Combine multiple signed PSBTs into one final PSBT.

        Uses bitcointx's built-in .combine() which clones the base PSBT
        and merges partial_sigs from each subsequent PSBT per-input.

        Args:
            psbt_hexes: hex-encoded PSBTs from each participant

        Returns: hex-encoded combined PSBT
        """
        if not psbt_hexes:
            return ""

        # Parse first PSBT as base (use from_binary — constructor is keyword-only)
        base_psbt = PartiallySignedBitcoinTransaction.from_binary(
            bytes.fromhex(psbt_hexes[0])
        )

        # Combine subsequent PSBTs into the base
        combined = base_psbt
        for hex_str in psbt_hexes[1:]:
            other_psbt = PartiallySignedBitcoinTransaction.from_binary(
                bytes.fromhex(hex_str)
            )
            combined = combined.combine(other_psbt)

        return b2x(combined.serialize())

    # --- Finalize & Extract Raw Transaction ---

    def finalize(self, combined_psbt_hex: str) -> Optional[str]:
        """Finalize the combined PSBT and extract the raw transaction hex.

        Internally calls extract_transaction() which:
        1. Verifies all inputs have complete partial_sigs (converted to final)
        2. Copies final_script_sig/final_script_witness to the tx
        3. Returns the immutable CTransaction

        Returns: hex-encoded raw transaction, or None on failure.

        C-E: logs the exception class + truncated message on failure so
        the operator can distinguish "missing/bad sigs" (expected, user
        misbehaviour) from "library/skeleton bug" (urgent — every mix would
        cancel-and-refund silently otherwise). NOT exc_info — that would
        dump frame locals containing PSBT hex with addresses.
        """
        import logging
        log = logging.getLogger(__name__)
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            combined_psbt = PartiallySignedBitcoinTransaction.from_binary(
                bytes.fromhex(combined_psbt_hex)
            )
            final_tx = combined_psbt.extract_transaction()
            return b2x(final_tx.serialize())
        except Exception as e:
            # Truncate the message — bitcointx exceptions sometimes embed
            # serialized PSBT fragments. 120 chars is plenty for diagnosis
            # without leaking addresses or scripts wholesale.
            msg = str(e)
            if len(msg) > 120:
                msg = msg[:120] + "...(truncated)"
            log.warning(
                "PSBT finalize failed: %s — %s "
                "(typically means at least one input is unsigned)",
                type(e).__name__, msg,
            )
            return None

    # --- Chunking ---

    def needs_chunking(self, psbt_hex: str) -> bool:
        return len(psbt_hex) > self.MAX_PSBT_HEX_SIZE

    def chunk_psbt(self, psbt_hex: str) -> List[str]:
        if not self.needs_chunking(psbt_hex):
            return [psbt_hex]
        chunk_size = self.MAX_PSBT_HEX_SIZE
        return [psbt_hex[i:i + chunk_size] for i in range(0, len(psbt_hex), chunk_size)]
