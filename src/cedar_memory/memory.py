"""业务侧异步入口；无网络依赖，不改变外部 Session 或触发设备执行。"""

import asyncio
from collections.abc import Awaitable, Callable

from . import validation as v
from .context import build_context
from .models import MemoryRecord, RecallResult, SessionSummary, SummaryRequest, Turn
from .store import MemoryStore

Summarizer = Callable[[SummaryRequest], Awaitable[str]]


class CedarMemory:
    def __init__(self, store: MemoryStore):
        self.store = store

    async def remember(
        self, scope: str, key: str, value: str, *, source_request_id: str
    ) -> MemoryRecord:
        """仅供已确认的显式保存请求调用，不负责自然语言意图判断。"""
        return await asyncio.to_thread(
            self.store.put, scope, key, value, source_request_id=source_request_id
        )

    async def get(self, scope: str, key: str) -> MemoryRecord | None:
        return await asyncio.to_thread(self.store.get, scope, key)

    async def list(self, scope: str, *, limit: int = 100) -> tuple[MemoryRecord, ...]:
        return await asyncio.to_thread(self.store.list, scope, limit=limit)

    async def forget(self, scope: str, key: str) -> bool:
        """删除数据库记录并失效摘要；业务侧仍须清理旧历史、缓存和在途请求。"""
        return await asyncio.to_thread(self.store.delete, scope, key)

    async def recall(
        self,
        scope: str,
        *,
        keys: tuple[str, ...] = (),
        query: str | None = None,
        session_id: str | None = None,
        limit: int = 5,
        max_chars: int = 4096,
    ) -> RecallResult:
        v.integer(max_chars, "max_chars", 128, 32768)
        snapshot = await asyncio.to_thread(
            self.store.snapshot,
            scope,
            keys=keys,
            query=query,
            session_id=session_id,
            limit=limit,
        )
        return build_context(*snapshot, max_chars=max_chars)

    async def summarize(
        self,
        scope: str,
        session_id: str,
        turns: tuple[Turn, ...],
        *,
        covered_through: int,
        summarizer: Summarizer,
        min_turns: int = 6,
        timeout: float = 10.0,
    ) -> SessionSummary | None:
        """按新增轮数触发摘要。turns 必须是上次摘要之后的连续、已清理轮次。

        covered_through 是业务维护的累计轮次，不能用滑动窗口长度代替。
        回调必须是可取消的异步函数；其内部应设置网络超时和输出 token 上限。
        """
        v.integer(min_turns, "min_turns", 1, 100)
        v.integer(covered_through, "covered_through", 1, 2**63 - 1)
        timeout = v.timeout(timeout)
        if not isinstance(turns, tuple) or len(turns) > 100:
            raise ValueError("turns 必须是最多 100 轮的 tuple")
        total_chars = 0
        for turn in turns:
            if not isinstance(turn, Turn):
                raise ValueError("turns 的元素必须是 Turn")
            v.text(turn.user, "turn.user", 8192)
            v.text(turn.assistant, "turn.assistant", 8192)
            total_chars += len(turn.user) + len(turn.assistant)
        if total_chars > 65536:
            raise ValueError("摘要输入总长度不能超过 65536 字符")
        if not callable(summarizer):
            raise ValueError("summarizer 必须是异步回调")
        _, previous, revision, _ = await asyncio.to_thread(
            self.store.snapshot, scope, session_id=session_id
        )
        if previous and covered_through <= previous.covered_through:
            return previous
        previous_end = previous.covered_through if previous else 0
        if covered_through - previous_end != len(turns):
            raise ValueError("turns 必须覆盖自上次有效摘要以来的连续轮次")
        if len(turns) < min_turns:
            return previous
        request = SummaryRequest(turns, previous)
        async with asyncio.timeout(timeout):
            result = await summarizer(request)
        v.text(result, "summary", request.max_chars)
        return await asyncio.to_thread(
            self.store.save_summary,
            scope,
            session_id,
            result,
            covered_through=covered_through,
            expected_revision=revision,
        )
