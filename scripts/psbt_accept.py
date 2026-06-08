#!/usr/bin/env python3
"""PSBT accept — the return half of the coinjoin, offline.

Where psbt_dryrun.py covers assembly (bot -> participant), this script covers
the return path (participant signs -> bot validates -> combine -> finalize).
It re-runs the SAME deterministic assembly to reproduce the exact skeleton the
participants signed, then drives the REAL Coordinator._cmd_accept_psbt for each
returned PSBT (so validate_returned's cryptographic checks, the multi-mix
matching, and the signing state machine are all production code), and finally
combines + finalizes via the REAL PSBTManager. It computes the would-be txid
but NEVER broadcasts and never sends a DM.

Two-pass workflow:

  Pass 1 — discover what to sign. Run with no "signed_psbt" fields:
      python scripts/psbt_accept.py scripts/accept-mix.json
  It prints ONE skeleton PSBT plus, per participant, the input indices they
  must sign and the outputs they should see. Import the skeleton into each
  participant's wallet, sign ONLY their inputs, export the signed PSBT.

  Pass 2 — verify acceptance. Paste each signed PSBT back into the spec under
  that participant's "signed_psbt" and re-run. The script reports, per
  participant, accepted / rejected (+ reason), then the combined + finalized
  raw transaction and its txid — clearly marked NOT broadcast.

Determinism: the skeleton depends on the fee rate, so the spec MUST pin a
numeric "fee_rate" (the same value across pass 1 and pass 2). A live estimate
would re-assemble a different tx than the one your wallet signed.

Spec JSON: the psbt_dryrun spec, plus an optional per-participant "signed_psbt".
Use REAL "txid:vout" inputs (looked up on-chain) so the PSBT is importable and
your wallet can actually sign it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

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

import tempfile


def _local_txid(raw_tx_hex: str) -> str:
    """Compute a transaction's txid from its raw hex (display only)."""
    from bitcointx.core import CTransaction, b2x
    return b2x(CTransaction.deserialize(bytes.fromhex(raw_tx_hex)).GetTxid()[::-1])


async def _build_and_assemble(spec: dict, env_path: str):
    """Recreate the dry-run assembly against a throwaway DB and return the
    coordinator + the assembled skeleton. Mirrors psbt_dryrun.run()'s intake +
    assembly so the skeleton is byte-identical to what was signed."""
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
    # Pin the rate so re-assembly reproduces the signed skeleton exactly.
    async def _fixed_rate():
        return float(fee_rate)
    chain.estimate_fee_rate = _fixed_rate

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
    await db.update_mix(mix_id, state="collecting", fee_rate=int(fee_rate),
                        input_type="p2wpkh", output_type="p2wpkh")

    # Intake through the real /addresses handler.
    name_to_pid: dict[str, str] = {}
    for p in spec["participants"]:
        name = p["name"]
        pid = await db.add_participant(mix_id, name, p.get("lud16", ""))
        name_to_pid[name] = pid
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

    rounds = await db.get_psbt_rounds_by_mix(mix_id)
    skeleton = next((r["psbt_sent"] for r in rounds if r.get("psbt_sent")), None)
    if not skeleton:
        raise RuntimeError("no skeleton PSBT was assembled")

    return coord, db, nostr, mix_id, skeleton, name_to_pid, db_path


async def run(spec: dict, env_path: str) -> int:
    if spec.get("fee_rate") is None:
        print("ERROR: this script needs a pinned numeric \"fee_rate\" in the spec "
              "so the re-assembled skeleton matches the PSBT your wallet signed. "
              "Add e.g. \"fee_rate\": 30.")
        return 2

    coord, db, nostr, mix_id, skeleton, name_to_pid, db_path = \
        await _build_and_assemble(spec, env_path)
    try:
        signed_by_name = {p["name"]: p.get("signed_psbt") for p in spec["participants"]}
        have_all = all(signed_by_name.get(p["name"]) for p in spec["participants"])
        round_num = (await db.get_mix(mix_id)).get("ghost_retries", 0) + 1

        if not have_all:
            # Pass 1: tell the operator exactly what each participant must sign.
            print("== PASS 1: nothing to accept yet — sign this skeleton ==")
            print("Import the skeleton below into each participant's wallet, have "
                  "them sign ONLY their own inputs, then paste each signed PSBT "
                  "into the spec under that participant's \"signed_psbt\" and re-run.\n")
            for p in await db.get_participants_by_mix(mix_id):
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

        # Pass 2: drive the REAL accept path for each returned PSBT.
        print("== PASS 2: accepting signed PSBTs (real validate_returned) ==")
        all_accepted = True
        for p in spec["participants"]:
            name = p["name"]
            signed_hex = signed_by_name[name]
            before = len(nostr.dms)
            await coord._cmd_accept_psbt(_Ctx(name), name, signed_hex)
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
        await db.close()
        _rm(db_path)


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/psbt_accept.py <mix-spec.json> [env-file]")
        return 2
    with open(sys.argv[1]) as f:
        spec = json.load(f)
    env_path = sys.argv[2] if len(sys.argv) > 2 else BotConfig.find_env_path()
    return asyncio.run(run(spec, env_path))


if __name__ == "__main__":
    raise SystemExit(main())
