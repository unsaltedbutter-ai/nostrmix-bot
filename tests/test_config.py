"""Tests for config loader."""

import os
import sys
import tempfile
import pytest

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
# Actually the main module paths assume root
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.config import BotConfig


class TestBotConfig:
    def test_defaults(self):
        """Test that config loads with defaults for missing values."""
        # Create a minimal env file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("ZAP_PROVIDER_PUBKEY_HEX=def...\n")
            f.write("BTCPAY_URL=https://pay.unsaltedbutter.ai\n")
            f.write("BTCPAY_STORE=store123\n")
            f.write("BTCPAY_API_KEY=key456\n")
            env_path = f.name

        cfg = BotConfig(env_path)

        assert cfg.NOSTR_PRIVATE_KEY_NPUB == "nsec1abc..."
        assert cfg.ZAP_PROVIDER_PUBKEY_HEX == "def..."
        # Service fee now defaults to 0 (zaps optional, off by default).
        assert cfg.FEE_PER_ELEMENT == 0
        assert cfg.MIN_FEE_RATE_SATS == 1.5
        assert cfg.MINIMUM_UTXO_SIZE == 10000
        # Conforming/non-conforming model defaults.
        assert cfg.DEFAULT_REQUIRED_NONCONFORMING == 3
        assert cfg.MAX_CONFORMING_UTXOS == 10
        assert cfg.MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT == 10
        assert cfg.MAX_OPEN_MIXES == 10
        # Donation address disabled by default (privacy-preserving fold-to-fee).
        assert cfg.DONATION_ADDRESS == ""
        assert cfg.DB_PATH == "./bot.db"
        assert cfg.BOT_NAME == "butterbot"
        assert isinstance(cfg.NOSTR_RELAYS, list)
        assert len(cfg.NOSTR_RELAYS) >= 1
        assert cfg.FEE_MULTIPLIER == 1.25

        # Clean up
        os.unlink(env_path)

    def test_max_open_mixes_clamped_to_one(self):
        """MAX_OPEN_MIXES < 1 is clamped up to 1 (a 0 cap would block the
        always-available default mix)."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("MAX_OPEN_MIXES=0\n")
            env_path = f.name

        try:
            cfg = BotConfig(env_path)
            assert cfg.MAX_OPEN_MIXES == 1
        finally:
            # load_dotenv(override=True) leaks this into os.environ; pop it so
            # it doesn't bleed into other tests' BotConfig instances.
            os.environ.pop("MAX_OPEN_MIXES", None)
            os.unlink(env_path)

    def test_relay_parsing(self):
        """Test relay list parsing."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("ZAP_PROVIDER_PUBKEY_HEX=def...\n")
            f.write("NOSTR_RELAYS=wss://relay1.com,wss://relay2.com,wss://relay3.com\n")
            env_path = f.name

        cfg = BotConfig(env_path)
        assert len(cfg.NOSTR_RELAYS) == 3
        assert "wss://relay1.com" in cfg.NOSTR_RELAYS
        assert "wss://relay2.com" in cfg.NOSTR_RELAYS
        assert "wss://relay3.com" in cfg.NOSTR_RELAYS
        os.unlink(env_path)

    def test_per_script_type_vsize_defaults(self):
        """Test per-script-type vsize maps have correct defaults."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("ZAP_PROVIDER_PUBKEY_HEX=def...\n")
            env_path = f.name

        cfg = BotConfig(env_path)
        ivm = cfg.INPUT_VSIZE_BY_TYPE
        ovm = cfg.OUTPUT_VSIZE_BY_TYPE

        # Calibrated against real mainnet txs, rounded up to nearest 5.
        assert ivm["p2wpkh"] == 70
        assert ivm["p2tr"] == 60
        assert ivm["p2pkh"] == 150
        assert ivm["p2sh"] == 135
        assert ivm["p2sh-p2wpkh"] == 95
        assert ivm["p2wsh"] == 100

        assert ovm["p2wpkh"] == 35
        assert ovm["p2tr"] == 45
        assert ovm["p2wsh"] == 45
        assert ovm["p2pkh"] == 35

        # Lookup methods
        assert cfg.input_vsize_for("p2wpkh") == 70
        assert cfg.input_vsize_for("p2tr") == 60
        assert cfg.output_vsize_for("p2tr") == 45

        os.unlink(env_path)

    def test_per_script_type_vsize_override(self):
        """Test that per-script-type values can be overridden in env."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("ZAP_PROVIDER_PUBKEY_HEX=def...\n")
            f.write("P2WPKH_INPUT_VSIZE=80\n")
            f.write("P2TR_OUTPUT_VSIZE=50\n")
            env_path = f.name

        cfg = BotConfig(env_path)
        assert cfg.input_vsize_for("p2wpkh") == 80
        assert cfg.output_vsize_for("p2tr") == 50
        # Other types unchanged
        assert cfg.input_vsize_for("p2pkh") == 150
        os.unlink(env_path)


