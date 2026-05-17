"""Configuration loader — reads environment file, validates, returns typed values."""

import os
import json
from typing import Optional, List, Dict, Tuple


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
    # How many recently-confirmed blocks to look back when estimating the
    # sat/vB rate that will probably confirm within an hour. Bitcoin's target
    # is ~10 min/block, so 6 ≈ 1 hour. Larger values smooth over short fee
    # spikes (safer) at the cost of paying more in calm mempools.
    "FEE_LOOKBACK_BLOCKS": 6,
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

    "BROADCAST_CHECK_INTERVAL_HOURS": 24,
    # Hour-of-day (UTC) at which the daily announcement post fires. 14 UTC =
    # morning Americas / evening Europe / late-night Asia. Range 0–23.
    "ANNOUNCEMENT_HOUR_UTC": 14,

    # Operator allowlist for script types. Comma-separated. Drives both /commit
    # (input UTXO acceptance) and /addresses (output address acceptance). Keeping
    # the MVP narrow to p2wpkh removes the multisig vsize variability and the
    # bech32m parsing edge cases. Widen here when you're ready.
    "ACCEPTED_INPUT_TYPES":  "p2wpkh",
    "ACCEPTED_OUTPUT_TYPES": "p2wpkh",

    # Bitcoin API
    "MEMPOOL_API": "https://mempool.space/api",
    # Backup Esplora-compatible mirror; tried if primary 5xx's or times out.
    # Set blank to disable.
    "MEMPOOL_API_BACKUP": "https://blockstream.info/api",

    # Database
    "DB_PATH": "./bot.db",

    # Per-script-type vbyte sizes. Calibrated against real mainnet
    # transactions, rounded up to the nearest 5 vbytes for a small fee
    # buffer. Confirmed values: p2wpkh ≈ 69, p2pkh ≈ 148, p2sh-p2wpkh ≈ 92,
    # p2tr key-path ≈ 58, p2wsh 2-of-2 ≈ 96, p2sh-p2wsh 2-of-3 ≈ 131.
    # Output sizes are structural: value(8) + length(1) + script bytes.
    # Input vBytes
    "P2PKH_INPUT_VSIZE": 150,
    "P2SH_INPUT_VSIZE": 135,        # covers p2sh-p2wsh 2-of-3; bare p2sh-multisig is heavier
    "P2SH_P2WPKH_INPUT_VSIZE": 95,
    "P2WPKH_INPUT_VSIZE": 70,
    "P2WSH_INPUT_VSIZE": 100,       # covers 2-of-2; 2-of-3 is ~104, larger N-of-M heavier
    "P2TR_INPUT_VSIZE": 60,         # key-path; script-path is heavier
    # Output vBytes
    "P2PKH_OUTPUT_VSIZE": 35,
    "P2SH_OUTPUT_VSIZE": 35,
    "P2SH_P2WPKH_OUTPUT_VSIZE": 35,
    "P2WPKH_OUTPUT_VSIZE": 35,
    "P2WSH_OUTPUT_VSIZE": 45,       # was 4 — that was a bug; real ≈ 43
    "P2TR_OUTPUT_VSIZE": 45,

    # Transaction overhead
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

        # DEFAULT_OUTPUT_SIZE must be >= MINIMUM_UTXO_SIZE — otherwise every
        # equal output we produce is below Bitcoin's standardness dust limit
        # and no wallet would later be able to spend them.
        if self.DEFAULT_OUTPUT_SIZE < self.MINIMUM_UTXO_SIZE:
            raise ValueError(
                f"DEFAULT_OUTPUT_SIZE ({self.DEFAULT_OUTPUT_SIZE}) must be "
                f">= MINIMUM_UTXO_SIZE ({self.MINIMUM_UTXO_SIZE}); otherwise "
                f"the mix would build dust equal-outputs."
            )

        # Parse relay URLs
        raw = self._values.get("NOSTR_RELAYS", "")
        relay_list = [r.strip() for r in raw.split(",") if r.strip()]
        self._values["_relays"] = relay_list

        # Parse accepted-type allowlists into sets. Empty config falls back to
        # {'p2wpkh'} rather than {} (which would reject everything).
        for key in ("ACCEPTED_INPUT_TYPES", "ACCEPTED_OUTPUT_TYPES"):
            raw = self._values.get(key, "")
            parsed = {t.strip().lower() for t in raw.split(",") if t.strip()}
            self._values[f"_{key.lower()}"] = parsed or {"p2wpkh"}

    # --- Property helpers ---

    @property
    def NOSTR_PRIVATE_KEY_NPUB(self) -> str:
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
    def FEE_LOOKBACK_BLOCKS(self) -> int:
        return self._values["FEE_LOOKBACK_BLOCKS"]

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
    def BROADCAST_CHECK_INTERVAL_HOURS(self) -> int:
        return self._values["BROADCAST_CHECK_INTERVAL_HOURS"]

    @property
    def ANNOUNCEMENT_HOUR_UTC(self) -> int:
        return self._values["ANNOUNCEMENT_HOUR_UTC"]

    @property
    def ACCEPTED_INPUT_TYPES(self) -> set:
        return self._values["_accepted_input_types"]

    @property
    def ACCEPTED_OUTPUT_TYPES(self) -> set:
        return self._values["_accepted_output_types"]

    @property
    def MEMPOOL_API(self) -> str:
        return self._values["MEMPOOL_API"]

    @property
    def MEMPOOL_API_BACKUP(self) -> str:
        return self._values["MEMPOOL_API_BACKUP"]

    @property
    def DB_PATH(self) -> str:
        return self._values["DB_PATH"]

    @property
    def TX_OVERHEAD_VSIZE(self) -> int:
        return self._values["TX_OVERHEAD_VSIZE"]

    # --- Per-script-type vbyte sizes ---

    @property
    def INPUT_VSIZE_BY_TYPE(self) -> Dict[str, int]:
        """Return dict mapping script_type string -> input vbytes."""
        return {
            "p2pkh": self._values["P2PKH_INPUT_VSIZE"],
            "p2sh": self._values["P2SH_INPUT_VSIZE"],
            "p2sh-p2wpkh": self._values["P2SH_P2WPKH_INPUT_VSIZE"],
            "p2wpkh": self._values["P2WPKH_INPUT_VSIZE"],
            "p2wsh": self._values["P2WSH_INPUT_VSIZE"],
            "p2tr": self._values["P2TR_INPUT_VSIZE"],
        }

    @property
    def OUTPUT_VSIZE_BY_TYPE(self) -> Dict[str, int]:
        """Return dict mapping script_type string -> output vbytes."""
        return {
            "p2pkh": self._values["P2PKH_OUTPUT_VSIZE"],
            "p2sh": self._values["P2SH_OUTPUT_VSIZE"],
            "p2sh-p2wpkh": self._values["P2SH_P2WPKH_OUTPUT_VSIZE"],
            "p2wpkh": self._values["P2WPKH_OUTPUT_VSIZE"],
            "p2wsh": self._values["P2WSH_OUTPUT_VSIZE"],
            "p2tr": self._values["P2TR_OUTPUT_VSIZE"],
        }

    def input_vsize_for(self, script_type: str) -> int:
        """Look up input vbytes for a given script type. Falls back to p2wpkh."""
        mapping = self.INPUT_VSIZE_BY_TYPE
        key = script_type.lower().replace("-", "_")
        # Try direct match first, then normalized
        if script_type.lower() in mapping:
            return mapping[script_type.lower()]
        # Normalize: strip hyphens, lowercase
        norm = script_type.lower().replace("-", "")
        for k, v in mapping.items():
            if k.replace("-", "") == norm:
                return v
        return mapping["p2wpkh"]

    def output_vsize_for(self, script_type: str) -> int:
        """Look up output vbytes for a given script type. Falls back to p2wpkh."""
        mapping = self.OUTPUT_VSIZE_BY_TYPE
        if script_type.lower() in mapping:
            return mapping[script_type.lower()]
        norm = script_type.lower().replace("-", "")
        for k, v in mapping.items():
            if k.replace("-", "") == norm:
                return v
        return mapping["p2wpkh"]

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
