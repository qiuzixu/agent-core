"""并行工具调用策略（声明式配置）。

ReAct 循环默认按工具 priority 分批并行。本模块在其上增加声明式安全策略：

- ``serial_blacklist``：黑名单工具永远单独执行（优先级最高、最保守）；
- ``mode="whitelist"``：只有白名单内的工具才允许同批并行；
- ``max_concurrency``：限制单个并行批次的工具数量（0 表示不限）。

子批次之间仍然串行执行，保持"高优先级失败阻断低优先级"的既有语义。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ParallelToolPolicy:
    """声明式并行策略。"""

    mode: str = "all"
    parallel_whitelist: frozenset[str] = field(default_factory=frozenset)
    serial_blacklist: frozenset[str] = field(default_factory=frozenset)
    max_concurrency: int = 0  # 0 = 不限制

    def __post_init__(self) -> None:
        if self.mode not in {"all", "whitelist"}:
            raise ValueError(f"未知的并行策略 mode：{self.mode}")
        if self.max_concurrency < 0:
            raise ValueError("max_concurrency 不能为负数")

    @staticmethod
    def _tool_name(entry: tuple[int, dict[str, Any]]) -> str:
        return str(entry[1].get("name") or "")

    def _is_serial_only(self, name: str) -> bool:
        if name in self.serial_blacklist:
            return True
        return self.mode == "whitelist" and name not in self.parallel_whitelist

    def split(self, batch: list[tuple[int, dict[str, Any]]]) -> list[list[tuple[int, dict[str, Any]]]]:
        """把同一优先级批次切分为可安全并行的子批次（保持原顺序）。"""
        if len(batch) <= 1:
            return [batch]
        groups: list[list[tuple[int, dict[str, Any]]]] = []
        current: list[tuple[int, dict[str, Any]]] = []
        changed = False
        for entry in batch:
            if self._is_serial_only(self._tool_name(entry)):
                changed = True
                if current:
                    groups.append(current)
                    current = []
                groups.append([entry])
            else:
                current.append(entry)
        if current:
            groups.append(current)
        if not changed:
            if self.max_concurrency > 0 and len(batch) > self.max_concurrency:
                return [
                    batch[start : start + self.max_concurrency]
                    for start in range(0, len(batch), self.max_concurrency)
                ]
            return [batch]
        result: list[list[tuple[int, dict[str, Any]]]] = []
        for group in groups:
            if self.max_concurrency > 0 and len(group) > self.max_concurrency:
                result.extend(
                    group[start : start + self.max_concurrency]
                    for start in range(0, len(group), self.max_concurrency)
                )
            else:
                result.append(group)
        return result

    @classmethod
    def from_env(cls) -> "ParallelToolPolicy":
        """从环境变量构建策略（AGENT_PARALLEL_MODE / WHITELIST / BLACKLIST / AGENT_TOOL_MAX_CONCURRENCY）。"""
        mode = (os.getenv("AGENT_PARALLEL_MODE") or "all").strip().lower() or "all"
        whitelist = frozenset(
            item.strip()
            for item in (os.getenv("AGENT_PARALLEL_WHITELIST") or "").split(",")
            if item.strip()
        )
        blacklist = frozenset(
            item.strip()
            for item in (os.getenv("AGENT_PARALLEL_BLACKLIST") or "").split(",")
            if item.strip()
        )
        try:
            max_concurrency = int(os.getenv("AGENT_TOOL_MAX_CONCURRENCY") or 0)
        except ValueError:
            max_concurrency = 0
        return cls(
            mode=mode,
            parallel_whitelist=whitelist,
            serial_blacklist=blacklist,
            max_concurrency=max_concurrency,
        )


__all__ = ["ParallelToolPolicy"]