class TestAcceptedTypes:
    """The operator allowlist for input/output script types.

    Default is p2wpkh-only — narrow on purpose so vsize variability and
    bech32m parsing edge cases don't bite the MVP.
    """

    def test_default_allowlist_is_p2wpkh_only(self):
        cfg = BotConfig("/nonexistent.env")
        assert cfg.ACCEPTED_INPUT_TYPES == {"p2wpkh"}
        assert cfg.ACCEPTED_OUTPUT_TYPES == {"p2wpkh"}

    def test_allowlist_parses_comma_separated_and_lowercases(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("ACCEPTED_INPUT_TYPES=p2wpkh, P2TR\n")
            f.write("ACCEPTED_OUTPUT_TYPES=p2wpkh,p2tr,p2sh\n")
            env_path = f.name
        try:
            cfg = BotConfig(env_path)
            assert cfg.ACCEPTED_INPUT_TYPES == {"p2wpkh", "p2tr"}
            assert cfg.ACCEPTED_OUTPUT_TYPES == {"p2wpkh", "p2tr", "p2sh"}
        finally:
            os.unlink(env_path)

    def test_empty_allowlist_falls_back_to_p2wpkh(self):
        """Empty config must not mean 'reject everything' — fall back instead."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("ACCEPTED_INPUT_TYPES=\n")
            f.write("ACCEPTED_OUTPUT_TYPES=   ,  \n")
            env_path = f.name
        try:
            cfg = BotConfig(env_path)
            assert cfg.ACCEPTED_INPUT_TYPES == {"p2wpkh"}
            assert cfg.ACCEPTED_OUTPUT_TYPES == {"p2wpkh"}
        finally:
            os.unlink(env_path)


class TestConfigWarnings:
    """The loader should warn (not crash) on the common foot-guns: a missing
    config file, an unknown/typo'd key, and a missing signing key. Warnings
    never include the VALUE of any key."""

    def test_warns_on_missing_file(self, caplog):
        import logging
        with caplog.at_level(logging.WARNING, logger="src.config"):
            BotConfig("/definitely/not/here/bot-config")
        assert any("not found" in r.message for r in caplog.records)

    def test_warns_on_unknown_key_without_leaking_value(self, caplog):
        import logging
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=placeholder\n")
            f.write("FEE_PER_ELEMNT=500\n")          # typo: missing 'E'
            env_path = f.name
        try:
            with caplog.at_level(logging.WARNING, logger="src.config"):
                BotConfig(env_path)
            msgs = " ".join(r.message for r in caplog.records)
            assert "unknown config key" in msgs.lower()
            assert "FEE_PER_ELEMNT" in msgs       # the name is named
            assert "500" not in msgs              # the value is NOT logged
        finally:
            os.unlink(env_path)

    def test_warns_on_missing_signing_key(self, caplog, monkeypatch):
        import logging
        # load_dotenv(override=True) leaks values into os.environ across
        # BotConfig instances, so a prior test's key can linger — clear it so
        # this test sees a genuinely-absent signing key.
        monkeypatch.delenv("NOSTR_PRIVATE_KEY_NPUB", raising=False)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("BOT_NAME=butterbot\n")        # no signing key set
            env_path = f.name
        try:
            with caplog.at_level(logging.WARNING, logger="src.config"):
                BotConfig(env_path)
            assert any("NOSTR_PRIVATE_KEY_NPUB is not set" in r.message
                       for r in caplog.records)
        finally:
            os.unlink(env_path)

    def test_no_unknown_warning_for_clean_file(self, caplog):
        import logging
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=placeholder\n")
            f.write("FEE_PER_ELEMENT=0\n")
            f.write("# a comment\n")
            env_path = f.name
        try:
            with caplog.at_level(logging.WARNING, logger="src.config"):
                BotConfig(env_path)
            msgs = " ".join(r.message for r in caplog.records).lower()
            assert "unknown config key" not in msgs
        finally:
            os.unlink(env_path)
