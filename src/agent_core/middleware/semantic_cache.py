"""语义缓存中间件。

对相同语义但措辞不同的问题命中缓存，避免重复调用 LLM。
基于文本相似度（Jaccard + 编辑距离混合），不依赖外部 embedding 服务。
保持 agent-core 零依赖原则。

工作原理：
1. before_model: 计算用户最新消息与缓存中已有请求的相似度
2. 如果相似度 >= threshold 且在 TTL 内，直接返回缓存结果（STOP）
3. after_model: 如果 LLM 返回了最终答案，存入缓存

配置：
    AGENT_SEMANTIC_CACHE_ENABLED: 是否启用（默认 false）
    AGENT_SEMANTIC_CACHE_THRESHOLD: 相似度阈值 0.0~1.0（默认 0.92，高阈值降低误命中）
    AGENT_SEMANTIC_CACHE_TTL_SECONDS: 缓存有效期（默认 3600s）
    AGENT_SEMANTIC_CACHE_MAX_ENTRIES: 最大缓存条目（默认 500）
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)

logger = logging.getLogger(__name__)

__all__ = ["SemanticCacheConfig", "SemanticCacheMiddleware"]


@dataclass
class SemanticCacheConfig:
    """语义缓存配置。"""

    enabled: bool = False
    threshold: float = 0.92  # 高阈值降低误命中
    ttl_seconds: float = 3600.0
    max_entries: int = 500
    # 排除缓存的消息模式（如工具调用轮次）
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class _CacheEntry:
    """缓存条目。"""

    fingerprint: str
    question: str
    response: str
    timestamp: float
    hit_count: int = 0


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


class SemanticCacheMiddleware(Middleware):
    """语义缓存：相似问题直接返回缓存结果，省去 LLM 调用。"""

    def __init__(self, config: SemanticCacheConfig | None = None) -> None:
        self._config = config or SemanticCacheConfig()
        self._cache: list[_CacheEntry] = []

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
        """查找相似度最高的缓存条目。"""
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

    def _store(self, question: str, response: str) -> None:
        """存入缓存。"""
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

    async def before_model(self, context: MiddlewareContext) -> MiddlewareResult:
        if not self._config.enabled:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        # 只在首轮迭代做缓存命中
        if context.iteration > 0:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        question = self._get_latest_user_message(context)
        if not question or len(question.strip()) < 4:
            return MiddlewareResult(action=MiddlewareAction.CONTINUE)
        entry = self._lookup(question.strip())
        if entry:
            entry.hit_count += 1
            logger.info(
                "[SemanticCache] 命中缓存 (相似度>=%.2f, 命中次数=%d): %s...",
                self._config.threshold,
                entry.hit_count,
                entry.fingerprint,
            )
            return MiddlewareResult(
                action=MiddlewareAction.STOP,
                data={
                    "cached_response": entry.response,
                    "cache_hit": True,
                    "cache_fingerprint": entry.fingerprint,
                },
            )
        return MiddlewareResult(action=MiddlewareAction.CONTINUE)

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
        question = self._get_latest_user_message(context)
        if question:
            self._store(question.strip(), r.content)
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
            "threshold": self._config.threshold,
            "enabled": self._config.enabled,
        }
