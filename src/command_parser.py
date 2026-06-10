"""Command Parser — rigid NIP-17 DM command protocol.

No NL parsing; commands must match exactly as defined in the plan.
"""

from __future__ import annotations

import re
from typing import Optional, Dict, Any, Tuple, List, Union

# Command prefix for all bot commands
CMD_PREFIX = "/"


class ParsedCommand:
    """Result of parsing a DM text."""

    def __init__(self, command: str, args: list, raw: str):
        self.command = command
        self.args = args
        self.raw = raw


# Extract txid:vout pairs. Accept upper- or mixed-case hex (some wallets/block
# explorers display txids capitalized); callers normalize the txid to lowercase,
# which is what the chain API and the DB's UNIQUE(txid, vout) constraint expect.
UTXO_PATTERN = re.compile(r'([a-fA-F0-9]{64}):(\d+)')

# A bare number (BTC amount) for `/join <amount>`. Mix names are always
# alphabetic "<adjective>-<noun>" pairs, so a pure number never collides with
# a name — the match unambiguously means "join/create a mix of this size".
AMOUNT_PATTERN = re.compile(r'^\d+(\.\d+)?$|^\.\d+$')


class CommandParser:
    """Parse incoming DM text into structured commands.

    Recognized commands:
    - /list or list → list_mixes
    - join <mix_id> <num_outputs> → join_mix
    - /commit <txid:vout> ... → commit_utxos
    - /addresses <addr> ... → provide_addresses
    - /psbt_accept <hex> → accept_psbt
    - /cancel or exit [mix_id] → exit_mix
    - /psbt_chunk <chunk_idx>/<total> <hex> → accept_psbt_chunk
    """

    def __init__(self, bot_name: str = "butterbot"):
        self._bot_name = bot_name

    def parse(self, text: str) -> ParsedCommand:
        """Parse a DM text and return a ParsedCommand with structured args."""

        raw = text.strip()
        if not raw:
            return ParsedCommand("unknown", [], raw)

        # Split into parts
        parts = raw.split()
        cmd = parts[0].lower().lstrip(CMD_PREFIX)
        args = parts[1:] if len(parts) > 1 else []

        # Check for known commands via simple prefix matching

        # -- LIST / OPEN MIXES --
        if cmd in ("list", "open", "mixes"):
            return ParsedCommand("list_mixes", [], raw)

        # -- HELP / COMMANDS --
        if cmd in ("help", "commands", "?"):
            return ParsedCommand("help", [], raw)

        # -- JOIN --
        # Args are [mix_id, alt, amount_btc]. A name-join fills the first two
        # (amount None); an amount-join fills only the third (name slots None).
        if cmd == "join":
            if len(args) >= 1:
                first = args[0].strip()
                if AMOUNT_PATTERN.match(first):
                    # "/join 0.01" — join-or-create a mix of this BTC size. The
                    # coordinator converts to sats, floors at MINIMUM_UTXO_SIZE,
                    # and finds-or-creates the mix.
                    return ParsedCommand("join_mix", [None, None, first], raw)
                mix_id = first.lower()
                # Names are "<word>-<word>". Tolerate a user who typed a space
                # instead of the hyphen ("/join silver cupcake"): also offer the
                # joined "<word1>-<word2>" as a fallback the coordinator tries if
                # the first token doesn't match an open mix.
                alt = None
                if len(args) >= 2:
                    alt = f"{args[0].strip()}-{args[1].strip()}".lower()
                return ParsedCommand("join_mix", [mix_id, alt, None], raw)
            return ParsedCommand("join_mix", [], raw)

        # -- COMMIT UTXOS --
        if cmd == "commit":
            # Find every txid:vout pair regardless of separator — spaces, commas,
            # or ", " (a wallet "copy all"). finditer ignores whatever sits
            # between matches, so all forms parse the same. (The "/commit" word
            # itself can't match a 64-hex:vout pattern.)
            utxos = [{"txid": m.group(1).lower(), "vout": int(m.group(2))}
                     for m in UTXO_PATTERN.finditer(raw)]
            return ParsedCommand("commit_utxos", [utxos], raw)

        # -- ADDRESSES --
        if cmd == "addresses":
            # Accept addresses separated by spaces and/or commas — a wallet
            # "copy all" often joins them with ", ".
            addrs = []
            for tok in args:
                for a in tok.split(","):
                    a = a.strip()
                    if a:
                        addrs.append(a)
            return ParsedCommand("provide_addresses", [addrs], raw)

        # -- PSBT ACCEPT --
        if cmd == "psbt_accept":
            hex_str = " ".join(args)
            # Remove any leading "hex" prefix or spaces
            return ParsedCommand("accept_psbt", [hex_str.strip()], raw)

        # -- PSBT CHUNK (reassembly) --
        if cmd == "psbt_chunk":
            # Format: /psbt_chunk <chunk_idx>/<total> <hex>
            if len(args) >= 2:
                chunk_info = args[0]  # e.g. "1/3"
                chunk_hex = " ".join(args[1:])
                chunk_match = re.match(r'(\d+)/(\d+)', chunk_info)
                if chunk_match:
                    chunk_idx = int(chunk_match.group(1))
                    chunk_total = int(chunk_match.group(2))
                    return ParsedCommand("accept_psbt_chunk", [chunk_idx, chunk_total, chunk_hex.strip()], raw)
            return ParsedCommand("accept_psbt", [" ".join(args).strip()], raw)

        # -- CANCEL / EXIT --
        if cmd in ("cancel", "exit", "leave"):
            if args:
                mix_id = args[0].strip().lower()
            else:
                mix_id = None
            return ParsedCommand("exit_mix", [mix_id], raw)

        # -- Unknown --
        return ParsedCommand("unknown", [raw], raw)

    def parse_commit_utxos(self, text: str) -> List[Dict]:
        """Parse 'txid:vout txid:vout ...' from raw text."""
        utxos = []
        for m in UTXO_PATTERN.finditer(text):
            utxos.append({"txid": m.group(1).lower(), "vout": int(m.group(2))})
        return utxos

    def parse_addresses(self, text: str) -> List[str]:
        """Parse space-separated bitcoin addresses."""
        parts = text.split()
        return [p.strip() for p in parts if p.startswith("bc1") or
                p.startswith("1") or p.startswith("3") or
                p.startswith("2") or p.startswith("q")][1:]  # skip command

    # One-line description per command, in the order we want them shown. The
    # help output is *filtered* to the keys the coordinator decides are relevant
    # to the user's current stage — every command still works regardless of
    # whether it's listed (the list only shapes guidance, not behaviour).
    _HELP_CATALOG = [
        ("list", "/list — show open mixes you can join"),
        ("join", "/join <mix>  (or /join 0.01 to pick a size) — join a mix"),
        ("commit", "/commit <txid:vout> … — register the UTXOs you're mixing"),
        ("addresses", "/addresses <addr> … — addresses to receive your outputs"),
        ("psbt_accept", "/psbt_accept <hex> — return your signed PSBT"),
        ("cancel", "/cancel — leave a mix"),
    ]

    def format_help(self, commands: List[str],
                    header: str = "Commands available to you:") -> str:
        """Render the help text for just the given command keys, in catalog
        order. Unknown keys are ignored; `list` is always shown as a floor so a
        user is never left without a way to see open mixes."""
        keys = set(commands) | {"list"}
        lines = [header]
        for key, desc in self._HELP_CATALOG:
            if key in keys:
                lines.append(f"  {desc}")
        return "\n".join(lines)

    def format_list_response(self, active_mixes: List[Dict]) -> str:
        """Format the /list response with active mixes."""
        if not active_mixes:
            return "No open mixes right now. Check back later."

        lines = []
        for mix in active_mixes:
            name = mix.get("id", "unknown")
            output_btc = mix.get("output_size", 0) / 1e8
            state = mix.get("state", "collecting")
            req = mix.get("required_nonconforming")
            cap = mix.get("max_conforming_utxos")
            extra = ""
            if req:
                extra += f" Needs {req} mixer(s)."
            if cap:
                extra += (
                    f" Up to {cap} same-size ({output_btc:.4f} BTC) UTXOs welcome "
                    f"free of charge."
                )
            lines.append(
                f"mix {name}: {output_btc:.4f} BTC outputs ({state})."
                + extra
                + " p2wpkh addresses only."
            )
        return "\n".join(lines)

    def format_paid_confirmation(self, mix_id: str, waiting: int) -> str:
        """Format the confirmation after payment received."""
        if waiting > 0:
            return f"You are all paid up for {mix_id}. We are waiting on {waiting} more participant(s)."
        else:
            return f"You are all paid up for {mix_id}."

    def format_psbt_request(self, mix_id: str, deadline_hours: int) -> str:
        """Format the PSBT signing request."""
        return (
            f"We are going to start the signing process for {mix_id}.\n"
            f"Once I send you the PSBT, you have {deadline_hours} hours "
            f"to add your signatures and return it to me."
        )

    def format_ghost_warning(self, mix_id: str) -> str:
        """Format message when someone ghosted."""
        return (
            f"Someone ghosted us during the signing phase and saw your addresses. "
            f"To insure your privacy, we've thrown out your addresses.\n"
            f"Reply with new addresses: /addresses <addr1> <addr2> ..."
        )

    def format_refund(self, amount_sats: int, reason: str = "") -> str:
        """Format the refund message."""
        if reason:
            return f"We're sorry to see you go. Refunding {amount_sats} sats ({reason})."
        return f"Refunding {amount_sats} sats."

    def format_error(self, msg: str) -> str:
        """Format an error response."""
        return msg

    def address_type_mismatch(self, address_type: str) -> str:
        return f"For this mix we're only accepting {address_type} addresses."

    def format_max_mixes(self, max_mixes: int) -> str:
        return f"You're already in {max_mixes} mixes. Let's finish one of these first."
