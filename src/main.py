"""Entry point — async loop for nostrmix-bot (PSBT coinjoin mixer over Nostr)."""

from __future__ import annotations

import asyncio
import os
import sys
import logging
from urllib.parse import urlparse

# Add src to path
SRC_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SRC_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from src.config import BotConfig
from src.database import Database
from src.nostr_handler import NostrHandler
from src.chain_monitor import ChainMonitor
from src.psbt_manager import PSBTManager
from src.fee_engine import FeeEngine
from src.lightning_handler import LightningHandler
from src.coordinator import Coordinator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# Privacy: HTTP client libraries log full request URLs at INFO, which would
# write coinjoin txids, user outpoints, and lnurl endpoints into the log
# file. chain_monitor caps httpx/httpcore at import; repeat here so the
# policy holds even if a future module talks HTTP through something else.
for _noisy_logger in ("httpx", "httpcore", "urllib3", "aiohttp.client"):
    logging.getLogger(_noisy_logger).setLevel(logging.WARNING)
logger = logging.getLogger("nostrmix-bot")

# ANSI styling for the startup mast. Renders when the log is tailed in a
# terminal; harmless escape bytes in the raw file.
_R, _B, _DIM = "\033[0m", "\033[1m", "\033[2m"
_GRN, _RED, _CYN, _YEL = "\033[32m", "\033[31m", "\033[36m", "\033[33m"


def _git_revision(root: str) -> tuple[str, str | None]:
    """Return (short_sha, branch) by reading .git directly — no `git` binary
    needed (it may be absent under launchd). Returns ("unknown", None) on any
    problem. Handles a .git *file* (worktree), a loose ref, and packed-refs."""
    try:
        git_dir = os.path.join(root, ".git")
        if os.path.isfile(git_dir):  # worktree/submodule: .git points elsewhere
            with open(git_dir) as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                git_dir = content[len("gitdir:"):].strip()
                if not os.path.isabs(git_dir):
                    git_dir = os.path.normpath(os.path.join(root, git_dir))
        with open(os.path.join(git_dir, "HEAD")) as f:
            head = f.read().strip()
        if head.startswith("ref:"):
            ref = head[4:].strip()
            branch = ref.rsplit("/", 1)[-1]
            ref_path = os.path.join(git_dir, ref)
            if os.path.exists(ref_path):
                with open(ref_path) as f:
                    sha = f.read().strip()
            else:  # ref is packed
                sha = ""
                packed = os.path.join(git_dir, "packed-refs")
                if os.path.exists(packed):
                    with open(packed) as f:
                        for line in f:
                            line = line.strip()
                            if line and not line.startswith(("#", "^")) \
                                    and line.endswith(" " + ref):
                                sha = line.split(" ", 1)[0]
                                break
        else:  # detached HEAD: the file holds the sha itself
            sha, branch = head, "detached"
        if sha:
            return sha[:7], branch
    except Exception:
        pass
    return "unknown", None


def _startup_banner(cfg: BotConfig, env_path: str) -> None:
    """Print a visual mast + the most operator-relevant config to stderr (the
    log stream), marking the start of a fresh process run."""
    sha, branch = _git_revision(ROOT_DIR)
    rev = sha if branch is None else f"{sha} ({branch})"
    relays = cfg.NOSTR_RELAYS
    relay_hosts = ", ".join(urlparse(u).hostname or u for u in relays) or "(none)"
    lud16 = cfg.BOT_LUD16 or f"{_DIM}(none){_R}"
    zaps = f"{_GRN}OFF{_R}" if cfg.FEE_PER_ELEMENT == 0 else f"{_YEL}ON{_R}"
    btc = cfg.DEFAULT_OUTPUT_SIZE / 1e8
    bar = f"{_GRN}{'═' * 64}{_R}"
    lines = [
        "",
        bar,
        f"{_GRN}{_B}  ▶  N O S T R M I X   B O T   —   starting{_R}",
        bar,
        f"  {_CYN}commit  {_R}: {_B}{rev}{_R}",
        f"  {_CYN}config  {_R}: {env_path}",
        f"  {_CYN}bot     {_R}: {_B}{cfg.BOT_NAME}{_R}  {lud16}",
        f"  {_CYN}relays  {_R}: {len(relays)} → {relay_hosts}",
        f"  {_CYN}mix size{_R}: {cfg.DEFAULT_OUTPUT_SIZE:,} sats  ({btc:.8f} BTC)",
        f"  {_CYN}min utxo{_R}: {cfg.MINIMUM_UTXO_SIZE:,} sats",
        f"  {_CYN}required{_R}: {_B}{cfg.DEFAULT_REQUIRED_NONCONFORMING}{_R} "
        f"non-conforming participant(s) per mix",
        f"  {_CYN}fee/elem{_R}: {cfg.FEE_PER_ELEMENT} sats  (zaps {zaps})",
        bar,
        "",
    ]
    print("\n".join(lines), file=sys.stderr, flush=True)


async def main():
    """Main entry point."""

    # 1. Load configuration
    env_path = BotConfig.find_env_path()
    cfg = BotConfig(env_path)
    _startup_banner(cfg, env_path)

    # 2. Initialize database
    db = Database(cfg.DB_PATH)
    await db.connect()
    logger.info(f"Database connected at {cfg.DB_PATH}")

    # 3. Initialize components
    chain = ChainMonitor(
        api_base=cfg.MEMPOOL_API,
        api_backup=cfg.MEMPOOL_API_BACKUP or None,
        min_fee_rate=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate=cfg.MAX_FEE_RATE_SATS,
        fee_multiplier=cfg.FEE_MULTIPLIER,
        fee_lookback_blocks=cfg.FEE_LOOKBACK_BLOCKS,
    )

    psbt_mgr = PSBTManager(
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
        overhead=cfg.TX_OVERHEAD_VSIZE,
    )

    fee_engine = FeeEngine(
        fee_per_element=cfg.FEE_PER_ELEMENT,
        min_fee_rate_sats=cfg.MIN_FEE_RATE_SATS,
        max_fee_rate_sats=cfg.MAX_FEE_RATE_SATS,
        overhead_vsize=cfg.TX_OVERHEAD_VSIZE,
        minimum_utxo_size=cfg.MINIMUM_UTXO_SIZE,
        input_vsize_map=cfg.INPUT_VSIZE_BY_TYPE,
        output_vsize_map=cfg.OUTPUT_VSIZE_BY_TYPE,
    )

    lightning = LightningHandler(cfg)
    await lightning.init()

    # 4. Initialize Nostr handler
    nostr = NostrHandler(cfg)

    # 5. Wire up coordinator
    coordinator = Coordinator(cfg, db)
    await coordinator.init(
        nostr=nostr,
        chain=chain,
        psbt_mgr=psbt_mgr,
        fee_engine=fee_engine,
        lightning=lightning,
    )

    # 6. Run
    logger.info("Starting nostrmix-bot...")
    await coordinator.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
