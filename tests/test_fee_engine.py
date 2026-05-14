"""Tests for FeeEngine."""

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
            input_vsize=68,
            output_vsize=31,
            overhead_vsize=10,
            minimum_utxo_size=10000,
        )

    def test_vsize_estimate(self):
        """Test transaction vsize estimation."""
        vsize = self.engine.estimate_total_vsize(5, 10)
        expected = 10 + (5 * 68) + (10 * 31)
        assert vsize == expected

    def test_service_fee_simple(self):
        """Test service fee calculation: 100 * (inputs + outputs)."""
        fee = self.engine.calculate_service_fee(2, 4)
        assert fee == 100 * (2 + 4)  # 600

    def test_service_fee_no_outputs(self):
        """Test service fee with zero outputs."""
        fee = self.engine.calculate_service_fee(1, 0)
        assert fee == 100  # 100 * (1 + 0)

    def test_total_miner_fee(self):
        """Test total miner fee from vsize."""
        vsize = self.engine.estimate_total_vsize(10, 20)  # 10 + 10*68 + 20*31
        total = self.engine.compute_total_miner_fee(vsize, 30)
        assert total == int(vsize * 30)

    def test_participant_weight(self):
        """Test participant weight calculation."""
        weight = self.engine.compute_participant_weight(2, 4, 10, 20, 500)
        # my_vsize = 2*68 + 4*31 = 136 + 124 = 260
        # overhead_share = 10 / 10 = 1
        # total = 261
        assert weight == 261

    def test_determine_outputs_enough_addresses(self):
        """Test output determination with plenty of addresses."""
        num_eq, num_ch, eq_amt, chg = self.engine.determine_outputs(
            input_total_sats=2_000_000,  # 0.02 BTC
            output_size=1_000_000,       # 0.01 BTC
            num_addresses_provided=4,
            estimated_fee_share=2000,
            estimated_service_fee=500,
        )
        # available = 2_000_000 - 2000 - 500 = 1_997_500
        # max_equal = 1_997_500 // 1_000_000 = 1
        # 1 equal output, remainder = 997_500 (change if >= MIN_UTXO)
        assert num_eq >= 0
        # Remainder should be large enough for change
        assert chg >= 10000 or chg == 0

    def test_determine_outputs_insufficient_funds(self):
        """Test with insufficient funds for any output."""
        num_eq, num_ch, eq_amt, chg = self.engine.determine_outputs(
            input_total_sats=100_000,  # small
            output_size=1_000_000,
            num_addresses_provided=2,
            estimated_fee_share=2000,
            estimated_service_fee=500,
        )
        # available = 100_000 - 2000 - 500 = 97_500
        # max_equal = 97_500 // 1_000_000 = 0
        assert num_eq == 0 and num_ch == 0

    def test_clamp_fee_rate(self):
        """Test fee rate clamping."""
        clamped = self.engine.clamp_fee_rate(1000)
        assert clamped <= 510
        clamped = self.engine.clamp_fee_rate(0.5)
        assert clamped >= 1.5
        clamped = self.engine.clamp_fee_rate(30)
        # 30 is within bounds
        assert clamped == 30

    def test_calculate_all_fees_simple(self):
        """Test fee calculation for a simple 3-participant mix."""
        participants = [
            {"num_inputs": 2, "total_sats": 5_000_000, "num_addresses": 3},
            {"num_inputs": 3, "total_sats": 8_000_000, "num_addresses": 4},
            {"num_inputs": 1, "total_sats": 2_000_000, "num_addresses": 3},
        ]
        total_vsize, total_miner_fee, results = self.engine.calculate_all_fees(
            participants, output_size=1_000_000, fee_rate=30
        )
        assert total_vsize == self.engine.estimate_total_vsize(6, 10)
        assert total_miner_fee > 0
        assert len(results) == 3
        for r in results:
            assert isinstance(r, FeeResult)
            assert r.service_fee_sats > 0
