"""统一创建常用存储后端的便捷入口。

`create_storage_bundle` 用于减少业务项目中的重复样板代码：一次创建
会话、运行时、上下文、记忆、模型选择、租约、工作流、幂等、事件和
知识图谱存储。仍然可以按需只取其中一两个仓库使用。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_core.storage.context import create_context_store
from agent_core.storage.governance import (
    create_session_event_store,
    create_workflow_idempotency_store,
)
from agent_core.storage.knowledge_graph import create_knowledge_graph_store
from agent_core.storage.lease import create_run_lease_store
from agent_core.storage.memory import create_memory_store
from agent_core.storage.model_selection import create_model_selection_store
from agent_core.storage.runtime import create_runtime_store
from agent_core.storage.session import create_session_store
from agent_core.storage.workflow import create_workflow_execution_store


@dataclass(frozen=True)
class StorageBundle:
    """常用存储后端集合。

    所有字段都按同一个 `env` / `sqlite_path` / `postgres_url` 配置创建，
    避免不同 store 使用不同后端导致的部署漂移。
    """

    env: str
    sqlite_path: str
    postgres_url: str | None
    require_access: bool
    session_store: Any
    runtime_store: Any
    context_store: Any
    memory_store: Any
    model_selection_store: Any
    run_lease_store: Any
    workflow_execution_store: Any
    workflow_idempotency_store: Any
    session_event_store: Any
    knowledge_graph_store: Any

    async def initialize(self) -> None:
        """初始化需要显式建连/建表的 PostgreSQL 后端。"""
        for store in (
            self.session_store,
            self.runtime_store,
            self.context_store,
            self.memory_store,
            self.model_selection_store,
            self.run_lease_store,
            self.workflow_execution_store,
            self.workflow_idempotency_store,
            self.session_event_store,
            self.knowledge_graph_store,
        ):
            initialize = getattr(store, "initialize", None)
            result = initialize() if callable(initialize) else initialize
            if inspect.isawaitable(result):
                await result

    async def close(self) -> None:
        """关闭需要释放连接池的后端。"""
        for store in (
            self.session_store,
            self.runtime_store,
            self.context_store,
            self.memory_store,
            self.model_selection_store,
            self.run_lease_store,
            self.workflow_execution_store,
            self.workflow_idempotency_store,
            self.session_event_store,
            self.knowledge_graph_store,
        ):
            close = getattr(store, "close", None)
            result = close() if callable(close) else close
            if inspect.isawaitable(result):
                await result


def create_storage_bundle(
    env: str = "development",
    *,
    sqlite_path: str | Path = "./sessions.db",
    postgres_url: str | None = None,
    require_access: bool = False,
) -> StorageBundle:
    """按统一配置创建常用存储后端集合。

    Args:
        env: 运行环境。`test/testing` 使用内存，`development` 使用 SQLite，
            `production/prod` 使用 PostgreSQL。
        sqlite_path: 开发环境共享的 SQLite 文件路径。
        postgres_url: 生产环境 PostgreSQL DSN。
        require_access: 是否强制支持访问隔离的存储校验 AccessContext。

    Returns:
        StorageBundle: 常用存储仓库集合。
    """
    resolved_path = str(sqlite_path)
    return StorageBundle(
        env=env,
        sqlite_path=resolved_path,
        postgres_url=postgres_url,
        require_access=require_access,
        session_store=create_session_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
            require_access=require_access,
        ),
        runtime_store=create_runtime_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        context_store=create_context_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
            require_access=require_access,
        ),
        memory_store=create_memory_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
            require_access=require_access,
        ),
        model_selection_store=create_model_selection_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        run_lease_store=create_run_lease_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        workflow_execution_store=create_workflow_execution_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        workflow_idempotency_store=create_workflow_idempotency_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        session_event_store=create_session_event_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
        knowledge_graph_store=create_knowledge_graph_store(
            env,
            sqlite_path=resolved_path,
            postgres_url=postgres_url,
        ),
    )


__all__ = ["StorageBundle", "create_storage_bundle"]
