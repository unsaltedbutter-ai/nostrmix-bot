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

# Bitcoin-address shapes, for routing a bare paste (no command verb) to
# provide_addresses. Routing only — the coordinator does the real validation.
# Nothing else in the grammar collides: mix names are word-word, amounts are
# bare digits, PSBT hex starts 70736274ff, npubs/invoices start npub1/lnbc1.
BECH32_ADDR_PATTERN = re.compile(r'^(?:bc1|tb1|bcrt1)[02-9ac-hj-np-z]{8,87}$')
BASE58_ADDR_PATTERN = re.compile(r'^[13][1-9A-HJ-NP-Za-km-z]{25,34}$')

# A bare-pasted PSBT: hex starting with the PSBT magic ("psbt" + 0xff). A
# signed PSBT pasted straight from a wallet routes to accept_psbt without
# the psbt_accept verb.
PSBT_HEX_PATTERN = re.compile(r'^70736274ff[0-9a-fA-F]+$')


def _looks_like_address(token: str) -> bool:
    # bech32 is case-insensitive (lowercase canonical); base58 is not.
    return bool(BECH32_ADDR_PATTERN.match(token.lower())
                or BASE58_ADDR_PATTERN.match(token))


class CommandParser:
    """Parse incoming DM text into structured commands.

    Recognized commands:
    - /list or list → list_mixes
    - join <mix_id> <num_outputs> → join_mix
    - /inputs (aliases /input, /commit) <txid:vout> ... → commit_utxos
    - /addresses (aliases /address, /outputs) <addr> ... → provide_addresses
    - /addresses clear → clear_addresses
    - /psbt_accept <hex> → accept_psbt
    - /cancel or exit [mix_id] → exit_mix
    - /psbt_chunk <chunk_idx>/<total> <hex> → accept_psbt_chunk

    A message with no verb that contains txid:vout pairs, bitcoin
    addresses, or PSBT hex routes to commit_utxos / provide_addresses /
    accept_psbt respectively — pasting the data is enough.
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

        # -- INPUTS (aliases: INPUT, COMMIT) --
        if cmd in ("inputs", "input", "commit"):
            # Find every txid:vout pair regardless of separator — spaces, commas,
            # or ", " (a wallet "copy all"). finditer ignores whatever sits
            # between matches, so all forms parse the same. (The verb word
            # itself can't match a 64-hex:vout pattern.)
            utxos = [{"txid": m.group(1).lower(), "vout": int(m.group(2))}
                     for m in UTXO_PATTERN.finditer(raw)]
            return ParsedCommand("commit_utxos", [utxos], raw)

        # -- ADDRESSES (aliases: ADDRESS, OUTPUTS) --
        if cmd in ("addresses", "address", "outputs"):
            # "addresses clear" — wipe the accumulated address list and start over.
            if len(args) == 1 and args[0].lower() == "clear":
                return ParsedCommand("clear_addresses", [], raw)
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

        # -- BARE PASTE — data with no verb. PSBT hex, txid:vout pairs, and
        # bitcoin addresses are each shape-unambiguous in this grammar (see
        # the patterns above), so pasting them straight from a wallet works
        # without typing a command first. Non-matching tokens are ignored,
        # same as the verb forms.
        # PSBT first: join the tokens (a client may hard-wrap the long hex)
        # and route only when the WHOLE message is one PSBT.
        joined = "".join(parts)
        if PSBT_HEX_PATTERN.match(joined):
            return ParsedCommand("accept_psbt", [joined], raw)
        utxos = [{"txid": m.group(1).lower(), "vout": int(m.group(2))}
                 for m in UTXO_PATTERN.finditer(raw)]
        if utxos:
            return ParsedCommand("commit_utxos", [utxos], raw)
        addrs = []
        for tok in parts:
            for a in tok.split(","):
                a = a.strip()
                if a and _looks_like_address(a):
                    addrs.append(a)
        if addrs:
            return ParsedCommand("provide_addresses", [addrs], raw)

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
    # Shown without a leading "/" — it's friendlier to type on mobile. The
    # parser strips a leading "/" anyway, so "join foo-bar" and "/join foo-bar"
    # both work.
    _HELP_CATALOG = [
        ("list", "list — open mixes"),
        ("join", "join <mix> — join (or join 0.01)"),
        ("inputs", "inputs <txid:vout> … — add UTXOs (or just paste them)"),
        ("addresses", "addresses <addr> … — payout addresses (or just paste them)"),
        ("psbt_accept", "psbt_accept <hex> — return signed PSBT"),
        ("cancel", "cancel — leave"),
    ]

    def format_help(self, commands: List[str],
                    header: str = "Commands:") -> str:
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
            parts = [f"{name}: {output_btc:.4f} BTC"]
            if req:
                parts.append(f"needs {req}")
            if cap:
                parts.append(f"+{cap} same-size free")
            parts.append("p2wpkh")
            lines.append(" · ".join(parts))
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
            f"Reply with new ones — just paste fresh addresses."
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
