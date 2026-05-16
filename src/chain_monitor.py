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
                 api_backup: Optional[str] = _DEFAULT_BACKUP_API):
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

    async def is_utxo_spent(self, txid: str, vout: int) -> bool:
        """Check whether a specific output (txid:vout) has been spent on-chain.

        Uses Esplora's /tx/:txid/outspend/:vout. Returns True if the output has
        been spent, False if unspent OR if the API call failed.

        Fail-open: an API outage looks like 'unspent'. The downstream broadcast
        would still fail if the UTXO were actually spent, so the realistic risk
        is wasting a participant's service fee.
        """
        data = await self._get_json(f"/tx/{txid}/outspend/{vout}")
        if data is None:
            return False
        return bool(data.get("spent", False))

    async def lookup_tx(self, txid: str) -> Optional[Dict]:
        """Get full transaction data."""
        return await self._get_json(f"/tx/{txid}")

    # --- Fee Estimation ---

    async def estimate_fee_rate(self) -> float:
        """Fetch the lowest fee rate confirmed in each of the last 4 blocks, average them.

        Returns the rate * FEE_MULTIPLIER, clamped to [MIN_FEE_RATE, MAX_FEE_RATE].
        """
        try:
            data = await self._get_json("/v1/fees/mempool-blocks")
            rates = []
            if data:
                for block in data[:4]:  # last 4 blocks
                    if "feeRange" in block and len(block["feeRange"]) > 0:
                        rates.append(block["feeRange"][0])  # lowest fee in block
            if not rates:
                rec = await self._get_json("/v1/fees/recommended")
                if rec:
                    rates = [rec.get("minimumFee", 30)]
            if not rates:
                return max(self._min_fee_rate, 30)

            avg = sum(rates) / max(len(rates), 1)
            final = max(avg * self._fee_multiplier, self._min_fee_rate)
            if self._max_fee_rate > 0:
                final = min(final, self._max_fee_rate)
            return final
        except Exception:
            return max(self._min_fee_rate, 30)

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

    # --- Re-broadcast ---

    async def re_broadcast(self, tx_hex: str) -> Optional[str]:
        """Re-broadcast a transaction (same as broadcast_tx)."""
        return await self.broadcast_tx(tx_hex)
