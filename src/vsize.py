"""Shared vbyte size estimation for coinjoin transactions.

Consolidated per-script-type vsize logic used by both PSBTManager and FeeEngine.
Sourced from script-vbytesize.md / env config.
"""

from __future__ import annotations

from typing import Dict, Optional

# Default per-script-type input vsize tables. Calibrated against real
# mainnet transactions and rounded up to nearest 5 for a small fee buffer.
# See src/config.py for the same values exposed as env-overridable settings.
DEFAULT_INPUT_VSIZE: Dict[str, int] = {
    "p2pkh": 150,
    "p2sh": 135,          # p2sh-p2wsh 2-of-3 ≈ 131; bare p2sh-multisig heavier
    "p2sh-p2wpkh": 95,
    "p2wpkh": 70,
    "p2wsh": 100,         # 2-of-2 ≈ 96; 2-of-3 ≈ 104; larger N-of-M heavier
    "p2tr": 60,           # key-path ≈ 58; script-path heavier
}

# Default per-script-type output vsize tables. Outputs are structural:
# value(8) + script_length(1) + scriptPubKey bytes.
DEFAULT_OUTPUT_VSIZE: Dict[str, int] = {
    "p2pkh": 35,          # real 34
    "p2sh": 35,           # real 32
    "p2sh-p2wpkh": 35,    # real 32
    "p2wpkh": 35,         # real 31
    "p2wsh": 45,          # real 43 — was 4 (bug)
    "p2tr": 45,           # real 43
}


def _normalize_key(script_type: str) -> str:
    return script_type.lower().replace("-", "")


def _lookup(script_type: str, mapping: Dict[str, int], fallback_key: str = "p2wpkh") -> int:
    """Look up a vsize value for a script type with fallback to default."""
    norm = _normalize_key(script_type)
    for k, v in mapping.items():
        if _normalize_key(k) == norm:
            return v
    return mapping.get(fallback_key, 70)


class VsizeCalculator:
    """Computes vbyte sizes per script type for transactions.

    Holds per-instance maps (from env config) with sensible defaults.
    Both PSBTManager and FeeEngine delegate to this.
    """

    def __init__(
        self,
        input_vsize_map: Optional[Dict[str, int]] = None,
        output_vsize_map: Optional[Dict[str, int]] = None,
        overhead: int = 10,
    ):
        self.input_vsize_map = input_vsize_map or dict(DEFAULT_INPUT_VSIZE)
        self.output_vsize_map = output_vsize_map or dict(DEFAULT_OUTPUT_VSIZE)
        self.overhead = overhead

    def input_vsize(self, script_type: str) -> int:
        """Look up input vbytes for a script type. Falls back to p2wpkh."""
        return _lookup(script_type, self.input_vsize_map)

    def output_vsize(self, script_type: str) -> int:
        """Look up output vbytes for a script type. Falls back to p2wpkh."""
        return _lookup(script_type, self.output_vsize_map)

    def total_inputs_vsize(self, inputs_by_type: Dict[str, int]) -> int:
        """Compute total input vsize from a count-per-type dict."""
        total = 0
        for stype, count in inputs_by_type.items():
            total += self.input_vsize(stype) * count
        return total

    def total_outputs_vsize(self, outputs_by_type: Dict[str, int]) -> int:
        """Compute total output vsize from a count-per-type dict."""
        total = 0
        for stype, count in outputs_by_type.items():
            total += self.output_vsize(stype) * count
        return total

    def estimate_total_vsize(
        self, inputs_by_type: Dict[str, int], outputs_by_type: Dict[str, int]
    ) -> int:
        """Estimate vsize for the entire transaction, per-script-type."""
        return (
            self.overhead
            + self.total_inputs_vsize(inputs_by_type)
            + self.total_outputs_vsize(outputs_by_type)
        )

    def compute_participant_weight(
        self,
        inputs_by_type: Dict[str, int],
        outputs_by_type: Dict[str, int],
        total_input_vsize: int,
        total_output_vsize: int,
        total_vsize: int,
    ) -> int:
        """Compute a participant's proportional weight of the tx vsize."""
        my_vsize = self.total_inputs_vsize(inputs_by_type) + self.total_outputs_vsize(outputs_by_type)
        num_inputs = sum(inputs_by_type.values())
        overhead_share = self.overhead / max(num_inputs, 1)
        return int(my_vsize + overhead_share)
