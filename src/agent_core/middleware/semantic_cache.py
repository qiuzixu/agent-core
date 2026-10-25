"""语义缓存中间件。

默认使用零依赖的 lexical 相似度（Jaccard + 编辑距离）；
可选启用 embedding 向量检索，或使用 hybrid 模式做“向量召回 + 字面校验”。

工作原理：
1. lexical 模式：直接对进程内缓存做 Jaccard/Levenshtein 相似度匹配。
2. embedding 模式：用 EmbeddingProvider 向量化问题，用 VectorStore 检索相似问题。
3. hybrid 模式：先用向量召回候选，再用字面相似度做保守校验，降低误命中。
4. after_model: 只有最终回答（没有 tool_calls）才会写入缓存。

配置：
    AGENT_SEMANTIC_CACHE_ENABLED: 是否启用（默认 false）
    AGENT_SEMANTIC_CACHE_MODE: lexical / embedding / hybrid（默认 lexical）
    AGENT_SEMANTIC_CACHE_THRESHOLD: lexical/embedding 主阈值（默认 0.92）
    AGENT_SEMANTIC_CACHE_TTL_SECONDS: 缓存有效期（默认 3600s）
    AGENT_SEMANTIC_CACHE_MAX_ENTRIES: lexical 进程内缓存最大条目（默认 500）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from agent_core.documents import Document
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.retrieval.core import Embeddings, VectorStore
from agent_core.retrieval.types import VectorQuery, VectorRecord

logger = logging.getLogger(__name__)

__all__ = [
    "SemanticCacheConfig",
    "SemanticCacheMiddleware",
    "SemanticCacheMode",
]


# 缓存匹配策略：lexical 是历史默认；embedding 走向量；hybrid 是向量召回 + 字面校验。
SemanticCacheMode = Literal["lexical", "embedding", "hybrid"]


@dataclass
class SemanticCacheConfig:
    """语义缓存配置。

    ``mode`` 决定匹配方式：
    - ``lexical``：保持原有行为，零外部依赖。
    - ``embedding``：只使用向量检索。
    - ``hybrid``：向量召回候选后，再用字面相似度做保守校验。
    """

    enabled: bool = False
    mode: SemanticCacheMode = "lexical"
    threshold: float = 0.92  # lexical 主阈值；embedding 模式也复用它作为向量阈值。
    vector_threshold: float = 0.92  # embedding/hybrid 的向量相似度阈值。
    lexical_floor: float = 0.70  # hybrid 中向量候选必须达到的最低字面相似度。
    ttl_seconds: float = 3600.0
    max_entries: int = 500
    embedding_top_k: int = 5
    namespace: str = "semantic_cache"
    scope: str = "global"
    entity_guard: bool = True
    # 可选依赖：不配置时 lexical 模式继续零依赖；embedding/hybrid 模式必须配置。
    embeddings: Embeddings | None = None
    vector_store: VectorStore | None = None
    # 排除缓存的消息模式（如工具调用轮次）
    exclude_patterns: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        """尽早暴露配置错误，避免请求阶段才失败。"""
        if self.mode not in ("lexical", "embedding", "hybrid"):
            raise ValueError("semantic cache mode 只支持 lexical / embedding / hybrid")
        if self.mode in ("embedding", "hybrid"):
            if self.embeddings is None or self.vector_store is None:
                raise ValueError("embedding/hybrid 语义缓存需要同时配置 embeddings 和 vector_store")
        if not (0.0 <= self.threshold <= 1.0):
            raise ValueError("threshold 必须在 0~1 之间")
        if not (0.0 <= self.vector_threshold <= 1.0):
            raise ValueError("vector_threshold 必须在 0~1 之间")
        if not (0.0 <= self.lexical_floor <= 1.0):
            raise ValueError("lexical_floor 必须在 0~1 之间")
        if self.embedding_top_k <= 0:
            raise ValueError("embedding_top_k 必须大于 0")
        if not self.namespace.strip():
            raise ValueError("namespace 不能为空")


@dataclass
class _CacheEntry:
    """lexical 进程内缓存条目。"""

    fingerprint: str
    question: str
    response: str
    timestamp: float
    hit_count: int = 0


@dataclass
class _VectorMatch:
    """向量检索命中的候选。"""

    fingerprint: str
    question: str
    response: str
    vector_score: float
    lexical_score: float = 0.0


def _tokenize(text: str) -> set[str]:
    """中文按二元切分，英文按单词切分。"""
    # 去标点
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]", " ", text.lower())
    tokens: set[str] = set()
    for segment in cleaned.split():
        # 检查是否包含中文
        if re.search(r"[\u4e00-\u9fff]", segment):
            # 中文二元切分
            for i in range(len(segment) - 1):
                tokens.add(segment[i : i + 2])
            if len(segment) == 1:
                tokens.add(segment)
        else:
            tokens.add(segment)
    return tokens


def _jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度。"""
    if not a or not b:
        return 0.0
    intersection = a & b
    union = a | b
    return len(intersection) / len(union) if union else 0.0


