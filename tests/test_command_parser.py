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
        assert parsed.args[2] is None

    def test_join_amount_decimal(self):
        """/join <amount> → amount in slot 2, name slots None."""
        parsed = self.parser.parse("/join 0.01")
        assert parsed.command == "join_mix"
        assert parsed.args[0] is None
        assert parsed.args[1] is None
        assert parsed.args[2] == "0.01"

    def test_join_amount_leading_dot(self):
        parsed = self.parser.parse("/join .5")
        assert parsed.command == "join_mix"
        assert parsed.args[2] == ".5"

    def test_join_amount_integer(self):
        """A bare integer is still a BTC amount, not a name."""
        parsed = self.parser.parse("/join 1000000")
        assert parsed.command == "join_mix"
        assert parsed.args[0] is None
        assert parsed.args[2] == "1000000"

    def test_join_name_has_amount_slot_none(self):
        """A name-join leaves the amount slot None."""
        parsed = self.parser.parse("/join east-gate")
        assert parsed.args[0] == "east-gate"
        assert parsed.args[2] is None

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

    def test_commit_accepts_commas_and_spaces(self):
        """/commit takes txid:vout pairs separated by spaces, commas, or ", "."""
        a = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
        b = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
        expected = [{"txid": a, "vout": 0}, {"txid": b, "vout": 1}]
        for sep in (
            f"{a}:0 {b}:1",      # spaces
            f"{a}:0,{b}:1",      # commas, no spaces
            f"{a}:0, {b}:1",     # comma + space (wallet copy-all)
        ):
            parsed = self.parser.parse(f"/commit {sep}")
            assert parsed.command == "commit_utxos"
            assert parsed.args[0] == expected, f"failed for: {sep!r}"

    def test_commit_accepts_mixed_case_txid_normalizes_to_lower(self):
        """Some wallets/explorers show txids capitalized. Accept upper/mixed
        case and normalize to lowercase (what the chain API + DB expect)."""
        upper = "ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890ABCDEF1234567890"
        parsed = self.parser.parse(f"/commit {upper}:2")
        assert parsed.command == "commit_utxos"
        utxos = parsed.args[0]
        assert len(utxos) == 1
        assert utxos[0]["txid"] == upper.lower()
        assert utxos[0]["vout"] == 2

    def test_addresses(self):
        """Test /addresses command."""
        parsed = self.parser.parse("/addresses bc1qabc... bc1qdef...")
        assert parsed.command == "provide_addresses"
        addrs = parsed.args[0]
        assert len(addrs) >= 1

    def test_addresses_accepts_commas_and_spaces(self):
        """Addresses may be separated by spaces, commas, or ", " (copy-all)."""
        expected = ["bc1qaaa", "bc1qbbb", "bc1qccc"]
        for sep_form in (
            "bc1qaaa bc1qbbb bc1qccc",      # spaces
            "bc1qaaa,bc1qbbb,bc1qccc",      # commas, no spaces
            "bc1qaaa, bc1qbbb, bc1qccc",    # comma + space (wallet copy-all)
            "bc1qaaa ,bc1qbbb , bc1qccc",   # messy mix
        ):
            parsed = self.parser.parse(f"/addresses {sep_form}")
            assert parsed.command == "provide_addresses"
            assert parsed.args[0] == expected, f"failed for: {sep_form!r}"

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


