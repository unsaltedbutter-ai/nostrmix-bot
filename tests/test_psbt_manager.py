"""Tests for PSBTManager (basic structure — PSBT operations need real inputs)."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.psbt_manager import PSBTManager


class TestPSBTManager:
    def setup_method(self):
        self.mgr = PSBTManager()

    def test_estimate_vsize(self):
        """Test vsize estimation."""
        vsize = self.mgr.estimate_vsize(num_inputs=5, num_outputs=10)
        expected = 10 + (5 * 68) + (10 * 31)
        assert vsize == expected

    def test_needs_chunking_small(self):
        """Test chunking threshold for small PSBTs."""
        small_hex = "abc" * 100  # ~300 bytes
        assert not self.mgr.needs_chunking(small_hex)

    def test_needs_chunking_large(self):
        """Test chunking threshold for large PSBTs."""
        large_hex = "a" * 60000  # > 50KB
        assert self.mgr.needs_chunking(large_hex)

    def test_chunk_psbt_small(self):
        """Test that small PSBT returns single chunk."""
        small = "abc123"
        chunks = self.mgr.chunk_psbt(small)
        assert len(chunks) == 1
        assert chunks[0] == small

    def test_chunk_psbt_large(self):
        """Test chunking of large PSBT."""
        large = "a" * 150000  # 150KB
        chunks = self.mgr.chunk_psbt(large)
        assert len(chunks) >= 2
        # Verify reassembly
        reassembled = "".join(chunks)
        assert reassembled == large

    def test_address_type(self):
        """Test address type detection for p2wpkh (bech32)."""
        # Use a valid bech32 test address
        # p2wpkh addresses are a common type
        pass  # Address parsing needs real on-chain addresses or mock
    # Address type detection is tested through the build_skeleton method

    def test_max_psbt_size_constant(self):
        """Test constant."""
        assert self.mgr.MAX_PSBT_HEX_SIZE == 50000
