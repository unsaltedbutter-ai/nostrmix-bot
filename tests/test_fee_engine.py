"""Tests for FeeEngine — per-script-type vsize support."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.fee_engine import FeeEngine, FeeResult


class TestFeeEngine:
    def setup_method(self):
        self.engine = FeeEngine(
            fee_per_element=100,
            min_fee_rate_sats=1.5,
            max_fee_rate_sats=510,
            overhead_vsize=10,
            minimum_utxo_size=10000,
        )

    def test_vsize_estimate_simple(self):
        """Test vsize with uniform p2wpkh inputs/outputs."""
        inp = {"p2wpkh": 5}
        out = {"p2wpkh": 10}
        vsize = self.engine.estimate_total_vsize(inp, out)
        # overhead=10 + 5*70(p2wpkh input) + 10*35(p2wpkh output) = 10+350+350 = 710
        expected = 10 + (5 * 70) + (10 * 35)
        assert vsize == expected, f"{vsize} != {expected}"

    def test_vsize_estimate_mixed_types(self):
        """Test vsize with mixed input types."""
        inp = {"p2wpkh": 2, "p2tr": 1, "p2pkh": 1}
        out = {"p2wpkh": 3, "p2tr": 2}
        vsize = self.engine.estimate_total_vsize(inp, out)
        # overhead=10 + 2*70 + 1*60 + 1*150 + 3*35 + 2*45
        expected = 10 + 140 + 60 + 150 + 105 + 90
        assert vsize == expected

    def test_service_fee_simple(self):
        """Test service fee: 100 * (inputs + outputs)."""
        fee = self.engine.calculate_service_fee(2, 4)
        assert fee == 100 * (2 + 4)

    def test_service_fee_no_outputs(self):
        fee = self.engine.calculate_service_fee(1, 0)
        assert fee == 100

    def test_total_miner_fee(self):
        """Test total miner fee from vsize."""
        inp = {"p2wpkh": 5, "p2tr": 5}
        out = {"p2wpkh": 10}
        vsize = self.engine.estimate_total_vsize(inp, out)
        total = self.engine.compute_total_miner_fee(vsize, 30)
        assert total == int(vsize * 30)

    def test_total_inputs_vsize(self):
        """Test per-type input calculation."""
        inp = {"p2wpkh": 2, "p2tr": 1}
        total = self.engine.total_inputs_vsize(inp)
        assert total == (2 * 70) + (1 * 60)

    def test_total_outputs_vsize(self):
        """Test per-type output calculation."""
        out = {"p2wpkh": 5, "p2sh": 2}
        total = self.engine.total_outputs_vsize(out)
        assert total == (5 * 35) + (2 * 35)

    def test_input_vsize_lookup(self):
        """Test individual script type lookup."""
        assert self.engine.input_vsize("p2wpkh") == 70
        assert self.engine.input_vsize("p2tr") == 60
        assert self.engine.input_vsize("p2pkh") == 150
        assert self.engine.input_vsize("p2sh") == 135
        assert self.engine.input_vsize("unknown") == 70  # fallback

    def test_output_vsize_lookup(self):
        assert self.engine.output_vsize("p2wpkh") == 35
        assert self.engine.output_vsize("p2tr") == 45
        assert self.engine.output_vsize("p2wsh") == 45
        assert self.engine.output_vsize("unknown") == 35  # fallback

    def test_compute_participant_weight(self):
        """Test participant weight with per-type inputs."""
        ibt = {"p2wpkh": 2, "p2tr": 1}
        obt = {"p2wpkh": 4}
        # my_vsize = 2*70 + 1*70 + 4*35 = 140+70+140 = 350
        # overhead_share = 10 / 3 = 3.33 ish
        weight = self.engine.compute_participant_weight(
            ibt, obt,
            210,  # total_input_vsize
            140,  # total_output_vsize
            10 + 210 + 140,  # total_vsize
        )
        assert weight > 0

    def test_determine_outputs_enough_addresses(self):
        num_eq, num_ch, eq_amt, chg = self.engine.determine_outputs(
            input_total_sats=2_000_000,
            output_size=1_000_000,
            num_addresses_provided=4,
            estimated_fee_share=2000,
            estimated_service_fee=500,
        )
        assert num_eq >= 0
        assert chg >= 10000 or chg == 0

    def test_determine_outputs_insufficient_funds(self):
        num_eq, num_ch, eq_amt, chg = self.engine.determine_outputs(
            input_total_sats=100_000,
            output_size=1_000_000,
            num_addresses_provided=2,
            estimated_fee_share=2000,
            estimated_service_fee=500,
        )
        assert num_eq == 0 and num_ch == 0

    def test_clamp_fee_rate(self):
        clamped = self.engine.clamp_fee_rate(1000)
        assert clamped <= 510
        clamped = self.engine.clamp_fee_rate(0.5)
        assert clamped >= 1.5
        clamped = self.engine.clamp_fee_rate(30)
        assert clamped == 30

    @staticmethod
    def _nc(total, naddr, ninputs, itype="p2wpkh", conforming=0):
        """Build a non-conforming participant_data record (new format)."""
        return {
            "total_sats": total,
            "num_addresses": naddr,
            "conforming_count": conforming,
            "nonconforming_total_sats": total - conforming * 1_000_000,
            "nonconforming_inputs_by_type": {itype: ninputs},
            "output_type": "p2wpkh",
            "is_nonconforming": True,
        }

    @staticmethod
    def _conforming_only(count):
        """A participant who brought only conforming UTXOs (free pass-through)."""
        return {
            "total_sats": count * 1_000_000,
            "num_addresses": count,
            "conforming_count": count,
            "nonconforming_total_sats": 0,
            "nonconforming_inputs_by_type": {},
            "output_type": "p2wpkh",
            "is_nonconforming": False,
        }

    def test_calculate_all_fees_simple(self):
        """Three non-conforming participants, no conforming cap."""
        participants = [
            self._nc(5_000_000, 3, 2),
            self._nc(8_000_000, 4, 3),
            self._nc(2_000_000, 3, 1),
        ]
        total_vsize, total_miner_fee, results = self.engine.calculate_all_fees(
            participants, output_size=1_000_000, fee_rate=30
        )
        assert total_vsize > 0
        assert total_miner_fee > 0
        assert len(results) == 3
        for r in results:
            assert isinstance(r, FeeResult)
            assert r.service_fee_sats > 0
            assert r.fee_share_sats > 0

    def test_calculate_all_fees_mixed_types(self):
        """Mixed non-conforming input types."""
        participants = [
            self._nc(5_000_000, 3, 2, itype="p2wpkh"),
            self._nc(3_000_000, 2, 1, itype="p2tr"),
        ]
        total_vsize, total_miner_fee, results = self.engine.calculate_all_fees(
            participants, output_size=1_000_000, fee_rate=20
        )
        assert total_vsize > 0
        assert total_miner_fee > 0
        assert len(results) == 2

    def test_conforming_only_participant_pays_nothing(self):
        """A conforming-only participant pays no service fee and no miner fee;
        the non-conforming participant carries the whole fee."""
        participants = [
            self._nc(5_000_000, 3, 2),
            self._conforming_only(2),
        ]
        _v, total_miner_fee, results = self.engine.calculate_all_fees(
            participants, output_size=1_000_000, fee_rate=30,
        )
        nc, conf = results[0], results[1]
        assert conf.fee_share_sats == 0
        assert conf.service_fee_sats == 0
        assert conf.conforming_count == 2
        # The conforming-only participant's equal outputs come 1:1 from their
        # conforming UTXOs (laid out by the coordinator), not NC-derived.
        assert conf.num_equal_outputs == 0
        assert nc.fee_share_sats > 0
        # The lone NC participant covers the entire miner fee.
        assert nc.fee_share_sats >= total_miner_fee - 2

    def test_conforming_burden_split_evenly_across_nc(self):
        """The conforming burden (sized from the ACTUAL conforming UTXOs present)
        is split evenly across the non-conforming participants."""
        two_nc = [self._nc(5_000_000, 3, 1), self._nc(5_000_000, 3, 1)]
        _v0, fee0, r0 = self.engine.calculate_all_fees(
            two_nc, output_size=1_000_000, fee_rate=30)          # 0 conforming
        with_conf = two_nc + [self._conforming_only(4)]          # 4 conforming UTXOs
        _v1, fee1, r1 = self.engine.calculate_all_fees(
            with_conf, output_size=1_000_000, fee_rate=30)
        # Actual conforming UTXOs raise the total fee...
        assert fee1 > fee0
        # ...split evenly across the two NC participants (remainder to the last).
        extra_each = (fee1 - fee0) // 2
        assert abs(r1[0].fee_share_sats - r0[0].fee_share_sats - extra_each) <= 2
        assert abs(r1[1].fee_share_sats - r0[1].fee_share_sats - extra_each) <= 2
        # The conforming-only participant pays none of it.
        assert r1[2].fee_share_sats == 0

    def test_mixed_conforming_and_nonconforming_participant(self):
        """Gap #1 (unit): a participant bringing BOTH a conforming UTXO and a
        non-conforming UTXO. Equal outputs/fee come from the NON-conforming
        portion only; the conforming UTXO is a free pass-through."""
        mixed = {
            "total_sats": 1_000_000 + 2_500_000,
            "num_addresses": 4,
            "conforming_count": 1,
            "nonconforming_total_sats": 2_500_000,
            "nonconforming_inputs_by_type": {"p2wpkh": 1},
            "output_type": "p2wpkh",
            "is_nonconforming": True,
        }
        plain = self._nc(3_000_000, 3, 1)
        _v, _fee, results = self.engine.calculate_all_fees(
            [mixed, plain], output_size=1_000_000, fee_rate=30,
        )
        rmix = results[0]
        assert rmix.is_nonconforming
        assert rmix.conforming_count == 1
        # NC-derived equal outputs come from 2.5M only → 2, NOT from 3.5M.
        assert rmix.num_equal_outputs == 2
        assert rmix.fee_share_sats > 0
        # Service fee counts only the 1 non-conforming input + its NC outputs,
        # never the conforming UTXO (engine fee_per_element == 100 here).
        assert rmix.service_fee_sats == 100 * (
            1 + rmix.num_equal_outputs + rmix.num_change_outputs
        )
        # Conforming sats are preserved: total out == total in − fee_share.
        # total out = conforming(1)*size + nc_equal*size + change.
        total_out = (rmix.conforming_count + rmix.num_equal_outputs) * 1_000_000 \
            + rmix.change_sats
        assert total_out == 3_500_000 - rmix.fee_share_sats

    def test_total_fee_uses_actual_conforming_and_hits_target_rate(self):
        """The fee is sized from the ACTUAL conforming UTXOs present (not a cap),
        so it scales with the real fill AND the total equals vsize × rate (no
        over-collection)."""
        two_nc = [self._nc(5_000_000, 3, 1), self._nc(5_000_000, 3, 1)]
        _v0, fee0, _r0 = self.engine.calculate_all_fees(
            two_nc, output_size=1_000_000, fee_rate=30)
        _v2, fee2, _r2 = self.engine.calculate_all_fees(
            two_nc + [self._conforming_only(2)], output_size=1_000_000, fee_rate=30)
        v4, fee4, _r4 = self.engine.calculate_all_fees(
            two_nc + [self._conforming_only(4)], output_size=1_000_000, fee_rate=30)
        # More actual conforming -> higher total fee (NC subsidise them).
        assert fee0 < fee2 < fee4
        # And the total fee is the real vsize at the target rate (no over-collect).
        assert abs(fee4 - int(v4 * 30)) <= 2


class TestNcOutputPlan:
    """nc_output_plan: never burn an above-dust leftover when the participant
    gave us somewhere to put it. Address count is the hard output cap."""

    def setup_method(self):
        self.engine = FeeEngine(
            fee_per_element=0, min_fee_rate_sats=1.5, max_fee_rate_sats=510,
            overhead_vsize=10, minimum_utxo_size=10_000,
        )

    def test_funds_bound_spare_address_holds_change(self):
        """Fewer equal outputs than addresses -> the spare address takes the
        above-dust leftover as ordinary change (<= output_size here)."""
        # 1.5M with 3 addresses, size 1M: 1 equal + 0.5M change, address to spare.
        ne, nch, chg = self.engine.nc_output_plan(1_500_000, 1_000_000, 3, 0)
        assert (ne, nch, chg) == (1, 1, 500_000)

    def test_address_bound_sacrifices_last_equal_to_avoid_burn(self):
        """Every address would be an equal output AND there's an above-dust
        leftover -> give back the last equal slot so its address holds the
        change (which now EXCEEDS output_size), rather than burning the sats."""
        # 2.5M, only 2 addresses, size 1M: naive plan = 2 equal + 0.5M with no
        # address -> burn. New plan: 1 equal + 1.5M change. Nothing burnt.
        ne, nch, chg = self.engine.nc_output_plan(2_500_000, 1_000_000, 2, 0)
        assert (ne, nch, chg) == (1, 1, 1_500_000)
        assert chg > 1_000_000  # oversized change is acceptable
        # Conservation: every sat is in an output (no burn).
        assert ne * 1_000_000 + chg == 2_500_000

    def test_single_address_fully_used_leaves_leftover_for_caller(self):
        """One address, fully consumed by an equal output, above-dust leftover:
        no slot to spare (giving it up = zero mixed outputs), so num_change=1
        but the coordinator must donate/fold (no spare address to land it)."""
        ne, nch, chg = self.engine.nc_output_plan(2_500_000, 1_000_000, 1, 0)
        assert ne == 1 and nch == 1 and chg == 1_500_000
        # num_equal == addrs_for_nc and addrs == 1: caller can't place it.

    def test_subdust_leftover_folds_into_fee(self):
        """A sub-dust leftover is never worth its own output -> folded (no
        change), even when an address is free."""
        ne, nch, chg = self.engine.nc_output_plan(1_005_000, 1_000_000, 3, 0)
        assert (ne, nch, chg) == (1, 0, 0)

    def test_exact_fit_no_change(self):
        """Funds divide evenly into equal outputs -> no change at all."""
        ne, nch, chg = self.engine.nc_output_plan(3_000_000, 1_000_000, 5, 0)
        assert (ne, nch, chg) == (3, 0, 0)

    def test_fee_share_can_trigger_the_sacrifice(self):
        """The miner-fee bite shrinks 'available'; the plan still avoids a burn
        when addresses are the binding constraint."""
        # 2.0M, 2 addresses, size 1M, fee 5000: available 1.995M -> naive 1 equal
        # + 0.995M change with a spare address (funds-bound, not address-bound).
        ne, nch, chg = self.engine.nc_output_plan(2_000_000, 1_000_000, 2, 5_000)
        assert ne == 1 and nch == 1
        assert ne * 1_000_000 + chg == 1_995_000  # all available sats placed
