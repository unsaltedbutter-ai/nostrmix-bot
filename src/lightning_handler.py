"""Lightning Handler — zap detection, refund sending via BTCPay."""

from __future__ import annotations

import logging
from typing import Optional
from nostrbot_sdk import BtcPayWallet, LnurlPayer, PayoutResult, FeePolicy
from nostr_sdk import Keys

logger = logging.getLogger(__name__)


class LightningHandler:
    """Handles Lightning interactions: zap detection, refund sending.

    For receiving zaps, we rely on the Nostr SDK's NIP-57 zap receipt validation.
    For sending refunds, we use the BTCPay wallet or LNURL-pay client.
    """

    def __init__(self, config):
        """Initialize with BotConfig."""
        self._cfg = config
        self._btcpay_wallet: Optional[BtcPayWallet] = None
        self._lnurl_payer: Optional[LnurlPayer] = None

    async def init(self):
        """Initialize wallets."""
        # Initialize BTCPay wallet for sending refunds
        if self._cfg.BTCPAY_URL and self._cfg.BTCPAY_STORE and self._cfg.BTCPAY_API_KEY:
            self._btcpay_wallet = BtcPayWallet(
                url=self._cfg.BTCPAY_URL,
                store_id=self._cfg.BTCPAY_STORE,
                api_key=self._cfg.BTCPAY_API_KEY,
            )

    async def init_payer_with_keys(self, keys: Keys):
        """Initialize LnurlPayer with the bot's Nostr keys.

        Must be called after the bot is started and keys are available.
        """
        self._lnurl_payer = LnurlPayer(keys=keys)

        # Also pass keys to BTCPay for NIP-57 zap request support
        if self._btcpay_wallet:
            # BtcPayWallet can also use keys for zap request signing
            pass

    async def get_balance(self) -> Optional[int]:
        """Check BTCPay balance for outbound capacity."""
        if self._btcpay_wallet:
            try:
                balance = await self._btcpay_wallet.get_balance()
                return balance
            except Exception as e:
                logger.warning("BTCPay get_balance failed: %s", e, exc_info=True)
                return None
        return None

    async def send_refund(self, lud16: str, amount_sats: int,
                          reason: str = "mix_cancellation") -> Optional[PayoutResult]:
        """Send a refund to a participant via their LNURL address.

        Args:
            lud16: participant's Lightning address (e.g., user@pay.domain.com)
            amount_sats: amount to send (minus REFUND_KEEP_PERCENT already applied)
            reason: reason for refund (for logging)

        Returns: PayoutResult or None on failure.
        """
        # If using BTCPay, we pay via the BTCPay wallet
        if self._btcpay_wallet and amount_sats > 0:
            try:
                result = await self._btcpay_wallet.send_to_lud16(lud16, amount_sats)
                return result
            except Exception as e:
                # Fall through to the LNURL payer rather than failing silently.
                logger.warning(
                    "BTCPay refund to %s for %d sats (reason=%s) failed: %s",
                    lud16, amount_sats, reason, e, exc_info=True,
                )

        # Fallback: use LNURL payer
        if self._lnurl_payer and amount_sats > 0:
            try:
                # Use default fee policy with refund minimum check
                fee_policy = FeePolicy(
                    operator_contribution_sats=0,
                    marker_percent=5,
                    retries=3,
                )
                result = await self._lnurl_payer.pay(
                    lud16=lud16,
                    amount_sats=amount_sats,
                    fee_policy=fee_policy,
                    zap_request=None,  # not a zap, just a payment
                )
                return result
            except Exception as e:
                logger.error(
                    "LNURL refund to %s for %d sats (reason=%s) failed: %s",
                    lud16, amount_sats, reason, e, exc_info=True,
                )

        logger.error(
            "Refund to %s for %d sats (reason=%s) — no working backend",
            lud16, amount_sats, reason,
        )
        return None

    async def send_refund_to_lnurl(self, lud16: str, zap_amount_sats: int,
                                   keep_percent: int = 5,
                                   keep_min_sats: int = 50) -> Optional[int]:
        """Send a refund minus the keep percentage.

        Returns the amount actually sent (or None on failure).
        """
        keep_sats = max(zap_amount_sats * keep_percent // 100, keep_min_sats)
        refund_sats = zap_amount_sats - keep_sats

        if refund_sats <= 0:
            return 0  # Nothing to refund

        result = await self.send_refund(lud16, refund_sats)
        if result:
            return refund_sats
        return None

    @staticmethod
    def check_payment(zap_amount_sats: int, expected_fee_sats: int) -> bool:
        """Check if a zap payment matches the expected fee.

        Partial payments are treated the same as no payment.
        """
        return zap_amount_sats >= expected_fee_sats
