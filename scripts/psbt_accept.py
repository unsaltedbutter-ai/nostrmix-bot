#!/usr/bin/env python3
"""PSBT accept — the return half of the coinjoin, offline.

Where psbt_dryrun.py covers assembly (bot -> participant), this script covers
the return path (participant signs -> bot validates -> combine -> finalize).

Like the real bot, it assembles the skeleton ONCE and PERSISTS it in a database,
then validates returned PSBTs against that stored skeleton — it never re-derives
the transaction. So, exactly as in production, the fee rate is fixed the moment
the skeleton is built and nothing downstream depends on re-running the fee math.
It drives the REAL Coordinator._cmd_accept_psbt for each returned PSBT (so
validate_returned's cryptographic checks, the multi-mix matching, and the
signing state machine are all production code), and combines + finalizes via the
REAL PSBTManager. It computes the would-be txid but NEVER broadcasts, never DMs.

Two-pass workflow (state carried in a persistent --db file, just like the bot):

  Pass 1 — assemble + discover what to sign. Run with no "signed_psbt" fields:
      python scripts/psbt_accept.py scripts/accept-mix.json
  It assembles the skeleton, saves it to the db, and prints ONE skeleton PSBT
  plus, per participant, the input indices they must sign and the outputs they
  should see. Import the skeleton into each participant's wallet, sign ONLY
  their inputs, export the signed PSBT.

  Pass 2 — verify acceptance. Paste each signed PSBT back into the spec under
  that participant's "signed_psbt" and re-run (same --db). The script loads the
  STORED skeleton (no re-assembly), reports per participant accepted / rejected
  (+ reason), then the combined + finalized raw transaction and its txid —
  clearly marked NOT broadcast.

Because the skeleton is persisted, the fee rate does NOT need to be re-supplied
on pass 2; it's baked into the stored skeleton. "fee_rate" in the spec is
optional — omit it for a live estimate (like the bot), or pin a number to run
fully offline (the example does, so it needs no network). Use REAL "txid:vout"
inputs so the PSBT is importable and your wallet can actually sign it.
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
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from src.config import BotConfig
from src.database import Database
from src.chain_monitor import ChainMonitor
from src.psbt_manager import PSBTManager
from src.fee_engine import FeeEngine
from src.coordinator import Coordinator

# Reuse the dry-run's throwaway handler stubs so the two scripts stay in lockstep.
from psbt_dryrun import _StubNostr, _StubLightning, _Ctx, _btc, _rm  # noqa: E402


def _local_txid(raw_tx_hex: str) -> str:
    """Compute a transaction's txid from its raw hex (display only)."""
    from bitcointx.core import CTransaction, b2x
    return b2x(CTransaction.deserialize(bytes.fromhex(raw_tx_hex)).GetTxid()[::-1])


async def _setup(spec: dict, env_path: str, db_path: str):
    """Build the coordinator against a PERSISTENT db at db_path. Returns
    (coord, db, nostr, chain)."""
    cfg = BotConfig(env_path)
    fee_rate = spec.get("fee_rate")

    chain = ChainMonitor(
        api_base=cfg.MEMPOOL_API,
        api_backup=cfg.MEMPOOL_API_BACKUP or None,
        min_fee_rate=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate=cfg.MAX_FEE_RATE_SATS,
        fee_multiplier=cfg.FEE_MULTIPLIER,
        fee_lookback_blocks=cfg.FEE_LOOKBACK_BLOCKS,
    )
    # Pin the rate only when the spec fixes one (lets the example run offline).
    # Omit it for a live estimate, exactly like the bot.
    if fee_rate is not None:
        async def _fixed_rate():
            return float(fee_rate)
        chain.estimate_fee_rate = _fixed_rate

    psbt_mgr = PSBTManager(
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
        overhead=cfg.TX_OVERHEAD_VSIZE,
    )
    fee_engine = FeeEngine(
        fee_per_element=int(spec.get("fee_per_element", cfg.FEE_PER_ELEMENT)),
        min_fee_rate_sats=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate_sats=cfg.MAX_FEE_RATE_SATS,
        overhead_vsize=cfg.TX_OVERHEAD_VSIZE,
        minimum_utxo_size=cfg.MINIMUM_UTXO_SIZE,
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
    )

    db = Database(db_path)
    await db.connect()
    nostr = _StubNostr()
    lightning = _StubLightning()
    coord = Coordinator(cfg, db)
    await coord.init(nostr=nostr, chain=chain, psbt_mgr=psbt_mgr,
                     fee_engine=fee_engine, lightning=lightning)
    return coord, db, nostr, chain