class TestAliasesAndBarePaste:
    """inputs/addresses are the documented verbs (input/commit and
    address/outputs stay as aliases), and pasting bare txid:vout pairs or
    bitcoin addresses needs no verb at all."""

    GOOD_TXID = "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef"
    ADDR_BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
    ADDR_BECH32_B = "bc1qrp33g0q5c5txsp9arysrx4k6zdkfs4nce4xj0gdcccefvpysxf3qccfmv3"
    ADDR_BASE58 = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"

    def setup_method(self):
        self.parser = CommandParser(bot_name="butterbot")

    def test_inputs_alias_for_commit(self):
        for verb in ("inputs", "/inputs", "input", "commit", "/commit"):
            parsed = self.parser.parse(f"{verb} {self.GOOD_TXID}:0")
            assert parsed.command == "commit_utxos"
            assert parsed.args[0] == [{"txid": self.GOOD_TXID, "vout": 0}]

    def test_outputs_alias_for_addresses(self):
        for verb in ("addresses", "/addresses", "address", "outputs", "/outputs"):
            parsed = self.parser.parse(f"{verb} {self.ADDR_BECH32}")
            assert parsed.command == "provide_addresses"
            assert parsed.args[0] == [self.ADDR_BECH32]

    def test_addresses_clear(self):
        for text in ("addresses clear", "address clear",
                     "/addresses CLEAR", "outputs clear"):
            assert self.parser.parse(text).command == "clear_addresses"

    def test_addresses_clear_with_extra_args_is_not_clear(self):
        # "clear" only as the sole argument — anything else is an address list.
        parsed = self.parser.parse(f"addresses clear {self.ADDR_BECH32}")
        assert parsed.command == "provide_addresses"

    def test_bare_utxo_paste(self):
        parsed = self.parser.parse(f"{self.GOOD_TXID}:0, {self.GOOD_TXID}:1")
        assert parsed.command == "commit_utxos"
        assert [u["vout"] for u in parsed.args[0]] == [0, 1]

    def test_bare_address_paste_single(self):
        parsed = self.parser.parse(self.ADDR_BECH32)
        assert parsed.command == "provide_addresses"
        assert parsed.args[0] == [self.ADDR_BECH32]

    def test_bare_address_paste_mixed_separators(self):
        text = f"{self.ADDR_BECH32}, {self.ADDR_BECH32_B} {self.ADDR_BASE58}"
        parsed = self.parser.parse(text)
        assert parsed.command == "provide_addresses"
        assert parsed.args[0] == [self.ADDR_BECH32, self.ADDR_BECH32_B,
                                  self.ADDR_BASE58]

    def test_bare_address_paste_ignores_surrounding_words(self):
        parsed = self.parser.parse(f"my address is {self.ADDR_BECH32}")
        assert parsed.command == "provide_addresses"
        assert parsed.args[0] == [self.ADDR_BECH32]

    def test_bare_utxo_wins_over_address_in_same_message(self):
        parsed = self.parser.parse(f"{self.GOOD_TXID}:0 {self.ADDR_BECH32}")
        assert parsed.command == "commit_utxos"

    def test_plain_text_still_unknown(self):
        for text in ("hello there friend",
                     "what is this bot",
                     "npub1sn0wdnkukk0lpma0ngsq6sjmfmjewxnsnvy7nptxqgu5dkj5z0hs5rxepl",
                     "silver-cupcake",
                     "0.01"):
            assert self.parser.parse(text).command == "unknown", text

    def test_bare_psbt_paste_routes_to_accept_psbt(self):
        # PSBT hex (70736274ff…) pasted with no verb is a signed-PSBT return.
        blob = "70736274ff" + "00" * 60
        parsed = self.parser.parse(blob)
        assert parsed.command == "accept_psbt"
        assert parsed.args[0] == blob

    def test_bare_psbt_paste_rejoins_client_wrapped_hex(self):
        # A client may hard-wrap the long hex; the parser re-joins it.
        blob = "70736274ff" + "ab" * 100
        wrapped = f"{blob[:80]}\n{blob[80:160]}\n{blob[160:]}"
        parsed = self.parser.parse(wrapped)
        assert parsed.command == "accept_psbt"
        assert parsed.args[0] == blob

    def test_psbt_with_trailing_garbage_is_not_routed(self):
        # Only an all-hex message is a PSBT — mixed content stays unknown.
        blob = "70736274ff" + "00" * 60
        assert self.parser.parse(f"{blob} please").command == "unknown"
