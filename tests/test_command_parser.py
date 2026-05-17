"""Tests for CommandParser."""

import os
import sys
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.command_parser import CommandParser, ParsedCommand


class TestCommandParser:
    def setup_method(self):
        self.parser = CommandParser(bot_name="butterbot")

    def test_list_command(self):
        """Test /list command."""
        parsed = self.parser.parse("/list")
        assert parsed.command == "list_mixes"

        parsed = self.parser.parse("list")
        assert parsed.command == "list_mixes"

    def test_join_command(self):
        """Test /join command."""
        parsed = self.parser.parse("/join east-gate")
        assert parsed.command == "join_mix"
        assert parsed.args[0] == "east-gate"

    def test_join_with_num_outputs(self):
        """Test /join with num_outputs."""
        parsed = self.parser.parse("/join east-gate 4")
        assert parsed.command == "join_mix"
        assert parsed.args[0] == "east-gate"
        assert parsed.args[1] == 4

    def test_commit_utxos(self):
        """Test /commit with UTXOs."""
        parsed = self.parser.parse(
            "/commit 1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef:0 "
            "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890:1"
        )
        assert parsed.command == "commit_utxos"
        utxos = parsed.args[0]
        assert len(utxos) == 2
        assert utxos[0]["vout"] == 0
        assert utxos[1]["vout"] == 1

    def test_addresses(self):
        """Test /addresses command."""
        parsed = self.parser.parse("/addresses bc1qabc... bc1qdef...")
        assert parsed.command == "provide_addresses"
        addrs = parsed.args[0]
        assert len(addrs) >= 1

    def test_psbt_accept(self):
        """Test /psbt_accept command."""
        hex_str = "70736274ff0100..."
        parsed = self.parser.parse(f"/psbt_accept {hex_str}")
        assert parsed.command == "accept_psbt"
        assert parsed.args[0] == hex_str

    def test_psbt_chunk(self):
        """Test /psbt_chunk command."""
        parsed = self.parser.parse("/psbt_chunk 1/3 abc123def...")
        assert parsed.command == "accept_psbt_chunk"
        assert parsed.args[0] == 1
        assert parsed.args[1] == 3
        assert parsed.args[2] == "abc123def..."

    def test_exit_mix(self):
        """Test exit/cancel command."""
        parsed = self.parser.parse("/cancel east-gate")
        assert parsed.command == "exit_mix"
        assert parsed.args[0] == "east-gate"

        parsed = self.parser.parse("/exit")
        assert parsed.command == "exit_mix"

    def test_unknown_command(self):
        """Test unknown command."""
        parsed = self.parser.parse("garbage text here")
        assert parsed.command == "unknown"

    def test_format_list_response(self):
        """Test format_list_response."""
        mixes = [
            {"id": "east-gate", "output_size": 1_000_000, "state": "collecting"},
            {"id": "buggy-whip", "output_size": 500_000, "state": "collecting"},
        ]
        response = self.parser.format_list_response(mixes)
        assert "east-gate" in response
        assert "buggy-whip" in response

    # format_fee_request / format_join_response were dead code (never
    # called from the real flow — the fee is quoted in _cmd_provide_addresses
    # AFTER both /commit and /addresses) and were removed to avoid misleading
    # readers about when the fee is asked for.
