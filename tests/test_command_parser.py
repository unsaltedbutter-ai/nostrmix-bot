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

    def test_join_two_words_offers_hyphen_fallback(self):
        """/join typed with a space instead of the hyphen → the parser keeps the
        first token AND offers the joined "<word1>-<word2>" as a fallback."""
        parsed = self.parser.parse("/join silver cupcake")
        assert parsed.command == "join_mix"
        assert parsed.args[0] == "silver"
        assert parsed.args[1] == "silver-cupcake"

    def test_join_hyphenated_has_no_fallback(self):
        """A correctly hyphenated name is a single token; no fallback needed."""
        parsed = self.parser.parse("/join silver-cupcake")
        assert parsed.command == "join_mix"
        assert parsed.args[0] == "silver-cupcake"
        assert parsed.args[1] is None

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

    # --- Edge cases (review gap #11) ---

    def test_commit_malformed_yields_no_utxos(self):
        """Malformed outpoints (no colon, wrong hex length) parse to an empty
        UTXO list — the coordinator then DMs 'No UTXOs found'."""
        for text in ("/commit deadbeef", "/commit 123:0", "/commit notxid:abc",
                     "/commit"):
            parsed = self.parser.parse(text)
            assert parsed.command == "commit_utxos"
            assert parsed.args[0] == [], f"{text!r} should yield no utxos"

    def test_commit_extracts_only_wellformed_outpoints(self):
        """A valid 64-hex:vout is extracted even when mixed with junk."""
        good = "a" * 64 + ":2"
        parsed = self.parser.parse(f"/commit garbage {good} alsobad:x")
        assert parsed.command == "commit_utxos"
        assert parsed.args[0] == [{"txid": "a" * 64, "vout": 2}]

    def test_open_mixes_text_trigger(self):
        """'open' and 'mixes' are accepted as /list synonyms (plan §3g)."""
        for text in ("open", "open mixes", "mixes"):
            assert self.parser.parse(text).command == "list_mixes"

    def test_commands_are_case_insensitive(self):
        assert self.parser.parse("/LIST").command == "list_mixes"
        assert self.parser.parse("OPEN").command == "list_mixes"
        p = self.parser.parse("/JOIN East-Gate")
        assert p.command == "join_mix"
        # mix id is lowercased for matching.
        assert p.args[0] == "east-gate"

    def test_join_without_name(self):
        """/join with no mix name → join_mix with no usable mix id."""
        parsed = self.parser.parse("/join")
        assert parsed.command == "join_mix"
        assert not parsed.args or not parsed.args[0]

    def test_cancel_without_args_autodetect(self):
        """/cancel with no args → exit_mix with mix_id None (coordinator
        auto-detects when the user is in exactly one mix)."""
        parsed = self.parser.parse("/cancel")
        assert parsed.command == "exit_mix"
        assert parsed.args[0] is None
