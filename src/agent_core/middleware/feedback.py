"""用户反馈闭环中间件 + API 端点。

收集用户对 Agent 回答的 👍/👎 反馈，存储到 JSONL 文件。
为后续评估 Agent 质量、优化 prompt 提供数据基础。

API:
    POST /feedback  {"session_id": "...", "message_id": "...", "rating": "up"|"down", "comment": "..."}

配置：
    AGENT_FEEDBACK_ENABLED: 是否启用（默认 true）
    AGENT_FEEDBACK_PATH: 反馈数据文件路径（默认 ./feedback.jsonl）
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["FeedbackConfig", "FeedbackMiddleware", "FeedbackRecord", "FeedbackStore"]


@dataclass
class FeedbackConfig:
    """反馈配置。"""

    enabled: bool = True
    file_path: str = "./feedback.jsonl"


@dataclass
class FeedbackRecord:
    """一条用户反馈记录。"""

    timestamp: float
    session_id: str
    message_id: str
    user_id: str
    rating: str  # "up" | "down"
    comment: str = ""
    # 上下文信息
    model_name: str = ""
    tool_calls: str = ""
    latency_ms: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class FeedbackStore:
    """反馈存储（JSONL 追加写入）。"""

    def __init__(self, config: FeedbackConfig | None = None) -> None:
        self._config = config or FeedbackConfig()
        log_dir = os.path.dirname(self._config.file_path)
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    def save(self, record: FeedbackRecord) -> None:
        try:
            with open(self._config.file_path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except OSError:
            logger.exception("[Feedback] 写入反馈文件失败")

    def load_recent(self, limit: int = 100) -> list[dict[str, Any]]:
        """读取最近的反馈记录（供分析用）。"""
        if not os.path.isfile(self._config.file_path):
            return []
        records = []
        try:
            with open(self._config.file_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            records.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            return []
        return records[-limit:]

    def summary(self) -> dict[str, Any]:
        """返回反馈统计摘要。"""
        records = self.load_recent(limit=100_000)
        if not records:
            return {"total": 0, "up": 0, "down": 0, "ratio": 0.0}
        up = sum(1 for r in records if r.get("rating") == "up")
        down = sum(1 for r in records if r.get("rating") == "down")
        total = up + down
        return {
            "total": total,
            "up": up,
            "down": down,
            "ratio": round(up / total, 4) if total else 0.0,
        }


class FeedbackMiddleware(Middleware):
    priority = 33
    """记录 LLM 调用上下文（session_id、model、耗时）供反馈关联。

    本中间件本身不处理反馈提交（那是 HTTP API 的事），
    但在每次 LLM 调用后把上下文存到 metadata，
    供 HTTP 层的 /feedback 端点关联使用。
    """

    def __init__(
        self,
        config: FeedbackConfig | None = None,
        store: FeedbackStore | None = None,
    ) -> None:
        self._config = config or FeedbackConfig()
        self._store = store or FeedbackStore(self._config)

    @property
    def store(self) -> FeedbackStore:
        return self._store

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        self._start = time.monotonic()
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        latency_ms = (time.monotonic() - getattr(self, "_start", time.monotonic())) * 1000
        # 把上下文存到 metadata，供 HTTP 层构建 FeedbackRecord
        context.metadata["feedback_context"] = {
            "session_id": context.metadata.get("session_id", ""),
            "user_id": context.metadata.get("user_id", ""),
            "model_name": context.metadata.get("model_name", ""),
            "latency_ms": round(latency_ms, 2),
            "iteration": context.iteration,
            "tool_calls": json.dumps(
                [tc.get("name", "?") for tc in (context.llm_response.tool_calls if context.llm_response else [])],
                ensure_ascii=False,
            ),
        }
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
