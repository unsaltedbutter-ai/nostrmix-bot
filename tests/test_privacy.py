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


# Valid mainnet p2wpkh addresses for building real PSBT skeletons.
_ADDRS = [
    "bc1q4gyakdgygyc8qweh39qxamywc9vdvrt82jsrcj",
    "bc1q670lslr8tlv9w5kk4zw7ckha74ll6lx48tnsks",
    "bc1qa9d476j967wv6xdq3zcxncqgufj3evm0qakga4",
    "bc1qcsz06k58myv2az3uy35krphtw6m4rzs7jmsy96",
]
# A valid p2wpkh scriptPubKey: OP_0 <20-byte-hash>.
_FAKE_SPK = "0014" + "00" * 20


def _skeleton_with_equal_outputs(num_equal, output_size=1_000_000, change=0):
    """Build a real PSBT skeleton with `num_equal` output_size outputs (+ an
    optional distinct change output) so check_psbt can be exercised against an
    actual transaction rather than a hand-rolled group dict."""
    from src.psbt_manager import PSBTManager
    mgr = PSBTManager()
    total = num_equal * output_size + change + 50_000  # leave room for a "fee"
    inputs = [{
        "txid": "ab" * 32, "vout": 0, "amount": total,
        "script_type": "p2wpkh", "scriptpubkey": _FAKE_SPK,
    }]
    outputs = [{"address": _ADDRS[i], "amount": output_size} for i in range(num_equal)]
    if change > 0:
        outputs.append({"address": _ADDRS[num_equal], "amount": change})
    return mgr.build_skeleton(inputs, outputs)


class TestCheckPsbtFloor:
    """check_psbt is the non-authoritative privacy guard the coordinator now
    calls with the mix's required_nonconforming count as the floor. Exercise it
    against real PSBTs at / below / above the floor."""

    def setup_method(self):
        self.check = PrivacyCheck()

    def test_passes_when_equal_outputs_meet_floor(self):
        psbt = _skeleton_with_equal_outputs(3, change=900_000)
        ok, msg = self.check.check_psbt(psbt, 3)
        assert ok is True, msg

    def test_passes_when_equal_outputs_exceed_floor(self):
        psbt = _skeleton_with_equal_outputs(3)
        ok, _ = self.check.check_psbt(psbt, 2)
        assert ok is True

    def test_fails_when_equal_group_below_floor(self):
        # 3 equal outputs but the floor demands 4.
        psbt = _skeleton_with_equal_outputs(3, change=900_000)
        ok, msg = self.check.check_psbt(psbt, 4)
        assert ok is False
        assert "4" in msg

    def test_fails_when_too_few_outputs_total(self):
        # Only 1 output, floor of 2 → fails the "too few outputs" branch.
        psbt = _skeleton_with_equal_outputs(1)
        ok, _ = self.check.check_psbt(psbt, 2)
        assert ok is False

    def test_solo_floor_of_one_passes_with_two_equal(self):
        # Solo-NC mix (required_nonconforming == 1): two equal outputs from
        # distinct parties clear a floor of 1.
        psbt = _skeleton_with_equal_outputs(2)
        ok, _ = self.check.check_psbt(psbt, 1)
        assert ok is True
