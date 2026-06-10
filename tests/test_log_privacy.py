"""Privacy regression guards for the logging surface.

Two kinds of tests:

  1. Static grep over src/*.py for unsafe log patterns. Fails CI if anyone
     reintroduces `logger.error(f"... {npub_hex}")` or `exc_info=True`
     in a handler-level error path. Cheap to run; catches the future
     accidental regression at the diff stage.

  2. Runtime tests under pytest's caplog: exercise representative flows
     (DM handler error, ghost detection, broadcast sweep, refund
     failure, overpayment, unmatched zap, stale-chunk cleanup) and
     assert NO raw npub / lud16 / UTXO-txid / output-address appears
     in the captured log output. Catches the failure if any code path
     is rewritten to bypass the tokenisation helpers.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Re-use the coordinator-test fixtures: same fake handlers, same temp
# database. Keeps the privacy test self-contained.
from test_coordinator import (
    make_coord, FakeCtx, FakeChainMonitor,  # noqa: F401
    TXID, P2WPKH_ADDRS, FAKE_SCRIPTPUBKEY, _fake_txout,
)
from src.log_tokens import tokens, SessionTokens


# ---------------------------------------------------------------------------
# Static grep: catch the future regression at the diff stage
# ---------------------------------------------------------------------------


class TestStaticLogPatternGuards:
    """Fail if anyone reintroduces a log call that interpolates a
    privacy-sensitive identifier directly, or that uses ``exc_info=True``
    in a handler error path."""

    SRC_DIR = Path(__file__).parent.parent / "src"

    # File-level allowlist for exc_info usage. None today — keep this
    # empty unless there's a non-user-data path that genuinely needs the
    # traceback (in which case add the file with a comment justifying).
    EXC_INFO_ALLOWED_FILES: set = set()

    # The token helpers (tokens.p, tokens.l, etc) are how to log these
    # values safely; anything else is suspect.
    # The token helpers (tokens.p, tokens.l, tokens.tx, ...) are how to log
    # these safely; a bare variable of one of these names is a leak. mix_id is
    # intentionally NOT here — the operator may log it plainly — but txid and
    # psbt must never hit a log raw (txid is a public on-chain handle, psbt hex
    # carries every address). Matches a bare `, name` / `{name`, not substrings
    # like local_txid / psbt_hex.
    BAD_VAR_NAMES = (
        "npub", "npub_hex", "sender_hex", "pubkey_hex",
        "lud16", "lightning_addr",
        "scriptpubkey", "address",
        "txid", "psbt",
    )

    def test_no_raw_identifier_in_logger_call(self):
        """Grep for `logger.<level>(...)` calls whose argument list
        interpolates one of the identifying variable names. This is a
        coarse check — it'll false-positive if you happen to name a
        helper local ``npub`` and pass it through ``tokens.p()`` on the
        same line. In practice the codebase doesn't, and the noise
        floor is fine for a regression guard."""
        # Match a logger call where the body contains "{<bad>" or "%s" + same name.
        bad_substr_patterns = [
            re.compile(rf"\{{{name}\b"      ) for name in self.BAD_VAR_NAMES
        ]
        # Also catch the positional pattern: logger.X("... %s ...", npub_hex, ...)
        # We're more permissive here — only flag if the var name appears
        # as a bare argument (preceded by comma+space).
        bad_positional = [
            re.compile(rf",\s*{name}\b(?![A-Za-z_0-9])") for name in self.BAD_VAR_NAMES
        ]

        violations = []
        for path in sorted(self.SRC_DIR.glob("*.py")):
            if path.name == "log_tokens.py":
                continue
            text = path.read_text()
            # Walk logger.* call sites (single-line in this codebase;
            # we don't have multi-line logger calls today, but if that
            # changes the grep needs to widen).
            in_call = False
            call_start_line = 0
            call_buf: list = []
            for line_no, line in enumerate(text.splitlines(), 1):
                if in_call:
                    call_buf.append(line)
                    if ")" in line:
                        joined = " ".join(call_buf)
                        self._check_call(
                            path.name, call_start_line, joined,
                            bad_substr_patterns, bad_positional, violations,
                        )
                        in_call = False
                        call_buf = []
                    continue
                if re.match(r"\s*logger\.[a-z]+\(", line):
                    if line.rstrip().endswith(")") and line.count("(") == line.count(")"):
                        # Single-line call.
                        self._check_call(
                            path.name, line_no, line,
                            bad_substr_patterns, bad_positional, violations,
                        )
                    else:
                        in_call = True
                        call_start_line = line_no
                        call_buf = [line]
        assert not violations, (
            "Privacy-unsafe logger calls found:\n  "
            + "\n  ".join(violations)
        )

    @staticmethod
    def _check_call(filename, line_no, call_text, substr_patterns, positional_patterns, violations):
        for pat in substr_patterns:
            if pat.search(call_text):
                violations.append(f"{filename}:{line_no} f-string-interpolates: {call_text.strip()[:120]}")
                return
        for pat in positional_patterns:
            if pat.search(call_text):
                violations.append(f"{filename}:{line_no} positional-arg: {call_text.strip()[:120]}")
                return

    def test_no_exc_info_true_in_src(self):
        """``exc_info=True`` dumps the call's frame locals into the log,
        which routinely include UTXOs / addresses / PSBT hex. Banned in
        production code; use ``type(e).__name__`` instead."""
        violations = []
        for path in sorted(self.SRC_DIR.glob("*.py")):
            if path.name in self.EXC_INFO_ALLOWED_FILES:
                continue
            for line_no, line in enumerate(path.read_text().splitlines(), 1):
                stripped = line.strip()
                # Skip pure-comment lines and docstring continuations
                # (false positives if the rule is mentioned in docs).
                if stripped.startswith("#"):
                    continue
                if re.search(r"exc_info\s*=\s*True", line):
                    violations.append(f"{path.name}:{line_no} {stripped[:120]}")
        assert not violations, (
            "exc_info=True in production code (banned — leaks frame locals):\n  "
            + "\n  ".join(violations)
        )


# ---------------------------------------------------------------------------
# Runtime: capture logs from real flows and assert no leak
# ---------------------------------------------------------------------------


def _assert_no_secrets_in(records, *, secrets_present, label):
    """Assert that none of ``secrets_present`` (raw strings) appears in
    the message body of any record. We check the rendered message text
    (formatted args resolved) — that's what actually lands in log files."""
    blob = "\n".join(r.getMessage() for r in records)
    for s in secrets_present:
        assert s not in blob, (
            f"[{label}] raw '{s[:24]}...' leaked into log output:\n{blob}"
        )


