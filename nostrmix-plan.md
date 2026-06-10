# Plan: nostrbot — Bitcoin Coinjoin Mixer

## Overview

A PSBT-based coinjoin bot that operates over Nostr NIP-17 DMs, accepts fees via Lightning zaps, and assembles equal-output mixing transactions from multiple participants. Participants discover open mixes via the bot's daily Nostr announcements, join via DM, and co-sign a single on-chain transaction.


Some basics:
- nostrbot-sdk is located at https://github.com/unsaltedbutter-ai/nostrbot-sdk
- - the nostrbot-sdk will have everything we need to listening to relays, receiving & sending NIP-17, receiving & validating zaps, sending refunds over lightning, publishing a daily "mixes available" listing post.
- we have a BTCPay server at pay.unsaltedbutter.ai It is fully operational and this coding project does not need to consider it in any way beyond knowing how to use the url/store/apikey to issue refunds.
- - we can create NIP-05 & LUD16 for the bot, and fill in with an env file later
- keep everything inside of ~/Documents/nostrmix-bot
- any venv should be named "venv" not ".venv"
- any env files should regular files, not dot-prefixed files
- our goal is to build this all. it can be done in phases, if you can define the phases and track the work
- for the python nostr client we want to use nostrbot-sdk which has nostr-sdk as a dependency (https://pypi.org/project/nostr-sdk/)
- we need unit tests for all aspects. Do what it takes to make sure the code is testable.
- the README.md at https://raw.githubusercontent.com/unsaltedbutter-ai/nostrbot-sdk/refs/heads/main/README.md should give you lots of features of the nostr bot
- payment of miner fee: the bot will assemble the transaction out of all participant's inputs, will allocate BTC to the outputs equally, and will use left over funds to pay the miner fee.
- The bitcoin miner fee is calculated based upon looking at recent blockchain activity, applying a multiple, such as 1.5x which is controlled by an env variable, and this amount is deducted from participants outputs in equal amounts so that outputs remain equal and the miner fee is paid.
- The on-chain need not be "known later." it can be precalculated based upon current/recent blockchain activity and adding a multiplier to give a buffer. If the blockchain becomes significantly more busy & expensive to get a confirmation it is acceptable for the join to take longer to become confirmed.
- Paying a service fee (a zap to the bot) is **optional and off by default**. `FEE_PER_ELEMENT` defaults to `0`; at 0 the bot requests no zap, skips the `paid`/pay-deadline gate, and tells the user they're all set. When `FEE_PER_ELEMENT > 0` there is one zap (per participant) required, charged only on the participant's **non-conforming** inputs and their derived outputs (see "Conforming vs non-conforming UTXOs" below).
- **Conforming vs non-conforming UTXOs** (see §3i): a UTXO whose amount equals the mix's `output_size` is *conforming* — it is moved 1-input→1-output unchanged, costs no service fee and no miner fee. Any other UTXO is *non-conforming* — it is carved into equal `output_size` outputs + change, and its owner pays the miner fee. A mix is sized by an exact target number of *non-conforming participants* and a cap on the number of *conforming UTXOs* it will absorb.
- all inputs must be of the same key type (such as p2wpkh). The cost per input/output should come from env variables so it can be raised or lowered if the example table (below) proves to be incorrect.
- all outputs must be of the same key type as the inputs (such as p2wpkh).
- let's us setup the database so that the same npub participant can be a party to more than one pending mix. This could result in the participant being asked to sign more than one PSBT at a time. We will need to match their reply to us against the n number of mixes they belong to. This should be a relatively small number. We can cap them to 5 simultaneous mixes, but let's use an env variable for the maximum allowed.
- in logging when printing npubs, print the bech32-encoded format, not the hex, for operator readability.
- The user decides for themselves how many outputs they want. They indicate the number by how many output addresses they send to us.
- When the bot has no mixes open, it should make one using DEFAULT_OUTPUT_SIZE and DEFAULT_REQUIRED_NONCONFORMING

---

## 1. Dependencies

```
nostrbot-sdk                    # user's wrapper around nostr-sdk (NIP-17, NIP-57, relays)
python-bitcointx                # PSBT building, validation, combination (maintained fork of python-bitcoinlib with first-class BIP-174 support)
httpx                           # mempool.space API calls (UTXO lookup, fee estimates, broadcast)
aiosqlite                       # async SQLite for state storage
python-dotenv                   # env config
```

---

## 2. Database Schema (SQLite)

File: `schema.sql`

```sql
CREATE TABLE mixes (
    id              TEXT PRIMARY KEY,          -- east-gate
    output_size     INTEGER NOT NULL,          -- sats (1_000_000 = 0.01 BTC); a UTXO of exactly this size is "conforming"
    max_participants INTEGER,
    -- Conforming/non-conforming model:
    --   required_nonconforming = exact number of non-conforming participants the
    --     mix waits for before assembling (also the fee-split denominator).
    --   max_conforming_utxos   = cap on conforming UTXOs the mix absorbs
    --     (bounds intake during collecting). The miner fee is sized from the
    --     ACTUAL conforming present at assembly, split evenly across the
    --     non-conforming participants.
    required_nonconforming INTEGER NOT NULL DEFAULT 3,
    max_conforming_utxos INTEGER NOT NULL DEFAULT 10,
    fee_rate        INTEGER DEFAULT 30,        -- sats/vbyte, set at assembly
    fee_per_element INTEGER DEFAULT 0,         -- sats, service-fee zap per NON-conforming element; 0 = no zap
    state           TEXT NOT NULL DEFAULT 'announced',
    -- announced | collecting | assembling | signing | broadcast | completed | cancelled
    deadline_unix   INTEGER,
    broadcast_txid  TEXT,
    broadcast_tx_hex TEXT,                     -- raw signed tx hex; kept so the broadcast sweep can re-push if it falls out of mempool
    input_type      TEXT,                      -- locked at first /commit; all subsequent commits must match
    output_type     TEXT,                      -- locked at first /addresses; all subsequent must match
    ghost_retries   INTEGER DEFAULT 0,         -- how many times this round has restarted due to ghosting
    max_ghost_retries INTEGER DEFAULT 3,       -- after this, cancel and refund everyone
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL
);

CREATE TABLE participants (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    npub_hex        TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'interested',
    -- interested | committed | paid | signing | signed | ghosted | broadcast | refunded
    fee_paid        INTEGER,                    -- sats zap received
    fee_share       INTEGER,                    -- on-chain fee share (calculated)
    change_amount   INTEGER,                    -- change output sats, 0 = no change
    lightning_addr  TEXT,                       -- from Nostr profile kind 0, for refunds
    psbt_sent_at_unix INTEGER,                 -- when bot sent the PSBT to this participant
    reminder_count  INTEGER DEFAULT 0,          -- how many ping DMs sent
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL
);

CREATE TABLE utxos (
    id              TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    txid            TEXT NOT NULL,
    vout            INTEGER NOT NULL,
    amount          INTEGER NOT NULL,           -- sats
    script_type     TEXT,                        -- p2wpkh | p2tr | p2pkh
    is_used         BOOLEAN DEFAULT 0,           -- prevent double-spend across mixes
    created_at_unix INTEGER NOT NULL
);

CREATE TABLE outputs (
    id              TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    address         TEXT NOT NULL,
    amount          INTEGER NOT NULL,           -- sats
    is_change       BOOLEAN DEFAULT 0,
    created_at_unix INTEGER NOT NULL
);

CREATE TABLE psbt_rounds (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    round_num       INTEGER DEFAULT 1,          -- ghost recovery bumps this; derived as mix.ghost_retries + 1 at assembly time
    psbt_sent_at_unix INTEGER,
    psbt_sent       TEXT,                        -- hex of skeleton PSBT
    psbt_returned   TEXT,                        -- hex of signed PSBT
    psbt_returned_at_unix INTEGER,
    psbt_valid      BOOLEAN,
    input_indices   TEXT,                        -- JSON list of vin indices the participant must sign; used by validate_returned's strict per-input check
    created_at_unix INTEGER,
    updated_at_unix INTEGER,
    UNIQUE(mix_id, participant_id, round_num)
);

CREATE TABLE blacklist (
    id              TEXT PRIMARY KEY,
    npub_hex        TEXT,                       -- ghosted participant pubkey
    utxo_txid_vout  TEXT,                       -- txid:vout string
    reason          TEXT DEFAULT 'ghosting',
    created_at_unix INTEGER NOT NULL
);

CREATE TABLE announcements (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    event_id        TEXT,                        -- Nostr event ID of the posted note
    posted_at_unix  INTEGER NOT NULL
);
```

You have authority to add indices on tables & columns as apprporitate such as the foreign keys.

Ghosting starts a new psbt_round. A mix only allows for <MAX_GHOST_RETRIES> rounds.


---

## 3. Component Specs

### 3a. Nostr DM Handler (File: `nostr_handler.py`)

Uses `nostrbot-sdk` for all transport. Handles:

- **Incoming DMs**: parse commands and route to coordinator
  - `/list`, `/join <mix_id> <num_outputs>`, `/commit <txid:vout> ...`, `/addresses <addr> ...`, `/psbt_accept <hex>`, `/cancel`
- **Outgoing DMs**: send structured messages to participants
- **Zap monitoring**: listen for kind 9735 (zap receipt) events on relays; match sender npub + amount to pending participant
- **Profile lookup**: fetch kind 0 for lightning address (refund path)
- **Daily announcements**: once per day at `ANNOUNCEMENT_HOUR_UTC` (default 14:00 UTC), post a single combined kind 1 note listing all currently-open mixes. If no mixes are open, auto-create one with `DEFAULT_OUTPUT_SIZE` / `DEFAULT_REQUIRED_NONCONFORMING`.
- **Ghosting pings**: graduated DMs at 1/8 of signing time, 1/4 of signing time, 1/2 of signing time after PSBT sent

Command protocol — rigid, no NL parsing required. The bot responds with structured text:

```
/list →
  "mix_abc: 0.01 BTC outputs, 3/5 participants, 12h deadline"
  "mix_def: 0.05 BTC outputs, 1/3 participants, 24h deadline"

/join mix_abc 4 →
  "Registered. Provide UTXOs with /commit <txid:vout> ..."

/commit 123abc:0 456def:1 →
  "3 UTXOs registered, total 0.0543 BTC.
   Provide 4 output addresses with /addresses <addr1> ..."

/addresses bc1q... bc1q... bc1q... bc1q... →
  "4 outputs @ 0.01 each.
   Pay 500 sats (1 input + 4 outputs × 100) as zap to ${lnurl}.
   On mix timeout, I'll return 95% (minus the REFUND_KEEP_PERCENT)."
```

### 3b. Lightning Handler (File: `lightning_handler.py`)

- **Receive zaps**: no special handling needed — the LNURL-pay endpoint generates invoices, the payer pays, the bot records the zap receipt (kind 9735) via Nostr. The zap_provider_pubkey_hex will be included in the env file. 
- **Send refunds**: on mix cancellation, send 95% of participant's fee back to their LNURL (derived from kind 0 profile). Keeps 5% to cover Lightning routing fees.
- **Balance check**: ensure the bot has sufficient outbound capacity for refunds before accepting new participants

No invoice generation is required — the bot registers their LUD16 which tells the user where to send a zap. The Nostr zap protocol handles the rest.

### 3c. Chain Monitor (File: `chain_monitor.py`)

Uses `mempool.space` API (configurable).

- **UTXO lookup**: fetch prevout for each txid:vout → returns amount (sats) + script type
- **Fee estimation**: fetch the lowest fee rate confirmed in each of the last 4 blocks, average them, clamp to `MIN_FEE_RATE` (1.5 sats/vbyte from env)
- **Broadcast**: submit final transaction hex via POST to mempool.space API → returns txid
- **Confirmation checking**: The bot checks for confirmation once per day. If the mix has confirmed, all information is fully destroyed/removed from the datagbase. No trace (besides blacklisting) remains.

### 3d. PSBT Manager (File: `psbt_manager.py`)

Uses `python-bitcoinlib`.

- **Build skeleton PSBT**: given all inputs (txid, vout, amount, script) and all outputs (address, amount), produce a valid PSBT
- **Validate returned PSBT**: compare against the original skeleton, allowing signatures from the participant. Reject if:
  - Output addresses changed
  - Output amounts changed  
  - Inputs removed or added
  - Signature count doesn't match participant's input count
- **Combine PSBTs**: extract partial signatures from all validated returned PSBTs and assemble a single final PSBT with every input signed
- **Finalize**: extract the raw transaction hex for broadcast
- **Vsize estimation**: given input count, output count, and script types, estimate vsize for fee calculation:
  ```
  total_vsize = 10 + (num_inputs × 68) + (num_outputs × 31)  # p2wpkh defaults
  ```

### 3e. Fee Engine (File: `fee_engine.py`)

Two-tier fee model. **Both tiers fall only on non-conforming inputs/outputs**; conforming UTXOs (amount == `output_size`) are free 1→1 pass-throughs and pay neither tier.

**Tier 1 — Service fee (Lightning zap) — OPTIONAL, off by default**
`FEE_PER_ELEMENT` defaults to `0`. When 0, no zap is requested: the participant goes straight from `committed` to `paid` after `/addresses` and is told there's no fee. When `> 0`:
Charged at commitment: `fee_per_element × (non_conforming_inputs + non_conforming_used_outputs)`. Known upfront, paid via zap.
- we tell the user to send us input information (txids & vouts) and output addresses and we calculate the amount and tell the user to zap us ### sats.
- we record their information (npub and inputs/output) and await a zap of at least the correct amount
- when the zap arrives, we know who sent it (npub) and we compare the amount to what we were waiting for and mark the user as a paid member if they paid enough.
- Partial payments are the same as no payment

**Tier 2 — On-chain miner fee (Bitcoin)**
Calculated at assembly, based on vsize, using the **actual** number of conforming UTXOs present in the frozen participant set (not the `max_conforming_utxos` cap). Only non-conforming participants pay it; the cap merely bounds intake during collecting.

Algorithm (`present_conforming` = conforming UTXOs actually present):

```
conforming_burden    = present_conforming × (conf_in_vsize + conf_out_vsize) × fee_rate
nonconforming_vsize  = overhead + Σ(nc_inputs_vsize) + Σ(nc_derived_outputs_vsize)
nonconforming_fee    = nonconforming_vsize × fee_rate
total_miner_fee      = nonconforming_fee + conforming_burden   # == actual tx vsize × fee_rate

For each NON-conforming participant (N of them):
  own        = nonconforming_fee × (their_nc_input+output_vsize) / total_nc_weight   # proportional
  burden     = conforming_burden / N                                                 # split evenly
  fee_share  = own + burden
  surplus    = sum(their_non_conforming_inputs) − (nc_equal_outputs × output_size)
  change     = surplus − fee_share
For each CONFORMING-only participant:
  fee_share  = 0     # free pass-through, miner fee subsidised by the non-conforming participants
```

- vsizes (e.g. 68/31 in earlier drafts) come from env on a per address-type basis.
- Because the burden uses the actual conforming count, `total_miner_fee == actual tx vsize × fee_rate` — the effective rate hits the target (no over-collection). The `fee_rate` itself is a live estimate (max of recent-blocks min-feerate × `FEE_MULTIPLIER`, clamped). A participant's leftover that has no change address, or is below the dust threshold, still folds into the miner fee.

Users should pay the miner fee proportional to the number of input and outputs they are contributing to the join.
The user doesn't directly tell us how many outputs they want. The tell us the input(s) and they provide us with n output addresses. The maximum number of outputs they can receive is the number of addresses they give us, but we calculate the number we will create based upon miner fee & minimum utxo size.
The number cannot be more than the number of addresses they give us, but if their inputs contain 0.02002111 and they send us 10 output addresses and we're creating 0.01 outputs, we can only make 2 outputs for them. If however they send us 0.52002111 and only send us 2 output addresses, and we're creating 0.01 outputs, we will create one 0.01 and the rest (minus calculated miner fees) will be the change in their 2nd output address. We do not need to prompt the user for more output addresses.

We start by taking the sum of one user's inputs and calculating the total number of outputs for the size the mix wants to create.
Then from their input bitcoin total we subtract the estimated fee per input & output.
- If the user hasn't given us enough output addresses, the will get a larger change output.
- If the user has given us too many output addresses, we only use as many as we need.
Once we subtract the fee based upon the number of outputs and our estimated fee per vbyte, we calculate their change based upon the remainder.
- If there is a remainder from their inputs, that is their proposed change.
- If the change is smaller than MINIMUM_UTXO_SIZE, those sats will be added to the miner fee.
- If the change is greater than MINIMUM_UTXO_SIZE, those sats will use one of the participant's output addresses.
It is acceptable for users to receive differing amounts of change. Those outputs are expected to be mixed by the user in a subsequent round.

Once we calculate how many outputs we will actually use we perform out (inputs + used-outputs) * FEE_PER_ELEMENT and ask for payment.


### 3f. Mixing Coordinator (File: `coordinator.py`)

State machines, event loop, tie everything together.

**Mix state machine:**

```
announced  → collecting  → assembling  → signing  → broadcast  → completed
                │               │              │              │
                ↓               ↓              ↓              ↓
             cancelled        cancelled     retry +         cancelled
               (timeout)       (ghost         restart        (double-spend
                                detected)                    detected,
                                                             or failed
                                                             broadcast)
```

**Participant state machine:**

```
interested  → committed  → paid  → signing  → signed  → broadcast  → completed
     │             │           │         │           │
     ↓             ↓           ↓         ↓           ↓
  cancelled     cancelled   ghosted    ghosted    cancelled
                              │                     (double-spend
                              │                     detected)
                              ↓
                          retry
                          (removed + 
                           round restarts)
```
There is an arrow from paid -> ghosted. If they do not pay within PAY_DEADLINE_HOURS they are removed from the mix.
There is an arrow from paid -> signing. When the transition to signing, if they do not pay within SIGNING_DEADLINE_HOURS they are removed from the mix & blacklisted.
There is an arrow from signing -> ghosted. If they do not pay within SIGNING_DEADLINE_HOURS they are removed from the mix & blacklisted.


**Event loop pseudocode:**

SIGNING_DEADLINE_HOURS is the
```python
async def run():
    while True:
        for mix in active_mixes:
            match mix.state:
                case "collecting":
                    if deadline_unix passed:
                        if participants < 2:
                            cancel_and_refund(mix)
                        elif non_conforming_participants < required_nonconforming:
                            cancel_and_refund(mix)
                        else:
                            proceed_to_assembling(mix)

                case "assembling":
                    assemble_psbt(mix)
                    update psbt_sent_at_unix for all paid participants
                    set p.state = "signing"
                    send_to_all_participants(mix)
                    mix.state = "signing"

                case "signing":
                    for p in mix.participants:
                        if p.state == "ghosted": continue
                        if psbt_sent + SIGNING_DEADLINE_HOURS passed and no return:
                            p.state = "ghosted"
                            blacklist(p)
                            ping_sequence(p)  # 6h, 12h
                        elif psbt_sent + (SIGNING_DEADLINE_HOURS / 8) passed and no return:
                            if p.reminder_count == 0:
                                send_ping(p)
                                p.reminder_count += 1
                        elif psbt_sent + (SIGNING_DEADLINE_HOURS / 4) passed and no return:
                            if p.reminder_count == 1:
                                send_ping(p)
                                p.reminder_count += 1
                        elif psbt_sent + (SIGNING_DEADLINE_HOURS / 2) passed and no return:
                            send_final_warning(p)
                            p.reminder_count += 1

                    # Check if all remaining participants have signed
                    remaining = [p for p in mix.participants
                                 if p.state not in ("ghosted", "cancelled")]
                    if all(p.state == "signed" for p in remaining):
                        combine_and_broadcast(mix)
                        announce_broadcast_to_participants()
                    else:
                        # Check if ghost_retries exceeds max
                        if mix.ghost_retries > mix.max_ghost_retries:
                            cancel_and_refund_everyone(mix) # all non-blacklisted participants are refunded (minus the REFUND_KEEP_PERCENT)
                        else:
                            mix.ghost_retries ++
                            remove_ghost_from_mix()
                            notify_participants_of_ghosting()
                            mix moves back into "collecting"

                case "broadcast":
                    if tx not confirmed after 1 hour:
                        re-broadcast
                    if confirmed:
                        mix.state = "completed"

        # Daily announcements (scheduled task)
        if time matches daily announcement time:
            assembled_message = ""
            for mix in active_mixes:
                assembled_message += summarize_mix(mix)
            post_announcement(assembled_message)

        await asyncio.sleep(1)
```


## 3g. Conversation Flow

This is a summary of some of what command_parser.py needs to handle.
If there are other flows that are required, a best guess is appropriate as long as it is written in a way that permits updating later.

> **Note (optional fee + conforming UTXOs):** the zap steps below only apply when `FEE_PER_ELEMENT > 0` and the participant brought non-conforming UTXOs. With the default `FEE_PER_ELEMENT=0`, or for a conforming-only participant, the bot skips the zap prompt, marks the participant `paid` immediately after `/addresses`, and replies "No service fee — you're all set." See §3i for conforming/non-conforming classification.

### Signup to mix (needing 2+ more people)
- Participant DMs: "list" or "open mixes" (or something similar)
- Bot replies with summmary or open mixes, including a short identifier such as:
- - Mix Buggy-Whip: Waiting on 2 more participants. 0.01 BTC outputs. p2wpkh addresses only.
- - Mix Fast-Muffin: Waiting on 1 more participant. 0.005 BTC outputs. p2wpkh addresses only.
- - Mix East-Gate: Waiting on 1 more participant. 0.025 BTC outputs. p2wpkh addresses only.
- Participant DMs: "Join East-Gate"
- Bot replies: "Send me txid and vout and a list of <p2wpkh> output addresses"
- - Bot adds npub to database as interested in "East-Gate"
- Participant send txid(s), vout(s), and 6 output addresses.
- Bot adds information to database, calculates joining fee (100 sats per input/output)
- Bot sends: "Zap me <number> sats to join <east-gate> mix"
- Participant sends zap
- Bot detects zap, updates database.
- Bot sends: "You are all paid up. We are waiting on 1 more participant"

### Signup to mix (needing only 1 more person)
- Participant DMs: "list" or "open mixes" (or something similar)
- Bot replies with summmary or open mixes, including a short identifier such as:
- - Mix Buggy-Whip: Waiting on 2 more participants. 0.01 BTC outputs. p2wpkh addresses only.
- - Mix Fast-Muffin: Waiting on 1 more participant. 0.005 BTC outputs. p2wpkh addresses only.
- - Mix East-Gate: Waiting on 1 more participant. 0.025 BTC outputs. p2wpkh addresses only.
- Participant DMs: "Join East-Gate"
- Bot replies: "Send me txid and vout and a list of <p2wpkh> output addresses"
- - Bot adds npub to database as interested in "East-Gate"
- Participant send txid(s), vout(s), and 6 output addresses.
- Bot adds information to database, calculates joining fee (100 sats per input/output)
- Bot sends: "Zap me <number> sats to join <east-gate> mix"
- Participant sends zap
- Bot detects zap, updates database.
- Bot sends: "You are all paid up."

### Signup to mix while already being in a different mix
- If participant is already a member to another mix and the other mix is missing txid/vout/address information, bot rejects
- Bot replies: "You need to send txid/vout/addresses for <east-gate> before you can join another mix.""

### Signup to mix while already being in a different mix
- If participant is already a member to another mix and has not paid, but rejects
- Bot replies: "You need to zap me <amount> sats for <east-gate> before you can join another mix.""

### Signup to mix while already paid for a different mix
- If participant is already a member of other mixes. If the count is less than <MAX_PENDING_MIXES>, we go through "Signup to mix" above
- If the count is >= MAX_PENDING_MIXES: "You're already in <MAX_PENDING_MIXES> mixes. Let's finish one of these first."

### User sends txid/vout/address already pledged to a different mix
- Bot detects duplicate and says: You can only use these in one mix. "Please send me a different txid - vout combo" or "Please send me different addresses"

### User sends txid/vout and only 1 address
- Bot tells them that they need to send us at least 2 addresses and takes no further action.


### When mix has filled participant slots
- Bot sends all participants: "We are going to start the signing process. Once I send you the PSBT, you have <SIGNING_DEADLINE_HOURS> hours to add your signatures and return it to me."
- Bot generates the PSBT
- Bot sends the PSBT to all participants using a timestamp positive relative to the "We are going to start..." message so they will be ordered correctly.

### When an npub wants out of a mix:
- Participant: exit east-gate
- Bot: "We're sorry to see you go. We will refund your joining fee, minus the REFUND_KEEP_PERCENT."
- Bot refunds 95% of fees to participant.

### When an npub wants out of a mix: (alt where user in 2+ mixes)
- Participant: exit
- Bot: You are a part of two mixes: east-gate & buggy-whip. Say exit east-gate or exit buggy-whip
- Participant: `exit east-gate` and the bot handles it in the flow described above.

### When an npub wants out of a mix: (alt where user in only 1 mix)
- Participant: exit
- Bot realizes the user is in only 1 mix and acts as though `exit east-gate` was sent, which is described above.

### When an npub wants out of a mix: (mix-name typo)
- Participant: exit plezt-gabe
- Bot checks if this user is in 1 mix, if so, uses the correct name and does the exit flow.
- If the user is in 2+ but the name doesn't match, it acts as though the user had typed only `exit` which is described above.

### When an npub wants out of a mix: (not a part of any mix)
- Participant: exit ...
- If the user is in 0 mixes, bot replies: "Done."

### when an npub sends us txid/vout/addresses
- If they have an unpaid mix, the information is added to the unpaid mix & continues with the "Signup to mix" script above
- If they are not in any mix, the bot finds them a mix that accepts the input/output address types that they've specified
- Bot says: we've added you to <east-gate> and resumes the "Signup to mix" script just after the txid,vout,address phase
- - this let's users join the closest to completed mix quickly and easily.
- - this needs to be atomic so we don't over-subscribe to a mix.
- If there is no open mix, the Bot creates a default mix with `output_size = DEFAULT_OUTPUT_SIZE` and `required_nonconforming = DEFAULT_REQUIRED_NONCONFORMING`. (An earlier draft computed a per-user output size from the sum of their inputs; that was abandoned because per-user output sizes fragment the anonymity set. A single shared `DEFAULT_OUTPUT_SIZE` keeps everyone in the mix on the same size band.)


If user sends the wrong output address type for the mix, the bot should reject by saying:
- "For this mix we're only accepting <address_type> addresses."

### 3h. Script-type policy

Two layers of script-type enforcement, both implemented:

1. **Operator allowlist** (env: `ACCEPTED_INPUT_TYPES`, `ACCEPTED_OUTPUT_TYPES`) — comma-separated, default `p2wpkh`. `/commit` rejects UTXOs whose normalized script type isn't on the input list; `/addresses` rejects addresses whose type isn't on the output list.
2. **Per-mix lock** (columns: `mixes.input_type`, `mixes.output_type`) — set by the first successful `/commit` and first `/addresses` to that mix. Subsequent commits/addresses must match the lock. This keeps the anonymity set within a mix to one type even when the operator allowlist permits multiple.

The MVP keeps the allowlist single-entry (`p2wpkh`), which makes the per-mix lock redundant but already in place for the day the allowlist is widened.

### 3i. Conforming vs non-conforming UTXOs

Every committed UTXO is auto-classified against the mix's `output_size` (the user never declares which kind it is):

- **Conforming** (`amount == output_size`): moved 1-input → 1-output unchanged to a fresh address. Pays **no service fee and no miner fee**. Can be contributed by anyone — a dedicated "conforming-only" participant, or alongside a non-conforming participant's own UTXOs. Conforming UTXOs exist to grow the anonymity set cheaply.
- **Non-conforming** (`amount != output_size`): carved into equal `output_size` outputs + change. Its owner pays the miner fee (and the optional service fee). A non-conforming participant's **total inputs must be ≥ `output_size`**.

**Mix sizing.** Each mix predefines two numbers:
- `required_nonconforming` — the **exact** number of non-conforming *participants* the mix waits for. Participant-counted, not UTXO-counted: one participant may bring several non-conforming UTXOs (capped per participant by `MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT`).
- `max_conforming_utxos` — the maximum number of conforming *UTXOs* the mix will absorb (bounds intake during collecting). Conforming UTXOs are a mining-fee burden the non-conforming participants subsidise — sized from the actual count present, not this cap.

**Proceeding.** A mix advances to assembling as soon as it has `required_nonconforming` non-conforming participants — it does **not** wait for conforming UTXOs — **except** when `required_nonconforming == 1`, where it must also have ≥1 conforming UTXO so there are ≥2 equal outputs from distinct parties. If the deadline passes before the exact target is met, the mix cancels and refunds.

**Miner fee.** Computed from the **actual** conforming UTXOs present at assembly (the `max_conforming_utxos` cap only bounds intake during collecting). The conforming burden is split **evenly** across the non-conforming participants; each also pays a proportional share of the non-conforming portion of the tx (see §3e). The total equals the real tx vsize × the live fee rate, so the effective rate hits the target.

**Addresses.** One fresh address per conforming UTXO, plus at least one for a non-conforming participant's equal output. So the **required floor** is `(conforming_count + 1)` for a non-conforming participant, or `max(conforming_count, 1)` for a conforming-only one. A change address is **optional** — the `/commit` guidance still *recommends* one address per mixed output plus one for change, so users don't donate change unintentionally.

**Leftover / change donation.** After the equal outputs and miner fee, any leftover from a non-conforming participant is handled by size against `MINIMUM_UTXO_SIZE` (the dust threshold). The guiding rule: **never burn an above-dust leftover when the participant gave us anywhere to put it.** The address count is the hard cap on a participant's outputs.
- **< dust:** folded into the miner fee (it can't form a spendable output).
- **≥ dust, a spare address is free** (funds are the binding constraint, i.e. fewer equal outputs than addresses): a normal change output to that spare address.
- **≥ dust, addresses are the binding constraint** (every address would be an equal output) **and ≥2 addresses supplied:** `nc_output_plan` **gives back the last equal slot** so its address holds the change instead — the change then **exceeds `output_size`**, which is accepted. Giving up one mixed output is far better than burning 10ks/100ks of sats. (Trade-off: fewer mixed outputs and a more distinctive change; re-mix it in a later round.)
- **≥ dust, only ONE address supplied** (fully consumed by the single equal output): no slot to spare without dropping to zero mixed outputs, so the participant is **warned at `/addresses`** ("~N sats will be donated — re-send with one more address to keep it") and at assembly the excess is paid to `DONATION_ADDRESS` if configured, otherwise folded into the miner fee. PRIVACY NOTE: a fixed `DONATION_ADDRESS` recurring across coinjoins is a linkable on-chain fingerprint; leaving it blank (fold-to-fee) is the privacy-maximising default.

**Privacy floor.** The non-authoritative privacy check requires at least `max(2, required_nonconforming)` equal `output_size` outputs from at least 2 inputs (NC + conforming combined). The `max(2, …)` keeps the solo (`required_nonconforming == 1`) case honest. See §8 — this is the whole bar; stronger anonymity is achieved by re-mixing in later rounds, not by subset-sum analysis.

**Input/output ordering.** At assembly the transaction's inputs and outputs are ordered deterministically (alphabetically — inputs by `txid:vout`, outputs by address) so a participant's inputs and outputs are not grouped together by position. An observer can't read mix membership off the transaction's ordering. Each participant's signing indices are re-derived against the final sorted order before the skeleton is sent.

**Signature verification.** When a participant returns a signed PSBT, the bot cryptographically verifies each of their signatures (p2wpkh: pubkey owns the input, hashtype is SIGHASH_ALL, ECDSA verifies against the input's BIP143 sighash) against the trusted skeleton — not just that a signature is present. A bogus/trolling or wrong-input signature is rejected at submission rather than wasting the whole signing round at broadcast.

---

## 4. Key Edge Cases

### Stale "interested" cleanup

A participant who `/join`s but never `/commit`s sits in the `interested` state with no UTXOs or outputs. When the mix leaves the collecting phase (advances to `assembling`), these never-committed stragglers are deleted (and DM'd) so they stop counting against the user's `MAX_PENDING_MIXES` / one-at-a-time gate and leave no on-disk trace. End-of-life phases already handle their own cleanup — cancellation scrubs all participant rows, and confirmation destroys them — so this only needs to fire on the collecting→assembling transition. (`committed`-but-unpaid participants are handled separately by the `PAY_DEADLINE_HOURS` timeout.)

### Ghosting Recovery

1. Ghoster detected at 24h → blacklisted (npub + all UTXOs)
2. A new psbt_rounds starts: remove ghoster's inputs + outputs from PSBT
4. DM remaining participants: "Someone ghosted us during the signing phase and saw your addresses. To insure your privacy, we've thrown out your addresses. Reply with new addresses: /addresses <new_addrs>" (and members go back to "paid" state.)
- we remove the existing addresses and the user goes back to needing to send us txid,vout,addresses
- we need to keep track of how many outputs they've paid for and only save that number if they send us more next time.
5. Participants wait for a new participant to join. 
6. Bot puts the job back into the advertising list until another party joins.

It is well understood that if someone ghosts the mix, the mix needs to start over from the beginning. We them move the group/mix back into a state of waiting for one more participant.
We should be transparent to the remaining users that someone has ghosted us, that their prior signatures are invalid & destroyed by us, and we're moving the group/mix back into advertising for another person.

If a person pays but never signs, they forfeit their payment, and they're added to a blacklist table.

When someone ghosts and we reset the mix, remaining participants move back to 'paid' state, awaiting another participant.
If the mix ultimately fails because time has expired, remaining participants will have their fee refunded (minus the REFUND_KEEP_PERCENT)



### Double-Spend Prevention

Before building the PSBT, query `utxos.is_used` across all active mixes. If the same txid:vout appears in another mix (whether collecting, signing, or pending), reject the new participant and flag.

### PSBT Size Limits

If a PSBT hex exceeds 50KB (relay size concern), split into numbered chunks across multiple DMs with a reassembly header:
```
/psbt_chunk 1/3 <hex>
/psbt_chunk 2/3 <hex>  
/psbt_chunk 3/3 <hex>
```
Participant concatenates and imports as a single PSBT.

### RBF Avoidance

Set fee rate to `max(estimate, MIN_FEE_RATE) * FEE_MULTIPLIER` — buffer over the minimum ensures the transaction clears on the first broadcast. If mempool spikes, the transaction is still broadcast to the mempool and will have to wait longer than desired.


### Crash recovery

When starting up, the bot should check the database for unfinished work and resume work.
We should make work and database writes idempotent so that a failure at the wrong time will not cause catastrophic failure of a mix.
We should not update database state without preserving the dependent state, such as txid,vout,addresses must be perserved before moving participants to committed. Zaps should be confirmed before moving to paid. If someone pays before we move them to paid and we crash, the operator will handle that manually. We will need logging to help debug these cases.



---

## 5. File Tree

```
~/Documents/nostrmix-bot/
├── nostrmix-bot.env
├── bot.db
├── src/
│   ├── main.py                # entry point, async loop
│   ├── config.py              # load env, validate
│   ├── coordinator.py         # state machines, event loop
│   ├── nostr_handler.py       # NIP-17 DMs, NIP-57 zaps, announcements
│   ├── lightning_handler.py   # LND, zap detection, refund sending
│   ├── chain_monitor.py       # UTXO lookup, fee estimate, broadcast
│   ├── psbt_manager.py        # build, validate, combine PSBTs
│   ├── fee_engine.py          # vsize calc, fee distribution
│   ├── command_parser.py      # rigid DM command protocol
│   ├── database.py            # SQLite access layer (aiosqlite)
│   ├── privacy.py             # sanity-check PSBT: >=2 equal-size outputs from >=2 inputs (see §8)
│   └── schema.sql             # CREATE TABLE statements
├── tests/
│   └── <add tests as necessary to cover common and error cases>
├── requirements.txt
└── README.md
```

Privacy.py is a non-authoritative sanity check, not a full anonymity analysis. The bar we enforce: **a mix must produce at least 2 equal-size outputs drawn from at least 2 inputs (non-conforming and conforming inputs counted together).** That is the minimum that achieves the bot's purpose — breaking the 1:1 link between a coin and its owner by giving each mixed output ≥1 indistinguishable sibling. Concretely the check requires `max(2, required_nonconforming)` identical `output_size` outputs to be present before signing. Higher anonymity is the user's choice: they can run the resulting coins through additional mixing rounds. We deliberately do NOT attempt subset-sum / N!/2 partition counting.

### What is and isn't private (change & the fee split)

The **equal `output_size` outputs are the anonymity set** — all identical, genuinely unlinkable. Everything else carries information:

- **Change is the dominant, irreducible leak.** Every coinjoin-with-change (Wasabi, JoinMarket, this bot) has it. For each participant the amounts balance as `inputs = equal·output_size + change + fee`. Because change amounts are unique and high-entropy, an observer doing subset-sum can usually re-associate a change output with its input cluster regardless of the fee policy. The request-1 "oversized change instead of burning" rule makes change *more* distinctive — an accepted trade, because the answer to toxic change is to **re-mix it**, not to shrink it.
- **The deterministic fee split is a minor, accepted signal.** We split the miner fee by each participant's input+output vsize (proportional). Any *deterministic* rule — proportional **or** a flat `total/N` — is computable by an attacker, so it lets them check a subset-sum candidate exactly; proportional additionally leaks a participant's input *count*. We considered **randomizing** fee shares to add ambiguity and rejected it: our fees are tiny (hundreds of sats) versus change amounts (10k–millions), so the ambiguity window is negligible — it would be security theater while introducing real fee unfairness (a 1-input user subsidizing a 10-input user). The honest position is that the fee split is **not** where privacy is won or lost.
- **Where privacy is actually won:** (1) **conforming amounts** — exact-multiple inputs have *no change* and pass through 1→1, perfectly unlinkable (the protocol makes these free to encourage them); (2) **re-mixing** change in later rounds; (3) **user hygiene** — never co-spend change together with mixed outputs.

So the design choice is deliberate: keep the proportional split (fair, and not the real exposure) and lean on conforming-amounts + re-mixing for anonymity.

---

## 6. `nostrmix-bot.env` Configuration

```nostrmix-bot.env
# Bot identity
NOSTR_PRIVATE_KEY_NPUB=7b...
NOSTR_RELAYS=wss://relay.damus.com,wss://nos.lol
BOT_NAME=butterbot
BOT_ABOUT=I help bitcoiners ...
BOT_LUD16=nostrmix-bot@pay.unsaltedbutter.ai
BOT_PICTURE=https://unsaltedbutter.ai/nostrmix-bot.png
BOT_NIP05=nostrmix-bot@unsaltedbutter.ai
BOT_WEBSITE=https://unsaltedbutter.ai


# Zap receiving
ZAP_PROVIDER_PUBKEY_HEX=64dd.....

# Zap Sending for refunds
BTCPAY_URL=pay.unsaltedbutter.ai
BTCPAY_STORE=abc123...
BTCPAY_API_KEY=456def...


# Fee defaults
# Service fee (zap) per NON-conforming element (input + used output). 0 = no
# zap requested (optional service fee, off by default). Conforming UTXOs are
# always free regardless of this value.
FEE_PER_ELEMENT=0
FEE_MULTIPLIER=1.5
MIN_FEE_RATE_SATS=1.5
MAX_FEE_RATE_SATS=510
REFUND_KEEP_PERCENT=5
REFUND_KEEP_MIN_SATS=50

# Mix parameters
DEFAULT_OUTPUT_SIZE=1000000
MAX_PARTICIPANTS_DEFAULT=20
MAX_PENDING_MIXES=3
MAX_OPEN_MIXES=10                              # cap on open mixes; gates every new-mix creation path
SIGNING_DEADLINE_HOURS=48
PAY_DEADLINE_HOURS=12
MAX_GHOST_RETRIES=3
MINIMUM_UTXO_SIZE=10000

# Conforming / non-conforming model (see §3i)
DEFAULT_REQUIRED_NONCONFORMING=3              # exact # of non-conforming participants a new mix waits for
MAX_CONFORMING_UTXOS=10                       # max conforming UTXOs a mix absorbs (miner fee assumes this many)
MAX_NONCONFORMING_UTXOS_PER_PARTICIPANT=10    # per-participant cap on non-conforming UTXOs
# Above-dust change with no change address is paid here (else folded into the
# miner fee). Blank = disabled. PRIVACY: a recurring operator address links
# coinjoins on-chain; prefer leaving blank.
DONATION_ADDRESS=

# Operator script-type allowlist (comma-separated). Gates which UTXO types
# the bot will accept at /commit and which output address types at /addresses.
# Default is single-type (p2wpkh) — widening past one entry requires the
# per-mix input_type/output_type lock to keep anonymity sets coherent.
ACCEPTED_INPUT_TYPES=p2wpkh
ACCEPTED_OUTPUT_TYPES=p2wpkh

# Daily announcement scheduling
ANNOUNCEMENT_HOUR_UTC=14           # 0..23; fires at this wall-clock hour each day

# Broadcast-sweep cadence (re-broadcast unconfirmed txs + confirm checks)
BROADCAST_CHECK_INTERVAL_HOURS=24

# Bitcoin API (mempool.space — free, no key needed)
MEMPOOL_API=https://mempool.space/api
# Esplora-compatible backup mirror — tried if MEMPOOL_API 5xx's or times out.
# Set blank to disable fallback.
MEMPOOL_API_BACKUP=https://blockstream.info/api

# Per-script-type vbyte sizes. Calibrated against real mainnet transactions
# (see nostrmix-status.md vsize-accuracy fixtures) and rounded up to nearest
# 5 for a small fee buffer. Operators rarely need to override these; the
# defaults are the consensus values for single-sig spends and a conservative
# 2-of-3 figure for p2sh / p2wsh.
P2PKH_INPUT_VSIZE=150
P2SH_INPUT_VSIZE=135
P2SH_P2WPKH_INPUT_VSIZE=95
P2WPKH_INPUT_VSIZE=70
P2WSH_INPUT_VSIZE=100
P2TR_INPUT_VSIZE=60
P2PKH_OUTPUT_VSIZE=35
P2SH_OUTPUT_VSIZE=35
P2SH_P2WPKH_OUTPUT_VSIZE=35
P2WPKH_OUTPUT_VSIZE=35
P2WSH_OUTPUT_VSIZE=45
P2TR_OUTPUT_VSIZE=45
TX_OVERHEAD_VSIZE=10

# Database
DB_PATH=./bot.db
```

All of these values are illustrative and placeholders. Make no assumption about them other than they will be strings/numbers, and should be whitespace trimmed when pulled out of the env file.

The app should not accept DEFAULT_REQUIRED_NONCONFORMING < 1. It should upgrade the value to 1 in that case (a mix needs at least one non-conforming participant to fund the miner fee).

---

## 7. Implementation Order

**Phase 1 — Foundation**
- Database layer (schema + aiosqlite access class)
- Configuration loader
- Nostr Handler (connect, send/receive DMs, listen for zaps)
- Chain Monitor (UTXO lookup, fee estimate, broadcast)

**Phase 2 — Core Logic (can be parallel)**
- PSBT Manager (build, validate, combine)
- Fee Engine (vsize, distribution, change rounding)
- Lightning Handler (refund sending)

**Phase 3 — Integration**
- Coordinator (state machines, event loop, ghosting detection)
- Wire all components together
- Operator will test himself with 2+ of his nostr identities and with owned UTXOs

**Phase 4 — Polish**
- Ghost ping sequence (6h/12h/24h DMs)
- Daily announcement scheduler
- Blacklist enforcement
- Error recovery and edge cases
- README + deploy instructions

---

## 8. Testing Strategy

- **Unit tests**: each component independently with mocked Nostr relays, LND, and mempool API
- **Integration**: manual testing with Operator's own nostr accounts and UTXOs
- **Privacy smoke test**: before signing, inspect the PSBT and confirm the minimum-viable mixing structure — **at least 2 equal-size (`output_size`) outputs, produced from at least 2 inputs (non-conforming + conforming combined)**. This is the bar the bot guarantees (`max(2, required_nonconforming)` identical outputs). We intentionally do NOT perform subset-sum / N!/2 partition counting; users who want stronger anonymity re-mix the outputs in subsequent rounds. 



## 9. Clarifications, if necessary

1. All numbers above are examples and placeholders. Forumla are what holds true. For example (FEE_PER_ELEMENT * (input count + used output count)) is the fee, regardless of what the examples show.

2. Users send us 2+ output addresses and we try to use as many as possible given the amount of BTC their inputs contain and the standardized size of the outputs for this mix.

3. All values in the env template are examples. You do not need them during coding.

4. Both inputs and outputs need be the same single type.