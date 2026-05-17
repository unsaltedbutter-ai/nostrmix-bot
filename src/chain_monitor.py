"""Chain Monitor — mempool.space wrapper for UTXO lookup, fee estimation, broadcast.

All networking uses httpx.AsyncClient so it never blocks the asyncio event loop.
"""
import httpx
from typing import Optional, Dict, List, Any

_DEFAULT_API = "https://mempool.space/api"
_DEFAULT_BACKUP_API = "https://blockstream.info/api"  # API-compatible Esplora mirror

# Esplora reports segwit/taproot with version prefixes. Map to the bare names
# used elsewhere in the project (config.py vsize maps, vsize.py defaults).
_SCRIPT_TYPE_NORMALIZE = {
    "v0_p2wpkh": "p2wpkh",
    "v0_p2wsh": "p2wsh",
    "v1_p2tr": "p2tr",
}


def _normalize_script_type(raw: str) -> str:
    if not raw:
        return "p2wpkh"
    return _SCRIPT_TYPE_NORMALIZE.get(raw, raw)


class ChainMonitor:
    """Bitcoin blockchain monitor via mempool.space API — fully async."""

    def __init__(self, api_base: str = _DEFAULT_API, min_fee_rate: float = 1.5,
                 max_fee_rate: float = 510, fee_multiplier: float = 1.5,
                 api_backup: Optional[str] = _DEFAULT_BACKUP_API,
                 fee_lookback_blocks: int = 6):
        self._api_base = api_base.rstrip("/")
        self._api_backup = api_backup.rstrip("/") if api_backup else None
        # Endpoints tried in order. The primary is always first; the backup is
        # only consulted on a network/HTTP failure of the primary.
        self._endpoints: List[str] = [self._api_base]
        if self._api_backup and self._api_backup != self._api_base:
            self._endpoints.append(self._api_backup)
        self._min_fee_rate = min_fee_rate
        self._max_fee_rate = max_fee_rate
        self._fee_multiplier = fee_multiplier
        # How many recently-confirmed blocks to inspect when estimating the
        # "minimum-to-confirm-within-an-hour" rate. ~6 blocks ≈ 1 hour.
        self._fee_lookback_blocks = max(1, fee_lookback_blocks)
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._client.aclose()

    # --- UTXO Lookup ---

    async def _get_json(self, path: str) -> Optional[Any]:
        """GET <endpoint>/<path> as JSON, falling back through self._endpoints."""
        last_exc: Optional[Exception] = None
        for base in self._endpoints:
            try:
                r = await self._client.get(f"{base}{path}")
                r.raise_for_status()
                return r.json()
            except Exception as e:
                last_exc = e
                continue
        return None

    async def lookup_txout(self, txid: str, vout: int) -> Optional[Dict]:
        """Look up a prevout by txid:vout.

        Returns dict with: txid, vout, status (parent-tx confirmed bool),
        value (sats), scriptpubkey (hex), scriptpubkey_type (normalized to the
        project's vocab — 'p2wpkh', 'p2tr', etc.), address. None if not found.

        This does NOT tell you whether the output is unspent — use is_utxo_spent
        for that.
        """
        tx_data = await self._get_json(f"/tx/{txid}")
        if tx_data is None:
            return None
        if "vout" not in tx_data or vout >= len(tx_data["vout"]):
            return None
        prevout = tx_data["vout"][vout]
        return {
            "txid": txid,
            "vout": vout,
            "status": tx_data.get("status", {}).get("confirmed", False),
            "value": prevout.get("value", 0),
            "scriptpubkey": prevout.get("scriptpubkey", ""),
            "scriptpubkey_type": _normalize_script_type(
                prevout.get("scriptpubkey_type", "")
            ),
            "address": prevout.get("scriptpubkey_address", ""),
        }

    async def is_utxo_spent(self, txid: str, vout: int) -> Optional[bool]:
        """Check whether a specific output (txid:vout) has been spent on-chain.

        Uses Esplora's /tx/:txid/outspend/:vout.

        Returns:
          - True  → output has been spent.
          - False → output is confirmed unspent.
          - None  → API failed on both endpoints; the caller cannot tell.

        S-B: previously this was fail-open (None → False), which meant a
        user could /commit a stale UTXO during an API outage, get charged
        the LN service fee, and then lose 5% of it on the inevitable
        broadcast-time cancel. Now returns None on failure; the coordinator
        rejects the commit with "couldn't verify, try later".

        Negative vouts and other malformed inputs that produce a 404 still
        return False (the endpoint exists and answered).
        """
        last_was_error = False
        for base in self._endpoints:
            try:
                r = await self._client.get(f"{base}/tx/{txid}/outspend/{vout}")
                if r.status_code == 200:
                    return bool(r.json().get("spent", False))
                if r.status_code == 404:
                    # Endpoint answered cleanly that the outpoint doesn't
                    # exist — treat as unspent (downstream lookup_txout
                    # check will reject anyway if the parent tx is missing).
                    return False
                last_was_error = True
            except Exception:
                last_was_error = True
                continue
        return None if last_was_error else False

    async def lookup_tx(self, txid: str) -> Optional[Dict]:
        """Get full transaction data."""
        return await self._get_json(f"/tx/{txid}")

    # --- Fee Estimation ---

    async def _recent_block_min_feerates(self) -> List[float]:
        """Return the min-sat/vB feeRange[0] for each of the last
        `fee_lookback_blocks` confirmed blocks.

        Uses mempool.space's /v1/blocks endpoint which exposes per-block
        extras.feeRange = [min, p10, p25, p50, p75, p90, max]. The min is
        the lowest fee rate that successfully confirmed in that block —
        i.e. the price of admission on a block-by-block basis.

        Returns [] if the endpoint isn't available or returns no usable
        blocks. The caller must decide what to do in that case.
        """
        blocks = await self._get_json("/v1/blocks")
        if not isinstance(blocks, list):
            return []
        rates: List[float] = []
        for blk in blocks[: self._fee_lookback_blocks]:
            extras = blk.get("extras") or {}
            feerange = extras.get("feeRange")
            if isinstance(feerange, list) and feerange:
                try:
                    rates.append(float(feerange[0]))
                except (TypeError, ValueError):
                    continue
        return rates

    async def estimate_fee_rate(self) -> float:
        """Estimate the sat/vB rate that will probably confirm within an hour.

        Strategy: take the MAX of the per-block minimum-confirmed feerates
        across the last `fee_lookback_blocks` (default 6 ≈ 1 hour). Paying
        that rate would have admitted us to the worst-case block in the
        recent window, so it should clear within a similar window going
        forward. Multiply by FEE_MULTIPLIER for a safety buffer, clamp to
        [MIN_FEE_RATE, MAX_FEE_RATE].

        Why MAX over the min-per-block (not average): we want
        "probably-confirms-within-N-blocks", not "would-have-confirmed-on-
        average". The max-of-mins gives the price of admission to the
        tightest block in the lookback — paying that rate makes any block
        in that window admit us.

        Fallbacks if /v1/blocks isn't usable:
          1. /v1/fees/recommended → hourFee (the API's own "probably within
             an hour" estimate). Still multiplied by FEE_MULTIPLIER.
          2. /v1/fees/mempool-blocks → min of the next-block projection
             (legacy behaviour, kept as last resort).
          3. Clamp floor as the absolute backstop.
        """
        try:
            rates = await self._recent_block_min_feerates()
            base_rate: Optional[float] = None
            if rates:
                base_rate = max(rates)
            else:
                # Fallback 1: API's own hourFee recommendation.
                rec = await self._get_json("/v1/fees/recommended")
                if rec and "hourFee" in rec:
                    try:
                        base_rate = float(rec["hourFee"])
                    except (TypeError, ValueError):
                        base_rate = None
                if base_rate is None:
                    # Fallback 2: legacy projected-block min.
                    data = await self._get_json("/v1/fees/mempool-blocks")
                    proj_rates: List[float] = []
                    if data:
                        for block in data[:4]:
                            fr = block.get("feeRange")
                            if isinstance(fr, list) and fr:
                                try:
                                    proj_rates.append(float(fr[0]))
                                except (TypeError, ValueError):
                                    continue
                    if proj_rates:
                        base_rate = max(proj_rates)

            if base_rate is None:
                # Final fallback: the clamp floor. Better to overpay slightly
                # and have the tx confirm than to undercollect.
                return max(self._min_fee_rate, 1.0)

            final = max(base_rate * self._fee_multiplier, self._min_fee_rate)
            if self._max_fee_rate > 0:
                final = min(final, self._max_fee_rate)
            return final
        except Exception:
            return max(self._min_fee_rate, 1.0)

    # --- Broadcast ---

    async def broadcast_tx(self, tx_hex: str) -> Optional[str]:
        """Submit a raw transaction to mempool.space.

        Returns the txid string on success, None on failure. Tries the backup
        endpoint if the primary fails (network error or non-2xx).

        Semantics by HTTP response:
          - 200: success. Use the body if present, otherwise compute the txid
                 locally from the raw hex.
          - 409: 'already in mempool' or 'mempool conflict'. If it's OUR tx
                 (which we can verify by comparing the local txid against any
                 subsequent confirm check), treat as success. Returning None
                 here would let the caller refund participants while the tx
                 is sitting in mempool — money at risk.
          - 4xx (other) / 5xx: actual rejection or transient server error.
                 Try the backup endpoint; on full exhaustion, return None.
        """
        local_txid: Optional[str] = None
        try:
            from bitcointx.core import CTransaction, b2x
            tx = CTransaction.deserialize(bytes.fromhex(tx_hex))
            # bitcoin txids are little-endian internally but displayed as
            # big-endian (reverse the bytes).
            local_txid = b2x(tx.GetTxid()[::-1])
        except Exception:
            # If we can't even parse our own tx, bail — broadcast won't work.
            return None

        rejecting_4xx = False
        for base in self._endpoints:
            try:
                r = await self._client.post(
                    f"{base}/tx",
                    content=tx_hex,
                    headers={"Content-Type": "text/plain"},
                )
                if r.status_code == 200:
                    body = r.text.strip()
                    return body or local_txid
                if r.status_code == 409:
                    # Already in mempool. Either ours or a conflict; downstream
                    # is_confirmed will tell us which on the next sweep.
                    return local_txid
                if r.status_code == 429:
                    # Rate limited. Not a hard rejection — try the backup.
                    continue
                if 400 <= r.status_code < 500:
                    # Hard rejection from the API (bad format, missing inputs).
                    # Both mirrors will agree — no point trying the backup.
                    rejecting_4xx = True
                    break
                # 5xx → try the next endpoint.
            except Exception:
                continue
        return None

    # --- Confirmation Check ---

    async def is_confirmed(self, txid: str) -> bool:
        """Check if a txid is confirmed (has at least 1 confirmation)."""
        data = await self._get_json(f"/tx/{txid}/status")
        if data is None:
            return False
        return bool(data.get("confirmed", False))

    async def tx_known(self, txid: str) -> Optional[bool]:
        """Does any chain endpoint know this txid (mempool or confirmed)?

        Used by the coordinator BEFORE refunding on a broadcast_tx None.
        A primary 5xx + backup 5xx could still mean the primary actually
        accepted the tx and is just slow to respond — refunding then would
        double-pay (refund the LN fee while the on-chain coinjoin confirms).

        Returns:
          - True  → at least one endpoint reports the tx exists.
          - False → both endpoints returned 404 / "not found".
          - None  → both endpoints failed to answer (we have no idea). The
                    caller MUST treat None as "do not refund" — i.e., park
                    the mix in broadcast state and check again later.
        """
        if not txid:
            return False
        last_was_error = False
        for base in self._endpoints:
            try:
                r = await self._client.get(f"{base}/tx/{txid}")
                if r.status_code == 200:
                    return True
                if r.status_code == 404:
                    last_was_error = False
                    continue
                # 5xx or other → endpoint failed. Try next, remember we
                # didn't get a clean answer.
                last_was_error = True
            except Exception:
                last_was_error = True
                continue
        # If every endpoint either 404'd or errored: distinguish "all
        # responded 404" (False) from "any endpoint failed" (None).
        return None if last_was_error else False

    # --- Re-broadcast ---

    async def re_broadcast(self, tx_hex: str) -> Optional[str]:
        """Re-broadcast a transaction (same as broadcast_tx)."""
        return await self.broadcast_tx(tx_hex)
