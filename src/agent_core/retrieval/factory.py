"""按环境和显式配置创建向量存储与 Embedding 服务。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from agent_core.retrieval.chroma import ChromaVectorStore
from agent_core.retrieval.core import Embeddings, VectorStore
from agent_core.retrieval.faiss import FaissVectorStore
from agent_core.retrieval.memory import InMemoryVectorStore
from agent_core.retrieval.openai import OpenAIEmbeddings
from agent_core.retrieval.pgvector import PgVectorStore
from agent_core.retrieval.qdrant import QdrantVectorStore

VectorStoreBackend = Literal["memory", "chroma", "faiss", "pgvector", "qdrant"]

# 目前所有内置 Embedding 适配器都走 OpenAI-compatible /embeddings 协议；
# 这能覆盖 OpenAI、DashScope/Qwen、Ollama、vLLM、SiliconFlow 等常见服务。
EmbeddingProvider = Literal[
    "openai",
    "openai_compatible",
    "custom",
    "dashscope",
    "qwen",
    "ollama",
    "siliconflow",
    "vllm",
]

# 不配置 base_url 时使用的厂商默认地址；用户显式传入 base_url 始终优先。
_EMBEDDING_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "openai_compatible": "https://api.openai.com/v1",
    "custom": "http://127.0.0.1:8000/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "vllm": "http://127.0.0.1:8000/v1",
}


def create_vector_store(
    env: str = "development",
    *,
    backend: str | None = None,
    chroma_path: str | Path = "./chroma_db",
    faiss_path: str | Path = "./faiss_db",
    qdrant_path: str | Path = "./qdrant_db",
    chroma_client: Any | None = None,
    qdrant_client: Any | None = None,
    qdrant_url: str | None = None,
    qdrant_api_key: str | None = None,
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
    if selected == "faiss":
        return FaissVectorStore(
            faiss_path,
            collection_name=collection_name,
            dimension=dimension,
            require_access=require_access,
        )
    if selected == "qdrant":
        return QdrantVectorStore(
            qdrant_path,
            collection_name=collection_name,
            require_access=require_access,
            client=qdrant_client,
            url=qdrant_url,
            api_key=qdrant_api_key,
            dimension=dimension,
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


def create_embeddings(
    provider: str = "openai_compatible",
    *,
    model: str,
    api_key: str | None = None,
    base_url: str | None = None,
    dimensions: int | None = None,
    timeout: float | None = None,
) -> Embeddings:
    """创建 Embedding 客户端的统一入口。

    内置适配器统一使用 OpenAI-compatible ``/embeddings`` 协议；这已经能覆盖
    OpenAI、DashScope/Qwen、Ollama、vLLM、SiliconFlow 等常见服务。
    如果厂商协议不兼容，请实现 ``Embeddings`` 协议后直接注入业务层，而不要
    在 Core 中为每一家都新增硬依赖。
    """
    normalized = provider.strip().lower()
    if normalized not in _EMBEDDING_DEFAULT_BASE_URLS:
        raise ValueError(f"不支持的 embedding provider：{provider}")
    if not model.strip():
        raise ValueError("embedding model 不能为空")

    selected_base_url = base_url or _EMBEDDING_DEFAULT_BASE_URLS[normalized]

    # 本地服务通常不需要真实 Key，但 OpenAI SDK 要求非空字符串。
    if normalized in {"ollama", "vllm"}:
        selected_api_key = api_key or "local"
    else:
        if not api_key:
            raise ValueError(f"使用 {normalized} embedding 服务需要配置 api_key")
        selected_api_key = api_key

    return OpenAIEmbeddings(
        api_key=selected_api_key,
        model=model,
        base_url=selected_base_url,
        dimensions=dimensions,
        timeout=timeout,
    )


__all__ = [
    "EmbeddingProvider",
    "VectorStoreBackend",
    "create_embeddings",
    "create_vector_store",
]