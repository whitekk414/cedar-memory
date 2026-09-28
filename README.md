# cedar-memory

Cedar 的独立 Python 本地记忆包。首版使用现有业务 Session + SQLite 持久记忆 + 按需摘要的组合：本包负责后两项，不接管 Session、Agent、设备控制或请求完成协议。

## 已实现范围

- 显式偏好保存、读取、列举、更新、删除，按业务传入的 scope 严格隔离。
- 每次操作独立 SQLite 连接，短事务、WAL、锁等待超时和数据库版本检查。
- 按键或字面子串召回，输出受字符预算约束的 JSON 背景数据。
- 摘要持久化、按新增轮数触发、异步模型回调、超时及过期结果拒绝。
- 偏好新增/修改/删除时使该 scope 的全部摘要失效，防止在途旧摘要写回。
- Python 3.11+，运行时无第三方依赖；无 import-time 文件或网络 I/O。

首版不包含：自动自然语言记忆提取、向量检索、内置 LLM 客户端、身份认证、密钥/PII 检测、数据库加密、云端同步、多 Station 同步和待确认动作持久化。

## 安装与测试

从仓库本地安装，尚未发布到 PyPI：

```bash
python -m venv .venv
# Ubuntu
source .venv/bin/activate
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1
python -m pip install -e .
python -m unittest discover -s tests -v
python examples/basic.py
```

维护者可安装 `python -m pip install -e ".[dev]"`，执行 `python -m ruff check src tests examples` 和 `python -m ruff format --check src tests examples`。开发工具不属于运行时依赖。

## 最小用法

```python
import asyncio
from cedar_memory import CedarMemory, MemoryStore

async def main():
    # 应用启动阶段显式初始化；生产路径由部署配置提供。
    store = await asyncio.to_thread(MemoryStore, "data/memory.db")
    memory = CedarMemory(store)
    # scope 来自服务端可信绑定，不直接信任客户端传入的用户 ID。
    scope = "station:demo/person:demo"
    await memory.remember(scope, "lighting.preference", "暖光", source_request_id="req-demo")
    result = await memory.recall(scope, keys=("lighting.preference",), max_chars=2048)
    print(result.context)
    await memory.forget(scope, "lighting.preference")

asyncio.run(main())
```

SDK 不能判断自然语言中的“记住”是否真实授权。业务方判断/确认后显式调用 `remember`，不得把每轮聊天自动写入偏好。同键更新保留 ID 并覆盖旧值，不保留历史正文。

## 公共接口

| 接口 | 行为 |
| --- | --- |
| `MemoryStore(path, timeout=5)` | 同步初始化本地文件；拒绝内存库、未知版本和其他应用的数据库 |
| `CedarMemory(store)` | 异步业务门面，不自行创建网络连接 |
| `await remember(scope, key, value, source_request_id=...)` | 显式新增/覆盖，返回 MemoryRecord |
| `await get(scope, key)` | 返回 MemoryRecord 或 None |
| `await list(scope, limit=100)` | 按 key 排序返回最多 limit 条；不是分页接口 |
| `await forget(scope, key)` | 返回是否存在并删除；即使键不存在也失效摘要 |
| `await recall(scope, keys=(), query=None, session_id=None, limit=5, max_chars=4096)` | 一致快照、预算内记录和摘要、revision、context、truncated |
| `await summarize(scope, session_id, turns, covered_through=..., summarizer=..., min_turns=6, timeout=10)` | 达阈值后调用异步回调并保存，未达阈值返回已有摘要或 None |
| `store.clear_summary(scope, session_id)` | 清理摘要；首版保守使整个 scope 的摘要失效，返回目标摘要是否存在 |
| `store.revision(scope)` | 当前 scope 修订号，用于业务侧检测快照是否过期 |

同步低层接口：`put/get/list/delete/get_summary/save_summary/snapshot`，均与 scope 绑定。`save_summary` 必须传 expected_revision，且只接受比现存摘要更新的 covered_through。

召回规则：不传 keys/query 时不召回偏好；提供 session_id 才读取该会话摘要。keys 与 query 同时提供时取交集；query 是完整字面子串，ASCII 不区分大小写，不是中文分词或语义理解。不存在的键直接返回空，不会调用模型猜测。

`max_chars` 是 Unicode 字符数，不是 token 数。预算不足时省略整条，绝不截断值或 JSON；`truncated=True` 表示条数或长度发生省略。context 只含 key/value、摘要和不可信数据标识，不带来源 ID。JSON 编码和标识不是 Prompt Injection 防护，业务仍须按不可信背景处理。

## 按需摘要

```python
from cedar_memory import SummaryRequest, Turn

async def summarize_with_your_model(request: SummaryRequest) -> str:
    # 示例替身：真实接入时将 turns 和 previous.text 交给业务的异步模型客户端。
    # 模型客户端需限制输出、设置网络超时；不要记录原始请求正文。
    return "本轮讨论客厅灯光；暂无待执行动作。"

# 在上文 main 的 memory/scope 上调用：
# result = await memory.summarize(
#     scope, "chat-1", (Turn("客厅灯太亮了", "需要调暗吗？"),),
#     covered_through=1, min_turns=1, summarizer=summarize_with_your_model,
# )
```

`turns` 必须是上次有效摘要之后的连续新增轮次；`covered_through` 是累计轮次，不能传 Session 滑动窗口长度。旧摘要通过 `request.previous` 提供，回调负责结合 previous.text 与新增轮次生成累计摘要。单批最多 100 轮、65536 字符，摘要最多 8192 字符。

偏好变更清空摘要后，业务必须清理相应旧历史，重置该摘要序列的累计计数，再提交新的安全轮次。不能拿包含已删除偏好的旧历史重新生成摘要。连续轮次检查会拒绝只提交滑动窗口尾部却声称覆盖全部历史的请求。

## 一致性、安全与异常边界

- scope 隔离是数据访问约定，不是鉴权。不同进程可访问同一个本机库，但相同 scope 才共享记忆；不会共享 Nebula 的进程内 Session。
- `StaleSummaryError`：生成期间记忆变化或别的摘要已覆盖同样/更多轮次；丢弃旧结果，不自动重试旧输入。
- `MemoryStorageError`：文件、锁或版本问题；业务决定降级，不能谎报“已记住”。不吞异常或自行上报。
- `ValueError`：参数或输出不符合约定；`TimeoutError`：摘要回调超时。回调自身异常向业务传播，业务不要将可能带原文的异常直接记入日志。
- `asyncio.to_thread` 的等待被取消后，已开始的 SQLite 短事务仍可能提交。取消不等于撤销写入，重读确认结果；删除后重新写入的显式调用属于新的操作。
- 摘要模型必须配合异步取消；不合作的回调不能依赖外层超时强行终止。数据库操作另受 store timeout 约束，因此 summarize 的 timeout 不是总端到端截止时间。
- recall 是调用时快照；删除不能收回已经交给模型的文本。业务应按用户/会话串行化相关请求，清理缓存及历史，必要时取消在途生成。
- 不存储密钥、令牌、完整敏感 payload。SDK 不自动识别或遮盖它们；SQLite 默认明文，部署方限制数据目录权限，按需要使用磁盘加密。
- 删除是逻辑数据删除，不保证旧 WAL、备份或磁盘扇区物理擦除；备份清理和保留周期由部署方制定。
- 设备状态继续查设备服务；pending、确认口令和设备执行结果不能从摘要恢复后自动执行。

详细接入边界见 [接入说明](docs/integration.md)。示例使用虚构数据，无公司业务代码与凭证。
