"""agent-core 平台工程中间件单元测试。

覆盖 P0 关键中间件：CircuitBreaker / Auth / Idempotency / Fallback / ToolCache / DLQ。
每个中间件至少覆盖：正常路径、边界条件、降级路径。
"""

import asyncio
import json
import time
from pathlib import Path

import pytest

from agent_core.middleware.auth import AuthConfig, AuthMiddleware, AuthMode, jwt_encode, jwt_decode
from agent_core.middleware.base import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareResult,
)
from agent_core.middleware.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerRegistry,
    CircuitBreakerMiddleware,
    CircuitState,
)
from agent_core.middleware.dlq import DLQConfig, DLQMiddleware, JsonlDeadLetterSink
from agent_core.middleware.fallback import FallbackConfig, FallbackMiddleware
from agent_core.middleware.idempotency import IdempotencyConfig, IdempotencyMiddleware
from agent_core.middleware.tool_cache import (
    ToolCacheConfig,
    ToolCacheMiddleware,
    InMemoryToolCacheStore,
)
from agent_core.ports.platform import DeadLetterRecord


# ── 辅助 ─────────────────────────────────────────────────

def make_ctx(**kwargs) -> MiddlewareContext:
    """构建 MiddlewareContext，字段名与 base.py 一致。"""
    return MiddlewareContext(**kwargs)


# ── CircuitBreaker ──────────────────────────────────────────

class TestCircuitBreakerRegistry:
    def test_initial_state_closed(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api")])
        assert reg.get_state("api") == CircuitState.CLOSED

    async def test_opens_after_threshold(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api", failure_threshold=3)])
        for _ in range(3):
            await reg.record_failure("api")
        assert reg.get_state("api") == CircuitState.OPEN

    async def test_check_rejects_when_open(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api", failure_threshold=1)])
        await reg.record_failure("api")
        assert not await reg.check("api")

    async def test_check_allows_when_closed(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api")])
        assert await reg.check("api")

    async def test_success_resets_failure_count(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api", failure_threshold=3)])
        await reg.record_failure("api")
        await reg.record_failure("api")
        await reg.record_success("api")
        await reg.record_failure("api")
        await reg.record_failure("api")
        # 2 次连续失败 < 3 阈值 → 仍 closed
        assert reg.get_state("api") == CircuitState.CLOSED

    async def test_half_open_after_cooldown(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api", failure_threshold=1, cooldown_seconds=0.01)])
        await reg.record_failure("api")
        assert reg.get_state("api") == CircuitState.OPEN
        await asyncio.sleep(0.05)
        assert await reg.check("api")  # cooldown 过了 → half_open 允许探测
        assert reg.get_state("api") == CircuitState.HALF_OPEN

    async def test_unregistered_service_always_passes(self):
        reg = CircuitBreakerRegistry()
        assert await reg.check("nonexistent")

    def test_get_stats(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(name="api", failure_threshold=2)])
        stats = reg.get_stats()
        assert "api" in stats
        assert stats["api"]["state"] == "closed"


class TestCircuitBreakerMiddleware:
    async def test_before_tool_blocks_when_open(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(
            name="geolocation", failure_threshold=1, tool_name_prefixes=["geo_"],
        )])
        await reg.record_failure("geolocation")
        mw = CircuitBreakerMiddleware(registry=reg)
        ctx = make_ctx(tool_name="geo_search", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.STOP

    async def test_before_tool_passes_when_closed(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(
            name="geolocation", failure_threshold=5, tool_name_prefixes=["geo_"],
        )])
        mw = CircuitBreakerMiddleware(registry=reg)
        ctx = make_ctx(tool_name="geo_search", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_no_matching_service_passes(self):
        reg = CircuitBreakerRegistry([CircuitBreakerConfig(
            name="api", failure_threshold=1, tool_name_prefixes=["other_"],
        )])
        await reg.record_failure("api")
        mw = CircuitBreakerMiddleware(registry=reg)
        ctx = make_ctx(tool_name="geo_search", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.CONTINUE


# ── Auth ─────────────────────────────────────────────────

class TestAuthMiddleware:
    def test_none_mode_always_passes(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.NONE))
        ok, err, info = mw.verify_request("/chat", {})
        assert ok and err is None

    def test_api_key_valid(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.API_KEY, api_keys={"secret-key-1"}))
        ok, err, info = mw.verify_request("/chat", {"x-api-key": "secret-key-1"})
        assert ok and err is None

    def test_api_key_invalid(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.API_KEY, api_keys={"secret-key-1"}))
        ok, err, _ = mw.verify_request("/chat", {"x-api-key": "wrong-key"})
        assert not ok and err == "无效的 API Key"

    def test_api_key_missing(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.API_KEY, api_keys={"secret-key-1"}))
        ok, err, _ = mw.verify_request("/chat", {})
        assert not ok and "缺少" in err

    def test_public_path_skips_auth(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.API_KEY, api_keys=set()))
        ok, err, _ = mw.verify_request("/health", {})
        assert ok

    def test_jwt_valid(self):
        secret = "test-secret"
        token = jwt_encode({"sub": "user-1"}, secret)
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.JWT, jwt_secret=secret))
        ok, err, payload = mw.verify_request("/chat", {"authorization": f"Bearer {token}"})
        assert ok and payload["sub"] == "user-1"

    def test_jwt_expired(self):
        secret = "test-secret"
        token = jwt_encode({"sub": "user-1", "exp": 1}, secret)  # exp=1 → 1970 年已过期
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.JWT, jwt_secret=secret))
        ok, err, _ = mw.verify_request("/chat", {"authorization": f"Bearer {token}"})
        assert not ok

    def test_jwt_wrong_secret(self):
        token = jwt_encode({"sub": "user-1"}, "secret-a")
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.JWT, jwt_secret="secret-b"))
        ok, err, _ = mw.verify_request("/chat", {"authorization": f"Bearer {token}"})
        assert not ok

    def test_jwt_missing_token(self):
        mw = AuthMiddleware(config=AuthConfig(mode=AuthMode.JWT, jwt_secret="s"))
        ok, err, _ = mw.verify_request("/chat", {})
        assert not ok and "缺少" in err


