"""Tests for PSBTManager — per-script-type vsize support."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.psbt_manager import PSBTManager


class TestPSBTManager:
    def setup_method(self):
        self.mgr = PSBTManager()

    def test_estimate_vsize_p2wpkh_only(self):
        """Test vsize for uniform p2wpkh inputs/outputs."""
        inp = {"p2wpkh": 5}
        out = {"p2wpkh": 10}
        vsize = self.mgr.estimate_vsize(inp, out)
        expected = 10 + (5 * 70) + (10 * 35)
        assert vsize == expected

    def test_estimate_vsize_mixed(self):
        """Test vsize for mixed script types."""
        inp = {"p2wpkh": 2, "p2tr": 1, "p2sh": 1}
        out = {"p2wpkh": 4, "p2sh": 2}
        vsize = self.mgr.estimate_vsize(inp, out)
        # 10 + 2*70 + 1*70 + 1*255 + 4*35 + 2*35 = 10+140+70+255+140+70 = 685
        assert vsize == 685

    def test_estimate_vsize_p2tr_outputs(self):
        """Test with p2tr outputs (45 vB each)."""
        inp = {"p2wpkh": 3}
        out = {"p2tr": 4}
        vsize = self.mgr.estimate_vsize(inp, out)
        expected = 10 + (3 * 70) + (4 * 45)
        assert vsize == expected

    def test_input_vsize_lookup(self):
        assert self.mgr.input_vsize("p2wpkh") == 70
        assert self.mgr.input_vsize("p2tr") == 70
        assert self.mgr.input_vsize("p2pkh") == 150
        assert self.mgr.input_vsize("p2sh") == 255
        assert self.mgr.input_vsize("p2wsh") == 1455

    def test_output_vsize_lookup(self):
        assert self.mgr.output_vsize("p2wpkh") == 35
        assert self.mgr.output_vsize("p2tr") == 45
        assert self.mgr.output_vsize("p2wsh") == 4
        assert self.mgr.output_vsize("p2pkh") == 35

    def test_needs_chunking_small(self):
        small_hex = "abc" * 100
        assert not self.mgr.needs_chunking(small_hex)

    def test_needs_chunking_large(self):
        large_hex = "a" * 60000
        assert self.mgr.needs_chunking(large_hex)

    def test_chunk_psbt_small(self):
        small = "abc123"
        chunks = self.mgr.chunk_psbt(small)
        assert len(chunks) == 1
        assert chunks[0] == small

    def test_chunk_psbt_large(self):
        large = "a" * 150000
        chunks = self.mgr.chunk_psbt(large)
        assert len(chunks) >= 2
        reassembled = "".join(chunks)
        assert reassembled == large

    def test_max_psbt_size_constant(self):
        assert self.mgr.MAX_PSBT_HEX_SIZE == 50000
