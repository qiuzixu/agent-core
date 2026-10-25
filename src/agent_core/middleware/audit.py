"""审计日志中间件。

记录"谁在什么时间用了什么工具做了什么"的不可变审计记录。
对低空业务（涉及飞行安全）这是合规刚需。

审计记录包含：
- 操作者（user_id / tenant_id）
- 操作类型（model_call / tool_call）
- 工具名 + 参数摘要
- 结果摘要（成功/失败）
- 时间戳

审计记录追加写入 JSONL 文件（每行一个 JSON 对象），
不可修改，支持后续导入 Elasticsearch / 数据库进行查询。

配置：
    AGENT_AUDIT_ENABLED: 是否启用（默认 true）
    AGENT_AUDIT_LOG_PATH: 审计日志文件路径（默认 ./audit.log）
    AGENT_AUDIT_LOG_TOOLS: 是否记录工具调用（默认 true）
    AGENT_AUDIT_LOG_MODELS: 是否记录 LLM 调用（默认 true）
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

__all__ = ["AuditConfig", "AuditMiddleware", "AuditRecord"]


@dataclass
class AuditConfig:
    """审计日志配置。"""

    enabled: bool = True
    log_path: str = "./audit.log"
    log_tools: bool = True
    log_models: bool = True
    # 参数值截断长度（防止大 payload 写入日志）
    max_value_length: int = 200


@dataclass
class AuditRecord:
    """一条审计记录。"""

    timestamp: float
    user_id: str
    tenant_id: str
    action: str  # "model_call" | "tool_call"
    detail: str
    success: bool
    session_id: str = ""
    tool_name: str = ""
    tool_args_summary: str = ""
    result_summary: str = ""
    latency_ms: float = 0.0

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


def _truncate(value: Any, max_len: int = 200) -> str:
    """截断值用于审计日志。"""
    text = str(value or "")
    if len(text) > max_len:
        return text[:max_len] + "..."
    return text


class AuditMiddleware(Middleware):
    priority = 31
    """审计日志中间件。

    在 before_model / after_model / before_tool / after_tool 各阶段写入审计记录。
    审计文件为 JSONL 格式（每行一个 JSON 对象），追加写入，不可修改。
    """

    def __init__(self, config: AuditConfig | None = None) -> None:
        self._config = config or AuditConfig()
        self._start_time: float = 0.0
        self._tool_start_time: float = 0.0
        # 确保 audit 目录存在
        log_dir = os.path.dirname(self._config.log_path)
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    def _write_record(self, record: AuditRecord) -> None:
        """追加写入一条审计记录。"""
        try:
            with open(self._config.log_path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except OSError:
            logger.exception("[Audit] 写入审计日志失败: %s", self._config.log_path)

    def _get_identity(self, context: MiddlewareContext) -> tuple[str, str, str]:
        """从 context.metadata 提取操作者身份。"""
        meta = context.metadata
        return (
            meta.get("user_id", "anonymous"),
            meta.get("tenant_id", "default"),
            meta.get("session_id", ""),
        )

    # ── before_model ─────────────────────────────────────────

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        self._start_time = time.monotonic()
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    # ── after_model ──────────────────────────────────────────

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not self._config.log_models:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        user_id, tenant_id, session_id = self._get_identity(context)
        latency_ms = (time.monotonic() - self._start_time) * 1000 if self._start_time else 0.0
        r = context.llm_response
        tool_names = [tc.get("name", "?") for tc in (r.tool_calls if r else [])]
        detail = f"LLM iteration {context.iteration}, tools={tool_names or 'none'}"
        record = AuditRecord(
            timestamp=time.time(),
            user_id=user_id,
            tenant_id=tenant_id,
            action="model_call",
            detail=_truncate(detail),
            success=r is not None,
            session_id=session_id,
            result_summary=_truncate(r.content if r else None, self._config.max_value_length),
            latency_ms=round(latency_ms, 2),
        )
        self._write_record(record)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    # ── before_tool ──────────────────────────────────────────

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        self._tool_start_time = time.monotonic()
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    # ── after_tool ───────────────────────────────────────────

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or not self._config.log_tools:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        user_id, tenant_id, session_id = self._get_identity(context)
        latency_ms = (time.monotonic() - self._tool_start_time) * 1000 if self._tool_start_time else 0.0
        success = context.tool_result is not None and not context.metadata.get("tool_error")
        record = AuditRecord(
            timestamp=time.time(),
            user_id=user_id,
            tenant_id=tenant_id,
            action="tool_call",
            detail=f"工具 {context.tool_name}",
            success=success,
            session_id=session_id,
            tool_name=context.tool_name or "",
            tool_args_summary=_truncate(json.dumps(context.tool_args, ensure_ascii=False), self._config.max_value_length),
            result_summary=_truncate(context.tool_result, self._config.max_value_length),
            latency_ms=round(latency_ms, 2),
        )
        self._write_record(record)
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)
