"""Privacy Module — PSBT sanity check for output partitioning."""

from __future__ import annotations

from typing import List, Dict, Set, Tuple


class PrivacyCheck:
    """Sanity-check output partitioning in a coinjoin PSBT."""

    @staticmethod
    def count_equal_outputs(output_groups: Dict[int, int]) -> int:
        """Return size of the largest equal-output group."""
        if not output_groups:
            return 0
        return max(output_groups.values())

    def check_psbt(self, psbt_hex: str, num_participants: int) -> Tuple[bool, str]:
        """Check if a PSBT meets minimum privacy requirements.

        At minimum: at least N equal outputs for N participants.
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            psbt_bytes = bytes.fromhex(psbt_hex)
            # The PSBT constructor is keyword-only; bytes deserialization
            # goes through from_binary.
            psbt_obj = PartiallySignedBitcoinTransaction.from_binary(psbt_bytes)

            outputs = psbt_obj.outputs
            if len(outputs) < num_participants:
                return False, (
                    f"Too few outputs: {len(outputs)} for {num_participants} participants. "
                    f"Need at least {num_participants}."
                )

            # Group outputs by amount
            from collections import Counter
            amount_counts = Counter(out.amount for out in outputs)
            largest_group = max(amount_counts.values()) if amount_counts else 0

            if largest_group < num_participants:
                return False, (
                    f"Largest equal-output group has {largest_group} outputs, "
                    f"below minimum of {num_participants}."
                )

            return True, (
                f"Privacy check passed: {largest_group} identical outputs "
                f"for {num_participants} participants."
            )

        except Exception as e:
            return False, f"Privacy check error: {str(e)}"
