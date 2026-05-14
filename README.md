# nostrmix-bot

PSBT-based Bitcoin coinjoin bot that operates over Nostr NIP-17 DMs, accepts fees
via Lightning zaps, and assembles equal-output mixing transactions from multiple
participants.

## Quick Start

```bash
cd ~/Documents/nostrmix-bot
source venv/bin/activate
cp nostrmix-bot.env nostrmix-bot.env.local   # edit with your keys
python src/main.py
```

## Prerequisites

- Python 3.12+ (uses Homebrew on macOS)
- Nostr keypair (nsec) for the bot
- LNURL provider pubkey for zap validation
- BTCPay Server (optional, for refunds)

## Install

```bash
cd ~/Documents/nostrmix-bot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Copy and edit `nostrmix-bot.env`:

| Variable | Description |
|---|---|
| `NOSTR_PRIVATE_KEY_NPUB` | Bot's nsec key |
| `NOSTR_RELAYS` | Comma-separated relay URLs |
| `BOT_LUD16` | Lightning address for receiving zaps |
| `ZAP_PROVIDER_PUBKEY_HEX` | LNURL provider's Nostr pubkey |
| `BTCPAY_URL` | BTCPay server URL for refunds |
| `MEMPOOL_API` | Mempool.space API base URL |

See the env file for all available options.

## Architecture

```
Participant (Nostr DM)
       ↓
NostrHandler (NIP-17 DMs, NIP-57 zaps)
       ↓
CommandParser (/list, /join, /commit, /addresses, /psbt_accept)
       ↓
Coordinator (state machine, event loop)
       ↓
Database <-> PSBTManager <-> FeeEngine <-> ChainMonitor
                       ↘                ↗
                     LightningHandler (refunds)
```

### State Machine

Mixes progress: `announced → collecting → assembling → signing → broadcast → completed`
Cancellation can occur at any point with refunds (minus REFUND_KEEP_PERCENT).

## Testing

```bash
cd ~/Documents/nostrmix-bot
source venv/bin/activate
python -m pytest tests/ -v
```

## Protocol Commands

All commands use `/` prefix. The bot responds with structured text:

- `/list` — List open mixes
- `/join <mix_name>` — Join a mix
- `/commit <txid:vout> ...` — Register UTXOs
- `/addresses <addr1> <addr2> ...` — Provide output addresses
- `/psbt_accept <hex>` — Return signed PSBT
- `/cancel [mix_name]` — Exit a mix

## License

MIT
