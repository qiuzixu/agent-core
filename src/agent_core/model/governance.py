"""模型并发、窗口限流、熔断和 fallback 治理。"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from agent_core.errors import (
    ModelInvocationError,
    ModelRateLimitError,
    ModelTimeoutError,
    ModelUnavailableError,
)
from agent_core.model.core import ContextUsage, ModelAdapter, StreamChunk
from agent_core.protocol.messages import Message

_CURRENT_SCOPE: ContextVar[str] = ContextVar("agent_core_model_scope", default="default")


@contextmanager
def model_execution_scope(scope: str) -> Iterator[None]:
    """为当前异步调用链设置模型治理范围，通常使用租户或用户 ID。"""

    normalized = scope.strip() or "default"
    token = _CURRENT_SCOPE.set(normalized)
    try:
        yield
    finally:
        _CURRENT_SCOPE.reset(token)


@dataclass(frozen=True)
class ModelExecutionPolicy:
    """模型调用治理策略。"""

    max_concurrency: int = 8
    requests_per_window: int | None = None
    tokens_per_window: int | None = None
    window_seconds: float = 60.0
    queue_timeout_seconds: float | None = 30.0
    call_timeout_seconds: float | None = 120.0
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency 必须大于 0")
        if self.requests_per_window is not None and self.requests_per_window <= 0:
            raise ValueError("requests_per_window 必须大于 0")
        if self.tokens_per_window is not None and self.tokens_per_window <= 0:
            raise ValueError("tokens_per_window 必须大于 0")
        if self.window_seconds <= 0:
            raise ValueError("window_seconds 必须大于 0")
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold 必须大于 0")


class SlidingWindowRateLimiter:
    """按 scope 统计请求数和估算输入 Token 的滑动窗口限流器。"""

    def __init__(self, policy: ModelExecutionPolicy) -> None:
        self._policy = policy
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._tokens: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def acquire(self, scope: str, estimated_tokens: int) -> None:
        deadline = (
            None
            if self._policy.queue_timeout_seconds is None
            else time.monotonic() + self._policy.queue_timeout_seconds
        )
        while True:
            async with self._lock:
                now = time.monotonic()
                wait_for = self._reserve_or_wait(scope, estimated_tokens, now)
                if wait_for <= 0:
                    return
            if deadline is not None and time.monotonic() + wait_for > deadline:
                raise ModelRateLimitError(f"模型限流队列等待超过 {self._policy.queue_timeout_seconds} 秒")
            await asyncio.sleep(wait_for)

    def _reserve_or_wait(self, scope: str, estimated_tokens: int, now: float) -> float:
        cutoff = now - self._policy.window_seconds
        requests = self._requests[scope]
        tokens = self._tokens[scope]
        while requests and requests[0] <= cutoff:
            requests.popleft()
        while tokens and tokens[0][0] <= cutoff:
            tokens.popleft()

        waits: list[float] = []
        request_limit = self._policy.requests_per_window
        if request_limit is not None and len(requests) >= request_limit:
            waits.append(requests[0] + self._policy.window_seconds - now)
        token_limit = self._policy.tokens_per_window
        token_total = sum(item[1] for item in tokens)
        if token_limit is not None and token_total + estimated_tokens > token_limit:
            if estimated_tokens > token_limit:
                raise ModelRateLimitError("单次请求估算 Token 已超过窗口上限")
            waits.append(tokens[0][0] + self._policy.window_seconds - now if tokens else 0.01)
        if waits:
            return max(0.01, min(waits))
        requests.append(now)
        tokens.append((now, max(0, estimated_tokens)))
        return 0.0


class _ScopedConcurrencyLimiter:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: dict[str, asyncio.Semaphore] = {}
        self._lock = asyncio.Lock()

    async def _get(self, scope: str) -> asyncio.Semaphore:
        async with self._lock:
            return self._items.setdefault(scope, asyncio.Semaphore(self._limit))

    @asynccontextmanager
    async def slot(self, scope: str, acquire_timeout: float | None) -> AsyncIterator[None]:
        semaphore = await self._get(scope)
        try:
            if acquire_timeout is None:
                await semaphore.acquire()
            else:
                await asyncio.wait_for(semaphore.acquire(), timeout=acquire_timeout)
        except TimeoutError as exc:
            raise ModelRateLimitError(f"模型并发队列等待超过 {acquire_timeout} 秒") from exc
        try:
            yield
        finally:
            semaphore.release()


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float | None = None


class GovernedModelAdapter:
    """为任意 ModelAdapter 增加限流、超时、熔断和候选模型降级。"""

    def __init__(
        self,
        primary: ModelAdapter,
        *,
        fallbacks: list[ModelAdapter] | None = None,
        policy: ModelExecutionPolicy | None = None,
        scope_resolver: Callable[[], str] | None = None,
        max_circuit_entries: int = 4096, #
    ) -> None:
        self._adapters = [primary, *(fallbacks or [])]
        self._policy = policy or ModelExecutionPolicy()
        self._rate_limiter = SlidingWindowRateLimiter(self._policy)
        self._concurrency = _ScopedConcurrencyLimiter(self._policy.max_concurrency)
        self._scope_resolver = scope_resolver or _CURRENT_SCOPE.get
        self._circuits: dict[tuple[int, str], _CircuitState] = {}
        self._circuit_lock = asyncio.Lock()
        if max_circuit_entries <= 0:
            raise ValueError("max_circuit_entries 必须大于 0")
        self._max_circuit_entries = max_circuit_entries

    def _evict_circuits_locked(self) -> None:
        """熔断状态按 (adapter, scope) 累积；容量满时先淘汰干净状态，再按插入序淘汰最旧。"""
        if len(self._circuits) < self._max_circuit_entries:
            return
        for key, state in list(self._circuits.items()):
            if len(self._circuits) < self._max_circuit_entries:
                break
            if state.opened_at is None and state.failures == 0:
                self._circuits.pop(key, None)
        while len(self._circuits) >= self._max_circuit_entries:
            self._circuits.pop(next(iter(self._circuits)))

    @staticmethod
    def _estimate_tokens(messages: list[Message]) -> int:
        return max(1, sum(len(message.content) for message in messages) // 4)

    async def _allow(self, index: int, scope: str) -> bool:
        async with self._circuit_lock:
            self._evict_circuits_locked()
            state = self._circuits.setdefault((index, scope), _CircuitState())
            if state.opened_at is None:
                return True
            if time.monotonic() - state.opened_at < self._policy.recovery_timeout_seconds:
                return False
            state.opened_at = None
            state.failures = max(0, self._policy.failure_threshold - 1)
            return True

    async def _success(self, index: int, scope: str) -> None:
        async with self._circuit_lock:
            self._circuits[(index, scope)] = _CircuitState()

    async def _failure(self, index: int, scope: str) -> None:
        async with self._circuit_lock:
            self._evict_circuits_locked()
            state = self._circuits.setdefault((index, scope), _CircuitState())
            state.failures += 1
            if state.failures >= self._policy.failure_threshold:
                state.opened_at = time.monotonic()

    async def _any_adapter_available(self, scope: str) -> bool:
        """是否存在至少一个未处于熔断恢复期的候选；短路探测，无副作用。"""
        for index in range(len(self._adapters)):
            if await self._allow(index, scope):
                return True
        return False

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        scope = self._scope_resolver().strip() or "default"
        last_error: Exception | None = None
        # 全部候选处于熔断恢复期时直接失败，不消耗本地限流配额。
        if not await self._any_adapter_available(scope):
            raise ModelUnavailableError("没有可用的模型适配器（全部处于熔断恢复期）")
        # 一个逻辑调用只消耗一次本地限流配额；fallback 属于同一次调用。
        await self._rate_limiter.acquire(scope, self._estimate_tokens(messages))
        async with self._concurrency.slot(scope, self._policy.queue_timeout_seconds):
            for index, adapter in enumerate(self._adapters):
                if not await self._allow(index, scope):
                    last_error = ModelUnavailableError(f"第 {index + 1} 个模型适配器熔断中")
                    continue
                try:
                    async with asyncio.timeout(self._policy.call_timeout_seconds):
                        result = await adapter.chat(
                            messages,
                            tools=tools,
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                    await self._success(index, scope)
                    return result
                except TimeoutError as exc:
                    last_error = ModelTimeoutError("模型调用超时")
                    last_error.__cause__ = exc
                    await self._failure(index, scope)
                except ModelInvocationError as exc:
                    last_error = exc
                    await self._failure(index, scope)
        if last_error is None:
            raise ModelUnavailableError("没有可用的模型适配器")
        raise last_error

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        scope = self._scope_resolver().strip() or "default"
        last_error: Exception | None = None
        # 与 chat() 相同：全部候选熔断时直接失败，不消耗本地限流配额。
        if not await self._any_adapter_available(scope):
            raise ModelUnavailableError("没有可用的模型适配器（全部处于熔断恢复期）")
        await self._rate_limiter.acquire(scope, self._estimate_tokens(messages))
        async with self._concurrency.slot(scope, self._policy.queue_timeout_seconds):
            for index, adapter in enumerate(self._adapters):
                emitted = False
                if not await self._allow(index, scope):
                    last_error = ModelUnavailableError(f"第 {index + 1} 个模型适配器熔断中")
                    continue
                try:
                    async with asyncio.timeout(self._policy.call_timeout_seconds):
                        async for chunk in adapter.stream_chat(
                            messages,
                            tools=tools,
                            temperature=temperature,
                            max_tokens=max_tokens,
                        ):
                            emitted = True
                            yield chunk
                    await self._success(index, scope)
                    return
                except TimeoutError as exc:
                    last_error = ModelTimeoutError("模型流式调用超时")
                    last_error.__cause__ = exc
                    await self._failure(index, scope)
                except ModelInvocationError as exc:
                    last_error = exc
                    await self._failure(index, scope)
                if emitted and last_error is not None:
                    raise last_error
        if last_error is None:
            raise ModelUnavailableError("没有可用的模型适配器")
        raise last_error

    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
    ) -> ContextUsage:
        return await self._adapters[0].context_usage(messages, tools=tools)


__all__ = [
    "GovernedModelAdapter",
    "ModelExecutionPolicy",
    "SlidingWindowRateLimiter",
    "model_execution_scope",
]
