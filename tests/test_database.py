"""Tests for Database layer."""

import os
import sys
import tempfile
import pytest
import asyncio

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Track temp files for cleanup
_db_paths = []


def cleanup_db():
    for p in _db_paths[:]:
        try:
            os.unlink(p)
            _db_paths.remove(p)
        except:
            pass


async def make_db():
    """Create a temporary database for testing."""
    import src.database as db_mod
    schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
    db_mod.SCHEMA_PATH = schema_path

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    _db_paths.append(db_path)

    database = db_mod.Database(db_path)
    await database.connect()
    return database


class TestDatabase:
    @pytest.mark.asyncio
    async def test_create_and_get_mix(self):
        db = await make_db()
        try:
            mid = await db.create_mix(
                output_size=1_000_000,
                min_participants=3,
                max_participants=10,
                fee_per_element=100,
            )
            assert mid is not None
            mix = await db.get_mix(mid)
            assert mix is not None
            assert mix["output_size"] == 1_000_000
            assert mix["min_participants"] == 3
            assert mix["state"] == "announced"
            # Conforming/non-conforming columns default in when not supplied.
            assert mix["required_nonconforming"] == 3
            assert mix["max_conforming_utxos"] == 10
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_create_mix_with_conforming_params(self):
        db = await make_db()
        try:
            mid = await db.create_mix(
                output_size=500_000, min_participants=2,
                required_nonconforming=2, max_conforming_utxos=4,
            )
            mix = await db.get_mix(mid)
            assert mix["required_nonconforming"] == 2
            assert mix["max_conforming_utxos"] == 4
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_get_utxos_for_mix_spans_participants(self):
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 2)
            p1 = await db.add_participant(mid, "n1", "")
            p2 = await db.add_participant(mid, "n2", "")
            await db.add_utxo(p1, "11" * 32, 0, 1_000_000, "p2wpkh")
            await db.add_utxo(p2, "22" * 32, 0, 2_000_000, "p2wpkh")
            rows = await db.get_utxos_for_mix(mid)
            assert len(rows) == 2
            # Conforming count (amount == output_size) is computable from the rows.
            conforming = sum(1 for r in rows if r["amount"] == 1_000_000)
            assert conforming == 1
            assert all("participant_state" in r for r in rows)
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_add_participant(self):
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "npub_hex_123", "user@pay.domain")
            assert pid is not None
            p = await db.get_participant(pid)
            assert p["npub_hex"] == "npub_hex_123"
            assert p["state"] == "interested"
            assert p["lightning_addr"] == "user@pay.domain"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_add_utxo(self):
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "npub_hex_123", "")
            uid = await db.add_utxo(pid, "abc123", 0, 100_000, "p2wpkh")
            assert uid is not None
            utxos = await db.get_utxos_by_participant(pid)
            assert len(utxos) == 1
            assert utxos[0]["amount"] == 100_000
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_add_output(self):
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "npub_hex_123", "")
            oid = await db.add_output(pid, "bc1qabc123", 1_000_000, False)
            assert oid is not None
            outputs = await db.get_outputs_by_participant(pid)
            assert len(outputs) == 1
            assert outputs[0]["address"] == "bc1qabc123"
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_utxo_double_spend_detection_marks_used(self):
        """The is_used / is_utxo_used / mark_utxo_used flow still works on
        a single-row outpoint. (Prior to S9 this test created two utxos
        rows for the same outpoint; that's now blocked by the UNIQUE
        constraint and the realistic flow is one row per active outpoint.)"""
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "npub1", "")
            await db.add_utxo(pid, "txid_abc", 0, 100_000)
            assert await db.is_utxo_used("txid_abc", 0) is False
            await db.mark_utxo_used(pid, "txid_abc", 0)
            assert await db.is_utxo_used("txid_abc", 0) is True
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_get_active_mixes(self):
        db = await make_db()
        try:
            await db.create_mix(1_000_000, 3)
            active = await db.get_active_mixes()
            assert len(active) == 1
            assert active[0]["state"] == "announced"

            await db.update_mix(active[0]["id"], state="completed")
            active = await db.get_active_mixes()
            assert len(active) == 0
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_participant_state_machine(self):
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "npub_hex", "")
            assert await db.get_participant(pid) is not None
            await db.update_participant(pid, state="paid", fee_paid=500)
            p = await db.get_participant(pid)
            assert p["state"] == "paid"
            assert p["fee_paid"] == 500
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_blacklist(self):
        db = await make_db()
        try:
            bid = await db.add_to_blacklist("npub_bad", "txid:vout", "ghosting")
            assert bid is not None
            assert await db.is_blacklisted("npub_bad")
            assert await db.is_blacklisted("npub_bad", "txid:vout")
            assert not await db.is_blacklisted("npub_good")
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_count_active_participant_mixes(self):
        db = await make_db()
        try:
            mid1 = await db.create_mix(1_000_000, 3)
            mid2 = await db.create_mix(1_000_000, 3)
            await db.add_participant(mid1, "npub_hex", "")
            await db.add_participant(mid2, "npub_hex", "")
            count = await db.count_active_participant_mixes("npub_hex")
            assert count == 2
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_add_utxo_duplicate_outpoint_is_blocked_by_unique_constraint(self):
        """S9: the schema's UNIQUE(txid, vout) is the defense of last
        resort against duplicate-outpoint inserts that slip past the
        coordinator's is_utxo_used check (e.g. two parallel /commit
        DMs racing between the check and the insert)."""
        import sqlite3
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid1 = await db.add_participant(mid, "n1", "")
            pid2 = await db.add_participant(mid, "n2", "")
            await db.add_utxo(pid1, "abc", 0, 100_000)
            with pytest.raises((sqlite3.IntegrityError, Exception)) as exc_info:
                await db.add_utxo(pid2, "abc", 0, 100_000)
            # Sanity check the error mentions the constraint.
            assert "UNIQUE" in str(exc_info.value) or "constraint" in str(exc_info.value).lower()
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_unique_constraint_allows_distinct_outpoints(self):
        """Same (txid, vout) → blocked. Same txid, different vout → fine."""
        db = await make_db()
        try:
            mid = await db.create_mix(1_000_000, 3)
            pid = await db.add_participant(mid, "n1", "")
            await db.add_utxo(pid, "abc", 0, 100_000)
            await db.add_utxo(pid, "abc", 1, 100_000)  # different vout: OK
            utxos = await db.get_utxos_by_participant(pid)
            assert len(utxos) == 2
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_reconnect_to_existing_db_file_does_not_crash(self):
        """C1 regression guard: schema.sql historically used CREATE TABLE
        without IF NOT EXISTS, which made the bot crash on every restart
        once bot.db existed. Verified-failing-then-fixed."""
        import tempfile
        import src.database as db_mod
        schema_path = os.path.join(os.path.dirname(__file__), "..", "src", "schema.sql")
        db_mod.SCHEMA_PATH = schema_path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            path = f.name
        _db_paths.append(path)

        db1 = db_mod.Database(path)
        await db1.connect()
        # Write some state so we can also verify it survives across reconnects.
        mid = await db1.create_mix(output_size=1_000_000, min_participants=3)
        await db1.close()

        # The historically-broken path: connect again to the same file.
        db2 = db_mod.Database(path)
        await db2.connect()  # must not raise
        try:
            survived = await db2.get_mix(mid)
            assert survived is not None, "data didn't survive the reconnect"
        finally:
            await db2.close()
