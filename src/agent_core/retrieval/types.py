"""检索层公共值对象。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agent_core.access import AccessContext
from agent_core.documents import Document


class SearchType(StrEnum):
    """Core 认识的检索策略；具体存储可以只支持其中一部分。"""

    SIMILARITY = "similarity"
    KEYWORD = "keyword"
    HYBRID = "hybrid"
    MMR = "mmr"


@dataclass(frozen=True)
class RetrievalQuery:
    """与具体检索后端无关的查询。"""

    text: str
    limit: int = 4
    namespace: str = "default"
    search_type: SearchType = SearchType.SIMILARITY
    metadata_filter: dict[str, Any] = field(default_factory=dict)
    min_score: float | None = None
    access: AccessContext | None = None

    def __post_init__(self) -> None:
        if self.limit <= 0:
            raise ValueError("limit 必须大于 0")
        if not self.namespace.strip():
            raise ValueError("namespace 不能为空")


@dataclass(frozen=True)
class RetrievalResult:
    """Retriever 返回的文档、相关度和来源信息。"""

    document: Document
    score: float
    rank: int
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VectorRecord:
    """向量和原始文档的存储记录。"""

    record_id: str
    vector: tuple[float, ...]
    document: Document
    namespace: str = "default"
    user_id: str | None = None
    tenant_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.record_id.strip():
            raise ValueError("record_id 不能为空")
        if not self.vector:
            raise ValueError("vector 不能为空")
        if not self.namespace.strip():
            raise ValueError("namespace 不能为空")


@dataclass(frozen=True)
class VectorQuery:
    """VectorStore 接收的底层向量查询。"""

    vector: tuple[float, ...]
    limit: int = 4
    namespace: str = "default"
    metadata_filter: dict[str, Any] = field(default_factory=dict)
    min_score: float | None = None
    access: AccessContext | None = None

    def __post_init__(self) -> None:
        if not self.vector:
            raise ValueError("查询 vector 不能为空")
        if self.limit <= 0:
            raise ValueError("limit 必须大于 0")


@dataclass(frozen=True)
class VectorSearchResult:
    """VectorStore 的底层检索结果。"""

    record: VectorRecord
    score: float
