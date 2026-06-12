"""Privacy Module — non-authoritative PSBT sanity check.

The bar this module enforces is intentionally simple and counts OUTPUTS ONLY,
by value: the transaction must have at least N outputs, and its largest
equal-value group must be at least N (the coordinator passes
N = max(2, required_nonconforming)). That's a cheap guard against an assembly
bug that produced a degenerate, obviously-unmixed transaction.

What it deliberately does NOT do:
  * It does NOT attribute outputs to owners. Two equal outputs that both belong
    to one participant still count toward the group — and that's fine: one
    participant legitimately receives several equal outputs, and the distinct-
    party guarantee (≥2 separate parties contributing equal outputs) is enforced
    upstream by the coordinator's _classify_ready solo_ok check, not here.
  * It does NOT count or verify inputs, and makes no subset-sum / N!/2 partition
    claim. Stronger anonymity is the user's choice via re-mixing in later rounds.

So this is a sanity smoke test, not an anonymity proof; the coordinator logs a
failure and continues (the real proceed/abort decision lives in _classify_ready).
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
        """Check if a PSBT meets the minimum-structure sanity bar.

        ``num_participants`` is the floor N the coordinator passes
        (max(2, required_nonconforming)). The check, by output VALUE only:
        there must be at least N outputs, and the largest equal-value group
        must be at least N. It does not attribute outputs to owners (one
        participant may hold several equal outputs) and does not inspect inputs
        — distinct-party contribution is guaranteed upstream by _classify_ready,
        not here. Non-authoritative: the coordinator logs failures and proceeds.
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
            # The message is logged by the coordinator — keep str(e) out of
            # it: bitcointx exceptions can embed PSBT fragments (addresses,
            # scripts). The class name is enough to triage.
            return False, f"Privacy check error: {type(e).__name__}"
