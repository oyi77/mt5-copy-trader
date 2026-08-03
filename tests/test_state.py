"""Tests for src.state (EventStore, SharedState, AgentInfo)."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest

from src.state import AgentInfo, EventStore, SharedState


def _event(action="open", **kw) -> dict:
    d = {
        "action": action, "symbol": "XAUUSD", "volume": 0.5, "price": 100.0,
        "sl": 99.0, "tp": 101.0, "master_ticket": 1, "position_type": 0,
        "comment": "", "magic": 0,
    }
    d.update(kw)
    return d


class EventStoreTest(unittest.TestCase):
    def _store(self, **kw) -> EventStore:
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        db = os.path.join(td.name, "events.db")
        store = EventStore(db, **kw)
        self.addCleanup(store.close)
        return store

    def test_append_returns_seq_and_reads_back(self):
        store = self._store()
        self.assertEqual(store.append_event(_event()), 1)
        self.assertEqual(store.get_last_seq(), 1)
        events = store.get_events_since(0)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e["_seq_id"], 1)
        self.assertNotIn("id", e)
        self.assertEqual(e["action"], "open")
        self.assertEqual(e["symbol"], "XAUUSD")
        self.assertEqual(e["master_ticket"], 1)
        self.assertAlmostEqual(e["created_at"], time.time(), delta=60)
        self.assertEqual(store.get_events_since(1), [])

    def test_seq_is_monotonic(self):
        store = self._store()
        self.assertEqual(store.append_event(_event(master_ticket=1)), 1)
        self.assertEqual(store.append_event(_event(master_ticket=2)), 2)
        self.assertEqual(store.get_last_seq(), 2)
        tickets = [e["master_ticket"] for e in store.get_events_since(0)]
        self.assertEqual(tickets, [1, 2])

    def test_fresh_schema_has_no_delivery_table(self):
        store = self._store()
        conn = sqlite3.connect(os.path.join(os.path.dirname(store._db_path), "events.db"))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("events", tables)
        self.assertNotIn("delivery", tables)

    def test_prune_by_max_events_cap(self):
        store = self._store(max_events=3)
        for i in range(5):
            store.append_event(_event(master_ticket=i))
        store._maybe_prune()
        seqs = [e["_seq_id"] for e in store.get_events_since(0)]
        self.assertEqual(seqs, [3, 4, 5])
        self.assertEqual(store.get_last_seq(), 5)

    def test_prune_auto_after_many_appends(self):
        # The cap is enforced on the append path every _PRUNE_INTERVAL (100)
        # appends, so exactly 100 appends -> one prune -> newest 50 remain.
        store = self._store(max_events=50)
        for i in range(100):
            store.append_event(_event(master_ticket=i))
        self.assertEqual(len(store.get_events_since(0)), 50)
        self.assertEqual(store.get_last_seq(), 100)
        seqs = [e["_seq_id"] for e in store.get_events_since(0)]
        self.assertEqual(seqs[0], 51)

    def test_prune_by_retention_days_age(self):
        store = self._store(retention_days=1)
        for i in range(3):
            store.append_event(_event(master_ticket=i))
        conn = store._get_conn()
        conn.execute("UPDATE events SET created_at = ? WHERE id = 1",
                     (time.time() - 3 * 86400,))
        conn.commit()
        store._maybe_prune()
        seqs = [e["_seq_id"] for e in store.get_events_since(0)]
        self.assertEqual(seqs, [2, 3])

    def test_get_events_since_filters_old_events(self):
        store = self._store()
        store.append_event(_event(master_ticket=1))
        store.append_event(_event(master_ticket=2))
        events = store.get_events_since(1)
        self.assertEqual([e["_seq_id"] for e in events], [2])


class SharedStateTest(unittest.TestCase):
    def test_known_tickets_setter_copies(self):
        st = SharedState()
        src = {1, 2}
        st.known_tickets = src
        src.add(3)  # mutating the source must not affect state
        self.assertEqual(st.known_tickets, {1, 2})

    def test_known_tickets_count_in_stats(self):
        st = SharedState()
        st.known_tickets = {1, 2, 3}
        self.assertEqual(len(st.known_tickets), 3)
        self.assertEqual(st.get_stats()["known_tickets"], 3)
        self.assertEqual(st.snapshot()["stats"]["known_tickets"], 3)

    def test_get_agents_returns_copies(self):
        st = SharedState()
        st.register_agent("a", "127.0.0.1")
        st.update_agent_status("a", {
            "balance": 100.0, "equity": 90.0,
            "positions": [{"profit": 1.0}], "position_count": 1,
        })
        snap = st.get_agents()
        self.assertIn("a", snap)
        snap["a"].balance = 999.0
        snap["a"].positions.append({"profit": 99.0})
        snap["a"].ping_history.append(1.5)
        snap["a"].config_overrides["x"] = 1
        live = st.get_agent("a")
        self.assertIsNot(snap["a"], live)
        self.assertEqual(live.balance, 100.0)
        self.assertEqual(live.positions, [{"profit": 1.0}])
        self.assertEqual(live.ping_history, [])
        self.assertEqual(live.config_overrides, {})

    def test_get_agents_empty_when_none_registered(self):
        st = SharedState()
        self.assertEqual(st.get_agents(), {})


class AgentInfoTest(unittest.TestCase):
    def test_to_dict_includes_ping_history(self):
        info = AgentInfo(name="a", ping_history=[1.0, 2.5])
        d = info.to_dict()
        self.assertEqual(d["ping_history"], [1.0, 2.5])
        # mutating the returned list must not affect the info object
        d["ping_history"].append(9.0)
        self.assertEqual(info.ping_history, [1.0, 2.5])

    def test_to_dict_ping_history_defaults_empty(self):
        self.assertEqual(AgentInfo(name="a").to_dict()["ping_history"], [])

    def test_record_agent_latency_appends_and_caps(self):
        st = SharedState()
        st.register_agent("a", "127.0.0.1")
        for i in range(65):
            st.record_agent_latency("a", float(i))
        info = st.get_agent("a")
        self.assertEqual(len(info.ping_history), 60)
        self.assertEqual(info.ping_history[-1], 64.0)
        self.assertEqual(info.latency_ms, 64.0)


if __name__ == "__main__":
    unittest.main()
