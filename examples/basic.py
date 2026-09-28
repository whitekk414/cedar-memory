"""不调用网络的可运行示例；全部数据写入临时目录。"""

import asyncio
import tempfile
from pathlib import Path

from cedar_memory import CedarMemory, MemoryStore, SummaryRequest, Turn


async def local_summary(request: SummaryRequest) -> str:
    """仅演示回调契约，不宣称提供真实大模型摘要能力。"""
    return f"已处理 {len(request.turns)} 轮演示对话，讨论客厅照明。"


async def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        store = await asyncio.to_thread(MemoryStore, Path(directory) / "memory.db")
        memory = CedarMemory(store)
        scope = "station:demo/person:demo"
        await memory.remember(scope, "lighting.preference", "暖光", source_request_id="demo-1")
        result = await memory.recall(scope, keys=("lighting.preference",))
        print(result.context)
        await memory.summarize(
            scope,
            "session-demo",
            (Turn("聊聊照明", "好的"),),
            covered_through=1,
            min_turns=1,
            summarizer=local_summary,
        )
        result = await memory.recall(scope, session_id="session-demo")
        print(result.context)
        await memory.forget(scope, "lighting.preference")
        assert await memory.get(scope, "lighting.preference") is None
        assert store.get_summary(scope, "session-demo") is None
        print("偏好删除后，关联摘要已失效。")


if __name__ == "__main__":
    asyncio.run(main())
