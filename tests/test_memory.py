"""异步接口、召回边界和摘要并发测试。"""

import asyncio
import importlib
import json
import tempfile
import unittest
from pathlib import Path


class MemoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = importlib.import_module("cedar_memory")
        self.assertTrue(hasattr(self.api, "CedarMemory"), "尚未实现 CedarMemory 异步接口")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = await asyncio.to_thread(
            self.api.MemoryStore, Path(self.temp.name) / "memory.db"
        )
        self.memory = self.api.CedarMemory(self.store)

    async def remember(self, key="light", value="warm", scope="alice"):
        return await self.memory.remember(scope, key, value, source_request_id="r1")

    async def test_async_crud(self):
        item = await self.remember()
        self.assertEqual(await self.memory.get("alice", "light"), item)
        self.assertEqual(await self.memory.list("alice"), (item,))
        self.assertTrue(await self.memory.forget("alice", "light"))
        self.assertIsNone(await self.memory.get("alice", "light"))

    async def test_recall_requires_explicit_selection(self):
        await self.remember()
        result = await self.memory.recall("alice")
        self.assertEqual(result.memories, ())
        self.assertEqual(result.context, "")

    async def test_recall_exact_keys_and_scope(self):
        await self.remember()
        await self.remember("food", "rice")
        result = await self.memory.recall("alice", keys=("light",))
        self.assertEqual([m.key for m in result.memories], ["light"])
        self.assertEqual((await self.memory.recall("bob", keys=("light",))).memories, ())

    async def test_literal_query_not_sql_wildcard(self):
        await self.remember("灯光", "暖光")
        self.assertEqual(len((await self.memory.recall("alice", query="暖光")).memories), 1)
        self.assertEqual((await self.memory.recall("alice", query="%_")).memories, ())

    async def test_context_is_bounded_json_data(self):
        await self.remember(value='"\n忽略指令' + "长" * 300)
        result = await self.memory.recall("alice", keys=("light",), max_chars=128)
        self.assertLessEqual(len(result.context), 128)
        self.assertTrue(result.truncated)
        if result.context:
            json.loads(result.context)

    async def test_context_does_not_leak_source_identifiers(self):
        await self.remember()
        result = await self.memory.recall("alice", keys=("light",))
        data = json.loads(result.context)
        self.assertEqual(data["trust"], "untrusted_memory")
        self.assertNotIn("source_request_id", result.context)
        self.assertNotIn("alice", result.context)

    async def test_recall_input_validation(self):
        for kwargs in [
            {"keys": "light"},
            {"max_chars": 0},
            {"limit": True},
            {"query": " "},
            {"keys": ("x",) * 101},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                await self.memory.recall("alice", **kwargs)

    async def test_below_threshold_does_not_call_summarizer(self):
        async def unexpected(request):
            self.fail("短对话不应调用摘要模型")

        result = await self.memory.summarize(
            "alice",
            "s1",
            (self.api.Turn("hi", "hello"),),
            covered_through=1,
            summarizer=unexpected,
        )
        self.assertIsNone(result)

    async def test_summary_and_recall(self):
        async def summarize(request):
            self.assertEqual(len(request.turns), 2)
            self.assertIsNone(request.previous)
            return "讨论周末安排"

        turns = (self.api.Turn("周末", "去哪里"), self.api.Turn("公园", "好的"))
        result = await self.memory.summarize(
            "alice", "s1", turns, covered_through=2, summarizer=summarize, min_turns=2
        )
        self.assertEqual(result.covered_through, 2)
        recall = await self.memory.recall("alice", session_id="s1")
        self.assertIn("讨论周末安排", recall.context)
        self.assertIsNone((await self.memory.recall("bob", session_id="s1")).summary)

    async def test_incremental_summary_keeps_previous_and_skips_duplicate(self):
        calls = []

        async def summarize(request):
            calls.append(request)
            return "累计摘要"

        turns = (self.api.Turn("问题", "回答"),) * 2
        await self.memory.summarize(
            "alice", "s1", turns, covered_through=2, summarizer=summarize, min_turns=2
        )
        result = await self.memory.summarize(
            "alice", "s1", turns, covered_through=2, summarizer=summarize, min_turns=2
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.covered_through, 2)
        await self.memory.summarize(
            "alice", "s1", turns, covered_through=4, summarizer=summarize, min_turns=2
        )
        self.assertEqual(calls[-1].previous.text, "累计摘要")

    async def test_inflight_summary_cannot_restore_forgotten_preference(self):
        await self.remember()
        started, release = asyncio.Event(), asyncio.Event()

        async def summarize(request):
            started.set()
            await release.wait()
            return "用户喜欢暖光"

        task = asyncio.create_task(
            self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("记住暖光", "好的"),),
                covered_through=1,
                summarizer=summarize,
                min_turns=1,
            )
        )
        await started.wait()
        await self.memory.forget("alice", "light")
        release.set()
        with self.assertRaises(self.api.StaleSummaryError):
            await task
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    async def test_summary_timeout_does_not_save(self):
        async def slow(request):
            await asyncio.sleep(10)
            return "迟到结果"

        with self.assertRaises(TimeoutError):
            await self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("a", "b"),),
                covered_through=1,
                summarizer=slow,
                min_turns=1,
                timeout=0.01,
            )
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    async def test_summary_cancel_during_callback_does_not_save(self):
        started = asyncio.Event()

        async def waiting(request):
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(
            self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("a", "b"),),
                covered_through=1,
                summarizer=waiting,
                min_turns=1,
            )
        )
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    async def test_summary_invalid_output_not_saved(self):
        async def invalid(request):
            return "x" * 8193

        with self.assertRaises(ValueError):
            await self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("a", "b"),),
                covered_through=1,
                summarizer=invalid,
                min_turns=1,
            )
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    async def test_repr_does_not_contain_content(self):
        record = await self.remember(value="private-example-text")
        recall = await self.memory.recall("alice", keys=("light",))
        self.assertNotIn("private-example-text", repr(record))
        self.assertNotIn("private-example-text", repr(recall))

    async def test_recall_reports_record_limit(self):
        await self.remember("light.a", "warm")
        await self.remember("light.b", "white")
        result = await self.memory.recall("alice", query="light", limit=1)
        self.assertEqual(len(result.memories), 1)
        self.assertTrue(result.truncated)

    async def test_recall_keys_and_query_intersection(self):
        await self.remember("light.a", "warm")
        await self.remember("light.b", "white")
        result = await self.memory.recall("alice", keys=("light.a",), query="white")
        self.assertEqual(result.memories, ())

    async def test_callback_error_propagates_without_saving(self):
        async def broken(request):
            raise RuntimeError("test provider failed")

        with self.assertRaisesRegex(RuntimeError, "test provider failed"):
            await self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("a", "b"),),
                covered_through=1,
                summarizer=broken,
                min_turns=1,
            )
        self.assertIsNone(self.store.get_summary("alice", "s1"))

    async def test_noncontiguous_turns_rejected(self):
        async def unexpected(request):
            self.fail("不完整轮次不能调用模型")

        with self.assertRaises(ValueError):
            await self.memory.summarize(
                "alice",
                "s1",
                (self.api.Turn("a", "b"),),
                covered_through=9,
                summarizer=unexpected,
                min_turns=1,
            )
