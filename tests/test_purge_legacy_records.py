"""Destructive cleanup must preserve ownership and offer recoverable originals."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "purge_legacy_records", Path(__file__).resolve().parents[1] / "tools/purge_legacy_records.py"
)
purge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(purge)


class PurgeLegacyRecordsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.ledger = self.root / "ledger.sqlite3"
        self.journal = self.root / "trades.jsonl"
        self.archive = self.root / "archive"
        self.connection = sqlite3.connect(self.ledger)
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            PRAGMA foreign_keys=ON;
            CREATE TABLE packages(package_id TEXT PRIMARY KEY, strategy_id TEXT,
                state TEXT, intent TEXT, position_id TEXT, trade_id TEXT,
                replacement_of TEXT, terminal_status TEXT);
            CREATE TABLE legs(order_id TEXT PRIMARY KEY,
                package_id TEXT REFERENCES packages(package_id), asset_key TEXT,
                filled_signed_quantity INTEGER);
            CREATE TABLE positions(position_id TEXT PRIMARY KEY, strategy_id TEXT, trade_id TEXT);
            CREATE TABLE journal_events(package_id TEXT REFERENCES packages(package_id), event TEXT);
            CREATE TABLE fee_evidence(identity TEXT, package_id TEXT, position_id TEXT, trade_id TEXT);
            CREATE TABLE settlement_reconciliations(position_id TEXT, strategy_id TEXT, trade_id TEXT);
            CREATE TABLE outbox(outbox_id INTEGER PRIMARY KEY, status TEXT, payload TEXT);
            CREATE TABLE system_state(key TEXT PRIMARY KEY, value TEXT);
            INSERT INTO system_state VALUES('panic_halted','true');
        """)
        self.add_position("completed", "IC_V06_OLD", "closed", 1)
        self.add_package("completed-close", "IC_V06_OLD", "closed", -1, position="completed")
        self.add_position("pending", "IC_V01_OLD", "submitted", 0)
        self.add_position("held", "IC_V20_OLD", "open", 1)
        self.add_package("held-cancel", "IC_V20_OLD", "canceled", 0, position="held")
        self.add_position("settlement", "IC_V07_OLD", "awaiting_settlement", 1)
        self.add_position("expired", "IC_V08_OLD", "expired", 1)
        self.add_position("current", "IC_V26_CURRENT", "closed", 1)
        self.add_package("current-close", "IC_V26_CURRENT", "closed", -1, position="current")
        self.connection.commit()
        rows = [{"strategy": owner, "trade_id": position, "event": "ENTRY"}
                for position, owner in self.connection.execute("SELECT position_id,strategy_id FROM positions")]
        rows += [{"strategy": "IC_V03_OLD", "trade_id": "journal-only", "event": "EXIT"}]
        self.journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
        self.original_journal = self.journal.read_bytes()

    def add_position(self, key, owner, state, quantity):
        self.connection.execute("INSERT INTO positions VALUES(?,?,?)", (key, owner, key))
        self.add_package(key, owner, state, quantity, position=key)

    def add_package(self, key, owner, state, quantity, *, position=None, trade=None):
        self.connection.execute("INSERT INTO packages VALUES(?,?,?,?,?,?,?,?)",
                                (key, owner, state, "open", position, trade or position, None, None))
        self.connection.execute("INSERT INTO legs VALUES(?,?,?,?)", (key, key, "contract", quantity))
        self.connection.execute("INSERT INTO journal_events VALUES(?,?)", (key, "ENTRY"))

    def plan(self):
        return purge.make_plan(self.ledger, [self.journal], {"V01", "V03", "V06", "V07", "V08", "V20"})

    def tables(self):
        with sqlite3.connect(self.ledger) as connection:
            connection.row_factory = sqlite3.Row
            return purge.read_tables(connection)

    def test_apply_preserves_current_pending_held_settlement_and_recovery_state(self):
        before = self.tables()
        plan = self.plan()
        self.assertEqual(set(plan["package_ids"]), {"completed", "completed-close", "expired"})
        self.assertEqual(set(plan["position_ids"]), {"completed", "expired"})
        self.assertEqual(plan["journals"][0]["removed_events"], 3)
        report = purge.apply_plan(plan, self.archive, writers_stopped=True)
        self.assertTrue(report["inventory_unchanged"])
        after = self.tables()
        for table, key in (("packages", "package_id"), ("positions", "position_id")):
            removed = set(plan["package_ids" if table == "packages" else "position_ids"])
            self.assertEqual(after[table], [row for row in before[table] if row[key] not in removed])
        self.assertEqual(before["system_state"], after["system_state"])
        owners = {json.loads(line)["strategy"] for line in self.journal.read_text().splitlines()}
        self.assertEqual(owners, {"IC_V01_OLD", "IC_V07_OLD", "IC_V20_OLD", "IC_V26_CURRENT"})
        with sqlite3.connect(self.archive / "ledger.sqlite3") as backup:
            backup.row_factory = sqlite3.Row
            self.assertEqual(purge.read_tables(backup), before)
        self.assertEqual((self.archive / "journal-0.jsonl").read_bytes(), self.original_journal)
        rerun = self.plan()
        self.assertEqual(rerun["package_ids"], [])
        self.assertEqual(rerun["position_ids"], [])
        self.assertEqual(rerun["journals"][0]["removed_events"], 0)

    def test_refuses_stale_plan_without_touching_records(self):
        plan = self.plan()
        self.journal.write_bytes(self.original_journal + b"\n")
        before = self.tables()
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            purge.apply_plan(plan, self.archive, writers_stopped=True)
        self.assertEqual(self.tables(), before)
        self.assertFalse(self.archive.exists())

    def test_apply_requires_stopped_writers(self):
        with self.assertRaisesRegex(ValueError, "Stop all record writers"):
            purge.apply_plan(self.plan(), self.archive)
        self.assertFalse(self.archive.exists())

    def test_journal_write_failure_rolls_back_ledger_and_journal(self):
        before = self.tables()
        plan = self.plan()
        actual_write = purge.atomic_write
        calls = []

        def fail_after_replace(path, data):
            actual_write(path, data)
            calls.append(path)
            if len(calls) == 1:
                raise OSError("injected write failure")

        with patch.object(purge, "atomic_write", side_effect=fail_after_replace):
            with self.assertRaisesRegex(OSError, "injected"):
                purge.apply_plan(plan, self.archive, writers_stopped=True)
        self.assertEqual(self.tables(), before)
        self.assertEqual(self.journal.read_bytes(), self.original_journal)

    def test_ambiguous_cross_owner_chain_is_retained(self):
        self.add_package("cross-owner", "IC_V26_CURRENT", "closed", 0, trade="completed")
        self.connection.commit()
        self.assertNotIn("completed", self.plan()["package_ids"])
        self.assertNotIn("completed-close", self.plan()["package_ids"])

    def test_terminal_state_with_unbalanced_fills_is_retained(self):
        self.add_position("misreported", "IC_V06_OLD", "canceled", 1)
        self.connection.commit()
        self.assertNotIn("misreported", self.plan()["package_ids"])

    def test_malformed_journal_refuses_planning(self):
        self.journal.write_bytes(self.original_journal + b"{broken\n")
        with self.assertRaisesRegex(ValueError, "Malformed journal"):
            self.plan()

    def test_pending_outbox_refuses_apply(self):
        self.connection.execute("INSERT INTO outbox VALUES(1,'pending','{}')")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "pending journal outbox"):
            purge.apply_plan(self.plan(), self.archive, writers_stopped=True)
        self.assertFalse(self.archive.exists())


if __name__ == "__main__":
    unittest.main()
