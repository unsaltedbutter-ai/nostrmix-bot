-- nostrmix-bot schema
-- PSBT-based coinjoin mixer over Nostr NIP-17

CREATE TABLE IF NOT EXISTS mixes (
    id              TEXT PRIMARY KEY,          -- human-readable name (east-gate)
    output_size     INTEGER NOT NULL,          -- sats per equal output
    min_participants INTEGER NOT NULL DEFAULT 3,
    max_participants INTEGER,
    fee_rate        INTEGER DEFAULT 30,        -- sats/vbyte, set at assembly
    fee_per_element INTEGER DEFAULT 100,       -- sats, zap fee per input+output
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
    -- interested | committed | paid | signing | signed | ghosted | broadcast | refunded
    fee_paid        INTEGER,                    -- sats zap received
    fee_share       INTEGER,                    -- on-chain fee share (calculated)
    change_amount   INTEGER,                    -- change output sats, 0 = no change
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
    created_at_unix INTEGER NOT NULL
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