async def _intake_and_assemble(coord, db, chain, spec: dict) -> str:
    """Run intake + assembly once (mirrors psbt_dryrun) and persist. Returns
    the assembled mix_id."""
    cfg = coord.cfg
    output_size = int(spec["output_size"])
    fee_per_element = int(spec.get("fee_per_element", cfg.FEE_PER_ELEMENT))
    required = int(spec.get("required_nonconforming", cfg.DEFAULT_REQUIRED_NONCONFORMING))
    max_conf = int(spec.get("max_conforming_utxos", cfg.MAX_CONFORMING_UTXOS))

    mix_id = await db.create_mix(
        output_size=output_size, fee_per_element=fee_per_element,
        required_nonconforming=required, max_conforming_utxos=max_conf,
    )
    await db.update_mix(mix_id, state="collecting",
                        input_type="p2wpkh", output_type="p2wpkh")

    for p in spec["participants"]:
        name = p["name"]
        pid = await db.add_participant(mix_id, name, p.get("lud16", ""))
        for u in p["utxos"]:
            if isinstance(u, str):
                txid, vout = u.split(":")
                vout = int(vout)
                txout = await chain.lookup_txout(txid, vout)
                if not txout:
                    raise RuntimeError(f"UTXO {u} not found on chain")
                amt = int(txout["value"]); st = txout["scriptpubkey_type"]; spk = txout["scriptpubkey"]
            else:
                amt = int(u["amount"]); st = u.get("script_type", "p2wpkh")
                txid = u.get("txid", os.urandom(32).hex()); vout = int(u.get("vout", 0))
                spk = u.get("scriptpubkey", "0014" + "00" * 20)
            await db.add_utxo(pid, txid, vout, amt, st, spk)
        await db.update_participant(pid, state="committed")
        await coord._cmd_provide_addresses(_Ctx(name), name, p["addresses"])

    paid = [p for p in await db.get_participants_by_mix(mix_id) if p["state"] == "paid"]
    proceed, _nc, _conf = await coord._classify_ready(await db.get_mix(mix_id), paid)
    if not proceed:
        raise RuntimeError(
            f"mix would not advance (needs {required} non-conforming participant(s)); "
            "fix the spec before testing the return path")

    await db.update_mix(mix_id, state="assembling")
    await coord._assemble_psbt(await db.get_mix(mix_id), paid)
    if (await db.get_mix(mix_id))["state"] != "signing":
        raise RuntimeError("assembly did not reach the signing phase")
    return mix_id


def _skeleton_of(db_rounds) -> str:
    return next((r["psbt_sent"] for r in db_rounds if r.get("psbt_sent")), None)


