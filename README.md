# nostrmix-bot

A PSBT-based Bitcoin coinjoin bot that operates over Nostr NIP-17 DMs. Participants
discover open mixes via the bot's daily announcement, join by DM, optionally pay a
Lightning service fee (off by default), and co-sign a single equal-output mixing
transaction.

Two kinds of input are accepted, classified automatically by amount against the
mix's `output_size`:

- **Conforming** (`amount == output_size`) — moved 1-input → 1-output to a fresh
  address. Pays **no** service fee and **no** miner fee. Pure pass-through that
  grows everyone's anonymity set.
- **Non-conforming** (`amount != output_size`) — carved into equal `output_size`
  outputs plus change. Its owner pays the miner fee (and the service fee, if one
  is configured).

A mix is sized by an **exact number of non-conforming participants** it waits for
(`required_nonconforming`) plus a **cap on conforming UTXOs** it will absorb
(`max_conforming_utxos`). The miner fee is computed assuming the conforming cap is
full and split evenly across the non-conforming participants, so each one's share
is deterministic.

## Quick Start

```bash
cd ~/Documents/nostrmix-bot
source venv/bin/activate

# Create the config file from the template (see Configuration below). The real
# file is git-ignored and holds your bot's secret key — never commit it.
cp nostrmix-bot.env.example nostrmix-bot.env
$EDITOR nostrmix-bot.env

python src/main.py
```

The bot looks for `nostrmix-bot.env` in the current directory, the parent
directory, then alongside `src/` (see `BotConfig.find_env_path`). Any key you omit
falls back to the default in the tables below.

## Prerequisites

- Python 3.12+
- `libsecp256k1` (system library — see Install; required by the test suite's real
  ECDSA signing)
- A Nostr secret key (nsec) for the bot
- A zap-provider Nostr pubkey, **only if** you charge a service fee
- BTCPay Server, **only if** you charge a service fee (to send refunds)

## Install

```bash
# 1. System dep — libsecp256k1.
brew install secp256k1                  # macOS
# or: sudo apt install libsecp256k1-dev # debian/ubuntu

# 2. Python deps
cd ~/Documents/nostrmix-bot
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Configuration

All settings live in `nostrmix-bot.env` (`KEY=value`, one per line). Values are
read as strings, whitespace-trimmed, and coerced to the type of their default
(int/float/string). The file is git-ignored because it contains secrets.

### How to read the tables

- **Default** is what you get if the key is absent.
- **Type / range** is the accepted form; out-of-range handling is noted where the
  loader clamps or rejects.
- Keys with an empty default and **(required / conditional)** must be set for the
  relevant feature to work.

### Bot identity

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `NOSTR_PRIVATE_KEY_NPUB` | `""` | nsec string **(required)** | The bot's Nostr **secret key**. Despite the `_NPUB` suffix this is the *private* key — it's passed to the SDK as `nsec`. The bot's identity and signer. |
| `NOSTR_RELAYS` | `wss://relay.damus.com,wss://nos.lol` | comma-separated wss URLs | Relays the bot connects to for DMs, zaps, and announcements. |
| `BOT_NAME` | `butterbot` | string | kind-0 profile name. |
| `BOT_ABOUT` | `I help bitcoiners mix…` | string | kind-0 profile about. |
| `BOT_LUD16` | `""` | lightning address | Advertised in DMs as the zap destination when a service fee is charged. |
| `BOT_PICTURE` | `""` | URL | kind-0 profile picture. |
| `BOT_NIP05` | `""` | nip05 id | kind-0 profile NIP-05. |
| `BOT_WEBSITE` | `""` | URL | kind-0 profile website. |

### Zap receiving (service fee)

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `ZAP_PROVIDER_PUBKEY_HEX` | `""` | 64-char hex pubkey **(conditional)** | The zap provider's Nostr pubkey, used to validate incoming zap receipts. Required only if `FEE_PER_ELEMENT > 0`. |

### Refunds (BTCPay) — only needed if you charge a service fee

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `BTCPAY_URL` | `""` | URL **(conditional)** | BTCPay Server base URL used to send Lightning refunds when a paid mix cancels. |
| `BTCPAY_STORE` | `""` | store id **(conditional)** | BTCPay store id. |
| `BTCPAY_API_KEY` | `""` | secret **(conditional)** | BTCPay API key. **Secret — keep it out of version control.** |

