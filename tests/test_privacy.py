"""Tests for PrivacyCheck."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.privacy import PrivacyCheck


class TestPrivacyCheck:
    def setup_method(self):
        self.check = PrivacyCheck()

    def test_count_equal_outputs(self):
        """Test counting equal outputs from amount groups."""
        groups = {1000: 3, 2000: 1, 500: 2}
        count = PrivacyCheck.count_equal_outputs(groups)
        assert count == 3  # largest group is 3 outputs of 1000

    def test_count_equal_outputs_empty(self):
        """Test empty groups."""
        count = PrivacyCheck.count_equal_outputs({})
        assert count == 0

    def test_count_equal_outputs_single(self):
        """Test single group."""
        groups = {1000: 5}
        assert PrivacyCheck.count_equal_outputs(groups) == 5