async def run(spec: dict, env_path: str, db_path: str, reset: bool) -> int:
    if reset:
        _rm(db_path)
    fresh = not os.path.exists(db_path)

    coord, db, nostr, chain = await _setup(spec, env_path, db_path)
    try:
        # Find an already-assembled mix in this persistent db; assemble once if
        # none exists yet. This is the bot's model: assemble + persist, then
        # validate returns against the STORED skeleton (never re-derive it).
        existing = await db.get_mixes_by_state("signing")
        if existing:
            mix_id = existing[0]["id"]
            print(f"(using assembled mix {mix_id} from {db_path})")
        else:
            if not fresh:
                print(f"(no assembled mix in {db_path}; assembling now)")
            mix_id = await _intake_and_assemble(coord, db, chain, spec)
            print(f"(assembled mix {mix_id}; skeleton persisted to {db_path})")

        round_num = (await db.get_mix(mix_id)).get("ghost_retries", 0) + 1
        rounds = await db.get_psbt_rounds_by_mix(mix_id)
        skeleton = _skeleton_of(rounds)
        if not skeleton:
            print("ERROR: no skeleton PSBT in the db.")
            return 2

        # name -> pid (these scripts use the participant name as the npub).
        parts = await db.get_participants_by_mix(mix_id)
        name_to_pid = {p["npub_hex"]: p["id"] for p in parts}
        signed_by_name = {p["name"]: p.get("signed_psbt") for p in spec["participants"]}
        have_all = all(signed_by_name.get(p["name"]) for p in spec["participants"])

        if not have_all:
            # Pass 1: tell the operator exactly what each participant must sign.
            print("\n== PASS 1: nothing to accept yet — sign this skeleton ==")
            print("Import the skeleton below into each participant's wallet, have "
                  "them sign ONLY their own inputs, then paste each signed PSBT "
                  "into the spec under that participant's \"signed_psbt\" and re-run "
                  "(same --db).\n")
            for p in parts:
                rd = await db.get_psbt_round(mix_id, p["id"], round_num)
                indices = json.loads((rd or {}).get("input_indices") or "[]")
                outs = await db.get_outputs_by_participant(p["id"])
                print(f"  {p['npub_hex']}:")
                print(f"    sign input index(es): {indices}")
                for o in outs:
                    tag = "change" if o.get("is_change") else "mixed "
                    print(f"    expect output [{tag}] {_btc(o['amount'])} -> {o['address']}")
                has = "yes" if signed_by_name.get(p["npub_hex"]) else "MISSING"
                print(f"    signed_psbt in spec: {has}")
            print("\n== skeleton PSBT (unsigned; safe to import, do NOT broadcast) ==")
            print(skeleton)
            return 1

        # Pass 2: drive the REAL accept path for each returned PSBT, validating
        # against the persisted skeleton (no re-assembly).
        print("\n== PASS 2: accepting signed PSBTs (real validate_returned) ==")
        all_accepted = True
        for p in spec["participants"]:
            name = p["name"]
            before = len(nostr.dms)
            await coord._cmd_accept_psbt(_Ctx(name), name, signed_by_name[name])
            row = await db.get_participant(name_to_pid[name])
            reply = next((m for _r, m in nostr.dms[before:]), "")
            ok = row["state"] == "signed"
            all_accepted = all_accepted and ok
            verdict = "ACCEPTED" if ok else "REJECTED"
            print(f"  {name:<10} -> {verdict}: {reply.splitlines()[0] if reply else ''}")

        if not all_accepted:
            print("\nOne or more PSBTs were rejected — fix and re-sign before "
                  "combining. (The bot would not proceed to broadcast.)")
            return 1

        # Combine + finalize via the REAL PSBTManager — no broadcast.
        signed_rows = [p for p in await db.get_participants_by_mix(mix_id)
                       if p["state"] == "signed"]
        hexes = []
        for p in signed_rows:
            rd = await db.get_psbt_round(mix_id, p["id"], round_num)
            if rd and rd.get("psbt_returned"):
                hexes.append(rd["psbt_returned"])

        print("\n== combine + finalize ==")
        combined = coord.psbt_mgr.combine_psbts(hexes)
        if not combined:
            print("  combine FAILED — the signed PSBTs could not be merged.")
            return 1
        print(f"  combined {len(hexes)} signed PSBT(s).")
        raw = coord.psbt_mgr.finalize(combined)
        if not raw:
            print("  finalize FAILED — the combined PSBT is not fully signed.")
            return 1

        txid = _local_txid(raw)
        print(f"  finalized. txid (computed locally): {txid}")
        print("\n== WOULD BROADCAST (this script does NOT) ==")
        print("  raw transaction hex:")
        print(raw)
        print("\n  Inspect/broadcast yourself only when you're ready, e.g.:")
        print("    bitcoin-cli testmempoolaccept '[\"<raw_hex>\"]'")
        return 0
    finally:
        await chain.close()
        await db.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Offline PSBT return-path test.")
    ap.add_argument("spec", help="mix spec JSON (see scripts/accept-mix.json)")
    ap.add_argument("env", nargs="?", help="env file (default: auto-detected)")
    ap.add_argument("--db", help="persistent state db (default: per-spec temp file)")
    ap.add_argument("--reset", action="store_true",
                    help="delete the db first and re-assemble from scratch")
    args = ap.parse_args()

    with open(args.spec) as f:
        spec = json.load(f)
    env_path = args.env or BotConfig.find_env_path()
    db_path = args.db or os.path.join(
        tempfile.gettempdir(),
        "nostrmix-accept-" + os.path.basename(args.spec) + ".db")
    return asyncio.run(run(spec, env_path, db_path, args.reset))


if __name__ == "__main__":
    raise SystemExit(main())