### Fees

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `FEE_PER_ELEMENT` | `0` | int ≥ 0 (negative → 0) | Service-fee zap, in sats, per **non-conforming** element (input + used output). **0 disables zaps entirely**: no payment is requested and participants go straight to `paid` after `/addresses`. Conforming UTXOs are always free. |
| `FEE_MULTIPLIER` | `1.5` | float > 0 | Safety multiplier applied to the estimated sat/vB miner-fee rate. |
| `MIN_FEE_RATE_SATS` | `1.5` | float > 0 | Floor for the miner fee-rate clamp, and the floor used by the pre-broadcast sum-invariant ("tx must pay at least this"). |
| `MAX_FEE_RATE_SATS` | `510` | float; `0` = no ceiling | Ceiling for the miner fee-rate clamp. Set `0` to disable the ceiling. |
| `FEE_LOOKBACK_BLOCKS` | `6` | int ≥ 1 (clamped) | How many recently-confirmed blocks the fee estimator inspects (≈10 min/block, so 6 ≈ 1 h). Larger = smoother/safer, pricier in calm mempools. |
| `REFUND_KEEP_PERCENT` | `5` | int 0–100 | Percentage of a paid fee the bot keeps on refund (covers LN routing). |
| `REFUND_KEEP_MIN_SATS` | `50` | int ≥ 0 | Minimum sats kept on refund (whichever of percent/min returns more to the user is used). |

### Mix parameters

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `DEFAULT_OUTPUT_SIZE` | `1000000` | int ≥ `MINIMUM_UTXO_SIZE` (else load fails) | Equal-output size (sats) of auto-created mixes. Also the conforming/non-conforming dividing line. |
| `MIN_PARTICIPANTS_DEFAULT` | `3` | int ≥ 2 (clamped up to 2) | Legacy minimum-participant floor. Proceed decisions now key off `required_nonconforming`; this is retained for compatibility. |
| `MAX_PARTICIPANTS_DEFAULT` | `20` | int > 0 | Upper bound used by the auto-mix-on-`/commit` capacity check. |
| `MAX_PENDING_MIXES` | `5` | int ≥ 1 | Max simultaneous **paid** mixes a single npub may be in. A 4th/Nth `/join` is refused. |
| `SIGNING_DEADLINE_HOURS` | `48` | int > 0 | Time participants have to return a signed PSBT. Reminder DMs fire at ⅛, ¼, ½ of this; past it, the participant is ghosted + blacklisted. |
| `PAY_DEADLINE_HOURS` | `12` | int > 0 | Time a `committed` participant has to pay (when a fee is set); also the collecting deadline and the ghost-recovery deadline extension. |
| `MAX_GHOST_RETRIES` | `3` | int ≥ 0 | How many times a mix restarts collecting after a ghost before it cancels and refunds everyone. |
| `MINIMUM_UTXO_SIZE` | `10000` | int > 0 | Dust threshold. Below this, a change/leftover is folded into the miner fee instead of becoming an output; UTXOs smaller than this are rejected at `/commit`. |
| `DEFAULT_MIX_USER_COUNT` | `3` | int | Seeds the (now largely vestigial) `min_participants` column of auto-created mixes. Proceed/cancel logic uses `DEFAULT_REQUIRED_NONCONFORMING` instead. |

