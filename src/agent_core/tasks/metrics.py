"""Agent 任务可观测性的轻量进程内计数器。"""

from __future__ import annotations


class TaskMetrics:
    """与线程无关的进程级计数器；暂按进程聚合。"""

    def __init__(self) -> None:
        self._counters: dict[str, int] = {}

    def inc(self, name: str, value: int = 1) -> None:
        if value <= 0:
            return
        self._counters[name] = self._counters.get(name, 0) + int(value)

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self._counters.items()))


default_task_metrics = TaskMetrics()

__all__ = ["TaskMetrics", "default_task_metrics"]

