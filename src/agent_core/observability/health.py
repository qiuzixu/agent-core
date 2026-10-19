"""聚合健康检查（Health Registry）。

把各依赖（DB / Redis / MCP Gateway / 业务服务）的健康探测注册进来，
统一输出 ``healthy / degraded / unhealthy`` 三态与逐项延迟。
服务层只需注册 ``HealthCheck`` 实现（协程或同步函数）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class HealthCheckResult:
    """单项健康检查结果。"""

    name: str
    ok: bool
    latency_ms: float = 0.0
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "latency_ms": round(self.latency_ms, 1),
            "detail": self.detail,
        }


@runtime_checkable
class HealthCheck(Protocol):
    """健康检查协议：业务层实现后注册到 HealthRegistry。"""

    @property
    def name(self) -> str: ...

    async def check_health(self) -> HealthCheckResult: ...


class CallableHealthCheck:
    """把同步/异步可调用对象包装成健康检查。

    可调用对象返回 ``False`` 或抛出异常视为不健康；其余返回值视为健康。
    """

    def __init__(self, name: str, func: Callable[..., Any], detail: str = "") -> None:
        self._name = name
        self._func = func
        self._detail = detail

    @property
    def name(self) -> str:
        return self._name

    async def check_health(self) -> HealthCheckResult:
        started = time.monotonic()
        try:
            outcome = self._func()
            if inspect.isawaitable(outcome):
                outcome = await outcome
            ok = outcome is not False
            detail = self._detail or (str(outcome) if isinstance(outcome, str) else "")
            return HealthCheckResult(
                name=self._name,
                ok=ok,
                latency_ms=(time.monotonic() - started) * 1000,
                detail=detail,
            )
        except Exception as exc:
            return HealthCheckResult(
                name=self._name,
                ok=False,
                latency_ms=(time.monotonic() - started) * 1000,
                detail=str(exc),
            )


class HealthRegistry:
    """聚合健康检查注册表：并发探测全部依赖，输出三态聚合结果。"""

    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self._checks: list[Any] = []
        self._timeout = max(0.1, float(timeout_seconds))

    def register(self, check: Any) -> None:
        self._checks.append(check)

    async def check(self) -> dict[str, Any]:
        """并发执行全部检查；单项失败/超时不影响其他项。"""
        if not self._checks:
            return {"status": "healthy", "latency_ms": 0.0, "checks": []}
        started = time.monotonic()
        tasks = {
            asyncio.create_task(
                asyncio.wait_for(check.check_health(), timeout=self._timeout)
            ): check
            for check in self._checks
        }
        results: list[dict[str, Any]] = []
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=self._timeout * 2)
            for future in done:
                check = tasks[future]
                name = getattr(check, "name", "unknown")
                try:
                    outcome = await future
                    results.append(outcome.to_dict())
                except asyncio.TimeoutError:
                    results.append(
                        {
                            "name": name,
                            "ok": False,
                            "latency_ms": round(self._timeout * 1000, 1),
                            "detail": "检查超时",
                        }
                    )
                except Exception as exc:
                    results.append({"name": name, "ok": False, "latency_ms": 0.0, "detail": str(exc)})
            if not done and pending:
                # 防御：避免病态检查造成死循环（wait_for 已按单项超时，正常不会走到这里）。
                break
        failures = [item for item in results if not item["ok"]]
        if not failures:
            status = "healthy"
        elif len(failures) == len(results) and results:
            status = "unhealthy"
        else:
            status = "degraded"
        return {
            "status": status,
            "latency_ms": round((time.monotonic() - started) * 1000, 1),
            "checks": results,
        }


__all__ = ["CallableHealthCheck", "HealthCheck", "HealthCheckResult", "HealthRegistry"]