def _levenshtein_ratio(s1: str, s2: str) -> float:
    """编辑距离相似度（0~1）。"""
    if len(s1) == 0:
        return 1.0 if len(s2) == 0 else 0.0
    if len(s2) == 0:
        return 0.0
    # 限制比较长度（防止 O(n²) 爆炸）
    max_len = 200
    s1 = s1[:max_len]
    s2 = s2[:max_len]
    matrix = [[0] * (len(s2) + 1) for _ in range(len(s1) + 1)]
    for i in range(len(s1) + 1):
        matrix[i][0] = i
    for j in range(len(s2) + 1):
        matrix[0][j] = j
    for i in range(1, len(s1) + 1):
        for j in range(1, len(s2) + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            matrix[i][j] = min(matrix[i - 1][j] + 1, matrix[i][j - 1] + 1, matrix[i - 1][j - 1] + cost)
    return 1.0 - matrix[len(s1)][len(s2)] / max(len(s1), len(s2))


def _similarity(text_a: str, text_b: str) -> float:
    """混合相似度：Jaccard (token 级) + Levenshtein (字符级) 加权。"""
    tokens_a = _tokenize(text_a)
    tokens_b = _tokenize(text_b)
    jac = _jaccard_similarity(tokens_a, tokens_b)
    lev = _levenshtein_ratio(text_a[:200], text_b[:200])
    return 0.6 * jac + 0.4 * lev


_NUMBER_PATTERN = re.compile(r"(?<!\w)[-+]?\d+(?:\.\d+)?(?!\w)")
_CODE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9_-]*\d[A-Za-z0-9_-]*(?![A-Za-z0-9])")


def _entity_conflict(question_a: str, question_b: str) -> bool:
    """判断两个高相似问题中的关键实体是否冲突。

    这里重点保护数字和编号，例如 “A123/A124”、“2025/2026”、“100/200”。
    向量模型可能认为这些问题整体相似，但关键值不同时不允许命中。
    """
    if not re.search(r"[\dA-Za-z]", question_a) or not re.search(r"[\dA-Za-z]", question_b):
        return False

    def _numbers(text: str) -> set[Decimal]:
        values: set[Decimal] = set()
        for raw in _NUMBER_PATTERN.findall(text):
            try:
                values.add(Decimal(raw).normalize())
            except InvalidOperation:
                values.add(Decimal(raw))
        return values

    if _numbers(question_a) != _numbers(question_b):
        return True

    codes_a = {value.upper() for value in _CODE_PATTERN.findall(question_a)}
    codes_b = {value.upper() for value in _CODE_PATTERN.findall(question_b)}
    return codes_a != codes_b


