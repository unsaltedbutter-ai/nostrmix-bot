"""Fee Engine — vsize calculation, fee distribution, change rounding."""

from __future__ import annotations

from typing import List, Dict, Tuple, Optional


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


class FeeEngine:
    """Calculates fees for coinjoin participants.

    Two-tier fee model:
    - Tier 1 (Service): zap fee at commitment — FEE_PER_ELEMENT x (inputs + used_outputs)
    - Tier 2 (Miner): on-chain fee proportional to each participant's vsize contribution
    """

    def __init__(self, fee_per_element: int = 100,
                 fee_multiplier: float = 1.5,
                 min_fee_rate_sats: float = 1.5,
                 max_fee_rate_sats: float = 510,
                 input_vsize: int = 68,
                 output_vsize: int = 31,
                 overhead_vsize: int = 10,
                 minimum_utxo_size: int = 10000):
        self.fee_per_element = fee_per_element
        self._min_fee_rate_sats = min_fee_rate_sats
        self._max_fee_rate_sats = max_fee_rate_sats
        self._input_vsize = input_vsize
        self._output_vsize = output_vsize
        self._overhead_vsize = overhead_vsize
        self._minimum_utxo_size = minimum_utxo_size

    def estimate_total_vsize(self, total_inputs: int, total_outputs: int) -> int:
        """Estimate vsize for the entire transaction."""
        return self._overhead_vsize + (total_inputs * self._input_vsize) + (total_outputs * self._output_vsize)

    def compute_total_miner_fee(self, total_vsize: int, fee_rate: float) -> int:
        """Calculate the total miner fee in sats."""
        return int(total_vsize * fee_rate)

    def compute_participant_weight(self, num_inputs: int, num_outputs: int,
                                   total_inputs: int, total_outputs: int,
                                   total_vsize: int) -> float:
        """Compute a participant's proportional weight of the tx vsize."""
        my_vsize = (num_inputs * self._input_vsize) + (num_outputs * self._output_vsize)
        overhead_share = self._overhead_vsize / max(total_inputs, 1)
        return my_vsize + overhead_share

    def compute_fee_share(self, my_weight: float, total_weight: float,
                          total_miner_fee: int) -> int:
        """Compute proportional miner fee for a participant."""
        if total_weight <= 0:
            return 0
        return int(total_miner_fee * my_weight / total_weight)

    def calculate_service_fee(self, num_inputs: int, num_used_outputs: int) -> int:
        """Calculate the service (zap) fee for a participant.

        Formula: FEE_PER_ELEMENT x (inputs + used_outputs)
        """
        return self.fee_per_element * (num_inputs + num_used_outputs)

    def determine_outputs(self, input_total_sats: int, output_size: int,
                          num_addresses_provided: int,
                          estimated_fee_share: int,
                          estimated_service_fee: int) -> Tuple[int, int, int, int]:
        """Determine how many equal outputs and change outputs for a participant.

        Args:
            input_total_sats: total BTC from this participant's inputs (sats)
            output_size: standardized output size for this mix (sats)
            num_addresses_provided: number of output addresses the participant provided
            estimated_fee_share: estimated on-chain fee share
            estimated_service_fee: service fee

        Returns:
            (num_equal_outputs, num_change_outputs, equal_output_sats, change_output_sats)
            - equal_output_sats = output_size
            - change_output_sats may be 0 if change would be below MINIMUM_UTXO_SIZE
            - num_equal_outputs is capped by available addresses and funds
        """
        # Funds available after fees
        available = input_total_sats - estimated_fee_share - estimated_service_fee

        if available <= 0:
            return (0, 0, 0, 0)

        # Maximum number of equal-sized outputs
        max_equal = available // output_size

        # Cap by addresses provided (but keep one for change if needed)
        # We can make at most num_addresses_provided total outputs (equal + change)
        num_equal = min(max_equal, num_addresses_provided)

        # But we may need one for change
        if num_equal == num_addresses_provided and num_addresses_provided < max_equal:
            # We could make more equal outputs but no more addresses
            # The last address becomes a change output larger than standard
            pass

        # Calculate proposed change
        total_equal_sats = num_equal * output_size
        remainder = available - total_equal_sats

        # Determine change output
        if num_equal == 0:
            # No equal outputs possible — don't create a change output either
            num_change = 0
            change_amount = 0
        elif remainder >= self._minimum_utxo_size:
            # Use one address for change
            num_change = 1
            change_amount = remainder
            # If we used an address for change, we can't use it for equal outputs
            if num_equal >= num_addresses_provided:
                # We need at least 1 address for change
                num_equal = max(num_equal - 1, 0)
                total_equal_sats = num_equal * output_size
                remainder = available - total_equal_sats
                change_amount = remainder
        else:
            # Change is too small, add to miner fee
            num_change = 0
            change_amount = 0

        return (num_equal, num_change, output_size, change_amount)

    def total_output_count(self, participants: int, num_equal_per_participant: int,
                           num_change_count: int) -> int:
        """Calculate total outputs across all participants."""
        return (participants * num_equal_per_participant) + num_change_count

    def calculate_all_fees(self, participants_data: List[Dict],
                           output_size: int, fee_rate: float) -> Tuple[int, int, List[FeeResult]]:
        """Calculate fees for all participants.

        Args:
            participants_data: list of dicts with keys: num_inputs, total_sats, num_addresses
            output_size: standardized output size
            fee_rate: sats/vbyte

        Returns:
            (total_vsize, total_miner_fee, list of FeeResult)
        """
        total_inputs = sum(p["num_inputs"] for p in participants_data)
        total_outputs = sum(p["num_addresses"] for p in participants_data)

        total_vsize = self.estimate_total_vsize(total_inputs, total_outputs)
        total_miner_fee = self.compute_total_miner_fee(total_vsize, fee_rate)

        total_weight = sum(
            self.compute_participant_weight(p["num_inputs"], p["num_addresses"],
                                            total_inputs, total_outputs, total_vsize)
            for p in participants_data
        )

        results = []
        for p in participants_data:
            weight = self.compute_participant_weight(
                p["num_inputs"], p["num_addresses"],
                total_inputs, total_outputs, total_vsize,
            )
            fee_share = self.compute_fee_share(weight, total_weight, total_miner_fee)

            service_fee = self.calculate_service_fee(
                p["num_inputs"], p["num_addresses"]
            )

            num_equal, num_change, eq_amt, chg_amt = self.determine_outputs(
                p["total_sats"], output_size, p["num_addresses"],
                fee_share, service_fee,
            )

            results.append(FeeResult(
                total_inputs=p["num_inputs"],
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

    def vsize_from_parts(self, num_inputs: int, num_outputs: int) -> int:
        """Shortcut vsize estimate."""
        return self.estimate_total_vsize(num_inputs, num_outputs)

    def round_output_sats(self, output_size: int) -> int:
        """Round output size to nearest common increment (not strictly needed but utility)."""
        return output_size