class TestSessionTokens:
    """Sanity-check the token helper itself before the runtime tests rely on it."""

    def test_tokens_stable_within_session(self):
        t = SessionTokens()
        a = t.p("abc")
        b = t.p("abc")
        assert a == b

    def test_tokens_differ_per_input(self):
        t = SessionTokens()
        assert t.p("abc") != t.p("def")

    def test_tokens_differ_per_process(self):
        a = SessionTokens().p("npub_hex_xyz")
        b = SessionTokens().p("npub_hex_xyz")
        assert a != b, "fresh SessionTokens should not collide — salt is random per instance"

    def test_tokens_do_not_contain_input(self):
        t = SessionTokens()
        npub = "deadbeefdeadbeefdeadbeefdeadbeef"
        token = t.p(npub)
        assert npub not in token
        # Also reject any suffix of the input as per the user's stated bar.
        for n in range(4, len(npub) + 1):
            assert npub[-n:] not in token, f"token contains tail suffix len={n}"

    def test_empty_input_yields_sentinel(self):
        t = SessionTokens()
        assert t.p("") == "p#?"


class TestRuntimeLogsHaveNoLeaks:
    """Drive representative flows and assert no raw npub / lud16 / UTXO
    txid / address ends up in the captured log output."""

    @pytest.mark.asyncio
    async def test_dm_handler_error_does_not_leak_npub_or_args(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            # Force an exception inside a DM handler. The handler error
            # path used to emit `logger.error(f"... {npub_hex}: {e}", exc_info=True)`
            # which leaks both the npub AND the exception args.
            async def boom(ctx):
                raise RuntimeError("boom UTXO=cafef00dcafe addr=bc1qsecretdeadbeef")
            coord._cmd_list_mixes = boom

            npub = "abcd" * 16  # 64-char hex npub
            with caplog.at_level(logging.ERROR, logger="src.coordinator"):
                await coord._on_dm(FakeCtx(npub), "/list")

            _assert_no_secrets_in(
                caplog.records,
                secrets_present=[
                    npub,
                    "cafef00dcafe",
                    "bc1qsecretdeadbeef",
                ],
                label="dm_handler_error",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unmatched_zap_does_not_log_raw_npub(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            class Zap:
                sender_hex = "deadbeef" * 8
                amount_sats = 4242
            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._on_zap(Zap(), FakeCtx(Zap.sender_hex))
            _assert_no_secrets_in(
                caplog.records,
                secrets_present=["deadbeef" * 8],
                label="unmatched_zap",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_overpayment_log_does_not_leak_npub_or_mix_id_text(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            secret_npub = "feed" * 16
            mix_id = await db.create_mix(
                output_size=1_000_000, fee_per_element=100,
            )
            await db.update_mix(mix_id, state="collecting")
            pid = await db.add_participant(mix_id, secret_npub, "")
            await db.add_utxo(pid, TXID[0], 0, 2_000_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.add_output(pid, P2WPKH_ADDRS[0], 1_000_000)
            await db.update_participant(pid, state="committed")

            class Zap:
                sender_hex = secret_npub
                amount_sats = 9999

            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._on_zap(Zap(), FakeCtx(secret_npub))

            _assert_no_secrets_in(
                caplog.records,
                # Both the raw npub AND the raw mix_id (since the latter
                # is what binds to txid in the broadcast log).
                secrets_present=[secret_npub, mix_id],
                label="overpayment",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_broadcast_sweep_logs_do_not_pair_mix_id_with_txid(self, caplog):
        """The cardinal sin: a log line that names both the bot's
        internal mix_id and the public on-chain txid. Anyone reading the
        log file then maps mix membership (from other lines) to the
        public coinjoin."""
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            real_txid = "abc123txid" + "0" * 54  # something obviously distinct
            mix_id = await db.create_mix(
                output_size=100_000,
            )
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid=real_txid,
                broadcast_tx_hex="cafef00d" * 8,
            )
            chain.confirmed[real_txid] = False  # forces the rebroadcast log

            await db.set_setting("last_broadcast_check_unix", "0")
            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._broadcast_sweep(int(time.time()))

            # Per-line check: no single record may contain BOTH the
            # mix_id and the txid. That's the pairing that breaks
            # anonymity.
            for r in caplog.records:
                msg = r.getMessage()
                if mix_id in msg and real_txid in msg:
                    pytest.fail(
                        f"log line pairs mix_id and txid (anonymity break):\n{msg}"
                    )
            # And per the broader rule: no raw mix_id in any line at all,
            # since downstream logs about participants might mention it.
            _assert_no_secrets_in(
                caplog.records, secrets_present=[mix_id], label="broadcast_sweep_unconfirmed",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_broadcast_confirmed_log_does_not_pair_mix_id_with_txid(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            real_txid = "ffaabbcctxidconfirmed" + "0" * 43
            mix_id = await db.create_mix(
                output_size=100_000,
            )
            await db.update_mix(
                mix_id, state="broadcast",
                broadcast_txid=real_txid,
                broadcast_tx_hex="abcd" * 16,
            )
            chain.confirmed[real_txid] = True

            await db.set_setting("last_broadcast_check_unix", "0")
            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._broadcast_sweep(int(time.time()))

            for r in caplog.records:
                msg = r.getMessage()
                if mix_id in msg and real_txid in msg:
                    pytest.fail(
                        f"log line pairs mix_id and txid (anonymity break):\n{msg}"
                    )
            _assert_no_secrets_in(
                caplog.records, secrets_present=[mix_id, real_txid],
                label="broadcast_sweep_confirmed",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_ghost_log_does_not_leak_npub_or_mix_id(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            ghoster_npub = "9999" * 16
            mix_id = await db.create_mix(output_size=1_000_000)
            await db.update_mix(mix_id, state="signing")
            deadline = coord.cfg.SIGNING_DEADLINE_HOURS * 3600
            past = int(time.time()) - (deadline + 120)

            ghost_pid = await db.add_participant(mix_id, ghoster_npub, "")
            await db.add_utxo(ghost_pid, TXID[0], 0, 500_000, "p2wpkh", FAKE_SCRIPTPUBKEY)
            await db.update_participant(ghost_pid, state="signing", psbt_sent_at_unix=past)
            # Survivor so ghost recovery doesn't auto-cancel.
            surv = await db.add_participant(mix_id, "survivor_npub", "")
            await db.update_participant(surv, state="signing",
                                        psbt_sent_at_unix=int(time.time()) - 60)

            mix_row = await db.get_mix(mix_id)
            active = await db.get_participants_by_mix(mix_id)
            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._handle_signing(mix_row, active, int(time.time()))

            _assert_no_secrets_in(
                caplog.records,
                secrets_present=[ghoster_npub, "survivor_npub", mix_id],
                label="ghost_detection",
            )
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_lightning_refund_failure_does_not_leak_lud16(self, caplog):
        from src.lightning_handler import LightningHandler

        class _NoOpCfg:
            BTCPAY_URL = BTCPAY_STORE = BTCPAY_API_KEY = ""

        class FakePayer:
            async def pay(self, lud16, amount_sats, **_):
                raise RuntimeError("BTCPay refused with secret_payload deadbeefdeadbeef")

        h = LightningHandler(_NoOpCfg())
        h._payer = FakePayer()

        lud16 = "alice@secret.example.com"
        with caplog.at_level(logging.ERROR, logger="src.lightning_handler"):
            await h.send_refund(lud16, 1000, reason="test_reason")

        _assert_no_secrets_in(
            caplog.records,
            # raw lud16 AND the secret_payload from the exception text
            secrets_present=[lud16, "deadbeefdeadbeef"],
            label="lightning_refund_failure",
        )

    @pytest.mark.asyncio
    async def test_stale_chunk_cleanup_does_not_leak_key(self, caplog):
        coord, db, nostr, chain, lightning = await make_coord()
        try:
            secret_npub = "1111" * 16
            secret_mix = "secret_mix_id_42"
            key = f"{secret_npub}:{secret_mix}"
            # Plant a stale chunk record (started a long time ago).
            coord._psbt_chunks[key] = {
                "chunks": {1: "deadbeef"}, "total": 2,
                "started": time.time() - coord.STALE_CHUNK_TIMEOUT - 10,
            }
            with caplog.at_level(logging.INFO, logger="src.coordinator"):
                await coord._tick()
            _assert_no_secrets_in(
                caplog.records,
                secrets_present=[secret_npub, secret_mix, key],
                label="stale_chunk_cleanup",
            )
        finally:
            await db.close()
