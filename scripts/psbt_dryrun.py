#!/usr/bin/env python3
"""PSBT dry-run — preview the exact coinjoin the bot would assemble.

Feeds a mix spec through the REAL Coordinator._assemble_psbt (against a throwaway
SQLite DB with stub Nostr/Lightning handlers), so the fee math, conforming/
non-conforming classification, under-funded drops, donation handling, the
pre-broadcast sum invariant, and the PSBT build are all the production code —
nothing is re-implemented here. It NEVER broadcasts and never sends a DM.

Usage:
    source venv/bin/activate
    python scripts/psbt_dryrun.py scripts/example-mix.json

Spec JSON:
    {
      "output_size": 1000000,
      "max_conforming_utxos": 10,         # optional (default: config)
      "required_nonconforming": 2,        # optional (default: config)
      "fee_per_element": 0,               # optional (default: config)
      "fee_rate": 30,                     # optional; omit for a LIVE estimate
      "participants": [
        {
          "name": "alice",
          "utxos": ["<txid>:<vout>", ...],          # looked up on-chain, OR
          "utxos": [{"amount": 2500000, "script_type": "p2wpkh"}],  # synthetic
          "addresses": ["bc1q...", "bc1q...", "bc1q..."]
        }
      ]
    }

Real `txid:vout` inputs are looked up on mempool.space (so the resulting PSBT is
real and importable). Synthetic `{"amount": ...}` inputs let you model fee/output
behaviour fully offline (pair with a fixed "fee_rate").
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import BotConfig
from src.database import Database
from src.chain_monitor import ChainMonitor
from src.psbt_manager import PSBTManager
from src.fee_engine import FeeEngine
from src.coordinator import Coordinator


class _StubNostr:
    """Records DMs instead of sending them; satisfies the handler interface."""
    def __init__(self):
        self.dms: list[tuple[str, str]] = []

    def set_dm_handler(self, cb): pass
    def set_zap_handler(self, cb): pass
    def set_heartbeat_handler(self, cb): pass
    def set_on_ready(self, cb): pass

    async def send_dm(self, recipient, message):
        self.dms.append((recipient, message))

    async def get_identity(self, pubkey_hex):
        return None

    async def post_announcement(self, text):
        return "dryrun"

    async def start(self): pass
    async def stop(self): pass

    @property
    def keys(self):
        return None


class _StubLightning:
    def __init__(self):
        self.refunds: list[tuple] = []

    async def init(self): pass
    async def init_payer_with_keys(self, keys): pass

    async def send_refund(self, lud16, sats, reason="x"):
        self.refunds.append((lud16, sats, reason))
        return "dryrun-refund"


class _Ctx:
    def __init__(self, sender_hex):
        self.sender_hex = sender_hex


def _btc(sats: int) -> str:
    return f"{sats/1e8:.8f} BTC ({sats} sats)"


def _print_messages(nostr, lightning):
    print("\n== bot messages (not sent) ==")
    for who, msg in nostr.dms:
        first = msg.splitlines()[0] if msg else ""
        if first.startswith("/psbt_accept") or first.startswith("/psbt_chunk"):
            first = first[:48] + " …(PSBT hex omitted)"
        print(f"  -> {who}: {first}")
    if lightning.refunds:
        print("\n== refunds that would be attempted ==")
        for lud16, sats, reason in lightning.refunds:
            print(f"  -> {lud16}: {sats} sats ({reason})")


def _rm(path):
    try:
        os.unlink(path)
    except OSError:
        pass


async def run(spec: dict, env_path: str) -> int:
    cfg = BotConfig(env_path)

    output_size = int(spec["output_size"])
    fee_rate = spec.get("fee_rate")
    fee_per_element = int(spec.get("fee_per_element", cfg.FEE_PER_ELEMENT))
    required = int(spec.get("required_nonconforming", cfg.DEFAULT_REQUIRED_NONCONFORMING))
    max_conf = int(spec.get("max_conforming_utxos", cfg.MAX_CONFORMING_UTXOS))

    chain = ChainMonitor(
        api_base=cfg.MEMPOOL_API,
        api_backup=cfg.MEMPOOL_API_BACKUP or None,
        min_fee_rate=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate=cfg.MAX_FEE_RATE_SATS,
        fee_multiplier=cfg.FEE_MULTIPLIER,
        fee_lookback_blocks=cfg.FEE_LOOKBACK_BLOCKS,
    )
    if fee_rate is not None:
        async def _fixed_rate():
            return float(fee_rate)
        chain.estimate_fee_rate = _fixed_rate  # deterministic / offline

    psbt_mgr = PSBTManager(
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
        overhead=cfg.TX_OVERHEAD_VSIZE,
    )
    fee_engine = FeeEngine(
        fee_per_element=fee_per_element,
        min_fee_rate_sats=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate_sats=cfg.MAX_FEE_RATE_SATS,
        overhead_vsize=cfg.TX_OVERHEAD_VSIZE,
        minimum_utxo_size=cfg.MINIMUM_UTXO_SIZE,
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
    )

    db_path = tempfile.mktemp(suffix=".db")
    db = Database(db_path)
    await db.connect()
    nostr = _StubNostr()
    lightning = _StubLightning()
    coord = Coordinator(cfg, db)
    await coord.init(nostr=nostr, chain=chain, psbt_mgr=psbt_mgr,
                     fee_engine=fee_engine, lightning=lightning)

    mix_id = await db.create_mix(
        output_size=output_size, fee_per_element=fee_per_element,
        required_nonconforming=required, max_conforming_utxos=max_conf,
    )
    # Only pin a stored fee_rate when the spec fixed one. For the LIVE path we
    # leave it unset so the rate the PSBT is built at is unambiguously the
    # estimate fetched inside _assemble_psbt (no misleading placeholder).
    collecting_kwargs = dict(state="collecting", input_type="p2wpkh", output_type="p2wpkh")
    if fee_rate is not None:
        collecting_kwargs["fee_rate"] = int(fee_rate)
    await db.update_mix(mix_id, **collecting_kwargs)

    sum_in = 0
    present_conforming = 0
    print("== mix ==")
    print(f"  output_size={output_size}  required_nonconforming={required}  "
          f"max_conforming_utxos={max_conf}  fee_per_element={fee_per_element}  "
          f"fee_rate={'LIVE' if fee_rate is None else fee_rate}")

    # Intake through the REAL /addresses handler so the address-count rule and
    # the donation/loss warning fire exactly as in production.
    print("\n== intake (/addresses, the real handler) ==")
    for p in spec["participants"]:
        name = p["name"]
        pid = await db.add_participant(mix_id, name, p.get("lud16", ""))
        p_in = 0
        for u in p["utxos"]:
            if isinstance(u, str):
                txid, vout = u.split(":")
                vout = int(vout)
                txout = await chain.lookup_txout(txid, vout)
                if not txout:
                    print(f"  ERROR: UTXO {u} not found on chain")
                    await chain.close(); await db.close(); _rm(db_path)
                    return 2
                amt = int(txout["value"]); st = txout["scriptpubkey_type"]; spk = txout["scriptpubkey"]
            else:
                amt = int(u["amount"]); st = u.get("script_type", "p2wpkh")
                txid = u.get("txid", os.urandom(32).hex()); vout = int(u.get("vout", 0))
                spk = u.get("scriptpubkey", "0014" + "00" * 20)
            await db.add_utxo(pid, txid, vout, amt, st, spk)
            p_in += amt
            sum_in += amt
            if amt == output_size:
                present_conforming += 1
        await db.update_participant(pid, state="committed")
        before = len(nostr.dms)
        await coord._cmd_provide_addresses(_Ctx(name), name, p["addresses"])
        state = (await db.get_participant(pid))["state"]
        new = [m for _r, m in nostr.dms[before:]]
        verdict = ("PAID/ready" if state == "paid"
                   else "committed (awaiting zap)" if (state == "committed" and fee_per_element > 0)
                   else "REJECTED")
        note = ""
        for m in new:
            low = m.lower()
            line0 = m.splitlines()[0]
            if "at least" in low and "address" in low:
                note = "  <-- TOO FEW ADDRESSES: " + line0
            elif "donated" in low:
                hit = next((ln for ln in m.splitlines() if "donat" in ln.lower()), line0)
                note = "  <-- LOSS: " + hit.strip()
            elif "below one" in low:
                note = "  <-- INPUTS TOO SMALL: " + line0
        print(f"  {name:<10} inputs={_btc(p_in)} addrs={len(p['addresses'])} -> {verdict}{note}")

    paid = [p for p in await db.get_participants_by_mix(mix_id) if p["state"] == "paid"]
    proceed, nc_count, _conf = await coord._classify_ready(await db.get_mix(mix_id), paid)
    print("\n== readiness ==")
    print(f"  paid participants={len(paid)}  non-conforming={nc_count}  "
          f"conforming UTXOs present={present_conforming}")
    if not proceed:
        print(f"  mix would NOT advance yet — needs {required} non-conforming participant(s)"
              + ("" if required >= 2 else " plus >=1 conforming UTXO") + ".")
        _print_messages(nostr, lightning)
        await chain.close(); await db.close(); _rm(db_path)
        return 1

    await db.update_mix(mix_id, state="assembling")
    await coord._assemble_psbt(await db.get_mix(mix_id), paid)
    state = (await db.get_mix(mix_id))["state"]
    print("\n== result ==")
    print(f"  mix state: {state}  ({'WOULD BROADCAST' if state == 'signing' else 'WOULD NOT proceed'})")

    print("\n== per participant ==  (change = computed leftover; if intake flagged"
          " LOSS it was donated/folded, NOT received)")
    for p in await db.get_participants_by_mix(mix_id):
        print(f"  {p['npub_hex']:<10} state={p['state']:<12} "
              f"fee_share={p['fee_share']}  change={p['change_amount']}")

    rounds = await db.get_psbt_rounds_by_mix(mix_id)
    psbt_hex = next((r["psbt_sent"] for r in rounds if r.get("psbt_sent")), None)
    if psbt_hex:
        from bitcointx.core.psbt import PartiallySignedBitcoinTransaction
        psbt = PartiallySignedBitcoinTransaction.from_binary(bytes.fromhex(psbt_hex))
        vins = psbt.unsigned_tx.vin
        vouts = psbt.unsigned_tx.vout
        sum_out = sum(o.nValue for o in vouts)
        miner_fee = sum_in - sum_out
        eq = sum(1 for o in vouts if o.nValue == output_size)
        actual_vsize = fee_engine.estimate_total_vsize(
            {"p2wpkh": len(vins)}, {"p2wpkh": len(vouts)})
        eff = miner_fee / max(actual_vsize, 1)
        target = float(fee_rate) if fee_rate is not None else await chain.estimate_fee_rate()
        expected = int(target * actual_vsize)
        excess = miner_fee - expected
        print("\n== transaction ==")
        print(f"  inputs : {len(vins)}  total {_btc(sum_in)}  (conforming present: {present_conforming})")
        print(f"  outputs: {len(vouts)} ({eq} equal @ {output_size} sats)  total {_btc(sum_out)}")
        print(f"  miner fee: {_btc(miner_fee)}  (~{eff:.1f} sat/vB effective over ~{actual_vsize} vB)")
        # The fee is sized from the ACTUAL conforming present, so the effective
        # rate matches the target unless above-dust change was folded into the
        # fee (a participant with no change address and no DONATION_ADDRESS set).
        if excess > 200:
            print(f"  NOTE: ~{excess} sats above the ~{target:.1f} sat/vB target for this "
                  f"{actual_vsize}-vB tx — above-dust change folded into the fee for "
                  f"participant(s) who gave no change address and no DONATION_ADDRESS is set "
                  f"(see LOSS lines above).")
        else:
            print(f"  effective rate matches the ~{target:.1f} sat/vB target.")
        floor = max(2, required)
        ok, msg = coord.privacy.check_psbt(psbt_hex, floor)
        print(f"  privacy check (floor {floor}): {'PASS' if ok else 'FAIL'} — {msg}")
        print("\n== unsigned PSBT (import into a wallet to inspect; do NOT broadcast) ==")
        print(psbt_hex)
    else:
        print("\n  (no PSBT assembled — see messages below)")

    _print_messages(nostr, lightning)
    await chain.close(); await db.close(); _rm(db_path)
    return 0 if state == "signing" else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        prog="psbt_dryrun.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Preview the exact coinjoin the bot would assemble from a mix spec.\n\n"
            "Feeds the spec through the REAL Coordinator._assemble_psbt against a\n"
            "throwaway DB with stub Nostr/Lightning handlers, so the fee math,\n"
            "conforming/non-conforming classification, under-funded drops, donation\n"
            "handling, the pre-broadcast sum invariant, and the PSBT build are all\n"
            "production code. It NEVER broadcasts and never sends a DM.\n\n"
            "Prints intake verdicts, readiness, the per-participant accounting, the\n"
            "assembled transaction with its fee breakdown, a privacy check, and the\n"
            "unsigned PSBT hex (import into a wallet to inspect; do NOT broadcast).\n"
            "Exit code: 0 = would reach signing, 1 = would not proceed, 2 = bad input."
        ),
        epilog=(
            "examples:\n"
            "  python scripts/psbt_dryrun.py scripts/example-mix.json\n"
            "  python scripts/psbt_dryrun.py my-mix.json /path/to/nostrmix-bot.env\n\n"
            "spec JSON: see scripts/example-mix.json. Use real \"txid:vout\" inputs\n"
            "(looked up on-chain) for an importable PSBT, or synthetic {\"amount\":N}\n"
            "inputs to model fee/output behaviour offline. Omit \"fee_rate\" for a\n"
            "live mempool.space estimate (needs network); pin a number to run offline."
        ),
    )
    ap.add_argument("spec", help="mix spec JSON (see scripts/example-mix.json)")
    ap.add_argument("env", nargs="?",
                    help="env file (default: auto-detected via BotConfig)")
    args = ap.parse_args()

    with open(args.spec) as f:
        spec = json.load(f)
    env_path = args.env or BotConfig.find_env_path()
    return asyncio.run(run(spec, env_path))


if __name__ == "__main__":
    raise SystemExit(main())
