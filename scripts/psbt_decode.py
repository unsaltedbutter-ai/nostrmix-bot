#!/usr/bin/env python3
"""Decode a PSBT (or raw tx) and show what it pays — the "are these coins
spendable by me?" check, fully local (python-bitcointx, no node).

For a PSBT (what you receive to sign) this prints every output as an ADDRESS +
amount + script type, flags anything nonstandard / OP_RETURN (unspendable), and
computes the miner fee from the input amounts the PSBT carries (witness_utxo).
"Spendable by you" == the output address is yours: pass --mine to total what
goes to your addresses and surface anything paid elsewhere.

Why PSBT, not raw tx: a finalized raw transaction does NOT contain input
amounts, so neither this script nor Bitcoin Core's `decoderawtransaction` can
show the fee or check inputs from it — only the outputs. A PSBT carries the
input values, so decoding it (like Core's `decodepsbt`) is the more informative
pre-broadcast check. The outputs are identical either way; signing never changes
them (SIGHASH_ALL commits to them), so verifying the PSBT before you sign is
final.

Usage:
    python scripts/psbt_decode.py <hex>                 # PSBT or raw-tx hex
    python scripts/psbt_decode.py --file skeleton.txt
    echo <hex> | python scripts/psbt_decode.py -
    ... [--mine bc1qaaa,bc1qbbb]                         # your receive addresses
"""
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from bitcointx.core import CTransaction
from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
from bitcointx.wallet import CCoinAddress

_PSBT_MAGIC = "70736274ff"  # b"psbt\xff"


def _btc(sats: int) -> str:
    return f"{sats/1e8:.8f} BTC ({sats} sats)"


def _classify(spk: bytes) -> tuple[str, bool]:
    """(script_type, spendable?) from the raw scriptPubKey bytes."""
    b = bytes(spk)
    if len(b) == 22 and b[0] == 0x00 and b[1] == 0x14:
        return "p2wpkh", True          # single-key
    if len(b) == 34 and b[0] == 0x51 and b[1] == 0x20:
        return "p2tr", True            # single-key (key path)
    if len(b) == 34 and b[0] == 0x00 and b[1] == 0x20:
        return "p2wsh", True           # script — spendable, but not single-key
    if len(b) == 25 and b[:3] == b"\x76\xa9\x14" and b[23:] == b"\x88\xac":
        return "p2pkh", True           # single-key
    if len(b) == 23 and b[0] == 0xA9 and b[1] == 0x14 and b[22] == 0x87:
        return "p2sh", True            # script — spendable, but not single-key
    if b and b[0] == 0x6A:
        return "OP_RETURN", False      # provably UNSPENDABLE (burn)
    return "nonstandard", False        # likely unspendable / won't relay


def _address_of(spk) -> str | None:
    try:
        return str(CCoinAddress.from_scriptPubKey(spk))
    except Exception:
        return None


