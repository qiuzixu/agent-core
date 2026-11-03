"""任务存储兼容入口。

实现已按后端拆分到 ``models`` / ``base`` / ``memory`` / ``sqlite`` / 
``postgres`` 模块；本文件保留 ``agent_core.tasks.store`` 导入路径与
``create_agent_task_store`` 环境工厂，向后兼容。
"""

from __future__ import annotations

from pathlib import Path

from agent_core.tasks.base import AgentTaskStore
from agent_core.tasks.memory import MemoryAgentTaskStore
from agent_core.tasks.models import AgentTask, _now, _parse_time, _task_from_row
from agent_core.tasks.postgres import PostgresAgentTaskStore
from agent_core.tasks.sqlite import SqliteAgentTaskStore

def create_agent_task_store(
    env: str,
    *,
    sqlite_path: str | Path,
    postgres_url: str | None = None,
) -> AgentTaskStore:
    normalized = env.lower()
    if normalized in {"test", "testing"}:
        return MemoryAgentTaskStore()
    if normalized in {"production", "prod"}:
        if not postgres_url:
            raise ValueError("生产环境 Agent 任务存储需要 PostgreSQL DSN")
        return PostgresAgentTaskStore(postgres_url)
    return SqliteAgentTaskStore(sqlite_path)



__all__ = [
    "AgentTask",
    "AgentTaskStore",
    "MemoryAgentTaskStore",
    "PostgresAgentTaskStore",
    "SqliteAgentTaskStore",
    "create_agent_task_store",
]
