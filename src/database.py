"""Async SQLite database layer for nostrmix-bot."""

import aiosqlite
import sqlite3
import uuid
import time
from typing import Optional, List, Dict, Any, Tuple


def _hex_id() -> str:
    """Short random hex ID for rows."""
    return uuid.uuid4().hex[:12]


def _now() -> int:
    return int(time.time())


SCHEMA_PATH = __file__.replace("database.py", "schema.sql")


class Database:
    """Async SQLite wrapper with CRUD for all tables."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Run schema
        with open(SCHEMA_PATH) as f:
            schema = f.read()
        await self._conn.executescript(schema)
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    # --- Internal ---

    async def _execute(self, sql: str, params=None) -> aiosqlite.Cursor:
        if self._conn is None:
            raise RuntimeError("Database not connected")
        cur = await self._conn.execute(sql, params or ())
        return cur

    async def _fetchone(self, sql: str, params=None) -> Optional[Dict]:
        cur = await self._execute(sql, params)
        row = await cur.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in cur.description]
        return dict(zip(cols, row))

    async def _fetchall(self, sql: str, params=None) -> List[Dict]:
        cur = await self._execute(sql, params)
        rows = await cur.fetchall()
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in rows]

    # --- Mix CRUD ---

    async def create_mix(self, output_size: int, min_participants: int = 3,
                         max_participants: Optional[int] = None,
                         fee_per_element: int = 100) -> str:
        mid = _hex_id()
        now = _now()
        await self._execute(
            """INSERT INTO mixes (id, output_size, min_participants, max_participants,
               fee_per_element, state, created_at_unix, updated_at_unix)
               VALUES (?, ?, ?, ?, ?, 'announced', ?, ?)""",
            (mid, output_size, min_participants, max_participants,
             fee_per_element, now, now),
        )
        await self._conn.commit()
        return mid

    async def get_mix(self, mix_id: str) -> Optional[Dict]:
        return await self._fetchone("SELECT * FROM mixes WHERE id = ?", (mix_id,))

    async def get_mixes_by_state(self, *states: str) -> List[Dict]:
        placeholders = ",".join("?" for _ in states)
        return await self._fetchall(
            f"SELECT * FROM mixes WHERE state IN ({placeholders}) ORDER BY created_at_unix",
            states,
        )

    async def update_mix(self, mix_id: str, **kwargs):
        now = _now()
        kwargs["updated_at_unix"] = now
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(mix_id)
        await self._execute(
            f"UPDATE mixes SET {cols} WHERE id=?",
            vals,
        )
        await self._conn.commit()

    async def delete_mix(self, mix_id: str):
        await self._execute("DELETE FROM mixes WHERE id=?", (mix_id,))
        await self._conn.commit()

    # --- Participant CRUD ---

    async def add_participant(self, mix_id: str, npub_hex: str,
                              lightning_addr: str = "") -> str:
        pid = _hex_id()
        now = _now()
        await self._execute(
            """INSERT INTO participants (id, mix_id, npub_hex, state,
               lightning_addr, created_at_unix, updated_at_unix)
               VALUES (?, ?, ?, 'interested', ?, ?, ?)""",
            (pid, mix_id, npub_hex, lightning_addr, now, now),
        )
        await self._conn.commit()
        return pid

    async def get_participant(self, pid: str) -> Optional[Dict]:
        return await self._fetchone("SELECT * FROM participants WHERE id = ?", (pid,))

    async def get_participants_by_mix(self, mix_id: str) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM participants WHERE mix_id = ? ORDER BY created_at_unix",
            (mix_id,),
        )

    async def get_participants_by_npub(self, npub_hex: str) -> List[Dict]:
        """Get all participants for a given npub across all mixes."""
        return await self._fetchall(
            "SELECT * FROM participants WHERE npub_hex = ? ORDER BY created_at_unix",
            (npub_hex,),
        )

    async def update_participant(self, pid: str, **kwargs):
        now = _now()
        kwargs["updated_at_unix"] = now
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(pid)
        await self._execute(f"UPDATE participants SET {cols} WHERE id=?", vals)
        await self._conn.commit()

    async def delete_participants_for_mix(self, mix_id: str):
        await self._execute("DELETE FROM participants WHERE mix_id=?", (mix_id,))
        await self._conn.commit()

    async def delete_participant(self, pid: str):
        await self._execute("DELETE FROM participants WHERE id=?", (pid,))
        await self._conn.commit()

    async def count_participants_by_mix(self, mix_id: str,
                                        exclude_states: Optional[List[str]] = None) -> int:
        sql = "SELECT COUNT(*) as cnt FROM participants WHERE mix_id=?"
        params = [mix_id]
        if exclude_states:
            placeholders = ",".join("?" for _ in exclude_states)
            sql += f" AND state NOT IN ({placeholders})"
            params.extend(exclude_states)
        row = await self._fetchone(sql, params)
        return row["cnt"] if row else 0

    async def count_active_participant_mixes(self, npub_hex: str) -> int:
        """Count mixes the npub is a participant in, excluding cancelled/ghosted."""
        row = await self._fetchone(
            """SELECT COUNT(*) as cnt FROM participants p
               JOIN mixes m ON p.mix_id = m.id
               WHERE p.npub_hex = ? AND p.state NOT IN ('cancelled', 'ghosted')
               AND m.state NOT IN ('cancelled', 'completed')""",
            (npub_hex,),
        )
        return row["cnt"] if row else 0

    # --- UTXO CRUD ---

    async def add_utxo(self, participant_id: str, txid: str, vout: int,
                       amount: int, script_type: str = "p2wpkh") -> str:
        uid = _hex_id()
        now = _now()
        await self._execute(
            """INSERT INTO utxos (id, participant_id, txid, vout, amount,
               script_type, created_at_unix)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (uid, participant_id, txid, vout, amount, script_type, now),
        )
        await self._conn.commit()
        return uid

    async def get_utxos_by_participant(self, participant_id: str) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM utxos WHERE participant_id = ? ORDER BY created_at_unix",
            (participant_id,),
        )

    async def get_utxo(self, txid: str, vout: int) -> Optional[Dict]:
        return await self._fetchone(
            "SELECT * FROM utxos WHERE txid = ? AND vout = ?",
            (txid, vout),
        )

    async def is_utxo_used(self, txid: str, vout: int) -> bool:
        """Check if a UTXO is already used in any active mix."""
        row = await self._fetchone(
            """SELECT COUNT(*) as cnt FROM utxos u
               JOIN participants p ON u.participant_id = p.id
               JOIN mixes m ON p.mix_id = m.id
               WHERE u.txid = ? AND u.vout = ? AND u.is_used = 1
               AND m.state NOT IN ('completed', 'cancelled')""",
            (txid, vout),
        )
        return row["cnt"] > 0 if row else False

    async def mark_utxo_used(self, pid: str, txid: str, vout: int):
        await self._execute(
            "UPDATE utxos SET is_used = 1 WHERE participant_id = ? AND txid = ? AND vout = ?",
            (pid, txid, vout),
        )
        await self._conn.commit()

    async def delete_utxos_by_participant(self, participant_id: str):
        await self._execute(
            "DELETE FROM utxos WHERE participant_id = ?",
            (participant_id,),
        )
        await self._conn.commit()

    # --- Output CRUD ---

    async def add_output(self, participant_id: str, address: str,
                         amount: int, is_change: bool = False) -> str:
        oid = _hex_id()
        now = _now()
        await self._execute(
            """INSERT INTO outputs (id, participant_id, address, amount,
               is_change, created_at_unix)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (oid, participant_id, address, amount, int(is_change), now),
        )
        await self._conn.commit()
        return oid

    async def get_outputs_by_participant(self, participant_id: str) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM outputs WHERE participant_id = ? ORDER BY created_at_unix",
            (participant_id,),
        )

    async def delete_outputs_by_participant(self, participant_id: str):
        await self._execute(
            "DELETE FROM outputs WHERE participant_id = ?",
            (participant_id,),
        )
        await self._conn.commit()

    async def delete_outputs_for_mix(self, mix_id: str):
        await self._execute(
            "DELETE FROM outputs WHERE participant_id IN (SELECT id FROM participants WHERE mix_id = ?)",
            (mix_id,),
        )
        await self._conn.commit()

    # --- PSBT Round CRUD ---

    async def add_psbt_round(self, mix_id: str, participant_id: str,
                             round_num: int = 1) -> str:
        rid = _hex_id()
        now = _now()
        await self._execute(
            """INSERT INTO psbt_rounds (id, mix_id, participant_id, round_num,
               psbt_sent_at_unix, created_at_unix)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (rid, mix_id, participant_id, round_num, now, now),
        )
        await self._conn.commit()
        return rid

    async def get_psbt_round(self, mix_id: str, participant_id: str,
                             round_num: int) -> Optional[Dict]:
        return await self._fetchone(
            "SELECT * FROM psbt_rounds WHERE mix_id=? AND participant_id=? AND round_num=?",
            (mix_id, participant_id, round_num),
        )

    async def update_psbt_round(self, rid: str, **kwargs):
        kwargs["updated_at_unix"] = _now()
        cols = ", ".join(f"{k}=?" for k in kwargs)
        vals = list(kwargs.values())
        vals.append(rid)
        await self._execute(f"UPDATE psbt_rounds SET {cols} WHERE id=?", vals)
        await self._conn.commit()

    async def get_psbt_rounds_by_mix(self, mix_id: str) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM psbt_rounds WHERE mix_id=? ORDER BY created_at_unix",
            (mix_id,),
        )

    # --- Blacklist CRUD ---

    async def add_to_blacklist(self, npub_hex: str, utxo_txid_vout: str = "",
                               reason: str = "ghosting") -> str:
        bid = _hex_id()
        now = _now()
        await self._execute(
            "INSERT INTO blacklist (id, npub_hex, utxo_txid_vout, reason, created_at_unix) VALUES (?, ?, ?, ?, ?)",
            (bid, npub_hex, utxo_txid_vout, reason, now),
        )
        await self._conn.commit()
        return bid

    async def is_blacklisted(self, npub_hex: str, utxo_txid_vout: str = "") -> bool:
        if utxo_txid_vout:
            row = await self._fetchone(
                "SELECT COUNT(*) as cnt FROM blacklist WHERE npub_hex=? OR utxo_txid_vout=?",
                (npub_hex, utxo_txid_vout),
            )
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) as cnt FROM blacklist WHERE npub_hex=?",
                (npub_hex,),
            )
        return row["cnt"] > 0 if row else False

    async def get_blacklist(self) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM blacklist ORDER BY created_at_unix DESC",
        )

    # --- Announcement CRUD ---

    async def add_announcement(self, mix_id: str, event_id: str = "") -> str:
        aid = _hex_id()
        now = _now()
        await self._execute(
            "INSERT INTO announcements (id, mix_id, event_id, posted_at_unix) VALUES (?, ?, ?, ?)",
            (aid, mix_id, event_id, now),
        )
        await self._conn.commit()
        return aid

    async def get_announcements_for_mix(self, mix_id: str) -> List[Dict]:
        return await self._fetchall(
            "SELECT * FROM announcements WHERE mix_id=? ORDER BY posted_at_unix",
            (mix_id,),
        )

    # --- Utility ---

    async def get_active_mixes(self) -> List[Dict]:
        return await self.get_mixes_by_state(
            "announced", "collecting", "assembling", "signing", "broadcast"
        )

    async def resume_unfinished(self) -> List[Dict]:
        """Return unfinished mixes for crash recovery."""
        return await self.get_active_mixes()
