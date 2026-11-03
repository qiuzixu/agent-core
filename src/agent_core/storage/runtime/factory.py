"""按运行环境创建 RuntimeStore 的工厂函数。"""

from __future__ import annotations

from agent_core.storage.runtime.base import RuntimeStore
from agent_core.storage.runtime.memory import MemoryRuntimeStore
from agent_core.storage.runtime.postgres import PostgresRuntimeStore
from agent_core.storage.runtime.sqlite import SqliteRuntimeStore


def create_runtime_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
) -> RuntimeStore:
    """按环境选择运行时状态存储。"""
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryRuntimeStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境需要配置 PostgreSQL 连接地址")
        return PostgresRuntimeStore(postgres_url)
    return SqliteRuntimeStore(sqlite_path)
