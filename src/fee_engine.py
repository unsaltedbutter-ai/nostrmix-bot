"""Fee Engine — vsize calculation, fee distribution, change rounding.

Handles per-script-type vbyte sizes sourced from env config.
"""

from __future__ import annotations

from typing import List, Dict, Tuple, Optional
from collections import Counter

from .vsize import VsizeCalculator


class FeeResult:
    """Result of fee calculation for one participant."""

    def __init__(self, total_inputs: int, total_sats: int,
                 num_equal_outputs: int, num_change_outputs: int,
                 fee_share_sats: int, change_sats: int,
                 service_fee_sats: int):
        self.total_inputs = total_inputs
        self.total_sats = total_sats
        self.num_equal_outputs = num_equal_outputs
        self.num_change_outputs = num_change_outputs
        self.fee_share_sats = fee_share_sats
        self.change_sats = change_sats
        self.service_fee_sats = service_fee_sats


# Vsize defaults and calculation are in vsize.py (shared by PSBTManager and FeeEngine)


class FeeEngine:
    """Calculates fees for coinjoin participants.

    Uses per-script-type vbyte sizes for accurate vsize estimation.

    Two-tier fee model:
    - Tier 1 (Service): zap fee — FEE_PER_ELEMENT x (inputs + used_outputs)
    - Tier 2 (Miner): on-chain fee proportional to each participant's vsize contribution
    """

    def __init__(self, fee_per_element: int = 100,
                 min_fee_rate_sats: float = 1.5,
                 max_fee_rate_sats: float = 510,
                 overhead_vsize: int = 10,
                 minimum_utxo_size: int = 10000,
                 input_vsize_map: Optional[Dict[str, int]] = None,
                 output_vsize_map: Optional[Dict[str, int]] = None):
        self.fee_per_element = fee_per_element
        self._min_fee_rate_sats = min_fee_rate_sats
        self._max_fee_rate_sats = max_fee_rate_sats
        self._minimum_utxo_size = minimum_utxo_size
        self._vsize = VsizeCalculator(input_vsize_map, output_vsize_map, overhead_vsize)

    # --- Vsize helpers — delegates to VsizeCalculator ---

    def input_vsize(self, script_type: str) -> int:
        """Look up input vbytes for a script type. Falls back to p2wpkh."""
        return self._vsize.input_vsize(script_type)

    def output_vsize(self, script_type: str) -> int:
        """Look up output vbytes for a script type. Falls back to p2wpkh."""
        return self._vsize.output_vsize(script_type)

    def vsize_of_input(self, script_type: str) -> int:
        """Alias for input_vsize."""
        return self.input_vsize(script_type)

    def vsize_of_output(self, script_type: str) -> int:
        """Alias for output_vsize."""
        return self.output_vsize(script_type)

    def total_inputs_vsize(self, inputs_by_type: Dict[str, int]) -> int:
        """Compute total input vsize from a count-per-type dict."""
        return self._vsize.total_inputs_vsize(inputs_by_type)

    def total_outputs_vsize(self, outputs_by_type: Dict[str, int]) -> int:
        """Compute total output vsize from a count-per-type dict."""
        return self._vsize.total_outputs_vsize(outputs_by_type)

    def estimate_total_vsize(self, inputs_by_type: Dict[str, int],
                              outputs_by_type: Dict[str, int]) -> int:
        """Estimate vsize for the entire transaction, per-script-type."""
        return self._vsize.estimate_total_vsize(inputs_by_type, outputs_by_type)

    def compute_total_miner_fee(self, total_vsize: int, fee_rate: float) -> int:
        """Calculate the total miner fee in sats."""
        return int(total_vsize * fee_rate)

    # --- Per-participant weight ---

    def compute_participant_weight(self, inputs_by_type: Dict[str, int],
                                    outputs_by_type: Dict[str, int],
                                    total_input_vsize: int, total_output_vsize: int,
                                    total_vsize: int) -> float:
        """Compute a participant's proportional weight of the tx vsize."""
        return self._vsize.compute_participant_weight(
            inputs_by_type, outputs_by_type,
            total_input_vsize, total_output_vsize, total_vsize,
        )

    def compute_fee_share(self, my_weight: float, total_weight: float,
                          total_miner_fee: int) -> int:
        """Compute proportional miner fee for a participant."""
        if total_weight <= 0:
            return 0
        return int(total_miner_fee * my_weight / total_weight)

    # --- Service fee ---

    def calculate_service_fee(self, num_inputs: int, num_used_outputs: int) -> int:
        """Formula: FEE_PER_ELEMENT x (inputs + used_outputs)"""
        return self.fee_per_element * (num_inputs + num_used_outputs)

    # --- Output determination ---

    def determine_outputs(self, input_total_sats: int, output_size: int,
                          num_addresses_provided: int,
                          estimated_fee_share: int,
                          estimated_service_fee: int) -> Tuple[int, int, int, int]:
        """Determine how many equal outputs and change outputs for a participant.

        Returns: (num_equal_outputs, num_change_outputs, equal_output_sats, change_output_sats)
        """
        # Per the plan, the service fee is a Lightning zap — it does NOT come
        # out of the on-chain inputs. Only the miner fee_share reduces what's
        # available for outputs. The estimated_service_fee parameter is kept
        # for callers' convenience but no longer affects the math.
        available = input_total_sats - estimated_fee_share

        if available <= 0:
            return (0, 0, 0, 0)

        max_equal = available // output_size
        num_equal = min(max_equal, num_addresses_provided)

        total_equal_sats = num_equal * output_size
        remainder = available - total_equal_sats

        if num_equal == 0:
            return (0, 0, 0, 0)
        elif remainder >= self._minimum_utxo_size:
            num_change = 1
            change_amount = remainder
            if num_equal >= num_addresses_provided:
                num_equal = max(num_equal - 1, 0)
                total_equal_sats = num_equal * output_size
                remainder = available - total_equal_sats
                change_amount = remainder
        else:
            num_change = 0
            change_amount = 0

        return (num_equal, num_change, output_size, change_amount)

    # --- Full calculation ---

    def calculate_all_fees(self, participants_data: List[Dict],
                           output_size: int, fee_rate: float) -> Tuple[int, int, List[FeeResult]]:
        """Calculate fees for all participants.

        Args:
            participants_data: list with keys:
                - num_inputs, total_sats, num_addresses
                - inputs_by_type: dict[str,int] or None
                - outputs_by_type: dict[str,int] or None
            output_size: standardized output size
            fee_rate: sats/vbyte

        Returns: (total_vsize, total_miner_fee, list of FeeResult)
        """
        # Aggregate all input/output types for total vsize
        agg_inputs: Dict[str, int] = Counter()
        agg_outputs: Dict[str, int] = Counter()
        for p in participants_data:
            ibt = p.get("inputs_by_type") or {"p2wpkh": p["num_inputs"]}
            obt = p.get("outputs_by_type") or {"p2wpkh": p["num_addresses"]}
            for k, v in ibt.items():
                agg_inputs[k] += v
            for k, v in obt.items():
                agg_outputs[k] += v

        total_vsize = self.estimate_total_vsize(dict(agg_inputs), dict(agg_outputs))
        total_miner_fee = self.compute_total_miner_fee(total_vsize, fee_rate)

        total_weight = sum(
            self.compute_participant_weight(
                p.get("inputs_by_type") or {"p2wpkh": p["num_inputs"]},
                p.get("outputs_by_type") or {"p2wpkh": p["num_addresses"]},
                self.total_inputs_vsize(dict(agg_inputs)),
                self.total_outputs_vsize(dict(agg_outputs)),
                total_vsize,
            )
            for p in participants_data
        )

        results = []
        for p in participants_data:
            ibt = p.get("inputs_by_type") or {"p2wpkh": p["num_inputs"]}
            obt = p.get("outputs_by_type") or {"p2wpkh": p["num_addresses"]}
            weight = self.compute_participant_weight(ibt, obt,
                                                     self.total_inputs_vsize(dict(agg_inputs)),
                                                     self.total_outputs_vsize(dict(agg_outputs)),
                                                     total_vsize)
            fee_share = self.compute_fee_share(weight, total_weight, total_miner_fee)
            num_inputs_total = sum(ibt.values())
            num_outputs_total = sum(obt.values())
            service_fee = self.calculate_service_fee(num_inputs_total, num_outputs_total)

            num_equal, num_change, eq_amt, chg_amt = self.determine_outputs(
                p["total_sats"], output_size, p["num_addresses"],
                fee_share, service_fee,
            )

            results.append(FeeResult(
                total_inputs=num_inputs_total,
                total_sats=p["total_sats"],
                num_equal_outputs=num_equal,
                num_change_outputs=num_change,
                fee_share_sats=fee_share,
                change_sats=chg_amt,
                service_fee_sats=service_fee,
            ))

        return total_vsize, total_miner_fee, results

    def clamp_fee_rate(self, estimated_rate: float) -> float:
        """Clamp fee rate within bounds."""
        r = max(estimated_rate, self._min_fee_rate_sats)
        if self._max_fee_rate_sats > 0:
            r = min(r, self._max_fee_rate_sats)
        return r



