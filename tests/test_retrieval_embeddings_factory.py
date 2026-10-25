"""Embedding 工厂的基础行为测试。"""

import pytest

from agent_core.retrieval import EmbeddingProvider, OpenAIEmbeddings, create_embeddings


def test_unknown_provider_rejected() -> None:
    """未知 provider 必须立即失败，避免配置错误延迟到首次调用才暴露。"""
    with pytest.raises(ValueError, match="不支持的 embedding provider"):
        create_embeddings("not-a-provider", model="text-embedding-3-small", api_key="key")


@pytest.mark.parametrize("provider", ["", "   "])
def test_empty_model_rejected(provider: str) -> None:
    """model 是调用 embedding API 的必填参数。"""
    with pytest.raises(ValueError, match="embedding model 不能为空"):
        create_embeddings("openai_compatible", model="", api_key="key")


@pytest.mark.parametrize("provider", ["ollama", "vllm"])
def test_local_services_allow_empty_api_key(provider: str) -> None:
    """本地推理服务通常不要求 Key，工厂内部使用占位 Key。"""
    client = create_embeddings(provider, model="nomic-embed-text")
    assert isinstance(client, OpenAIEmbeddings)
    assert client.provider_name == "openai"


@pytest.mark.parametrize("provider", ["openai", "dashscope", "siliconflow"])
def test_cloud_services_require_api_key(provider: str) -> None:
    """云厂商服务缺少 Key 时必须给出明确配置错误。"""
    with pytest.raises(ValueError, match="需要配置 api_key"):
        create_embeddings(provider, model="text-embedding-3-small")


def test_openai_compatible_client_created() -> None:
    """默认 provider 应创建 OpenAI-compatible 客户端。"""
    client = create_embeddings(
        "openai_compatible",
        model="text-embedding-3-small",
        api_key="test-key",
        dimensions=1024,
    )
    assert isinstance(client, OpenAIEmbeddings)


def test_provider_literal_contains_builtin_providers() -> None:
    """静态类型中的 provider 清单应覆盖常见云服务和本地服务。"""
    assert set(EmbeddingProvider.__args__ if hasattr(EmbeddingProvider, "__args__") else []) >= {
        "openai",
        "openai_compatible",
        "dashscope",
        "qwen",
        "ollama",
        "siliconflow",
        "vllm",
    }