class SemanticCacheMiddleware(Middleware):
    priority = 22
    hook_timeout = 30.0
    """语义缓存：相似问题直接返回缓存结果，省去 LLM 调用。"""

    def __init__(self, config: SemanticCacheConfig | None = None) -> None:
        self._config = config or SemanticCacheConfig()
        self._cache: list[_CacheEntry] = []
        self._vector_lock = asyncio.Lock()
        self._vector_ready = False
        self._lexical_hits = 0
        self._vector_hits = 0
        self._vector_errors = 0

    def _fingerprint(self, text: str) -> str:
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _get_latest_user_message(self, context: MiddlewareContext) -> str:
        """获取最新的用户消息文本。"""
        for msg in reversed(context.messages):
            role = getattr(msg, "role", "")
            if role in ("user", "human"):
                return str(getattr(msg, "content", "") or "")
        return ""

    def _is_tool_iteration(self, context: MiddlewareContext) -> bool:
        """判断当前是否为工具调用迭代（工具中间轮次不缓存）。"""
        if context.iteration > 0:
            return True
        for pattern in self._config.exclude_patterns:
            if pattern in context.metadata.get("session_id", ""):
                return True
        return False

    def _lookup(self, question: str) -> _CacheEntry | None:
        """原有 lexical 查找逻辑，保持完全兼容。"""
        now = time.monotonic()
        # 清理过期
        self._cache = [e for e in self._cache if (now - e.timestamp) < self._config.ttl_seconds]
        if not self._cache:
            return None
        best_entry: _CacheEntry | None = None
        best_score = 0.0
        for entry in self._cache:
            score = _similarity(question, entry.question)
            if score > best_score:
                best_score = score
                best_entry = entry
        if best_entry and best_score >= self._config.threshold:
            return best_entry
        return None

    async def _ensure_vector_store(self) -> None:
        """惰性初始化向量存储，兼容 PgVectorStore 这类需要显式初始化的后端。"""
        if self._vector_ready or self._config.vector_store is None:
            return
        async with self._vector_lock:
            if self._vector_ready:
                return
            initialize = getattr(self._config.vector_store, "initialize", None)
            if callable(initialize):
                await initialize()
            self._vector_ready = True

    async def _vector_lookup(self, question: str) -> _VectorMatch | None:
        """通过向量库查找语义相似的历史问题。"""
        if self._config.embeddings is None or self._config.vector_store is None:
            return None
        await self._ensure_vector_store()
        vector = await self._config.embeddings.embed_query(question)
        matches = await self._config.vector_store.search(
            VectorQuery(
                vector=tuple(float(value) for value in vector),
                limit=self._config.embedding_top_k,
                namespace=self._config.namespace,
                metadata_filter={"scope": self._config.scope},
                min_score=self._config.vector_threshold,
            )
        )
        now = time.time()
        best: _VectorMatch | None = None
        for item in matches:
            metadata = item.record.metadata
            created_at = float(metadata.get("created_at", 0.0))
            if created_at <= 0.0 or (now - created_at) >= self._config.ttl_seconds:
                continue
            candidate_question = str(metadata.get("question", ""))
            response = str(metadata.get("response", ""))
            if not candidate_question or not response:
                continue
            if self._config.entity_guard and _entity_conflict(question, candidate_question):
                logger.info(
                    "[SemanticCache] 向量候选被实体守卫拦截：%s...",
                    metadata.get("fingerprint", item.record.record_id),
                )
                continue
            lexical_score = _similarity(question, candidate_question)
            if self._config.mode == "hybrid" and lexical_score < self._config.lexical_floor:
                continue
            match = _VectorMatch(
                fingerprint=str(metadata.get("fingerprint", item.record.record_id)),
                question=candidate_question,
                response=response,
                vector_score=float(item.score),
                lexical_score=lexical_score,
            )
            if best is None or match.vector_score > best.vector_score:
                best = match
        return best

    def _store(self, question: str, response: str) -> None:
        """原有 lexical 存储逻辑，保持完全兼容。"""
        # 容量控制：LRU 淘汰
        if len(self._cache) >= self._config.max_entries:
            self._cache.sort(key=lambda e: e.timestamp)
            self._cache = self._cache[len(self._cache) // 2 :]  # 淘汰一半旧的
        entry = _CacheEntry(
            fingerprint=self._fingerprint(question),
            question=question,
            response=response,
            timestamp=time.monotonic(),
        )
        # 精确重复不重复存
        for e in self._cache:
            if e.fingerprint == entry.fingerprint:
                e.timestamp = time.monotonic()
                return
        self._cache.append(entry)

    async def _store_vector(self, question: str, response: str) -> None:
        """把问题和最终回答写入向量缓存。"""
        if self._config.embeddings is None or self._config.vector_store is None:
            return
        await self._ensure_vector_store()
        vectors = await self._config.embeddings.embed_documents([question])
        if len(vectors) != 1:
            raise ValueError("Embedding 返回数量与输入数量不一致")
        fingerprint = self._fingerprint(question)
        record = VectorRecord(
            record_id=fingerprint,
            vector=tuple(float(value) for value in vectors[0]),
            document=Document(document_id=fingerprint, content=question),
            namespace=self._config.namespace,
            metadata={
                "question": question,
                "response": response,
                "fingerprint": fingerprint,
                "scope": self._config.scope,
                "created_at": time.time(),
            },
        )
        await self._config.vector_store.upsert([record])

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 只在首轮迭代做缓存命中
        if context.iteration > 0:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        question = self._get_latest_user_message(context).strip()
        if not question or len(question) < 4:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        lexical_entry: _CacheEntry | None = None
        vector_match: _VectorMatch | None = None
        matched_mode: Literal["lexical", "embedding", "hybrid"] | None = None
        score = 0.0

        # hybrid 先走原有 lexical 保守匹配；未命中再尝试向量召回。
        if self._config.mode in ("lexical", "hybrid"):
            lexical_entry = self._lookup(question)
            if lexical_entry is not None:
                matched_mode = "lexical"
                score = _similarity(question, lexical_entry.question)
                self._lexical_hits += 1

        if matched_mode is None and self._config.mode in ("embedding", "hybrid"):
            try:
                vector_match = await self._vector_lookup(question)
            except Exception:
                self._vector_errors += 1
                logger.exception("[SemanticCache] 向量检索失败，降级为继续调用模型")
            if vector_match is not None:
                matched_mode = "embedding" if self._config.mode == "embedding" else "hybrid"
                score = vector_match.vector_score
                self._vector_hits += 1

        if matched_mode == "lexical" and lexical_entry is not None:
            lexical_entry.hit_count += 1
            response = lexical_entry.response
            fingerprint = lexical_entry.fingerprint
        elif matched_mode in ("embedding", "hybrid") and vector_match is not None:
            response = vector_match.response
            fingerprint = vector_match.fingerprint
        else:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        logger.info(
            "[SemanticCache] 命中缓存 (mode=%s, score=%.3f, fingerprint=%s)",
            matched_mode,
            score,
            fingerprint,
        )
        return MiddlewareResult(
            action=MiddlewareAction.STOP,
            data={
                "cached_response": response,
                "cache_hit": True,
                "cache_mode": matched_mode,
                "cache_score": score,
                "cache_fingerprint": fingerprint,
            },
        )

    async def after_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 只在首轮且有最终回答时存入缓存（工具中间轮不存）
        if context.iteration > 0:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        r = context.llm_response
        if r is None or not r.content:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 如果有 tool_calls，说明还没结束，不缓存
        if r.tool_calls:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        question = self._get_latest_user_message(context).strip()
        if not question:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)

        if self._config.mode in ("lexical", "hybrid"):
            self._store(question, r.content)
        if self._config.mode in ("embedding", "hybrid"):
            try:
                await self._store_vector(question, r.content)
            except Exception:
                logger.exception("[SemanticCache] 写入向量缓存失败")

        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def before_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    async def after_tool(self, context: MiddlewareContext) -> MiddlewareResult:
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

    def get_stats(self) -> dict[str, Any]:
        """返回缓存统计（供 /system 端点）。"""
        total_hits = sum(e.hit_count for e in self._cache)
        return {
            "entries": len(self._cache),
            "max_entries": self._config.max_entries,
            "total_hits": total_hits,
            "lexical_hits": self._lexical_hits,
            "vector_hits": self._vector_hits,
            "vector_errors": self._vector_errors,
            "mode": self._config.mode,
            "threshold": self._config.threshold,
            "vector_threshold": self._config.vector_threshold,
            "lexical_floor": self._config.lexical_floor,
            "enabled": self._config.enabled,
            "embedding_configured": self._config.embeddings is not None,
            "vector_store_configured": self._config.vector_store is not None,
        }