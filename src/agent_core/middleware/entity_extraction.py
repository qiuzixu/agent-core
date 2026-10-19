"""实体提取 / 知识图谱（简化版）中间件。

从对话消息中提取结构化实体（坐标 / 设备编号 / 航线 / 飞行编号 / 高度 / 时间 / 距离），
存储到 JSONL 文件，供后续"上次那个地方"等追问场景解析。

简化版不引入 NER 模型或 Neo4j，使用正则模式匹配 + 存储到 JSONL。
后续可升级为 LLM 辅助提取 + PostgreSQL/Neo4j 存储。

配置：
    AGENT_ENTITY_EXTRACTION_ENABLED: 是否启用（默认 true）
    AGENT_ENTITY_STORAGE_PATH: 实体存储文件路径（默认 ./entities.jsonl）
    AGENT_ENTITY_MAX_PER_TURN: 每轮最多提取实体数（默认 10）
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["EntityExtractionConfig", "EntityExtractionMiddleware"]


@dataclass
class EntityExtractionConfig:
    enabled: bool = True
    path: str = "./entities.jsonl"
    max_per_turn: int = 10


@dataclass
class EntityRecord:
    timestamp: float
    session_id: str
    user_id: str
    entity_type: str
    entity_value: str
    source_text: str

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


_ENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # 坐标：N39.9012 / E116.3912
    ("coordinate", re.compile(r"(?:N|S|E|W)\s*(\d{1,3}\.\d{1,6})", re.IGNORECASE)),
    # 设备编号：DR-001 / UAV-123
    ("device_id", re.compile(r"\b((?:DR|UAV|drone)[-_]?\d{1,6})\b", re.IGNORECASE)),
    # 航线编号：RT-A01
    ("route_id", re.compile(r"\b((?:RT|route)[-_]?\w{1,8})\b", re.IGNORECASE)),
    # 飞行编号：FLT-2024-001
    ("flight_id", re.compile(r"\b((?:FLT|flt|flight)[-_]?\d{1,8})\b", re.IGNORECASE)),
    # 高度：500 M / 300m
    ("altitude", re.compile(r"\b(\d{2,5})\s*[Mm]\b")),
    # 时间：14:30 / 3 hours later
    ("time_expr", re.compile(r"\d{1,2}:\d{2}|\d+\s*(?:min|hour|day)s?\s*(?:later|ago)", re.IGNORECASE)),
    # 距离：5 km / 500 m
    ("distance", re.compile(r"\b(\d{1,6}(?:\.\d+)?)\s*(?:km|m)\b", re.IGNORECASE)),
    # 中文地名：2-6字 + 常见后缀
    ("location", re.compile(r"([\u4e00-\u9fff]{2,6}(?:市|区|县|镇|村|路|街|道|桥|公园|广场|机场|站|湖|山|岛))")),
]


class EntityExtractionMiddleware(Middleware):
    """实体提取：从用户消息中提取结构化实体并持久化。"""

    def __init__(self, config: EntityExtractionConfig | None = None) -> None:
        self._config = config or EntityExtractionConfig()
        log_dir = os.path.dirname(self._config.path)
        if log_dir and not os.path.isdir(log_dir):
            os.makedirs(log_dir, exist_ok=True)

    def _write(self, record: EntityRecord) -> None:
        try:
            with open(self._config.path, "a", encoding="utf-8") as f:
                f.write(record.to_json() + "\n")
        except OSError:
            logger.exception("[Entity] 写入失败: %s", self._config.path)

    def _extract(self, text: str) -> list[tuple[str, str]]:
        entities: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for etype, pattern in _ENTITY_PATTERNS:
            for match in pattern.finditer(text):
                value = next((g for g in match.groups() if g), match.group(0))
                value = value.strip()
                if value and (etype, value) not in seen:
                    seen.add((etype, value))
                    entities.append((etype, value))
                    if len(entities) >= self._config.max_per_turn:
                        return entities
        return entities

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled or context.iteration > 0:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        for msg in reversed(context.messages):
            role = getattr(msg, "role", "")
            if role in ("user", "human"):
                content = str(getattr(msg, "content", "") or "")
                if not content:
                    continue
                entities = self._extract(content)
                session_id = context.metadata.get("session_id", "")
                user_id = context.metadata.get("user_id", "")
                for etype, value in entities:
                    self._write(EntityRecord(
                        timestamp=time.time(), session_id=session_id, user_id=user_id,
                        entity_type=etype, entity_value=value, source_text=content[:100],
                    ))
                if entities:
                    context.metadata["extracted_entities"] = [{"type": t, "value": v} for t, v in entities]
                break
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    @staticmethod
    def search_entities(path: str, entity_type: str, limit: int = 10) -> list[dict]:
        """查询最近的指定类型实体（供"上次那个地方"场景使用）。"""
        if not os.path.isfile(path):
            return []
        results: list[dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        if obj.get("entity_type") == entity_type:
                            results.append(obj)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            return []
        return results[-limit:]