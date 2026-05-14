"""Configuration loader — reads environment file, validates, returns typed values."""

import os
import json
from typing import Optional, List


# Defaults matching the plan's env template
_DEFAULTS = {
    # Bot identity
    "NOSTR_PRIVATE_KEY_NPUB": "",
    "NOSTR_RELAYS": "wss://relay.damus.com,wss://nos.lol",
    "BOT_NAME": "butterbot",
    "BOT_ABOUT": "I help bitcoiners mix their coins trustlessly over Nostr.",
    "BOT_LUD16": "",
    "BOT_PICTURE": "",
    "BOT_NIP05": "",
    "BOT_WEBSITE": "",

    # Zap receiving
    "ZAP_PROVIDER_PUBKEY_HEX": "",

    # Zap Sending for refunds
    "BTCPAY_URL": "",
    "BTCPAY_STORE": "",
    "BTCPAY_API_KEY": "",

    # Fee defaults
    "FEE_PER_ELEMENT": 100,
    "FEE_MULTIPLIER": 1.5,
    "MIN_FEE_RATE_SATS": 1.5,
    "MAX_FEE_RATE_SATS": 510,
    "REFUND_KEEP_PERCENT": 5,
    "REFUND_KEEP_MIN_SATS": 50,

    # Mix parameters
    "DEFAULT_OUTPUT_SIZE": 1000000,
    "MIN_PARTICIPANTS_DEFAULT": 3,
    "MAX_PARTICIPANTS_DEFAULT": 20,
    "MAX_PENDING_MIXES": 5,
    "SIGNING_DEADLINE_HOURS": 48,
    "PAY_DEADLINE_HOURS": 12,
    "MAX_GHOST_RETRIES": 3,
    "MINIMUM_UTXO_SIZE": 10000,
    "DEFAULT_MIX_OUTPUT_COUNT": 4,
    "DEFAULT_MIX_USER_COUNT": 3,

    # Bitcoin API
    "MEMPOOL_API": "https://mempool.space/api",

    # Database
    "DB_PATH": "./bot.db",

    # Input/output script type & vsize constants
    "INPUT_VSIZE": 68,
    "OUTPUT_VSIZE": 31,
    "TX_OVERHEAD_VSIZE": 10,
}


