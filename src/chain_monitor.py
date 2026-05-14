"""Chain Monitor — mempool.space wrapper for UTXO lookup, fee estimation, broadcast."""

import httpx
from typing import Optional, Dict, List, Tuple

# Default API base
_DEFAULT_API = "https://mempool.space/api"


class ChainMonitor:
    """Bitcoin blockchain monitor via mempool.space API."""

    def __init__(self, api_base: str = _DEFAULT_API, min_fee_rate: float = 1.5,
                 max_fee_rate: float = 510, fee_multiplier: float = 1.5):
        self._api_base = api_base.rstrip("/")
        self._min_fee_rate = min_fee_rate
        self._max_fee_rate = max_fee_rate
        self._fee_multiplier = fee_multiplier
        self._client = httpx.Client(timeout=30)

    def close(self):
        self._client.close()

    # --- UTXO Lookup ---

    def lookup_txout(self, txid: str, vout: int) -> Optional[Dict]:
        """Look up a prevout by txid:vout.

        Returns dict with keys: txid, vout, status (confirmed|mempool), value (sats),
        scriptpubkey, scriptpubkey_type, address, or None if not found.
        """
        try:
            r = self._client.get(f"{self._api_base}/tx/{txid}/spend/{vout}")
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, httpx.RequestError) as e:
            # Fallback: try the tx endpoint and parse outputs
            try:
                r = self._client.get(f"{self._api_base}/tx/{txid}")
                r.raise_for_status()
                tx_data = r.json()
                if "vout" in tx_data and vout < len(tx_data["vout"]):
                    prevout = tx_data["vout"][vout]
                    return {
                        "txid": txid,
                        "vout": vout,
                        "status": tx_data.get("status", {}).get("confirmed", False),
                        "value": prevout.get("value", 0),
                        "scriptpubkey": prevout.get("scriptpubkey", ""),
                        "scriptpubkey_type": prevout.get("scriptpubkeytype", "p2wpkh"),
                        "address": prevout.get("scriptpubkey_address", ""),
                    }
                return None
            except Exception as e2:
                return None

    def lookup_tx(self, txid: str) -> Optional[Dict]:
        """Get full transaction data."""
        try:
            r = self._client.get(f"{self._api_base}/tx/{txid}")
            r.raise_for_status()
            return r.json()
        except Exception:
            return None

    # --- Fee Estimation ---

    def estimate_fee_rate(self) -> float:
        """Fetch the lowest fee rate confirmed in each of the last 4 blocks, average them.

        Returns the rate * FEE_MULTIPLIER, clamped to [MIN_FEE_RATE, MAX_FEE_RATE].
        """
        try:
            r = self._client.get(f"{self._api_base}/v1/fees/mempool-blocks")
            r.raise_for_status()
            data = r.json()

            # mempool-blocks returns an array of block summaries with feeRanges
            # We want the min fee rate from recent blocks
            rates = []
            for block in data[:4]:  # last 4 blocks
                if "feeRange" in block and len(block["feeRange"]) > 0:
                    rates.append(block["feeRange"][0])  # lowest fee in block
            if not rates:
                # Fallback to /v1/fees/recommended
                r2 = self._client.get(f"{self._api_base}/v1/fees/recommended")
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

    def broadcast_tx(self, tx_hex: str) -> Optional[str]:
        """Submit a raw transaction to mempool.space.

        Returns the txid string on success, None on failure.
        """
        try:
            r = self._client.post(
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

    def is_confirmed(self, txid: str) -> bool:
        """Check if a txid is confirmed (has at least 1 confirmation)."""
        try:
            r = self._client.get(f"{self._api_base}/tx/{txid}/status")
            r.raise_for_status()
            data = r.json()
            return data.get("confirmed", False)
        except Exception:
            return False

    # --- Convenience: vsize estimation ---

    @staticmethod
    def estimate_vsize(num_inputs: int, num_outputs: int,
                       input_vsize: int = 68, output_vsize: int = 31,
                       overhead: int = 10) -> int:
        """Estimate transaction vsize."""
        return overhead + (num_inputs * input_vsize) + (num_outputs * output_vsize)

    # --- Re-broadcast ---

    def re_broadcast(self, tx_hex: str) -> Optional[str]:
        """Re-broadcast a transaction (same as broadcast_tx)."""
        return self.broadcast_tx(tx_hex)
