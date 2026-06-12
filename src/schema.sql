-- nostrmix-bot schema
-- PSBT-based coinjoin mixer over Nostr NIP-17

CREATE TABLE IF NOT EXISTS mixes (
    id              TEXT PRIMARY KEY,          -- human-readable name (east-gate)
    output_size     INTEGER NOT NULL,          -- sats per equal output; a UTXO of exactly this size is "conforming"
    max_participants INTEGER,
    -- Conforming/non-conforming model:
    --   required_nonconforming = exact number of non-conforming participants
    --     the mix waits for before assembling (the fee-split denominator).
    --   max_conforming_utxos   = cap on conforming UTXOs the mix absorbs; the
    --     miner fee is computed assuming this many, split evenly across the
    --     non-conforming participants.
    required_nonconforming INTEGER NOT NULL DEFAULT 3,
    max_conforming_utxos INTEGER NOT NULL DEFAULT 10,
    fee_rate        REAL DEFAULT 30,           -- sats/vbyte (fractional), set at assembly
    fee_per_element INTEGER DEFAULT 0,         -- sats, service-fee zap per non-conforming element; 0 = no zap
    state           TEXT NOT NULL DEFAULT 'announced',
    -- announced | collecting | assembling | signing | broadcast | completed | cancelled
    deadline_unix   INTEGER,
    broadcast_txid  TEXT,
    broadcast_tx_hex TEXT,                    -- raw signed tx hex, kept so _broadcast_sweep can re-push if needed
    input_type      TEXT,                     -- locked at first /commit; all subsequent commits must match
    output_type     TEXT,                     -- locked at first /addresses; all subsequent must match
    ghost_retries   INTEGER DEFAULT 0,
    max_ghost_retries INTEGER DEFAULT 3,
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    npub_hex        TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'interested',
    -- interested | committed | paid | signing | signed | ghosted | broadcast
    -- | refunding | refunded | refund_failed | cancelled
    -- 'refunding' is set BEFORE we call the LN wallet so a crash-resume
    -- never re-attempts a payout that may already have left the wallet.
    -- 'refund_failed' is set if both LN backends returned None — operator
    -- must reconcile manually.
    fee_paid        INTEGER,                    -- sats zap received
    fee_share       INTEGER,                    -- on-chain fee share (calculated)
    change_amount   INTEGER,                    -- change output sats, 0 = no change
    -- JSON list of payout addresses accumulated across `outputs` messages
    -- (append-mode intake). Cleared whenever the participant's outputs are
    -- cleared — delete_outputs_by_participant keeps the pair in lockstep.
    pending_addresses TEXT,
    lightning_addr  TEXT,                       -- from Nostr profile kind 0, for refunds
    psbt_sent_at_unix INTEGER,
    reminder_count  INTEGER DEFAULT 0,
    created_at_unix INTEGER NOT NULL,
    updated_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS utxos (
    id              TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    txid            TEXT NOT NULL,
    vout            INTEGER NOT NULL,
    amount          INTEGER NOT NULL,           -- sats
    script_type     TEXT,                        -- p2wpkh | p2tr | p2pkh
    scriptpubkey    TEXT,                        -- hex of the prevout script (from chain lookup)
    is_used         BOOLEAN DEFAULT 0,
    created_at_unix INTEGER NOT NULL,
    -- S9: at most one row per (txid, vout) in the table at any time.
    -- All paths that "release" an outpoint (whole-mix cancel, per-participant
    -- drop / cancel / exit, ghost detection, broadcast-confirmation destroy)
    -- delete the utxos row, so the same outpoint can be re-committed later.
    UNIQUE(txid, vout)
);

CREATE TABLE IF NOT EXISTS outputs (
    id              TEXT PRIMARY KEY,
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    address         TEXT NOT NULL,
    amount          INTEGER NOT NULL,           -- sats
    is_change       BOOLEAN DEFAULT 0,
    created_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS psbt_rounds (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    participant_id  TEXT NOT NULL REFERENCES participants(id),
    round_num       INTEGER DEFAULT 1,
    psbt_sent_at_unix INTEGER,
    psbt_sent       TEXT,                        -- hex of skeleton PSBT
    psbt_returned   TEXT,                        -- hex of signed PSBT
    psbt_returned_at_unix INTEGER,
    psbt_valid      BOOLEAN,
    input_indices   TEXT,                      -- JSON list of vin indices the participant must sign
    created_at_unix INTEGER,
    updated_at_unix INTEGER,
    UNIQUE(mix_id, participant_id, round_num)
);

CREATE TABLE IF NOT EXISTS blacklist (
    id              TEXT PRIMARY KEY,
    npub_hex        TEXT,
    utxo_txid_vout  TEXT,                        -- txid:vout string
    reason          TEXT DEFAULT 'ghosting',
    created_at_unix INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
    id              TEXT PRIMARY KEY,
    mix_id          TEXT NOT NULL REFERENCES mixes(id),
    event_id        TEXT,                        -- Nostr event ID
    posted_at_unix  INTEGER NOT NULL
);

-- Simple key-value settings table (hackable: sqlite3 bot.db "UPDATE settings SET value='0' WHERE key='last_broadcast_check_unix'")
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Outstanding refund debts. When a mix is destroyed (confirmed OR failed) every
-- trace of it is wiped — EXCEPT, for a participant whose service-fee refund the
-- Lightning backend rejected (state 'refund_failed'), a minimal record of who we
-- owe and how much, so the operator can reconcile by hand. Deliberately holds no
-- mix link, no npub, no UTXOs/outputs/PSBT — just the Lightning address + sats.
-- The opaque participant_id is the PK only so re-recording on crash-resume is a
-- no-op (INSERT OR IGNORE). Only ever populated when a service fee was charged.
CREATE TABLE IF NOT EXISTS refunds_owed (
    participant_id  TEXT PRIMARY KEY,
    lightning_addr  TEXT NOT NULL,
    sats            INTEGER NOT NULL,
    reason          TEXT,
    created_at_unix INTEGER NOT NULL
);

-- Indices for common query patterns
CREATE INDEX IF NOT EXISTS idx_participants_mix ON participants(mix_id);
CREATE INDEX IF NOT EXISTS idx_participants_npub ON participants(npub_hex);
CREATE INDEX IF NOT EXISTS idx_utxos_participant ON utxos(participant_id);
CREATE INDEX IF NOT EXISTS idx_utxos_txid_vout ON utxos(txid, vout);
CREATE INDEX IF NOT EXISTS idx_outputs_participant ON outputs(participant_id);
CREATE INDEX IF NOT EXISTS idx_psbt_rounds_mix ON psbt_rounds(mix_id);
CREATE INDEX IF NOT EXISTS idx_psbt_rounds_participant ON psbt_rounds(participant_id);
CREATE INDEX IF NOT EXISTS idx_blacklist_npub ON blacklist(npub_hex);
CREATE INDEX IF NOT EXISTS idx_announcements_mix ON announcements(mix_id);
CREATE INDEX IF NOT EXISTS idx_mixes_state ON mixes(state);
