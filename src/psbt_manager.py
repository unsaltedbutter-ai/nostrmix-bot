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

from typing import List, Dict, Optional, Tuple, Any
from collections import Counter

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
        psbt = PartiallySignedBitcoinTransaction(unsigned_tx=unsigned_tx)

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

            # Check input/output counts match
            if len(returned_psbt.inputs) != len(skeleton_psbt.inputs):
                return False, f"Input count changed: {len(returned_psbt.inputs)} vs {len(skeleton_psbt.inputs)}"

            if len(returned_psbt.outputs) != len(skeleton_psbt.outputs):
                return False, f"Output count changed: {len(returned_psbt.outputs)} vs {len(skeleton_psbt.outputs)}"

            # Check output addresses and amounts
            for i, (skel_out, ret_out) in enumerate(zip(skeleton_psbt.outputs, returned_psbt.outputs)):
                if ret_out.amount != skel_out.amount:
                    return False, f"Output #{i} amount changed: {ret_out.amount} vs {skel_out.amount}"
                if ret_out.script_pubkey != skel_out.script_pubkey:
                    return False, f"Output #{i} address changed"

            # Check signatures.
            if participant_input_indices is None:
                # Legacy: only count signed inputs (can be fooled by signing
                # a peer's input).
                signed_count = sum(1 for inp in returned_psbt.inputs if inp.partial_sigs)
                if signed_count < participant_input_count:
                    return False, f"Only {signed_count}/{participant_input_count} inputs signed"
            else:
                # Strict: each expected index must carry partial_sigs that
                # weren't there in the skeleton; no other index may have new
                # partial_sigs added by this participant.
                expected = set(participant_input_indices)
                for i, (skel_in, ret_in) in enumerate(zip(skeleton_psbt.inputs, returned_psbt.inputs)):
                    skel_sigs = set(skel_in.partial_sigs.keys()) if skel_in.partial_sigs else set()
                    ret_sigs = set(ret_in.partial_sigs.keys()) if ret_in.partial_sigs else set()
                    added = ret_sigs - skel_sigs
                    if i in expected and not added:
                        return False, f"Input #{i} (yours) was not signed"
                    if i not in expected and added:
                        return False, f"Input #{i} (not yours) was signed — refusing"

            return True, "valid"

        except Exception as e:
            return False, f"Validation error: {str(e)}"

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
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            combined_psbt = PartiallySignedBitcoinTransaction.from_binary(
                bytes.fromhex(combined_psbt_hex)
            )
            final_tx = combined_psbt.extract_transaction()
            return b2x(final_tx.serialize())
        except Exception as e:
            return None

    # --- Chunking ---

    def needs_chunking(self, psbt_hex: str) -> bool:
        return len(psbt_hex) > self.MAX_PSBT_HEX_SIZE

    def chunk_psbt(self, psbt_hex: str) -> List[str]:
        if not self.needs_chunking(psbt_hex):
            return [psbt_hex]
        chunk_size = self.MAX_PSBT_HEX_SIZE
        return [psbt_hex[i:i + chunk_size] for i in range(0, len(psbt_hex), chunk_size)]
