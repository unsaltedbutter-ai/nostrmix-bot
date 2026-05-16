"""Entry point — async loop for nostrmix-bot (PSBT coinjoin mixer over Nostr)."""

from __future__ import annotations

import asyncio
import os
import sys
import logging

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
logger = logging.getLogger("nostrmix-bot")


async def main():
    """Main entry point."""

    # 1. Load configuration
    env_path = BotConfig.find_env_path()
    cfg = BotConfig(env_path)
    logger.info(f"Config loaded from {env_path}")

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
