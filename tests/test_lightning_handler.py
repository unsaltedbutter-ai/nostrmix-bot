"""Tests for LightningHandler — the single LNURL-pay refund path.

The handler delegates to nostrbot_sdk's LnurlPayer (which itself pays resolved
invoices via a BtcPayWallet). We swap a fake payer onto the handler instance so
we never hit a network, PLUS one interface-pinning test that imports the REAL
SDK classes and asserts their signatures still match how the handler calls them
— so a future SDK bump that renames/repositions an argument fails loudly here
instead of silently breaking every refund in production.
"""

import os
import sys
import inspect
import logging
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.lightning_handler import LightningHandler


class _Result:
    """Stand-in for nostrbot_sdk.PayoutResult (status/fee_sats/actual_sats)."""

    def __init__(self, status="paid", fee_sats=1, actual_sats=None):
        self.status = status
        self.fee_sats = fee_sats
        self.actual_sats = actual_sats


class FakePayer:
    """Matches LnurlPayer.pay(lud16, amount_sats, *, ...) → PayoutResult."""

    def __init__(self, status="paid", raises=False):
        self.status = status
        self.raises = raises
        self.pay_calls: list = []

    async def pay(self, lud16, amount_sats, *, zap_target_pubkey=None,
                  comment=None, source_url=""):
        self.pay_calls.append((lud16, amount_sats))
        if self.raises:
            raise RuntimeError("lnurl 503")
        return _Result(status=self.status, actual_sats=amount_sats)


class _NoOpConfig:
    """The handler reads BTCPAY_* off the config only in init(), which these
    tests don't call — we wire the fake payer directly."""
    BTCPAY_URL = ""
    BTCPAY_STORE = ""
    BTCPAY_API_KEY = ""


def _make_handler(payer=None) -> LightningHandler:
    h = LightningHandler(_NoOpConfig())
    h._payer = payer
    return h


class TestSendRefund:
    @pytest.mark.asyncio
    async def test_paid_returns_result(self):
        payer = FakePayer(status="paid")
        h = _make_handler(payer)
        result = await h.send_refund("user@example.com", 1000, reason="test")
        assert result is not None
        assert result.status == "paid"
        assert payer.pay_calls == [("user@example.com", 1000)]

    @pytest.mark.asyncio
    async def test_skipped_returns_none(self, caplog):
        """A provider that can't honour the amount yields status='skipped';
        the coordinator must NOT treat that as a completed refund."""
        payer = FakePayer(status="skipped")
        h = _make_handler(payer)
        with caplog.at_level(logging.ERROR):
            result = await h.send_refund("user@example.com", 1000, reason="test")
        assert result is None
        assert any("not settled" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_payer_exception_returns_none_and_logs(self, caplog):
        payer = FakePayer(raises=True)
        h = _make_handler(payer)
        with caplog.at_level(logging.ERROR):
            result = await h.send_refund("user@example.com", 3000, reason="boom")
        assert result is None
        assert any("refund" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_no_backend_returns_none(self, caplog):
        h = _make_handler(payer=None)
        with caplog.at_level(logging.ERROR):
            result = await h.send_refund("user@example.com", 500)
        assert result is None
        assert any("no refund backend" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_zero_amount_is_a_noop(self):
        payer = FakePayer()
        h = _make_handler(payer)
        result = await h.send_refund("user@example.com", 0)
        assert result is None
        assert payer.pay_calls == []

    @pytest.mark.asyncio
    async def test_missing_lud16_returns_none(self, caplog):
        payer = FakePayer()
        h = _make_handler(payer)
        with caplog.at_level(logging.ERROR):
            result = await h.send_refund("", 500)
        assert result is None
        assert payer.pay_calls == []


class TestSdkInterfacePinning:
    """Guard against SDK drift: these assert the REAL nostrbot_sdk signatures
    are still shaped the way LightningHandler calls them. If the SDK renames or
    repositions an argument, these fail instead of every refund silently dying."""

    def test_lnurl_payer_constructor_shape(self):
        from nostrbot_sdk import LnurlPayer
        params = inspect.signature(LnurlPayer.__init__).parameters
        # Handler constructs LnurlPayer(keys=, wallet=, fee_policy=, default_comment=)
        assert "keys" in params
        assert "wallet" in params
        assert "fee_policy" in params
        assert "default_comment" in params

    def test_lnurl_payer_pay_shape(self):
        from nostrbot_sdk import LnurlPayer
        params = inspect.signature(LnurlPayer.pay).parameters
        # Handler calls payer.pay(lud16=, amount_sats=)
        assert "lud16" in params
        assert "amount_sats" in params

    def test_btcpay_wallet_constructor_shape(self):
        from nostrbot_sdk import BtcPayWallet
        params = inspect.signature(BtcPayWallet.__init__).parameters
        # Handler constructs BtcPayWallet(url=, store_id=, api_key=)
        assert "url" in params
        assert "store_id" in params
        assert "api_key" in params

    def test_payout_result_has_status(self):
        from nostrbot_sdk import PayoutResult
        fields = inspect.signature(PayoutResult).parameters
        assert "status" in fields
