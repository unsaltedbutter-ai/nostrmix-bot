"""Per-process opaque tokens for log correlation without identity leakage.

The bot deals in identifiers that are individually sensitive (npub, lud16)
or whose pairing is sensitive (mix_id alongside an on-chain txid would let
anyone reading the log file map every participant onto the public coinjoin
transaction). We still want some grep-ability in the logs — "what happened
to participant X today?" — so this module hands out short opaque tokens.

Properties:

  * Salted per process. A 16-byte random salt is generated at module
    import. Restarting the bot generates a fresh salt; log files from
    different runs CANNOT be joined.
  * Hash inputs include a kind prefix ("p" for participant, "m" for mix,
    "l" for lud16, ...) so the same underlying string in different
    contexts produces distinct tokens.
  * 4-byte BLAKE2s tags surfaced as 8 hex chars. Birthday-collision at
    ~65k entries; for a single bot process that's plenty.
  * No reverse mapping is stored. The cache holds string -> token; you
    cannot recover the input from a token, even from inside the same
    process. (You could brute-force if the input space is small —
    npubs/lud16s are 256-bit / unbounded so this is fine.)

Usage:

    from src.log_tokens import tokens
    logger.info("Participant %s ghosted %s", tokens.p(npub_hex), tokens.m(mix_id))

The module-level ``tokens`` singleton is what you almost always want.
Tests can construct fresh SessionTokens() instances if they need
reset-on-test semantics.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Dict


class SessionTokens:
    """In-memory salted-hash tokeniser. See module docstring."""

    def __init__(self) -> None:
        self._salt = secrets.token_bytes(16)
        self._cache: Dict[str, str] = {}

    def for_kind(self, kind: str, value: str) -> str:
        """Return a stable token for (kind, value) within this process.

        kind: a short label distinguishing the namespace ("p", "m", "l",
              "tx", ...). Logged as ``<kind>#<8 hex chars>`` so a quick
              glance tells you what kind of thing the token refers to.
        value: the underlying string. Empty / None values get a sentinel
               so missing inputs don't accidentally collide.
        """
        if not value:
            return f"{kind}#?"
        key = f"{kind}:{value}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        digest = hashlib.blake2s(self._salt + key.encode(), digest_size=4).hexdigest()
        token = f"{kind}#{digest}"
        self._cache[key] = token
        return token

    # Convenience accessors for the kinds we actually use. Keeping them
    # as one-liners is verbose but means callers don't have to remember
    # the kind prefixes (and a code review can audit the call sites).

    def p(self, npub_hex: str) -> str:
        """Participant token from a hex npub."""
        return self.for_kind("p", npub_hex)

    def m(self, mix_id: str) -> str:
        """Mix token from a mix_id."""
        return self.for_kind("m", mix_id)

    def l(self, lud16: str) -> str:  # noqa: E741 (single-char method name is intentional)
        """Lud16 token from a Lightning address."""
        return self.for_kind("l", lud16)

    def tx(self, txid: str) -> str:
        """Txid token. A bare on-chain txid in a log is a privacy leak —
        paired with anything mix-internal it reconstructs coinjoin membership,
        and on its own it still says "this bot broadcast this public tx". Log
        ``tokens.tx(txid)`` instead of the raw value."""
        return self.for_kind("tx", txid)


# Module-level singleton: every production log call should use this.
tokens = SessionTokens()
