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
(`max_conforming_utxos`). The miner fee is computed from the **actual** number of
conforming UTXOs present at assembly (the cap only bounds intake) and split evenly
across the non-conforming participants, so the effective rate hits the target.

### Getting the most privacy

The equal `output_size` outputs are the anonymity set — identical and unlinkable.
**Change is the weak point** of any coinjoin: change amounts are unique, so a chain
observer can often re-link a change output to the inputs that funded it. The bot's
fee split doesn't change that (it's a minor signal at most). To actually maximise
privacy:

- **Bring conforming amounts** (exact multiples of `output_size`) — they have **no
  change**, pass through 1→1, and are free. This is the strongest lever.
- **Re-mix your change** in a later round — toxic change becomes a fresh input.
- **Never co-spend** change together with your mixed outputs in a later transaction.

### Data retention

The bot keeps participant data (npub, Lightning address, UTXOs, addresses, PSBTs)
only while it's needed: a mix's data is destroyed (with SQLite `secure_delete`)
within minutes of the coinjoin confirming, a whole-mix cancel destroys it after
refunds, and a participant who **cancels or is dropped** has their row deleted the
moment their refund settles — immediately when no fee was paid. The only rows that
outlive a departure are: a minimal `refunds_owed` record (Lightning address + sats,
no npub, no mix link) when a refund failed, the participant row itself in the rare
case sats are owed but there's no Lightning address to pay them to, and `blacklist`
entries for ghosters. Logs are tokenised (no raw npubs/txids/addresses), and HTTP
client loggers are capped so request URLs never write txids to disk.