### Conforming / non-conforming model

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `DEFAULT_REQUIRED_NONCONFORMING` | `3` | int ≥ 1 (clamped up to 1) | Exact number of non-conforming participants an auto-created mix waits for before assembling. Also the even-split denominator for the conforming miner-fee burden. |
| `MAX_CONFORMING_UTXOS` | `10` | int ≥ 0 | Max conforming UTXOs a mix absorbs. The miner fee is computed **as if** this many are present (deterministic); under-fill just pays a slightly higher effective rate. |
| `MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT` | `10` | int ≥ 1 | Cap on non-conforming UTXOs one participant may commit. |
| `DONATION_ADDRESS` | `""` | bitcoin address; **recommended blank** | Where an above-dust leftover goes when a non-conforming participant supplies no change address (they're warned first). **Poor-privacy feature — recommended to leave blank**, in which case the leftover folds into the miner fee (most private). A fixed address recurring across coinjoins is a linkable on-chain fingerprint; only set it if you accept that cost to keep those sats. |

### Scheduling & broadcast

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `ANNOUNCEMENT_HOUR_UTC` | `14` | int 0–23 (clamped) | UTC hour the daily "open mixes" note is posted (auto-creating a default mix if none are open). |
| `BROADCAST_CHECK_INTERVAL_HOURS` | `24` | int > 0 | How often the sweep re-checks broadcast txs for confirmation and re-pushes unconfirmed ones. |

### Script-type allowlist

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `ACCEPTED_INPUT_TYPES` | `p2wpkh` | comma-separated; empty → `p2wpkh` | UTXO script types accepted at `/commit`. Each mix additionally locks to the type of its first commit. |
| `ACCEPTED_OUTPUT_TYPES` | `p2wpkh` | comma-separated; empty → `p2wpkh` | Output address types accepted at `/addresses`. Each mix locks to the type of its first `/addresses`. |

Recognized type tokens: `p2pkh`, `p2sh`, `p2sh-p2wpkh`, `p2wpkh`, `p2wsh`, `p2tr`.
The MVP defaults to `p2wpkh` only.

### Bitcoin API

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `MEMPOOL_API` | `https://mempool.space/api` | Esplora-compatible base URL | Primary endpoint for UTXO lookup, fee estimation, broadcast, and confirmation. |
| `MEMPOOL_API_BACKUP` | `https://blockstream.info/api` | URL; blank = disabled | Fallback tried when the primary 5xx's / times out / rate-limits. |

### Database

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `DB_PATH` | `./bot.db` | filesystem path | SQLite database location (WAL mode). |

### Per-script-type vbyte sizes (advanced)

These calibrate the fee/vsize estimator and rarely need changing. All are integers
(vbytes). Defaults are calibrated against real mainnet transactions and rounded up
a few vbytes for buffer.

| Key | Default | Key | Default |
|---|---|---|---|
| `P2PKH_INPUT_VSIZE` | `150` | `P2PKH_OUTPUT_VSIZE` | `35` |
| `P2SH_INPUT_VSIZE` | `135` | `P2SH_OUTPUT_VSIZE` | `35` |
| `P2SH_P2WPKH_INPUT_VSIZE` | `95` | `P2SH_P2WPKH_OUTPUT_VSIZE` | `35` |
| `P2WPKH_INPUT_VSIZE` | `70` | `P2WPKH_OUTPUT_VSIZE` | `35` |
| `P2WSH_INPUT_VSIZE` | `100` | `P2WSH_OUTPUT_VSIZE` | `45` |
| `P2TR_INPUT_VSIZE` | `60` | `P2TR_OUTPUT_VSIZE` | `45` |
| `TX_OVERHEAD_VSIZE` | `10` | | |

> **Security:** `nostrmix-bot.env` holds your bot's nsec and (if used) your BTCPay
> API key. It is git-ignored — never commit it, and rotate any key that leaks.

## Architecture

```
Participant (Nostr DM)
       ↓
NostrHandler (NIP-17 DMs, NIP-57 zaps, daily announcements)
       ↓
CommandParser (/list, /join, /commit, /addresses, /psbt_accept, /cancel)
       ↓
Coordinator (state machine, event loop)
       ↓
Database <-> PSBTManager <-> FeeEngine <-> ChainMonitor
                       ↘                 ↗
                     LightningHandler (refunds)
```

### State machine

Mixes progress: `announced → collecting → assembling → signing → broadcast → completed`.
Cancellation can occur at any point, refunding paid participants (minus
`REFUND_KEEP_PERCENT` / `REFUND_KEEP_MIN_SATS`). Participant states:
`interested → committed → paid → signing → signed → broadcast`, with
`ghosted`/`cancelled`/`refunding`/`refunded`/`refund_failed` branches.

A mix advances to `assembling` as soon as it has `required_nonconforming`
non-conforming participants — it does not wait for conforming UTXOs, **except** a
solo (`required_nonconforming == 1`) mix, which needs ≥1 conforming UTXO so there
are ≥2 equal outputs from distinct parties.

### Privacy bar

The privacy check is a non-authoritative sanity guard: a mix must produce **at
least 2 equal-size outputs from at least 2 inputs** (non-conforming + conforming
combined) — concretely `max(2, required_nonconforming)` identical outputs. That
breaks the 1:1 coin↔owner link. Stronger anonymity is the user's choice via
additional mixing rounds; the bot does not attempt subset-sum analysis.

## Protocol commands

Commands are matched case-insensitively; the `/` prefix is optional for `list`.

- `/list` (or `open`, `mixes`) — list open mixes
- `/join <mix_name>` — join a mix
- `/commit <txid:vout> ...` — register UTXOs (may be sent more than once)
- `/addresses <addr1> <addr2> ...` — provide output addresses (one per conforming
  UTXO; non-conforming participants need ≥1 more for an equal output, and one more
  for change or the above-dust leftover is donated)
- `/psbt_accept <hex>` — return a signed PSBT (or `/psbt_chunk <i>/<n> <hex>` for
  large PSBTs)
- `/cancel [mix_name]` — exit a mix (auto-detects when you're in exactly one)

## Testing

```bash
cd ~/Documents/nostrmix-bot
source venv/bin/activate

python -m pytest -q                 # full suite (includes live mempool.space tests)
python -m pytest -m "not live" -q   # hermetic offline lane (no network)
```

Tests that hit the live mempool.space API are marked `@pytest.mark.live`.

## License

MIT
