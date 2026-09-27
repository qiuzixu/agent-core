"""按环境和显式配置创建向量存储。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from agent_core.retrieval.chroma import ChromaVectorStore
from agent_core.retrieval.core import VectorStore
from agent_core.retrieval.memory import InMemoryVectorStore
from agent_core.retrieval.pgvector import PgVectorStore

VectorStoreBackend = Literal["memory", "chroma", "pgvector"]


def create_vector_store(
    env: str = "development",
    *,
    backend: str | None = None,
    chroma_path: str | Path = "./chroma_db",
    chroma_client: Any | None = None,
    postgres_url: str | None = None,
    dimension: int | None = None,
    collection_name: str = "agent_core",
    table_name: str = "agent_vector_records",
    require_access: bool = False,
    pg_create_extension: bool = True,
    pg_create_index: bool = True,
) -> VectorStore:
    """创建向量存储；显式 backend 优先于环境默认值。

    默认映射：测试使用内存，开发使用 Chroma，生产使用 pgvector。
    PostgreSQL 实现返回后仍需调用 ``initialize()``。
    """
    normalized_env = env.strip().lower()
    selected = (backend or _default_backend(normalized_env)).strip().lower()
    selected = {"postgres": "pgvector", "postgresql": "pgvector"}.get(selected, selected)
    if selected == "memory":
        return InMemoryVectorStore(require_access=require_access)
    if selected == "chroma":
        return ChromaVectorStore(
            chroma_path,
            collection_name=collection_name,
            require_access=require_access,
            client=chroma_client,
        )
    if selected == "pgvector":
        if not postgres_url:
            raise ValueError("pgvector 后端需要配置 PostgreSQL 连接地址")
        if dimension is None:
            raise ValueError("pgvector 后端需要配置 embedding dimension")
        return PgVectorStore(
            postgres_url,
            dimension=dimension,
            collection_name=collection_name,
            table_name=table_name,
            require_access=require_access,
            create_extension=pg_create_extension,
            create_index=pg_create_index,
        )
    raise ValueError(f"不支持的向量存储后端：{selected}")


def _default_backend(env: str) -> VectorStoreBackend:
    if env in {"test", "testing"}:
        return "memory"
    if env in {"production", "prod"}:
        return "pgvector"
    return "chroma"


__all__ = ["VectorStoreBackend", "create_vector_store"]