class BotConfig:
    """Typed configuration object with validation."""

    def __init__(self, env_path: str):
        # Load env file
        self._env_path = env_path
        self._values: dict = {}

        # Try loading from file
        from dotenv import load_dotenv
        load_dotenv(env_path, override=True)

        for key, default in _DEFAULTS.items():
            raw = os.environ.get(key, default)
            # Type-coerce based on default type
            if isinstance(default, bool):
                val = raw.strip().lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int):
                val = int(str(raw).strip())
            elif isinstance(default, float):
                val = float(str(raw).strip())
            else:
                val = str(raw).strip()
            self._values[key] = val

        # Derived / validated values
        self._validate()

    def _validate(self):
        # Enforce MIN_PARTICIPANTS_DEFAULT >= 2
        if self.MIN_PARTICIPANTS_DEFAULT < 2:
            self._values["MIN_PARTICIPANTS_DEFAULT"] = 2

        # Parse relay URLs
        raw = self._values.get("NOSTR_RELAYS", "")
        relay_list = [r.strip() for r in raw.split(",") if r.strip()]
        self._values["_relays"] = relay_list

        # Check required keys
        missing = []
        if not self.NOSTR_PRIVATE_KEY_NPUB:
            missing.append("NOSTR_PRIVATE_KEY_NPUB")
        if not self.ZAP_PROVIDER_PUBKEY_HEX:
            missing.append("ZAP_PROVIDER_PUBKEY_HEX")

        # BTCPay is optional for initial development
        # but we'll warn if missing

    # --- Properties ---

    @property
    def NOSTR_PRIVATE_KEY_NPUB(self) -> str:  # Actually nsec, will support both
        return self._values["NOSTR_PRIVATE_KEY_NPUB"]

    @property
    def NOSTR_RELAYS(self) -> list:
        return self._values.get("_relays", ["wss://relay.damus.com", "wss://nos.lol"])

    @property
    def BOT_NAME(self) -> str:
        return self._values["BOT_NAME"]

    @property
    def BOT_ABOUT(self) -> str:
        return self._values["BOT_ABOUT"]

    @property
    def BOT_LUD16(self) -> str:
        return self._values["BOT_LUD16"]

    @property
    def BOT_PICTURE(self) -> str:
        return self._values["BOT_PICTURE"]

    @property
    def BOT_NIP05(self) -> str:
        return self._values["BOT_NIP05"]

    @property
    def BOT_WEBSITE(self) -> str:
        return self._values["BOT_WEBSITE"]

    @property
    def ZAP_PROVIDER_PUBKEY_HEX(self) -> str:
        return self._values["ZAP_PROVIDER_PUBKEY_HEX"]

    @property
    def BTCPAY_URL(self) -> str:
        return self._values["BTCPAY_URL"]

    @property
    def BTCPAY_STORE(self) -> str:
        return self._values["BTCPAY_STORE"]

    @property
    def BTCPAY_API_KEY(self) -> str:
        return self._values["BTCPAY_API_KEY"]

    @property
    def FEE_PER_ELEMENT(self) -> int:
        return self._values["FEE_PER_ELEMENT"]

    @property
    def FEE_MULTIPLIER(self) -> float:
        return self._values["FEE_MULTIPLIER"]

    @property
    def MIN_FEE_RATE_SATS(self) -> float:
        return self._values["MIN_FEE_RATE_SATS"]

    @property
    def MAX_FEE_RATE_SATS(self) -> float:
        return self._values["MAX_FEE_RATE_SATS"]

    @property
    def REFUND_KEEP_PERCENT(self) -> int:
        return self._values["REFUND_KEEP_PERCENT"]

    @property
    def REFUND_KEEP_MIN_SATS(self) -> int:
        return self._values["REFUND_KEEP_MIN_SATS"]

    @property
    def DEFAULT_OUTPUT_SIZE(self) -> int:
        return self._values["DEFAULT_OUTPUT_SIZE"]

    @property
    def MIN_PARTICIPANTS_DEFAULT(self) -> int:
        return self._values["MIN_PARTICIPANTS_DEFAULT"]

    @property
    def MAX_PARTICIPANTS_DEFAULT(self) -> int:
        return self._values["MAX_PARTICIPANTS_DEFAULT"]

    @property
    def MAX_PENDING_MIXES(self) -> int:
        return self._values["MAX_PENDING_MIXES"]

    @property
    def SIGNING_DEADLINE_HOURS(self) -> int:
        return self._values["SIGNING_DEADLINE_HOURS"]

    @property
    def PAY_DEADLINE_HOURS(self) -> int:
        return self._values["PAY_DEADLINE_HOURS"]

    @property
    def MAX_GHOST_RETRIES(self) -> int:
        return self._values["MAX_GHOST_RETRIES"]

    @property
    def MINIMUM_UTXO_SIZE(self) -> int:
        return self._values["MINIMUM_UTXO_SIZE"]

    @property
    def DEFAULT_MIX_OUTPUT_COUNT(self) -> int:
        return self._values["DEFAULT_MIX_OUTPUT_COUNT"]

    @property
    def DEFAULT_MIX_USER_COUNT(self) -> int:
        return self._values["DEFAULT_MIX_USER_COUNT"]

    @property
    def MEMPOOL_API(self) -> str:
        return self._values["MEMPOOL_API"]

    @property
    def DB_PATH(self) -> str:
        return self._values["DB_PATH"]

    @property
    def INPUT_VSIZE(self) -> int:
        return self._values["INPUT_VSIZE"]

    @property
    def OUTPUT_VSIZE(self) -> int:
        return self._values["OUTPUT_VSIZE"]

    @property
    def TX_OVERHEAD_VSIZE(self) -> int:
        return self._values["TX_OVERHEAD_VSIZE"]

    @property
    def profile(self) -> dict:
        """Return profile dict for NostrBotConfig."""
        p = {}
        if self.BOT_NAME:
            p["name"] = self.BOT_NAME
        if self.BOT_ABOUT:
            p["about"] = self.BOT_ABOUT
        if self.BOT_LUD16:
            p["lud16"] = self.BOT_LUD16
        if self.BOT_PICTURE:
            p["picture"] = self.BOT_PICTURE
        if self.BOT_NIP05:
            p["nip05"] = self.BOT_NIP05
        if self.BOT_WEBSITE:
            p["website"] = self.BOT_WEBSITE
        return p

    def as_dict(self) -> dict:
        return dict(self._values)

    @staticmethod
    def find_env_path() -> str:
        """Look for env file in standard locations."""
        candidates = [
            "nostrmix-bot.env",
            "../nostrmix-bot.env",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nostrmix-bot.env"),
        ]
        for c in candidates:
            resolved = os.path.abspath(c)
            if os.path.exists(resolved):
                return resolved
        # Default to local path
        return "nostrmix-bot.env"
