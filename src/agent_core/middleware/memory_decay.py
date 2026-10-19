"""长期记忆衰减 / 遗忘策略。

在 MemoryStore 的 TTL（expires_at）之上增加三层遗忘机制：

1. **时间衰减**：记忆分值随时间指数衰减，``score = importance * 0.5 ** (age_hours / half_life_hours)``；
2. **重要性评分**：``metadata.importance``（默认 1.0），写入方可按业务语义调高（如用户显式收藏）；
3. **容量上限**：同一 user/tenant 的记忆超过 ``capacity_per_owner`` 时淘汰衰减分最低的记忆。

纯逻辑实现（不依赖具体存储）：输入 MemoryRecord 列表，输出应删除的 memory_id，
由服务层调用 ``store.delete`` 完成淘汰。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class MemoryDecayConfig:
    """记忆衰减配置。"""

    enabled: bool = True
    # 衰减半衰期（小时）：默认 7 天。
    half_life_hours: float = 168.0
    # 衰减分低于该阈值的记忆将被遗忘。
    min_score: float = 0.05
    # 同一归属（user_id, tenant_id）下的记忆容量上限。
    capacity_per_owner: int = 500
    # 未声明重要性时的默认分值。
    default_importance: float = 1.0


def importance_of(record: Any, config: MemoryDecayConfig) -> float:
    """读取记忆的重要性分值。"""
    try:
        value = float((getattr(record, "metadata", None) or {}).get("importance", config.default_importance))
    except (TypeError, ValueError):
        value = config.default_importance
    return max(0.0, value)


def decay_score(record: Any, config: MemoryDecayConfig, *, now: datetime | None = None) -> float:
    """计算一条记忆的当前衰减分：importance * 0.5 ** (age / half_life)。"""
    current = now or datetime.now(UTC)
    updated = _parse_time(getattr(record, "updated_at", None)) or _parse_time(
        getattr(record, "created_at", None)
    )
    if updated is None:
        return importance_of(record, config)
    age_hours = max(0.0, (current - updated).total_seconds() / 3600.0)
    return importance_of(record, config) * (0.5 ** (age_hours / max(config.half_life_hours, 0.001)))


def select_forgetting(
    records: list[Any],
    config: MemoryDecayConfig,
    *,
    now: datetime | None = None,
) -> list[str]:
    """返回应当删除的 memory_id 列表（衰减遗忘 + 容量淘汰）。

    Args:
        records: 同一 namespace（通常同一归属）下的记忆列表。
        config: 衰减配置。
        now: 注入当前时间，便于测试。

    Returns:
        需要删除的 memory_id（按淘汰顺序：先低于阈值者，再容量溢出者）。
    """
    if not config.enabled or not records:
        return []
    scored = sorted(
        ((decay_score(record, config, now=now), str(getattr(record, "memory_id", ""))) for record in records),
        key=lambda item: item[0],
    )
    forgotten: list[str] = []
    for score, memory_id in scored:
        if score < config.min_score and memory_id:
            forgotten.append(memory_id)
    if config.capacity_per_owner > 0 and len(records) > config.capacity_per_owner:
        survivors = [(score, memory_id) for score, memory_id in scored if memory_id not in forgotten]
        overflow = len(records) - config.capacity_per_owner
        forgotten.extend(memory_id for _, memory_id in survivors[:overflow])
    return forgotten


class MemoryDecayService:
    """把衰减策略落到 MemoryStore 的应用服务（vanilla/LangGraph 均可复用）。"""

    def __init__(self, store: Any, config: MemoryDecayConfig | None = None) -> None:
        self._store = store
        self._config = config or MemoryDecayConfig()

    async def prune(
        self,
        namespace: str,
        *,
        access: Any = None,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """扫描 namespace 下的记忆并淘汰；返回统计。"""
        if not self._config.enabled:
            return {"scanned": 0, "removed": 0}
        results = await self._store.search(namespace, query="", limit=100000, access=access)
        records = [item.record if hasattr(item, "record") else item for item in results]
        to_delete = select_forgetting(records, self._config, now=now)
        removed = 0
        for memory_id in to_delete:
            try:
                if await self._store.delete(memory_id, access=access):
                    removed += 1
            except Exception:
                logger.exception("遗忘记忆 %s 失败", memory_id)
        if removed:
            logger.info("记忆衰减淘汰 %d/%d 条（namespace=%s）", removed, len(records), namespace)
        return {"scanned": len(records), "removed": removed}


__all__ = [
    "MemoryDecayConfig",
    "MemoryDecayService",
    "decay_score",
    "importance_of",
    "select_forgetting",
]