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
        assert cfg.FEE_PER_ELEMENT == 100
        assert cfg.MIN_FEE_RATE_SATS == 1.5
        assert cfg.MIN_PARTICIPANTS_DEFAULT == 3
        assert cfg.MINIMUM_UTXO_SIZE == 10000
        assert cfg.DB_PATH == "./bot.db"
        assert cfg.BOT_NAME == "butterbot"
        assert isinstance(cfg.NOSTR_RELAYS, list)
        assert len(cfg.NOSTR_RELAYS) >= 1
        assert cfg.FEE_MULTIPLIER == 1.5

        # Clean up
        os.unlink(env_path)

    def test_min_participants_enforcement(self):
        """Test that MIN_PARTICIPANTS_DEFAULT is enforced to at least 2."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write("NOSTR_PRIVATE_KEY_NPUB=nsec1abc...\n")
            f.write("ZAP_PROVIDER_PUBKEY_HEX=def...\n")
            f.write("MIN_PARTICIPANTS_DEFAULT=1\n")
            env_path = f.name

        cfg = BotConfig(env_path)
        assert cfg.MIN_PARTICIPANTS_DEFAULT >= 2
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

        # From script-vbytesize.md table
        assert ivm["p2wpkh"] == 70
        assert ivm["p2tr"] == 70
        assert ivm["p2pkh"] == 150
        assert ivm["p2sh"] == 255
        assert ivm["p2sh-p2wpkh"] == 95
        assert ivm["p2wsh"] == 1455

        assert ovm["p2wpkh"] == 35
        assert ovm["p2tr"] == 45
        assert ovm["p2wsh"] == 4
        assert ovm["p2pkh"] == 35

        # Lookup methods
        assert cfg.input_vsize_for("p2wpkh") == 70
        assert cfg.input_vsize_for("p2tr") == 70
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