# ── Idempotency ──────────────────────────────────────────────

class TestIdempotencyMiddleware:
    async def test_first_request_passes(self):
        mw = IdempotencyMiddleware()
        ctx = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"})
        result = await mw.before_model(ctx)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_duplicate_request_rejected(self):
        mw = IdempotencyMiddleware()
        ctx1 = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"})
        ctx2 = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"})
        await mw.before_model(ctx1)
        result = await mw.before_model(ctx2)
        assert result.action == MiddlewareAction.STOP

    async def test_no_key_passes(self):
        mw = IdempotencyMiddleware()
        ctx = make_ctx(metadata={})
        result = await mw.before_model(ctx)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_disabled_passes_duplicates(self):
        mw = IdempotencyMiddleware(config=IdempotencyConfig(enabled=False))
        ctx = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"})
        await mw.before_model(ctx)
        result = await mw.before_model(make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"}))
        assert result.action == MiddlewareAction.CONTINUE

    async def test_different_sessions_same_key_passes(self):
        mw = IdempotencyMiddleware()
        ctx1 = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s1"})
        ctx2 = make_ctx(metadata={"idempotency_key": "req-1", "session_id": "s2"})
        await mw.before_model(ctx1)
        result = await mw.before_model(ctx2)
        assert result.action == MiddlewareAction.CONTINUE


# ── Fallback ─────────────────────────────────────────────────

class TestFallbackMiddleware:
    async def test_first_failure_passes(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=3))
        ctx = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, metadata={"tool_error": "timeout"})
        result = await mw.after_tool(ctx)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_below_threshold_passes(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=3))
        for _ in range(2):
            ctx = make_ctx(tool_name="geo_search", tool_args={}, metadata={"tool_error": "fail"})
            await mw.after_tool(ctx)
        ctx = make_ctx(tool_name="geo_search", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_cache_fallback_after_threshold(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=2))
        # 第一次成功 → 缓存
        ctx_ok = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, tool_result='{"lat": 39.9}')
        await mw.after_tool(ctx_ok)
        # 两次失败 → 达到阈值
        for _ in range(2):
            ctx_fail = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, metadata={"tool_error": "fail"})
            await mw.after_tool(ctx_fail)
        # 下一次 before_tool → 返回缓存
        ctx = make_ctx(tool_name="geo_search", tool_args={"q": "北京"})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.STOP
        assert result.data["fallback_source"] == "cache"
        assert "39.9" in result.data["fallback_result"]

    async def test_non_critical_skip_after_threshold(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=1))
        for _ in range(1):
            ctx = make_ctx(tool_name="get_weather", tool_args={}, metadata={"tool_error": "fail"})
            await mw.after_tool(ctx)
        ctx = make_ctx(tool_name="get_weather", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.STOP
        assert result.data["fallback_source"] == "skip"

    async def test_critical_no_cache_passes(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=1))
        for _ in range(1):
            ctx = make_ctx(tool_name="create_route", tool_args={}, metadata={"tool_error": "fail"})
            await mw.after_tool(ctx)
        ctx = make_ctx(tool_name="create_route", tool_args={})
        result = await mw.before_tool(ctx)
        # 关键工具且无缓存 → 照常放行
        assert result.action == MiddlewareAction.CONTINUE

    async def test_success_resets_count(self):
        mw = FallbackMiddleware(config=FallbackConfig(failure_threshold=2))
        for _ in range(2):
            ctx = make_ctx(tool_name="geo_search", tool_args={}, metadata={"tool_error": "fail"})
            await mw.after_tool(ctx)
        # 成功一次 → 计数清零
        ctx_ok = make_ctx(tool_name="geo_search", tool_args={}, tool_result="ok")
        await mw.after_tool(ctx_ok)
        # 一次失败不会达到阈值 2
        ctx_fail = make_ctx(tool_name="geo_search", tool_args={}, metadata={"tool_error": "fail"})
        await mw.after_tool(ctx_fail)
        ctx = make_ctx(tool_name="geo_search", tool_args={})
        result = await mw.before_tool(ctx)
        assert result.action == MiddlewareAction.CONTINUE


