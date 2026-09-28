"""存储契约测试：全部使用真实临时数据库。"""

import importlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module("cedar_memory")
        self.assertTrue(hasattr(self.api, "MemoryStore"), "尚未实现 MemoryStore 公共接口")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "memory.db"
        self.store = self.api.MemoryStore(self.path)

    def put(self, scope="alice", key="light", value="warm"):
        return self.store.put(scope, key, value, source_request_id="req-1")

    def test_persists_after_reopen(self):
        original = self.put()
        restored = self.api.MemoryStore(self.path).get("alice", "light")
        self.assertEqual(restored, original)

    def test_scope_isolation(self):
        self.put()
        self.assertIsNone(self.store.get("bob", "light"))
        self.assertEqual(self.store.list("bob"), ())
        self.assertFalse(self.store.delete("bob", "light"))

    def test_update_retains_id_and_replaces_value(self):
        old = self.put()
        new = self.put(value="white")
        self.assertEqual(old.id, new.id)
        self.assertEqual(new.value, "white")
        self.assertEqual(len(self.store.list("alice")), 1)
        self.assertEqual(self.store.revision("alice"), 2)

    def test_delete_and_idempotent_missing(self):
        self.put()
        self.assertTrue(self.store.delete("alice", "light"))
        self.assertIsNone(self.store.get("alice", "light"))
        self.assertFalse(self.store.delete("alice", "light"))

    def test_list_is_bounded_and_stable(self):
        self.put(key="z")
        self.put(key="a")
        self.assertEqual([m.key for m in self.store.list("alice", limit=1)], ["a"])

    def test_validation(self):
        for scope, key, value in [
            ("", "x", "y"),
            (" x", "x", "y"),
            ("a", "", "y"),
            ("a", "x", " "),
            ("a", "x", "x" * 4097),
            ("a", "x\x00", "y"),
        ]:
            with self.subTest(scope=scope, key=key), self.assertRaises(ValueError):
                self.put(scope, key, value)
        for limit in [0, -1, 1001, True]:
            with self.assertRaises(ValueError):
                self.store.list("alice", limit=limit)

    def test_sql_text_is_data(self):
        key = "'; DROP TABLE memories; --"
        self.put(key=key)
        self.assertEqual(self.store.get("alice", key).value, "warm")

    def test_summary_persists_and_is_session_scoped(self):
        s = self.store.save_summary(
            "alice", "s1", "讨论灯光", covered_through=6, expected_revision=0
        )
        self.assertEqual(self.api.MemoryStore(self.path).get_summary("alice", "s1"), s)
        self.assertIsNone(self.store.get_summary("alice", "s2"))
        self.assertIsNone(self.store.get_summary("bob", "s1"))

    def test_change_invalidates_all_scope_summaries(self):
        self.store.save_summary("alice", "s1", "旧内容", covered_through=6, expected_revision=0)
        self.store.save_summary("bob", "s1", "其他人", covered_through=6, expected_revision=0)
        self.put()
        self.assertIsNone(self.store.get_summary("alice", "s1"))
        self.assertIsNotNone(self.store.get_summary("bob", "s1"))

    def test_deleted_memory_cannot_be_revived_by_inflight_summary(self):
        self.put()
        revision = self.store.revision("alice")
        self.store.delete("alice", "light")
        with self.assertRaises(self.api.StaleSummaryError):
            self.store.save_summary(
                "alice", "s1", "旧偏好", covered_through=8, expected_revision=revision
            )
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    def test_older_summary_cannot_replace_newer(self):
        self.store.save_summary("alice", "s1", "新", covered_through=10, expected_revision=0)
        with self.assertRaises(self.api.StaleSummaryError):
            self.store.save_summary("alice", "s1", "旧", covered_through=8, expected_revision=0)

    def test_clear_summary(self):
        self.store.save_summary("alice", "s1", "摘要", covered_through=6, expected_revision=0)
        self.assertTrue(self.store.clear_summary("alice", "s1"))
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    def test_concurrent_writers(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: self.put(key=f"key-{i}"), range(20)))
        self.assertEqual(len(self.store.list("alice")), 20)
        self.assertEqual(self.store.revision("alice"), 20)

    def test_unknown_database_version_rejected(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("PRAGMA user_version = 99")
        with self.assertRaises(self.api.MemoryStorageError):
            self.api.MemoryStore(self.path)

    def test_foreign_database_rejected(self):
        other = Path(self.temp.name) / "foreign.db"
        with closing(sqlite3.connect(other)) as conn:
            conn.execute("CREATE TABLE important (value TEXT)")
            conn.commit()
        with self.assertRaises(self.api.MemoryStorageError):
            self.api.MemoryStore(other)

    def test_memory_path_rejected(self):
        with self.assertRaises(ValueError):
            self.api.MemoryStore(":memory:")

    def test_invalid_unicode_rejected_before_database(self):
        with self.assertRaises(ValueError) as caught:
            self.put(value="broken\ud800")
        self.assertIs(type(caught.exception), ValueError)
        self.assertEqual(self.store.revision("alice"), 0)

    def test_locked_database_has_safe_error(self):
        store = self.api.MemoryStore(self.path, timeout=0.02)
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            with self.assertRaises(self.api.MemoryStorageError) as caught:
                store.put("alice", "key", "private-payload", source_request_id="r1")
            self.assertNotIn("private-payload", str(caught.exception))

    def test_clear_rejects_inflight_summary(self):
        revision = self.store.revision("alice")
        self.store.clear_summary("alice", "s1")
        with self.assertRaises(self.api.StaleSummaryError):
            self.store.save_summary(
                "alice", "s1", "过期", covered_through=1, expected_revision=revision
            )

    def test_invalid_timeout(self):
        for value in [0, -1, float("inf"), float("nan"), True]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.api.MemoryStore(self.path, timeout=value)
