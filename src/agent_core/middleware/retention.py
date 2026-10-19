"""数据过期 / 轮转清理（Retention）。

覆盖三类过期数据：

1. **JSONL 追加日志**（audit.log / feedback.jsonl / dlq.jsonl）：按行内时间戳
   淘汰超龄记录 + 按大小整体轮转归档；
2. **归档目录**：轮转产生的归档文件超过保留数量时删除最旧的；
3. **自定义清理任务**：服务层可注册异步协程（如 PG 会话归档 SQL）。

纯标准库实现；周期调度由应用 lifespan 负责。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DEFAULT_TIMESTAMP_KEYS = ("ts", "timestamp", "time", "created_at", "updated_at")


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass
class RetentionPolicy:
    """一个 JSONL 文件的保留策略。"""

    name: str
    path: str
    # 超过该天数的记录被淘汰；0 表示不按时间清理。
    max_age_days: float = 0.0
    # 文件超过该字节数时整体轮转归档；0 表示不按大小轮转。
    max_size_bytes: int = 0
    # 归档文件最多保留数量（超出删除最旧）；0 表示不限。
    max_archive_files: int = 10
    timestamp_keys: tuple[str, ...] = field(default_factory=lambda: _DEFAULT_TIMESTAMP_KEYS)
    archive_directory: str | None = None


def apply_retention(policy: RetentionPolicy, *, now: datetime | None = None) -> dict[str, Any]:
    """对单个文件执行时间淘汰与大小轮转；返回统计（纯同步、可在线程池运行）。"""
    current = now or datetime.now(UTC)
    path = Path(policy.path)
    result: dict[str, Any] = {"name": policy.name, "scanned": 0, "removed": 0, "archived": None}
    if not path.exists():
        return result
    cutoff = (
        current - timedelta(days=policy.max_age_days)
        if policy.max_age_days > 0
        else None
    )
    kept_lines: list[str] = []
    scanned = 0
    removed = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        scanned += 1
        if cutoff is not None:
            record = None
            try:
                import json

                record = json.loads(stripped)
            except ValueError:
                record = None
            timestamp = None
            if isinstance(record, dict):
                for key in policy.timestamp_keys:
                    timestamp = _parse_time(record.get(key))
                    if timestamp is not None:
                        break
            if timestamp is not None and timestamp <= cutoff:
                removed += 1
                continue
        kept_lines.append(stripped)
    result["scanned"] = scanned
    result["removed"] = removed

    should_rotate = bool(policy.max_size_bytes and path.stat().st_size > policy.max_size_bytes)
    if should_rotate:
        archive_dir = Path(policy.archive_directory) if policy.archive_directory else path.parent
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_name = f"{path.stem}.{current.strftime('%Y%m%dT%H%M%S')}{path.suffix}"
        archive_path = archive_directory / archive_name
        path.replace(archive_path)
        result["archived"] = str(archive_path)
        # 原路径重新写回保留下来的行（归档后文件消失，重建保留集）。
        if kept_lines:
            path.write_text("\n".join(kept_lines) + "\n", encoding="utf-8")
    elif removed > 0:
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text("\n".join(kept_lines) + ("\n" if kept_lines else ""), encoding="utf-8")
        temp_path.replace(path)

    # 归档数量收敛。
    if policy.max_archive_files > 0:
        archive_directory = Path(policy.archive_directory) if policy.archive_directory else path.parent
        archives = sorted(
            archive_directory.glob(f"{path.stem}.*{path.suffix}"),
            key=lambda item: item.stat().st_mtime,
        )
        excess = len(archives) - policy.max_archive_files
        for item in archives[:excess] if excess > 0 else []:
            item.unlink(missing_ok=True)
    return result


@dataclass
class _RegisteredTask:
    name: str
    factory: Callable[[], Any]


class RetentionSweeper:
    """聚合多个保留策略与自定义清理任务，供 lifespan 周期调用。"""

    def __init__(self) -> None:
        self._policies: list[RetentionPolicy] = []
        self._tasks: list[_RegisteredTask] = []

    def register_policy(self, policy: RetentionPolicy) -> None:
        self._policies.append(policy)

    def register_task(self, name: str, factory: Callable[[], Any]) -> None:
        """注册自定义清理任务；factory 返回协程或 None。"""
        self._tasks.append(_RegisteredTask(name=name, factory=factory))

    async def sweep(self) -> dict[str, Any]:
        """执行一轮清理，返回逐项结果（失败项诚实标记 error）。"""
        report: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        for policy in self._policies:
            try:
                report[policy.name] = await loop.run_in_executor(None, apply_retention, policy)
            except Exception as exc:
                logger.exception("保留策略 %s 执行失败", policy.name)
                report[policy.name] = {"error": str(exc)}
        for task in self._tasks:
            try:
                outcome = task.factory()
                if asyncio.iscoroutine(outcome):
                    outcome = await outcome
                report[task.name] = outcome if outcome is not None else "ok"
            except Exception as exc:
                logger.exception("清理任务 %s 执行失败", task.name)
                report[task.name] = {"error": str(exc)}
        return report


__all__ = ["RetentionPolicy", "RetentionSweeper", "apply_retention"]