def _read_hex(args) -> str:
    if args.file:
        with open(args.file) as f:
            raw = f.read()
    elif args.hex in (None, "-"):
        raw = sys.stdin.read()
    else:
        raw = args.hex
    return "".join(raw.split()).lower()


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="psbt_decode.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Decode a PSBT (or raw tx) locally and show what it pays — the\n"
            "\"are these coins spendable by me?\" check. No node required.\n\n"
            "For each output it prints the ADDRESS + amount + script type, flags\n"
            "OP_RETURN / nonstandard outputs as UNSPENDABLE (a burn -> exit 1), and\n"
            "for a PSBT computes the miner fee from the input amounts it carries.\n"
            "Pass --mine to total what goes to YOUR addresses. Exit code: 0 = all\n"
            "outputs spendable, 1 = an unspendable output found, 2 = bad input."
        ),
        epilog=(
            "examples:\n"
            "  python scripts/psbt_decode.py <psbt-or-rawtx-hex>\n"
            "  python scripts/psbt_decode.py --file skeleton.txt\n"
            "  cat skeleton.txt | python scripts/psbt_decode.py -\n"
            "  python scripts/psbt_decode.py <hex> --mine bc1qaaa...,bc1qbbb...\n\n"
            "tip: a PSBT carries input amounts (so the fee is shown); a finalized\n"
            "raw tx does not, so only its outputs can be checked."
        ),
    )
    ap.add_argument("hex", nargs="?",
                    help="PSBT or raw-tx hex; use '-' or omit to read from stdin")
    ap.add_argument("--file", metavar="PATH",
                    help="read the hex from a file instead of the argument")
    ap.add_argument("--mine", metavar="ADDR,ADDR",
                    help="comma-separated list of YOUR addresses; totals what they receive")
    args = ap.parse_args()

    hx = _read_hex(args)
    if not hx:
        print("no hex provided.")
        return 2
    mine = {a.strip() for a in args.mine.split(",")} if args.mine else set()

    is_psbt = hx.startswith(_PSBT_MAGIC)
    in_amounts: list[int | None] = []
    if is_psbt:
        psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(hx))
        tx = psbt.unsigned_tx
        for inp in psbt.inputs:
            u = getattr(inp, "utxo", None) or getattr(inp, "witness_utxo", None)
            in_amounts.append(int(u.nValue) if u is not None else None)
        kind = "PSBT (unsigned skeleton view)"
    else:
        tx = CTransaction.deserialize(bytes.fromhex(hx))
        in_amounts = [None] * len(tx.vin)
        kind = "raw transaction (no input amounts — fee not computable)"

    from bitcointx.core import b2lx
    print(f"== {kind} ==")
    print(f"  inputs: {len(tx.vin)}   outputs: {len(tx.vout)}")

    print("\n== inputs ==")
    for i, vin in enumerate(tx.vin):
        op = vin.prevout
        amt = in_amounts[i]
        amt_s = _btc(amt) if amt is not None else "amount unknown (not in a raw tx)"
        print(f"  [{i}] {b2lx(op.hash)}:{op.n}  {amt_s}")

    print("\n== outputs (each is a NEW coin; 'spendable by' = holder of this address's key) ==")
    out_total = 0
    mine_total = 0
    unspendable = []
    other_addrs = []
    for j, vout in enumerate(tx.vout):
        out_total += int(vout.nValue)
        stype, spendable = _classify(vout.scriptPubKey)
        addr = _address_of(vout.scriptPubKey)
        tag = ""
        if mine:
            if addr in mine:
                mine_total += int(vout.nValue)
                tag = "  <= YOURS"
            elif addr is not None:
                other_addrs.append(addr)
                tag = "  (not in --mine)"
        if not spendable:
            unspendable.append(j)
        flag = "" if spendable else "  *** UNSPENDABLE ***"
        shown = addr if addr else f"<{stype}: {bytes(vout.scriptPubKey).hex()}>"
        print(f"  [{j}] {stype:<12} {_btc(int(vout.nValue))}{flag}")
        print(f"      -> {shown}{tag}")

    print("\n== totals ==")
    print(f"  outputs total: {_btc(out_total)}")
    if all(a is not None for a in in_amounts) and in_amounts:
        in_total = sum(in_amounts)
        fee = in_total - out_total
        print(f"  inputs total:  {_btc(in_total)}")
        print(f"  miner fee:     {_btc(fee)}"
              + ("   *** NEGATIVE — INVALID ***" if fee < 0 else ""))
    else:
        print("  miner fee:     n/a (raw tx carries no input amounts; decode the PSBT for fee)")

    if mine:
        print(f"\n== your check (--mine, {len(mine)} address(es)) ==")
        print(f"  total paid to YOUR addresses: {_btc(mine_total)}")
        missing = mine - {a for a in other_addrs} - {
            _address_of(v.scriptPubKey) for v in tx.vout}
        # (the set above already includes your matched ones; report any of your
        # addresses that appear NOWHERE in the outputs)
        seen = {_address_of(v.scriptPubKey) for v in tx.vout}
        not_paid = sorted(a for a in mine if a not in seen)
        if not_paid:
            print(f"  WARNING: these --mine addresses get NOTHING: {not_paid}")
        if other_addrs:
            print(f"  outputs to addresses NOT in --mine: {len(other_addrs)} "
                  "(expected — other participants + your change if you didn't list it)")

    if unspendable:
        print(f"\n*** {len(unspendable)} output(s) are UNSPENDABLE (indices {unspendable}) — "
              "coins paid here would be BURNT. Do NOT sign. ***")
        return 1
    print("\nAll outputs are standard, spendable scripts. Confirm the addresses "
          "above are the ones you expect (and the fee is sane) before signing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
