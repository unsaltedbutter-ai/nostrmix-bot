"""Chain Monitor — mempool.space wrapper for UTXO lookup, fee estimation, broadcast.

All networking uses httpx.AsyncClient so it never blocks the asyncio event loop.
"""
import httpx
from typing import Optional, Dict, List

_DEFAULT_API = "https://mempool.space/api"

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
                 max_fee_rate: float = 510, fee_multiplier: float = 1.5):
        self._api_base = api_base.rstrip("/")
        self._min_fee_rate = min_fee_rate
        self._max_fee_rate = max_fee_rate
        self._fee_multiplier = fee_multiplier
        self._client = httpx.AsyncClient(timeout=30)

    async def close(self):
        await self._client.aclose()

    # --- UTXO Lookup ---

    async def lookup_txout(self, txid: str, vout: int) -> Optional[Dict]:
        """Look up a prevout by txid:vout.

        Returns dict with: txid, vout, status (parent-tx confirmed bool),
        value (sats), scriptpubkey (hex), scriptpubkey_type (normalized to the
        project's vocab — 'p2wpkh', 'p2tr', etc.), address. None if not found.

        This does NOT tell you whether the output is unspent — use is_utxo_spent
        for that.
        """
        try:
            r = await self._client.get(f"{self._api_base}/tx/{txid}")
            r.raise_for_status()
            tx_data = r.json()
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
        except Exception:
            return None

    async def is_utxo_spent(self, txid: str, vout: int) -> bool:
        """Check whether a specific output (txid:vout) has been spent on-chain.

        Uses Esplora's /tx/:txid/outspend/:vout. Returns True if the output has
        been spent, False if unspent OR if the API call failed.

        Fail-open: an API outage looks like 'unspent'. The downstream broadcast
        would still fail if the UTXO were actually spent, so the realistic risk
        is wasting a participant's service fee.
        """
        try:
            r = await self._client.get(
                f"{self._api_base}/tx/{txid}/outspend/{vout}"
            )
            r.raise_for_status()
            data = r.json()
            return bool(data.get("spent", False))
        except Exception:
            return False

    async def lookup_tx(self, txid: str) -> Optional[Dict]:
        """Get full transaction data."""
        try:
            r = await self._client.get(f"{self._api_base}/tx/{txid}")
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    # --- Fee Estimation ---

    async def estimate_fee_rate(self) -> float:
        """Fetch the lowest fee rate confirmed in each of the last 4 blocks, average them.

        Returns the rate * FEE_MULTIPLIER, clamped to [MIN_FEE_RATE, MAX_FEE_RATE].
        """
        try:
            r = await self._client.get(f"{self._api_base}/v1/fees/mempool-blocks")
            r.raise_for_status()
            data = r.json()

            rates = []
            for block in data[:4]:  # last 4 blocks
                if "feeRange" in block and len(block["feeRange"]) > 0:
                    rates.append(block["feeRange"][0])  # lowest fee in block
            if not rates:
                # Fallback to /v1/fees/recommended
                r2 = await self._client.get(f"{self._api_base}/v1/fees/recommended")
                r2.raise_for_status()
                rec = r2.json()
                rates = [rec.get("minimumFee", 30)]

            avg = sum(rates) / max(len(rates), 1)
            # Clamp
            final = max(avg * self._fee_multiplier, self._min_fee_rate)
            if self._max_fee_rate > 0:
                final = min(final, self._max_fee_rate)
            return final

        except Exception:
            # Return conservative default
            return max(self._min_fee_rate, 30)

    # --- Broadcast ---

    async def broadcast_tx(self, tx_hex: str) -> Optional[str]:
        """Submit a raw transaction to mempool.space.

        Returns the txid string on success, None on failure.
        """
        try:
            r = await self._client.post(
                f"{self._api_base}/tx",
                data=tx_hex,
                headers={"Content-Type": "text/plain"},
            )
            if r.status_code == 200:
                return r.text.strip()
            else:
                return None
        except Exception:
            return None

    # --- Confirmation Check ---

    async def is_confirmed(self, txid: str) -> bool:
        """Check if a txid is confirmed (has at least 1 confirmation)."""
        try:
            r = await self._client.get(f"{self._api_base}/tx/{txid}/status")
            r.raise_for_status()
            data = r.json()
            return data.get("confirmed", False)
        except Exception:
            return False

    # --- Re-broadcast ---

    async def re_broadcast(self, tx_hex: str) -> Optional[str]:
        """Re-broadcast a transaction (same as broadcast_tx)."""
        return await self.broadcast_tx(tx_hex)
