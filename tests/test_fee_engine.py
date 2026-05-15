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
        # overhead=10 + 2*70 + 1*70 + 1*150 + 3*35 + 2*45
        expected = 10 + 140 + 70 + 150 + 105 + 90
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
        assert total == (2 * 70) + (1 * 70)

    def test_total_outputs_vsize(self):
        """Test per-type output calculation."""
        out = {"p2wpkh": 5, "p2sh": 2}
        total = self.engine.total_outputs_vsize(out)
        assert total == (5 * 35) + (2 * 35)

    def test_input_vsize_lookup(self):
        """Test individual script type lookup."""
        assert self.engine.input_vsize("p2wpkh") == 70
        assert self.engine.input_vsize("p2tr") == 70
        assert self.engine.input_vsize("p2pkh") == 150
        assert self.engine.input_vsize("p2sh") == 255
        assert self.engine.input_vsize("unknown") == 70  # fallback

    def test_output_vsize_lookup(self):
        assert self.engine.output_vsize("p2wpkh") == 35
        assert self.engine.output_vsize("p2tr") == 45
        assert self.engine.output_vsize("p2wsh") == 4
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

    def test_calculate_all_fees_simple(self):
        """Test fee calculation with per-type data."""
        participants = [
            {"num_inputs": 2, "total_sats": 5_000_000, "num_addresses": 3,
             "inputs_by_type": {"p2wpkh": 2}, "outputs_by_type": {"p2wpkh": 3}},
            {"num_inputs": 3, "total_sats": 8_000_000, "num_addresses": 4,
             "inputs_by_type": {"p2wpkh": 3}, "outputs_by_type": {"p2wpkh": 4}},
            {"num_inputs": 1, "total_sats": 2_000_000, "num_addresses": 3,
             "inputs_by_type": {"p2wpkh": 1}, "outputs_by_type": {"p2wpkh": 3}},
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

    def test_calculate_all_fees_mixed_types(self):
        """Test fee calculation with mixed input types."""
        participants = [
            {"num_inputs": 2, "total_sats": 5_000_000, "num_addresses": 3,
             "inputs_by_type": {"p2wpkh": 1, "p2tr": 1}, "outputs_by_type": {"p2wpkh": 3}},
            {"num_inputs": 1, "total_sats": 3_000_000, "num_addresses": 2,
             "inputs_by_type": {"p2pkh": 1}, "outputs_by_type": {"p2sh": 2}},
        ]
        total_vsize, total_miner_fee, results = self.engine.calculate_all_fees(
            participants, output_size=1_000_000, fee_rate=20
        )
        assert total_vsize > 0
        assert total_miner_fee > 0
        assert len(results) == 2
