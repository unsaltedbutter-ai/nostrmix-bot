"""PSBT Manager — build, validate, combine PSBTs for coinjoin transactions.

Uses python-bitcointx for PSBT operations (BIP-174).
Per-script-type vbyte sizes for accurate vsize estimation.
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
    P2WPKHBitcoinAddress, P2WPKHBitcoinAddress,
)
from bitcointx.core.psbt import (
    PartiallySignedBitcoinTransaction,
    PSBT_Input, PSBT_Output,
)


# Default per-script-type vsize tables (from script-vbytesize.md)
_DEFAULT_INPUT_VSIZE: Dict[str, int] = {
    "p2pkh": 150,
    "p2sh": 255,
    "p2sh-p2wpkh": 95,
    "p2wpkh": 70,
    "p2wsh": 1455,
    "p2tr": 70,
}

_DEFAULT_OUTPUT_VSIZE: Dict[str, int] = {
    "p2pkh": 35,
    "p2sh": 35,
    "p2sh-p2wpkh": 35,
    "p2wpkh": 35,
    "p2wsh": 4,
    "p2tr": 45,
}


class PSBTManager:
    """Build, validate, and combine PSBTs for coinjoin."""

    MAX_PSBT_HEX_SIZE = 50000  # 50KB relay concern

    def __init__(self, network: str = "mainnet",
                 input_vsize_map: Optional[Dict[str, int]] = None,
                 output_vsize_map: Optional[Dict[str, int]] = None,
                 overhead: int = 10):
        self._network = network
        self._input_vsize_map = input_vsize_map or _DEFAULT_INPUT_VSIZE
        self._output_vsize_map = output_vsize_map or _DEFAULT_OUTPUT_VSIZE
        self._overhead = overhead

    def _parse_address(self, address: str) -> CBitcoinAddress:
        """Parse a bitcoin address."""
        return CBitcoinAddress(address)

    def _address_type(self, address: str) -> str:
        """Determine the type of an address."""
        addr = self._parse_address(address)
        if isinstance(addr, P2WPKHBitcoinAddress):
            return "p2wpkh"
        elif isinstance(addr, P2PKHBitcoinAddress):
            return "p2pkh"
        elif isinstance(addr, P2SHBitcoinAddress):
            return "p2sh"
        else:
            return "p2wpkh"

    # --- Vsize estimation (per-script-type) ---

    def input_vsize(self, script_type: str) -> int:
        key = script_type.lower().replace("-", "")
        for k, v in self._input_vsize_map.items():
            if k.replace("-", "") == key:
                return v
        return self._input_vsize_map.get("p2wpkh", 70)

    def output_vsize(self, script_type: str) -> int:
        key = script_type.lower().replace("-", "")
        for k, v in self._output_vsize_map.items():
            if k.replace("-", "") == key:
                return v
        return self._output_vsize_map.get("p2wpkh", 35)

    def estimate_vsize(self, inputs_by_type: Dict[str, int],
                       outputs_by_type: Dict[str, int]) -> int:
        """Estimate transaction vsize using per-script-type counts."""
        total = self._overhead
        for stype, count in inputs_by_type.items():
            total += self.input_vsize(stype) * count
        for stype, count in outputs_by_type.items():
            total += self.output_vsize(stype) * count
        return total

    # --- Build Skeleton PSBT ---

    def build_skeleton(self, inputs: List[Dict], outputs: List[Dict]) -> str:
        """Build a skeleton PSBT.

        Args:
            inputs: list with keys: txid, vout, amount, script_type
            outputs: list with keys: address, amount

        Returns: hex-encoded PSBT.
        """
        psbt_inps: List[PSBT_Input] = []
        psbt_outs: List[PSBT_Output] = []

        for inp in inputs:
            txid_hex = inp["txid"]
            vout = inp["vout"]
            txid_bytes = bytes.fromhex(txid_hex)[::-1]
            outpoint = COutPoint(txid_bytes, vout)
            txin = CMutableTxIn(outpoint)
            psbt_in = PSBT_Input(txin)
            psbt_in.sighash_type = 0x01
            psbt_inps.append(psbt_in)

        for out in outputs:
            address = out["address"]
            amount = out["amount"]
            addr = self._parse_address(address)
            pay_script = addr.to_scriptpubkey()
            txout = CMutableTxOut(amount, pay_script)
            psbt_out = PSBT_Output(txout)
            psbt_outs.append(psbt_out)

        psbt = PartiallySignedBitcoinTransaction(psbt_inps, psbt_outs)
        return b2x(psbt.serialize())

    # --- Validate Returned PSBT ---

    def validate_returned(self, skeleton_hex: str, returned_hex: str,
                          participant_input_count: int,
                          expected_output_addresses: List[str]) -> Tuple[bool, str]:
        """Validate a returned (signed) PSBT.

        Checks:
        - No outputs changed
        - No inputs removed or added
        - Participant has signed their inputs

        Returns: (is_valid, reason)
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            skeleton_bytes = bytes.fromhex(skeleton_hex)
            skeleton_psbt = PartiallySignedBitcoinTransaction(skeleton_bytes)

            returned_bytes = bytes.fromhex(returned_hex)
            returned_psbt = PartiallySignedBitcoinTransaction(returned_bytes)

            # Check input count matches
            if len(returned_psbt.inputs) != len(skeleton_psbt.inputs):
                return False, f"Input count changed: {len(returned_psbt.inputs)} vs {len(skeleton_psbt.inputs)}"

            # Check output count matches
            if len(returned_psbt.outputs) != len(skeleton_psbt.outputs):
                return False, f"Output count changed: {len(returned_psbt.outputs)} vs {len(skeleton_psbt.outputs)}"

            # Check output addresses and amounts
            for i, (skel_out, ret_out) in enumerate(zip(skeleton_psbt.outputs, returned_psbt.outputs)):
                if ret_out.amount != skel_out.amount:
                    return False, f"Output #{i} amount changed: {ret_out.amount} vs {skel_out.amount}"

                if ret_out.script_pubkey != skel_out.script_pubkey:
                    return False, f"Output #{i} address changed"

            # Check participant has signed their inputs
            signed_count = sum(1 for inp in returned_psbt.inputs if inp.partial_sigs)
            if signed_count < participant_input_count:
                return False, f"Only {signed_count}/{participant_input_count} inputs signed"

            return True, "valid"

        except Exception as e:
            return False, f"Validation error: {str(e)}"

    # --- Combine PSBTs ---

    def combine_psbts(self, psbt_hexes: List[str]) -> str:
        """Combine multiple signed PSBTs into one final PSBT.

        Uses bitcointx's built-in PSBT combine/merge to aggregate
        partial signatures from all participants.

        Args:
            psbt_hexes: hex-encoded PSBTs from each participant

        Returns: hex-encoded final PSBT
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        if not psbt_hexes:
            return ""

        # Parse first PSBT as base
        base_bytes = bytes.fromhex(psbt_hexes[0])
        base_psbt = PartiallySignedBitcoinTransaction(base_bytes)

        # Combine subsequent PSBTs into the base using .combine() which
        # clones self and merges the other, returning a new combined PSBT
        # with all partial signatures aggregated.
        combined = base_psbt
        for hex_str in psbt_hexes[1:]:
            other_bytes = bytes.fromhex(hex_str)
            other_psbt = PartiallySignedBitcoinTransaction(other_bytes)
            combined = combined.combine(other_psbt)

        return b2x(combined.serialize())

    # --- Finalize & Extract ---

    def finalize(self, combined_psbt_hex: str) -> Optional[str]:
        """Finalize the PSBT and extract raw transaction hex.

        Returns: hex-encoded raw transaction, or None on failure.
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            combined_bytes = bytes.fromhex(combined_psbt_hex)
            combined_psbt = PartiallySignedBitcoinTransaction(combined_bytes)
            return b2x(combined_psbt.serialize())
        except Exception:
            return None

    # --- Chunking ---

    def needs_chunking(self, psbt_hex: str) -> bool:
        return len(psbt_hex) > self.MAX_PSBT_HEX_SIZE

    def chunk_psbt(self, psbt_hex: str) -> List[str]:
        if not self.needs_chunking(psbt_hex):
            return [psbt_hex]
        chunk_size = self.MAX_PSBT_HEX_SIZE
        return [psbt_hex[i:i + chunk_size] for i in range(0, len(psbt_hex), chunk_size)]

    # --- Vsize Estimation ---

    def estimate_vsize(self, num_inputs: int, num_outputs: int,
                       input_vsize: int = None, output_vsize: int = None,
                       overhead: int = None) -> int:
        iv = input_vsize or self._input_vsize
        ov = output_vsize or self._output_vsize
        oh = overhead or self._overhead
        return oh + (num_inputs * iv) + (num_outputs * ov)
