#!/usr/bin/env python3
"""Preflight check — validate config + connectivity before launching the bot.

Run on the host you'll deploy to (e.g. butter.local):

    source venv/bin/activate
    python scripts/preflight.py

It does NOT start the bot, touch any funds, or send anything. It:
  - loads the bot config (the same loader main.py uses) and prints a
    non-secret summary, warning about missing/incoherent settings;
  - checks mempool.space (and the backup) are reachable and prints the live
    fee-rate estimate;
  - checks each configured relay host accepts a TCP connection.

Secrets (the nsec, BTCPay key) are never printed — only whether they are set.
Exit code is non-zero if a hard problem is found.
"""
from __future__ import annotations

import asyncio
import os
import sys
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.config import BotConfig
from src.chain_monitor import ChainMonitor

# A famous always-confirmed txid (Satoshi's block-1 coinbase) for a liveness
# probe that doesn't depend on any of our own data.
_BLOCK1_COINBASE = "0e3e2357e806b6cdb1f70b54c3a3a17b6714ee1f0e68bebb44a74b1efd512098"


def _check_config(cfg: BotConfig) -> list[str]:
    """Print a non-secret config summary; return a list of hard problems."""
    problems: list[str] = []
    print("== config ==")
    print(f"  config file        : {cfg._env_path}")
    print(f"  bot key set        : {bool(cfg.NOSTR_PRIVATE_KEY_NPUB)}")
    print(f"  relays             : {len(cfg.NOSTR_RELAYS)} -> {', '.join(cfg.NOSTR_RELAYS)}")
    print(f"  FEE_PER_ELEMENT    : {cfg.FEE_PER_ELEMENT} ({'zaps OFF' if cfg.FEE_PER_ELEMENT == 0 else 'zaps ON'})")
    print(f"  output_size        : {cfg.DEFAULT_OUTPUT_SIZE} sats")
    print(f"  required NC parts   : {cfg.DEFAULT_REQUIRED_NONCONFORMING}")
    print(f"  max conforming UTXOs: {cfg.MAX_CONFORMING_UTXOS}")
    print(f"  accepted input/out  : {sorted(cfg.ACCEPTED_INPUT_TYPES)} / {sorted(cfg.ACCEPTED_OUTPUT_TYPES)}")
    print(f"  donation address    : {'SET' if cfg.DONATION_ADDRESS else 'blank (fold-to-fee)'}")
    print(f"  db path             : {cfg.DB_PATH}")
    print(f"  mempool api         : {cfg.MEMPOOL_API}  backup={cfg.MEMPOOL_API_BACKUP or '(none)'}")

    if not cfg.NOSTR_PRIVATE_KEY_NPUB:
        problems.append("NOSTR_PRIVATE_KEY_NPUB is empty — the bot has no identity to sign as.")
    if not cfg.NOSTR_RELAYS:
        problems.append("NOSTR_RELAYS is empty — the bot can't reach any relay.")
    if cfg.FEE_PER_ELEMENT > 0:
        if not cfg.ZAP_PROVIDER_PUBKEY_HEX:
            problems.append("FEE_PER_ELEMENT > 0 but ZAP_PROVIDER_PUBKEY_HEX is empty — zaps can't be validated.")
        if not (cfg.BTCPAY_URL and cfg.BTCPAY_STORE and cfg.BTCPAY_API_KEY):
            problems.append("FEE_PER_ELEMENT > 0 but BTCPAY_* is incomplete — refunds will fail.")
    if cfg.DONATION_ADDRESS:
        print("  ! NOTE: DONATION_ADDRESS is set — a recurring operator address is a "
              "linkable on-chain fingerprint. Blank (fold-to-fee) is more private.")
    return problems


async def _check_mempool(cfg: BotConfig) -> list[str]:
    problems: list[str] = []
    print("\n== bitcoin api ==")
    chain = ChainMonitor(
        api_base=cfg.MEMPOOL_API,
        api_backup=cfg.MEMPOOL_API_BACKUP or None,
        min_fee_rate=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate=cfg.MAX_FEE_RATE_SATS,
        fee_multiplier=cfg.FEE_MULTIPLIER,
        fee_lookback_blocks=cfg.FEE_LOOKBACK_BLOCKS,
    )
    try:
        confirmed = await chain.is_confirmed(_BLOCK1_COINBASE)
        if confirmed:
            print(f"  reachable           : yes (confirmed a known tx)")
        else:
            problems.append("mempool API reachable but a known-confirmed tx came back unconfirmed — unexpected.")
            print("  reachable           : DEGRADED (known tx not confirmed?)")
        rate = await chain.estimate_fee_rate()
        print(f"  fee estimate        : {rate:.2f} sat/vB "
              f"(clamped to [{cfg.MIN_FEE_RATE_SATS}, {cfg.MAX_FEE_RATE_SATS}])")
        if rate <= 0:
            problems.append("fee estimate returned <= 0.")
    except Exception as e:
        problems.append(f"mempool API unreachable: {type(e).__name__}")
        print(f"  reachable           : NO ({type(e).__name__})")
    finally:
        await chain.close()
    return problems


async def _check_relays(cfg: BotConfig) -> list[str]:
    """Individual unreachable relays are warnings (the bot tolerates stale ones);
    a hard problem only if NONE are reachable."""
    print("\n== relays (TCP reachability) ==")
    reachable = 0
    for url in cfg.NOSTR_RELAYS:
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme in ("wss", "https") else 80)
        if not host:
            print(f"  {url}: UNPARSEABLE (warning)")
            continue
        try:
            fut = asyncio.open_connection(host, port)
            _reader, writer = await asyncio.wait_for(fut, timeout=5)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            print(f"  {url}: reachable ({host}:{port})")
            reachable += 1
        except Exception as e:
            print(f"  {url}: UNREACHABLE ({type(e).__name__}) (warning)")
    if reachable == 0:
        return ["NONE of the configured relays are reachable."]
    return []


async def main() -> int:
    env_path = sys.argv[1] if len(sys.argv) > 1 else BotConfig.find_env_path()
    try:
        cfg = BotConfig(env_path)
    except Exception as e:
        print(f"config FAILED to load: {type(e).__name__}: {e}")
        return 2

    problems = _check_config(cfg)
    problems += await _check_mempool(cfg)
    problems += await _check_relays(cfg)

    print("\n== result ==")
    if problems:
        print(f"  {len(problems)} problem(s):")
        for p in problems:
            print(f"   - {p}")
        print("  PREFLIGHT: FAIL")
        return 1
    print("  PREFLIGHT: PASS — config + connectivity look good.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