Copies the bot doesn't hold are covered too: every outbound DM carries a NIP-40
expiration (`DM_EXPIRY_HOURS`, 7 days by default) on the gift wrap as well as the
message inside it, so the relay storing it and the recipient's client drop it at
the same moment rather than only one of them knowing when it dies. The shared
timestamp is anchored slightly in the past so it can't be read backwards to
recover when the DM was sent.

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
- `libsecp256k1` (system library — see Install). **Required at runtime**: the bot
  cryptographically verifies each participant's returned signature, which is an EC
  operation. (Also used by the test suite's real ECDSA signing.)
- A Nostr secret key (nsec) for the bot
- A zap-provider Nostr pubkey, **only if** you charge a service fee
- BTCPay Server, **only if** you charge a service fee (to send refunds)

## Install

```bash
# 1. System dep — libsecp256k1 (required at runtime for signature verification).
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
| `DM_EXPIRY_HOURS` | `168` | int > 0 (clamped up, see note) | NIP-40 lifetime of the bot's outbound DMs, carried on the gift wrap **and** the message inside it so relay and client expire it together. The SDK anchors the shared tag up to ¼ of the window in the past (capped at 2 days) so `expiration − window` can't reveal the send time, so the **guaranteed** lifetime is ¾ of this — 126h at the default. The loader raises the value if that floor wouldn't clear `SIGNING_DEADLINE_HOURS` (a PSBT expiring before its deadline would ghost an honest participant) and logs a warning when it does. |

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
| `FEE_PER_ELEMENT` | `0` | int ≥ 0 (negative → 0) | Service-fee zap, in sats, per **non-conforming** element (input + used output). **0 disables zaps entirely**: no payment is requested and participants go straight to `paid` after `addresses`. Conforming UTXOs are always free. |
| `FEE_MULTIPLIER` | `1.25` | float > 0 | Headroom multiplier applied to the estimated sat/vB miner-fee rate (the fee is locked at signing and can't be bumped). |
| `MIN_FEE_RATE_SATS` | `1.5` | float > 0 | Floor for the miner fee-rate clamp, and the floor used by the pre-broadcast sum-invariant ("tx must pay at least this"). |
| `MAX_FEE_RATE_SATS` | `510` | float; `0` = no ceiling | Ceiling for the miner fee-rate clamp. Set `0` to disable the ceiling. |
| `FEE_LOOKBACK_BLOCKS` | `6` | int ≥ 1 (clamped) | How many recently-confirmed blocks the fee estimator inspects (≈10 min/block, so 6 ≈ 1 h). The rate is the MEDIAN of those blocks' per-block minimum feerates — robust to a single anomalous block. |
| `REFUND_KEEP_PERCENT` | `5` | int 0–100 | Percentage of a paid fee the bot keeps on refund (covers LN routing). |
| `REFUND_KEEP_MIN_SATS` | `50` | int ≥ 0 | Minimum sats kept on refund (whichever of percent/min returns more to the user is used). |

### Mix parameters

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `DEFAULT_OUTPUT_SIZE` | `1000000` | int ≥ `MINIMUM_UTXO_SIZE` (else load fails) | Equal-output size (sats) of auto-created mixes. Also the conforming/non-conforming dividing line. |
| `MAX_PARTICIPANTS_DEFAULT` | `20` | int > 0 | Upper bound used by the capacity check when pasted inputs auto-create a mix. |
| `MAX_PENDING_MIXES` | `5` | int ≥ 1 | Max simultaneous **paid** mixes a single npub may be in. A 4th/Nth `join` is refused. |
| `MAX_OPEN_MIXES` | `10` | int ≥ 1 (clamped up to 1) | Cap on simultaneously-open mixes (state `announced`/`collecting`). Gates **every** new-mix creation path — `join <amount>` and the pasted-inputs auto-create. At the cap, creation is refused and the user is pointed at `list`. (The daily auto-create only fires when zero mixes are open, so it never hits this.) |
| `SIGNING_DEADLINE_HOURS` | `48` | int > 0 | Time participants have to return a signed PSBT. Reminder DMs fire at ⅛, ¼, ½ of this; past it, the participant is ghosted + blacklisted. |
| `PAY_DEADLINE_HOURS` | `12` | int > 0 | Time a `committed` participant has to pay (when a fee is set). Only the per-participant pay timeout — the collecting/fill window is now `FILL_DEADLINE_HOURS`. |
| `EMPTY_MIX_EXPIRY_HOURS` | `168` | int > 0 | How long a mix with **zero** participants stays open before it's retired (held by age since creation). Long so a morning-announced mix is still joinable that night / for days. |
| `FILL_DEADLINE_HOURS` | `168` | int > 0 | Once a mix **has** participants but hasn't reached its non-conforming target, how long it keeps collecting before it cancels + refunds (freeing committed UTXOs). Refreshed each time a new participant joins. Long by default because gathering is slow on a small bot; shorten as traffic grows. Also the ghost-recovery deadline extension. |
| `MAX_GHOST_RETRIES` | `3` | int ≥ 0 | How many times a mix restarts collecting after a ghost before it cancels and refunds everyone. |
| `MINIMUM_UTXO_SIZE` | `10000` | int > 0 | Dust threshold. Below this, a change/leftover is folded into the miner fee instead of becoming an output; UTXOs smaller than this are rejected at input intake. |

### Conforming / non-conforming model

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `DEFAULT_REQUIRED_NONCONFORMING` | `3` | int ≥ 1 (clamped up to 1) | Exact number of non-conforming participants an auto-created mix waits for before assembling. Also the even-split denominator for the conforming miner-fee burden. |
| `MAX_CONFORMING_UTXOS` | `10` | int ≥ 0 | Max conforming UTXOs a mix absorbs (bounds intake during collecting). The miner fee is sized from the **actual** conforming present at assembly, not this cap. |
| `MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT` | `10` | int ≥ 1 | Cap on non-conforming UTXOs one participant may commit. |
| `DONATION_ADDRESS` | `""` | bitcoin address; **recommended blank** | Where an above-dust leftover goes **only** when a non-conforming participant supplies a *single* address (no room for change). With ≥2 addresses the leftover becomes the participant's own oversized change instead, so this rarely fires. **Poor-privacy feature — recommended to leave blank**, in which case the leftover folds into the miner fee (most private). A fixed address recurring across coinjoins is a linkable on-chain fingerprint; only set it if you accept that cost to keep those sats. |

### Scheduling & broadcast

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `ANNOUNCEMENT_HOUR_UTC` | `14` | int 0–23 (clamped) | UTC hour the daily "open mixes" note is posted (auto-creating a default mix if none are open). |
| `BROADCAST_CHECK_INTERVAL_MINUTES` | `5` | int > 0 | How often the sweep re-checks broadcast txs for confirmation (and re-pushes unconfirmed ones). Short on purpose: participant data is destroyed the moment the tx confirms, so this bounds how long the linkage data outlives the public coinjoin. Skipped entirely when no broadcast is pending. |

### Script-type allowlist

| Key | Default | Type / range | Influences |
|---|---|---|---|
| `ACCEPTED_INPUT_TYPES` | `p2wpkh` | comma-separated; empty → `p2wpkh` | UTXO script types accepted at input intake. Each mix additionally locks to the type of its first accepted input. |
| `ACCEPTED_OUTPUT_TYPES` | `p2wpkh` | comma-separated; empty → `p2wpkh` | Address types accepted at address intake. Each mix locks to the type of its first accepted address. |

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
CommandParser (list, join, inputs, addresses, psbt_accept, cancel — verbs optional)
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

> **Using the bot as a participant?** See the [**User's Guide**](docs/USER_GUIDE.md)
> for the full walkthrough — joining, pasting inputs and addresses, verifying the
> PSBT before you sign, toxic change, and re-mixing.

**Mostly, participants just paste.** A `txid:vout` list becomes the sender's mix
**inputs**, bitcoin addresses become their payout **outputs**, and signed PSBT
hex is taken as their **signature** — each recognized by shape, no verb needed.
Items may be separated by spaces, commas, or new lines, and one message may
carry both inputs and addresses. The verbs below all still work (they're what
`help` shows); commands are matched case-insensitively and the leading `/` is
optional — `join 0.01` and `/join 0.01` both work.

- `list` (or `open`, `mixes`) — list open mixes. If none are open, the bot opens
  a default one (`DEFAULT_OUTPUT_SIZE` / `DEFAULT_REQUIRED_NONCONFORMING`) and lists
  it, so there's always something to join.
- `help` (or `commands`, `?`) — show the commands relevant to your current stage
  (e.g. `inputs` while gathering, `psbt_accept` while signing). Every command
  still works regardless of what's listed; this only tunes the guidance.
- `join <mix_name>` — join a mix by name; or `join <amount>` (e.g. `join 0.01`)
  to join an open mix of that BTC output size, or create one if none exists
- `<txid:vout> ...` (or `inputs <txid:vout> ...`; aliases `input`, `commit`) —
  register UTXOs. Sent more than once, the new outpoints are **added** to your
  set.
- `<addr1> <addr2> ...` (or `addresses <addr1> ...`; aliases `address`,
  `outputs`) — provide payout addresses, one or more per message; they
  **accumulate** until you've sent enough (the bot replies with a running tally,
  e.g. `2 of 3 address(es) on file`). You need one per conforming UTXO;
  non-conforming participants need ≥1 more for an equal output, plus one more to
  receive change. The address count caps your outputs: if you supply too few,
  the bot turns your last address into an (oversized) change output rather than
  burning the leftover — so you get fewer mixed outputs but keep the sats. Only
  with a single address and an above-dust leftover is that excess donated/folded.
- `addresses clear` — wipe your accumulated/stored addresses and start the list
  over (the fix for a mis-pasted address). Addresses lock once your mix starts
  assembling, or once you've paid a service fee quoted against them.
- `70736274ff...` (or `psbt_accept <hex>`) — return a signed PSBT.
  `psbt_chunk <i>/<n> <hex>` returns it in pieces when it exceeds the DM size
  threshold (the one command that needs its verb).
- `cancel [mix_name]` — exit a mix (auto-detects when you're in exactly one)

## Testing

```bash
cd ~/Documents/nostrmix-bot
source venv/bin/activate

python -m pytest -q                 # full suite (includes live mempool.space tests)
python -m pytest -m "not live" -q   # hermetic offline lane (no network)
```

Tests that hit the live mempool.space API are marked `@pytest.mark.live`.

## Local validation (before launching the bot)

Two scripts let you validate the deployment in layers without launching the full
bot or risking funds. Launching `src/main.py` itself touches no funds — it
connects and waits; a transaction is only built/broadcast once participants
register input UTXOs and a mix reaches its non-conforming target.

**`scripts/preflight.py`** — config sanity + connectivity. Loads the same config
`main.py` uses (printing only non-secret fields), confirms mempool.space is
reachable with a live fee estimate, and TCP-checks each relay. Run it on the
deploy host:

```bash
python scripts/preflight.py            # uses the configured nostrmix-bot.env
```

**`scripts/psbt_dryrun.py`** — preview the exact coinjoin the bot would assemble,
offline and without broadcasting. It runs your mix spec through the real
`Coordinator._assemble_psbt` (temp DB, stub Nostr/Lightning), printing each
participant's fee share + change, the transaction's outputs and miner fee, the
privacy-check result, and the unsigned PSBT (importable into a wallet to inspect):

```bash
python scripts/psbt_dryrun.py scripts/example-mix.json
```

The example is fully offline (synthetic inputs + fixed fee rate). Swap the
`utxos` for real `"txid:vout"` strings and drop `fee_rate` to look up real inputs
on-chain and produce a real, importable PSBT. See the file headers for the spec
format. Neither script broadcasts or sends a DM.

Suggested order before a live coinjoin: `pytest -m "not live"` → `preflight.py` →
`psbt_dryrun.py` → launch + DM `list` → a controlled self-test with your own
identities and small-value UTXOs.

## License

MIT
