"""Tests for LightningHandler — refund paths and silent-failure logging.

The handler depends on nostrbot_sdk's BtcPayWallet and LnurlPayer. We stub
those out by swapping in fakes on the handler instance after construction,
so we never touch the real SDK objects or hit a network.
"""

import os
import sys
import logging
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src.lightning_handler import LightningHandler


class FakeBtcPay:
    def __init__(self, send_raises: bool = False, balance: int = 10_000,
                 balance_raises: bool = False):
        self.send_raises = send_raises
        self.balance = balance
        self.balance_raises = balance_raises
        self.send_calls: list = []

    async def send_to_lud16(self, lud16, sats):
        self.send_calls.append((lud16, sats))
        if self.send_raises:
            raise RuntimeError("btcpay 401")
        return ("ok", sats)

    async def get_balance(self):
        if self.balance_raises:
            raise RuntimeError("btcpay get_balance 500")
        return self.balance


class FakeLnurlPayer:
    def __init__(self, pay_raises: bool = False):
        self.pay_raises = pay_raises
        self.pay_calls: list = []

    async def pay(self, lud16, amount_sats, fee_policy, zap_request):
        self.pay_calls.append((lud16, amount_sats))
        if self.pay_raises:
            raise RuntimeError("lnurl 503")
        return ("lnurl-paid", amount_sats)


class _NoOpConfig:
    """The handler only reads BTCPAY_URL/STORE/API_KEY off the config in
    init(), and we never call init() in these tests — we wire fakes directly."""
    BTCPAY_URL = ""
    BTCPAY_STORE = ""
    BTCPAY_API_KEY = ""


def _make_handler(btcpay=None, lnurl=None) -> LightningHandler:
    h = LightningHandler(_NoOpConfig())
    h._btcpay_wallet = btcpay
    h._lnurl_payer = lnurl
    return h


class TestSendRefund:
    @pytest.mark.asyncio
    async def test_btcpay_success_does_not_fall_through_to_lnurl(self):
        btc = FakeBtcPay(send_raises=False)
        lnurl = FakeLnurlPayer(pay_raises=False)
        h = _make_handler(btc, lnurl)
        result = await h.send_refund("user@example.com", 1000, reason="test")
        assert result is not None
        assert btc.send_calls == [("user@example.com", 1000)]
        assert lnurl.pay_calls == []

    @pytest.mark.asyncio
    async def test_btcpay_failure_falls_back_to_lnurl(self, caplog):
        btc = FakeBtcPay(send_raises=True)
        lnurl = FakeLnurlPayer(pay_raises=False)
        h = _make_handler(btc, lnurl)
        with caplog.at_level(logging.WARNING):
            result = await h.send_refund("user@example.com", 2000, reason="fallback")
        assert result is not None
        assert btc.send_calls == [("user@example.com", 2000)]
        assert lnurl.pay_calls == [("user@example.com", 2000)]
        # And we logged the BTCPay failure rather than swallowing.
        assert any("btcpay refund" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_both_backends_fail_returns_none_and_logs_error(self, caplog):
        btc = FakeBtcPay(send_raises=True)
        lnurl = FakeLnurlPayer(pay_raises=True)
        h = _make_handler(btc, lnurl)
        with caplog.at_level(logging.ERROR):
            result = await h.send_refund("user@example.com", 3000, reason="catastrophe")
        assert result is None
        # Two failure logs: the LNURL exception, plus the final "no working
        # backend" line.
        error_msgs = [r.message.lower() for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("no working backend" in m for m in error_msgs)

    @pytest.mark.asyncio
    async def test_no_btcpay_uses_lnurl_directly(self):
        lnurl = FakeLnurlPayer(pay_raises=False)
        h = _make_handler(btcpay=None, lnurl=lnurl)
        result = await h.send_refund("user@example.com", 500)
        assert result is not None
        assert lnurl.pay_calls == [("user@example.com", 500)]

    @pytest.mark.asyncio
    async def test_zero_amount_is_a_noop(self):
        btc = FakeBtcPay()
        lnurl = FakeLnurlPayer()
        h = _make_handler(btc, lnurl)
        result = await h.send_refund("user@example.com", 0)
        # No backend was attempted for amount=0
        assert btc.send_calls == []
        assert lnurl.pay_calls == []
        # And the handler still emits the final "no working backend" log,
        # which is acceptable — the caller shouldn't be asking us to send 0.
        assert result is None


class TestGetBalance:
    @pytest.mark.asyncio
    async def test_returns_balance_when_available(self):
        h = _make_handler(btcpay=FakeBtcPay(balance=42_000))
        assert await h.get_balance() == 42_000

    @pytest.mark.asyncio
    async def test_logs_and_returns_none_when_btcpay_raises(self, caplog):
        h = _make_handler(btcpay=FakeBtcPay(balance_raises=True))
        with caplog.at_level(logging.WARNING):
            result = await h.get_balance()
        assert result is None
        assert any("btcpay get_balance" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_btcpay_configured(self):
        h = _make_handler(btcpay=None)
        assert await h.get_balance() is None


class TestSendRefundToLnurl:
    """send_refund_to_lnurl computes the keep deduction and delegates to send_refund."""

    @pytest.mark.asyncio
    async def test_subtracts_keep_percent(self):
        btc = FakeBtcPay()
        h = _make_handler(btc, lnurl=None)
        # 10000 sats × (1 - 5%) = 9500, but keep_min_sats=50 wins only if
        # 5% < 50. At zap=10000, 5% = 500 > 50, so 500 is kept.
        sent = await h.send_refund_to_lnurl("u@x", 10_000, keep_percent=5, keep_min_sats=50)
        assert sent == 9_500
        assert btc.send_calls == [("u@x", 9_500)]

    @pytest.mark.asyncio
    async def test_keep_min_sats_kicks_in_for_tiny_zaps(self):
        btc = FakeBtcPay()
        h = _make_handler(btc, lnurl=None)
        # 100 sats × 5% = 5, but keep_min_sats=50 wins. Refund = 100 - 50 = 50.
        sent = await h.send_refund_to_lnurl("u@x", 100, keep_percent=5, keep_min_sats=50)
        assert sent == 50

    @pytest.mark.asyncio
    async def test_returns_zero_when_refund_would_be_nonpositive(self):
        h = _make_handler(btcpay=FakeBtcPay(), lnurl=None)
        # 30 sats - max(5%, 50) = 30 - 50 = -20, refund = 0
        sent = await h.send_refund_to_lnurl("u@x", 30, keep_percent=5, keep_min_sats=50)
        assert sent == 0
