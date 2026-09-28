"""不可变结果类型；默认 repr 不展示记忆正文与来源标识。"""

from dataclasses import dataclass, field


class MemoryStorageError(RuntimeError):
    """数据库不可用或格式不兼容；异常信息不包含数据库正文。"""


class StaleSummaryError(RuntimeError):
    """生成期间记忆已变化，或存在覆盖更多轮次的摘要。"""


@dataclass(frozen=True)
class MemoryRecord:
    id: str
    scope: str = field(repr=False)
    key: str = field(repr=False)
    value: str = field(repr=False)
    source_request_id: str = field(repr=False)
    updated_at: str


@dataclass(frozen=True)
class SessionSummary:
    scope: str = field(repr=False)
    session_id: str = field(repr=False)
    text: str = field(repr=False)
    covered_through: int
    revision: int
    updated_at: str


@dataclass(frozen=True)
class Turn:
    user: str = field(repr=False)
    assistant: str = field(repr=False)


@dataclass(frozen=True)
class SummaryRequest:
    """提供给摘要回调的数据；回调仅选取 turns 和 previous.text 发给模型。"""

    turns: tuple[Turn, ...] = field(repr=False)
    previous: SessionSummary | None = field(repr=False)
    max_chars: int = 8192


@dataclass(frozen=True)
class RecallResult:
    """一次一致快照；返回后若发生删除，调用方须丢弃该旧快照。"""

    memories: tuple[MemoryRecord, ...] = field(repr=False)
    summary: SessionSummary | None = field(repr=False)
    revision: int
    context: str = field(repr=False)
    truncated: bool
