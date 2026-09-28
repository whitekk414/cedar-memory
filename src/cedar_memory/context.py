"""把快照编码为有限长度的背景数据，不截断 JSON 或记忆值。"""

import json

from .models import MemoryRecord, RecallResult, SessionSummary
from .validation import integer


def build_context(
    records: tuple[MemoryRecord, ...],
    summary: SessionSummary | None,
    revision: int,
    truncated: bool,
    *,
    max_chars: int,
) -> RecallResult:
    integer(max_chars, "max_chars", 128, 32768)
    selected: list[MemoryRecord] = []
    selected_summary = None

    def render(items, summary_text):
        return json.dumps(
            {
                "trust": "untrusted_memory",
                "preferences": items,
                "summary": summary_text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    # 优先放最新结构化偏好，再放旧摘要；放不下时省略整条，避免截断改变含义。
    items = []
    for record in records:
        candidate = {"key": record.key, "value": record.value}
        if len(render([*items, candidate], None)) <= max_chars:
            items.append(candidate)
            selected.append(record)
        else:
            truncated = True
    if summary is not None:
        if len(render(items, summary.text)) <= max_chars:
            selected_summary = summary
        else:
            truncated = True
    context = (
        render(items, selected_summary.text if selected_summary else None)
        if items or selected_summary
        else ""
    )
    return RecallResult(tuple(selected), selected_summary, revision, context, truncated)
