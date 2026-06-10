"""Lightning Handler — refund sending via LNURL-pay (BTCPay-backed)."""

from __future__ import annotations

import logging
from typing import Optional
from nostrbot_sdk import BtcPayWallet, LnurlPayer, PayoutResult, FeePolicy
from nostr_sdk import Keys

from .log_tokens import tokens

logger = logging.getLogger(__name__)


# Logging discipline (see src/coordinator.py top-of-file): never log raw
# lud16 or exception tracebacks. Tokens + exception class names only.


class LightningHandler:
    """Handles outbound Lightning refunds.

    The SDK's LnurlPayer does the whole job: it resolves a participant's
    lud16 to a bolt11 invoice and pays it via an InvoiceWallet. BtcPayWallet
    is that wallet — it is passed INTO the payer, not used directly. So there
    is a single refund path (payer.pay), not two independent backends.

    Receiving zaps is handled entirely by the Nostr SDK's NIP-57 receipt
    validation in nostr_handler; nothing here is involved in that.
    """

    def __init__(self, config):
        """Initialize with BotConfig."""
        self._cfg = config
        self._wallet: Optional[BtcPayWallet] = None
        self._payer: Optional[LnurlPayer] = None

    async def init(self):
        """Construct the BTCPay invoice wallet, if configured.

        Refunds are only possible when BTCPay credentials are present (they
        are only needed when a service fee is charged). Without them the
        wallet stays None and send_refund reports no backend.
        """
        if self._cfg.BTCPAY_URL and self._cfg.BTCPAY_STORE and self._cfg.BTCPAY_API_KEY:
            self._wallet = BtcPayWallet(
                url=self._cfg.BTCPAY_URL,
                store_id=self._cfg.BTCPAY_STORE,
                api_key=self._cfg.BTCPAY_API_KEY,
            )

    async def init_payer_with_keys(self, keys: Keys):
        """Build the LnurlPayer once the bot's Nostr keys are available.

        The payer needs an InvoiceWallet to pay the resolved invoices; with
        no BTCPay wallet configured we can't send refunds, so we leave the
        payer None (send_refund then logs 'no refund backend').

        FeePolicy(operator_contribution_sats=0): total outflow per refund is
        capped at amount_sats — we already deducted REFUND_KEEP_PERCENT, so
        the Lightning routing fee comes out of that keep, never on top of it.
        """
        if self._wallet is None:
            logger.warning(
                "No BTCPay wallet configured — Lightning refunds are disabled."
            )
            return
        self._payer = LnurlPayer(
            keys=keys,
            wallet=self._wallet,
            fee_policy=FeePolicy(operator_contribution_sats=0),
            default_comment="nostrmix refund",
        )

    async def send_refund(self, lud16: str, amount_sats: int,
                          reason: str = "mix_cancellation") -> Optional[PayoutResult]:
        """Send a refund to a participant's LNURL address.

        Args:
            lud16: participant's Lightning address (e.g., user@pay.domain.com)
            amount_sats: amount to send (REFUND_KEEP_PERCENT already deducted)
            reason: reason for logging

        Returns: a PayoutResult ONLY when the payment actually settled
        (status == "paid"). Returns None on any other outcome — no backend,
        a missing lud16, an amount the provider can't honour ("skipped"), or
        a failure. The coordinator treats a non-None result as a completed
        refund, so anything not truly paid must come back as None.
        """
        if amount_sats <= 0:
            return None
        if not lud16:
            logger.error(
                "Refund for %d sats (reason=%s) has no lightning_addr — "
                "operator must reconcile.",
                amount_sats, reason,
            )
            return None
        if self._payer is None:
            logger.error(
                "Refund to %s for %d sats (reason=%s) — no refund backend "
                "(BTCPay not configured).",
                tokens.l(lud16), amount_sats, reason,
            )
            return None

        try:
            result = await self._payer.pay(lud16=lud16, amount_sats=amount_sats)
        except Exception as e:
            # Privacy: tokenise the lud16 (maps to npub via DB) and drop the
            # traceback (it would dump call-site locals).
            logger.error(
                "LNURL refund to %s for %d sats (reason=%s) failed: %s",
                tokens.l(lud16), amount_sats, reason, type(e).__name__,
            )
            return None

        if result is None or result.status != "paid":
            status = getattr(result, "status", "none")
            logger.error(
                "LNURL refund to %s for %d sats (reason=%s) not settled "
                "(status=%s) — operator must reconcile.",
                tokens.l(lud16), amount_sats, reason, status,
            )
            return None
        return result

    @staticmethod
    def check_payment(zap_amount_sats: int, expected_fee_sats: int) -> bool:
        """Check if a zap payment matches the expected fee.

        Partial payments are treated the same as no payment.
        """
        return zap_amount_sats >= expected_fee_sats