# ── ToolCache ────────────────────────────────────────────────

class TestToolCacheMiddleware:
    async def test_cache_miss_then_hit(self):
        mw = ToolCacheMiddleware()
        ctx = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, tool_result='{"lat": 39.9}')
        await mw.after_tool(ctx)  # 写缓存
        ctx2 = make_ctx(tool_name="geo_search", tool_args={"q": "北京"})
        result = await mw.before_tool(ctx2)
        assert result.action == MiddlewareAction.STOP
        assert result.data["tool_cache_hit"] is True

    async def test_different_args_no_hit(self):
        mw = ToolCacheMiddleware()
        ctx = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, tool_result="r1")
        await mw.after_tool(ctx)
        ctx2 = make_ctx(tool_name="geo_search", tool_args={"q": "上海"})
        result = await mw.before_tool(ctx2)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_excluded_tools_not_cached(self):
        mw = ToolCacheMiddleware()
        ctx = make_ctx(tool_name="create_route", tool_args={}, tool_result="created")
        await mw.after_tool(ctx)
        ctx2 = make_ctx(tool_name="create_route", tool_args={})
        result = await mw.before_tool(ctx2)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_disabled_always_passes(self):
        mw = ToolCacheMiddleware(config=ToolCacheConfig(enabled=False))
        ctx = make_ctx(tool_name="geo_search", tool_args={"q": "北京"}, tool_result="r1")
        await mw.after_tool(ctx)
        ctx2 = make_ctx(tool_name="geo_search", tool_args={"q": "北京"})
        result = await mw.before_tool(ctx2)
        assert result.action == MiddlewareAction.CONTINUE

    async def test_ttl_expiry(self):
        store = InMemoryToolCacheStore()
        mw = ToolCacheMiddleware(config=ToolCacheConfig(default_ttl_seconds=0.01), store=store)
        ctx = make_ctx(tool_name="lookup_city", tool_args={"q": "北京"}, tool_result="r1")
        await mw.after_tool(ctx)
        await asyncio.sleep(0.05)
        ctx2 = make_ctx(tool_name="lookup_city", tool_args={"q": "北京"})
        result = await mw.before_tool(ctx2)
        assert result.action == MiddlewareAction.CONTINUE  # TTL 已过期

    def test_get_stats(self):
        mw = ToolCacheMiddleware()
        stats = mw.get_stats()
        assert stats["enabled"] is True


# ── DLQ ──────────────────────────────────────────────────────

# 清理 DLQ 测试产生的临时目录
import shutil as _shutil
import atexit as _atexit

def _cleanup_test_dlq():
    d = Path("_test_dlq")
    if d.exists():
        _shutil.rmtree(d, ignore_errors=True)

_atexit.register(_cleanup_test_dlq)


class TestDLQSink:
    async def test_jsonl_sink_writes_record(self):
        dlq_file = Path("_test_dlq") / "dlq_test.jsonl"
        sink = JsonlDeadLetterSink(path=str(dlq_file))
        record = DeadLetterRecord(
            timestamp=time.time(),
            tool_name="geo_search",
            args_summary='{"q": "北京"}',
            error="Connection refused",
            session_id="s1",
            user_id="u1",
        )
        await sink.push(record)
        lines = dlq_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["tool_name"] == "geo_search"
        assert data["error"] == "Connection refused"

    async def test_jsonl_sink_appends(self):
        dlq_file = Path("_test_dlq") / "dlq_append.jsonl"
        sink = JsonlDeadLetterSink(path=str(dlq_file))
        for i in range(3):
            await sink.push(DeadLetterRecord(
                timestamp=time.time(), tool_name=f"tool_{i}",
                args_summary="{}", error="err", session_id="", user_id="",
            ))
        lines = dlq_file.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 3


# ── 模块导出完整性 ──────────────────────────────────────────

class TestMiddlewareExports:
    def test_all_middleware_importable(self):
        from agent_core.middleware import (
            AuthMiddleware, CircuitBreakerMiddleware, DLQMiddleware,
            FallbackMiddleware, IdempotencyMiddleware, ToolCacheMiddleware,
        )
        assert all(issubclass(c, Middleware) for c in [
            AuthMiddleware, CircuitBreakerMiddleware, DLQMiddleware,
            FallbackMiddleware, IdempotencyMiddleware, ToolCacheMiddleware,
        ])