"""Fee Engine — vsize calculation, fee distribution, change rounding.

Handles per-script-type vbyte sizes sourced from env config.
"""

from __future__ import annotations

from typing import List, Dict, Tuple, Optional
from collections import Counter

from .vsize import VsizeCalculator


class FeeResult:
    """Result of fee calculation for one participant.

    ``num_equal_outputs`` counts ONLY the equal outputs carved out of the
    participant's non-conforming inputs. ``conforming_count`` is the number of
    pass-through conforming outputs (one per conforming UTXO they brought),
    which are free and laid out separately by the coordinator. A participant's
    total equal outputs is therefore ``num_equal_outputs + conforming_count``.
    """

    def __init__(self, total_inputs: int, total_sats: int,
                 num_equal_outputs: int, num_change_outputs: int,
                 fee_share_sats: int, change_sats: int,
                 service_fee_sats: int, conforming_count: int = 0,
                 is_nonconforming: bool = True):
        self.total_inputs = total_inputs
        self.total_sats = total_sats
        self.num_equal_outputs = num_equal_outputs
        self.num_change_outputs = num_change_outputs
        self.fee_share_sats = fee_share_sats
        self.change_sats = change_sats
        self.service_fee_sats = service_fee_sats
        self.conforming_count = conforming_count
        self.is_nonconforming = is_nonconforming


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

    def calculate_service_fee(self, num_inputs: int, num_used_outputs: int,
                              fee_per_element: Optional[int] = None) -> int:
        """Formula: FEE_PER_ELEMENT x (inputs + used_outputs).

        Only NON-conforming inputs/outputs should be passed here — conforming
        pass-throughs are always free. ``fee_per_element`` overrides the engine
        default so a per-mix fee (mix.fee_per_element) is honoured; 0 disables
        the service fee entirely.
        """
        fpe = self.fee_per_element if fee_per_element is None else fee_per_element
        return fpe * (num_inputs + num_used_outputs)

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

    def nc_output_plan(self, nc_total: int, output_size: int,
                       addrs_for_nc: int, fee_share: int) -> Tuple[int, int, int]:
        """Non-conforming output layout.

        Returns (num_equal, num_change, change_sats).

        Goal: maximise equal (mixed) outputs, but NEVER burn an above-dust
        leftover when the participant gave us somewhere to send it. The address
        count is the hard cap on outputs — a participant who supplies A NC
        addresses gets at most A outputs.

          * If the funds are the binding constraint (fewer equal outputs than
            addresses), the spare address holds the above-dust leftover as change.
          * If addresses are the binding constraint (equal outputs would consume
            every address) AND there's an above-dust leftover, we sacrifice the
            LAST equal output so its address can hold the change instead — even
            though that change then exceeds output_size. Giving up one mixed
            output is far better than burning 10ks/100ks of sats. This needs
            >=2 addresses (so >=1 mixed output survives); with a single address
            there's no slot to spare, so the leftover is left for the coordinator
            to donate/fold (num_change=1 but no spare address).
          * A sub-dust leftover is always absorbed into the miner fee
            (num_change=0) — too small to be worth its own output.
        """
        available = nc_total - fee_share
        if available <= 0 or addrs_for_nc <= 0:
            return (0, 0, 0)
        num_equal = min(available // output_size, addrs_for_nc)
        if num_equal == 0:
            return (0, 0, 0)
        remainder = available - num_equal * output_size
        if remainder < self._minimum_utxo_size:
            return (num_equal, 0, 0)
        # Above-dust leftover. If a spare address is free (funds-bound), it
        # becomes change there.
        if num_equal < addrs_for_nc:
            return (num_equal, 1, remainder)
        # Address-bound: every address is taken by an equal output. Rather than
        # burn/donate the leftover, give back the last equal slot and roll its
        # output_size into the change (change > output_size). Requires >=2
        # addresses so at least one mixed output remains.
        if addrs_for_nc >= 2:
            num_equal -= 1
            return (num_equal, 1, available - num_equal * output_size)
        # Single address, fully used: nothing to spare. Caller donates/folds.
        return (num_equal, 1, remainder)

    # --- Full calculation ---

    def calculate_all_fees(self, participants_data: List[Dict],
                           output_size: int, fee_rate: float,
                           conf_input_type: str = "p2wpkh",
                           conf_output_type: str = "p2wpkh") -> Tuple[int, int, List[FeeResult]]:
        """Calculate fees under the conforming / non-conforming model.

        Args:
            participants_data: list with keys:
                - pid (optional, echoed back via FeeResult ordering)
                - total_sats: ALL inputs (conforming + non-conforming)
                - num_addresses: count of output addresses provided
                - conforming_count: number of conforming UTXOs (amount==output_size)
                - nonconforming_total_sats: sum of non-conforming input amounts
                - nonconforming_inputs_by_type: dict[str,int] of NC inputs
                - output_type: script type of this participant's outputs
                - is_nonconforming: True if they brought >=1 non-conforming UTXO
            output_size: standardized equal output size
            fee_rate: sats/vbyte
            conf_input_type / conf_output_type: script types used to size the
                conforming input/output vbytes.

        The conforming miner burden is computed from the ACTUAL number of
        conforming UTXOs present (summed from participants_data), not the mix's
        MAX_CONFORMING_UTXOS cap. The cap only bounds intake during collecting;
        by the time fees are computed (assembly, the frozen participant set that
        is sent for signing) the real conforming count is known, so we target
        the correct fee rather than over-collecting for slots that never filled.

        Returns: (total_fee_vsize, total_miner_fee, list of FeeResult)

        Fee model:
          * conforming-only participants pay NOTHING (fee_share = 0); their
            conforming UTXOs map 1->1 to equal outputs.
          * each non-conforming participant pays a proportional share of the
            *non-conforming portion* of the tx (overhead + NC inputs + NC-derived
            outputs), by their input+output vsize weight, PLUS an even 1/N slice
            of the conforming burden (the ACTUAL conforming UTXOs present).
        """
        nc = [p for p in participants_data if p.get("is_nonconforming")]
        num_nc = len(nc)
        overhead = self._vsize.overhead

        # Conforming burden — sized from the ACTUAL conforming UTXOs present in
        # this (frozen) participant set, so the total fee matches the real tx
        # vsize at the target rate. Split evenly across the non-conforming
        # participants (conforming-only participants pay nothing).
        present_conforming = sum(p.get("conforming_count", 0) for p in participants_data)
        conf_unit_vsize = (self.input_vsize(conf_input_type)
                           + self.output_vsize(conf_output_type))
        conforming_burden_vsize = present_conforming * conf_unit_vsize
        conforming_burden_fee = int(conforming_burden_vsize * fee_rate)

        def _nc_layout(rec: Dict, fee_share: int) -> Tuple[int, int, int]:
            """(num_equal, num_change, change_sats) carved from NC inputs via
            nc_output_plan. With >=2 addresses an above-dust leftover always
            lands in a change output (the plan gives back the last equal slot if
            needed) rather than being burnt; only the single-address case can
            still donate/fold. The change output counts for vsize/fee here."""
            addrs_for_nc = max(0, rec.get("num_addresses", 0)
                               - rec.get("conforming_count", 0))
            return self.nc_output_plan(
                rec.get("nonconforming_total_sats", 0), output_size,
                addrs_for_nc, fee_share,
            )

        # Iterate: NC-derived output count depends on fee_share, and fee_share
        # depends on NC-derived output vsize. Converges fast (trimming outputs
        # only shrinks the next pass's fee). Distribute the proportional
        # remainder and the conforming-burden remainder to the last NC
        # participant so the shares sum exactly to total_miner_fee.
        fee_shares: Dict[int, int] = {id(rec): 0 for rec in nc}
        nc_portion_vsize = overhead
        nc_portion_fee = 0
        for _pass in range(3):
            weights: Dict[int, int] = {}
            nc_in_v_total = 0
            nc_out_v_total = 0
            for rec in nc:
                ne, nch, chg = _nc_layout(rec, fee_shares[id(rec)])
                in_v = self.total_inputs_vsize(
                    rec.get("nonconforming_inputs_by_type") or {})
                out_v = (ne + nch) * self.output_vsize(
                    rec.get("output_type", "p2wpkh"))
                weights[id(rec)] = in_v + out_v
                nc_in_v_total += in_v
                nc_out_v_total += out_v
            nc_portion_vsize = overhead + nc_in_v_total + nc_out_v_total
            nc_portion_fee = int(nc_portion_vsize * fee_rate)
            total_weight = sum(weights.values())

            new_shares: Dict[int, int] = {}
            own_running = 0
            burden_running = 0
            for i, rec in enumerate(nc):
                last = (i == num_nc - 1)
                if total_weight > 0:
                    own = int(nc_portion_fee * weights[id(rec)] / total_weight)
                else:
                    own = 0
                if last:
                    own = nc_portion_fee - own_running           # absorb remainder
                    burden = conforming_burden_fee - burden_running
                else:
                    burden = conforming_burden_fee // num_nc if num_nc else 0
                own_running += own
                burden_running += burden
                new_shares[id(rec)] = max(0, own) + max(0, burden)
            if new_shares == fee_shares:
                break
            fee_shares = new_shares

        total_fee_vsize = nc_portion_vsize + conforming_burden_vsize
        total_miner_fee = nc_portion_fee + conforming_burden_fee

        # Emit results in the original participant order.
        results: List[FeeResult] = []
        for p in participants_data:
            conforming_count = p.get("conforming_count", 0)
            if p.get("is_nonconforming"):
                # Recompute the layout from the FINAL fee share so num_equal /
                # change_sats are consistent with the fee_share we report. The
                # iteration loop's intermediate `layouts` lagged the shares by
                # one pass (it stored the layout BEFORE updating the share), and
                # for a participant whose surplus is near one output_size the
                # output count oscillates and never converges within 3 passes —
                # emitting a stale layout would (a) tell the user a fee they
                # don't actually pay and (b) hide a participant who can no longer
                # fund an equal output from the coordinator's underfunded-drop
                # check. Deriving the layout from the final share here makes
                # per-participant accounting exact and surfaces ne==0 correctly.
                fee_share = fee_shares.get(id(p), 0)
                ne, nch, chg = _nc_layout(p, fee_share)
                num_inputs_total = sum(
                    (p.get("nonconforming_inputs_by_type") or {}).values())
                service_fee = self.calculate_service_fee(num_inputs_total, ne + nch)
            else:
                # Conforming-only: free pass-through. Equal outputs == conforming
                # UTXO count; no NC-derived outputs, no change, no fees.
                ne, nch, chg = 0, 0, 0
                fee_share = 0
                num_inputs_total = 0
                service_fee = 0

            results.append(FeeResult(
                total_inputs=num_inputs_total + conforming_count,
                total_sats=p.get("total_sats", 0),
                num_equal_outputs=ne,
                num_change_outputs=nch,
                fee_share_sats=fee_share,
                change_sats=chg,
                service_fee_sats=service_fee,
                conforming_count=conforming_count,
                is_nonconforming=bool(p.get("is_nonconforming")),
            ))

        return total_fee_vsize, total_miner_fee, results

    def clamp_fee_rate(self, estimated_rate: float) -> float:
        """Clamp fee rate within bounds."""
        r = max(estimated_rate, self._min_fee_rate_sats)
        if self._max_fee_rate_sats > 0:
            r = min(r, self._max_fee_rate_sats)
        return r



