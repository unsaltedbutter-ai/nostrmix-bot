"""Privacy Module — non-authoritative PSBT sanity check.

The bar we enforce is intentionally simple: a mix must produce at least 2
equal-size (output_size) outputs drawn from at least 2 inputs (non-conforming
and conforming counted together). That breaks the 1:1 coin↔owner link, which is
the bot's purpose. We deliberately do NOT attempt subset-sum / N!/2 partition
counting; users who want stronger anonymity re-mix the outputs in later rounds.
The coordinator calls check_psbt with floor = max(2, required_nonconforming).
"""

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

        ``num_participants`` is the anonymity-set floor. Under the
        conforming/non-conforming model the coordinator passes the mix's
        required non-conforming participant count (N distinct equal-output
        contributors); conforming UTXOs only add more equal outputs. The check
        is a non-authoritative sanity guard: there must be at least N outputs,
        and the largest equal-value group must be at least N.
        """
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction

        try:
            psbt_bytes = bytes.fromhex(psbt_hex)
            # The PSBT constructor is keyword-only; bytes deserialization
            # goes through from_binary.
            psbt_obj = PartiallySignedBitcoinTransaction.from_binary(psbt_bytes)

            # bitcointx's PSBT_Output objects carry derivation paths and the
            # like — NOT amounts. The actual output values live on the
            # unsigned transaction's vout list.
            vouts = psbt_obj.unsigned_tx.vout if psbt_obj.unsigned_tx else []
            if len(vouts) < num_participants:
                return False, (
                    f"Too few outputs: {len(vouts)} for {num_participants} participants. "
                    f"Need at least {num_participants}."
                )

            # Group outputs by amount (nValue on CTxOut).
            from collections import Counter
            amount_counts = Counter(o.nValue for o in vouts)
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
