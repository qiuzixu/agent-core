"""StorageBundle 便捷创建入口测试。"""

from pathlib import Path

import pytest

from agent_core.storage import create_storage_bundle


def test_create_storage_bundle_uses_sqlite_in_development(tmp_path: Path) -> None:
    """开发环境应统一使用 SQLite 后端。"""
    bundle = create_storage_bundle("development", sqlite_path=tmp_path / "agent.db")
    assert bundle.env == "development"
    assert bundle.sqlite_path == str(tmp_path / "agent.db")
    assert bundle.postgres_url is None
    assert type(bundle.session_store).__name__ == "SqliteSessionStore"
    assert type(bundle.runtime_store).__name__ == "SqliteRuntimeStore"
    assert type(bundle.context_store).__name__ == "SqliteContextStore"
    assert type(bundle.memory_store).__name__ == "SqliteMemoryStore"
    assert type(bundle.model_selection_store).__name__ == "SqliteModelSelectionStore"
    assert type(bundle.run_lease_store).__name__ == "SqliteRunLeaseStore"
    assert type(bundle.workflow_execution_store).__name__ == "SqliteWorkflowExecutionStore"
    assert type(bundle.workflow_idempotency_store).__name__ == "SqliteWorkflowIdempotencyStore"
    assert type(bundle.session_event_store).__name__ == "SqliteSessionEventStore"
    assert type(bundle.knowledge_graph_store).__name__ == "SqliteKnowledgeGraphStore"


def test_create_storage_bundle_uses_memory_in_testing() -> None:
    """测试环境应统一使用内存后端。"""
    bundle = create_storage_bundle("testing")
    assert type(bundle.session_store).__name__ == "MemorySessionStore"
    assert type(bundle.runtime_store).__name__ == "MemoryRuntimeStore"
    assert type(bundle.context_store).__name__ == "MemoryContextStore"
    assert type(bundle.memory_store).__name__ == "InMemoryMemoryStore"
    assert type(bundle.model_selection_store).__name__ == "MemoryModelSelectionStore"
    assert type(bundle.run_lease_store).__name__ == "MemoryRunLeaseStore"
    assert type(bundle.workflow_execution_store).__name__ == "MemoryWorkflowExecutionStore"
    assert type(bundle.workflow_idempotency_store).__name__ == "MemoryWorkflowIdempotencyStore"
    assert type(bundle.session_event_store).__name__ == "MemorySessionEventStore"
    assert type(bundle.knowledge_graph_store).__name__ == "InMemoryKnowledgeGraphStore"


def test_create_storage_bundle_requires_postgres_url() -> None:
    """生产环境必须提供 PostgreSQL DSN。"""
    with pytest.raises(ValueError):
        create_storage_bundle("production")
