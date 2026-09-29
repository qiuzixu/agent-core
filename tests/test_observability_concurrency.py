"""可观测性中间件的并发上下文回归测试。"""

from __future__ import annotations

import asyncio
import uuid

from agent_core.middleware import MiddlewareContext
from agent_core.observability import ObservabilityMiddleware, agent_stats
from agent_core.protocol import assistant_message, user_message


async def test_observability_uses_per_call_thread_and_timing_state() -> None:
    middleware = ObservabilityMiddleware()
    first_thread = f"obs-{uuid.uuid4()}"
    second_thread = f"obs-{uuid.uuid4()}"

    async def invoke(thread_id: str, delay: float) -> None:
        context = MiddlewareContext(
            messages=[user_message("查询")],
            metadata={"thread_id": thread_id},
        )
        await middleware.before_model(context)
        await asyncio.sleep(delay)
        context.llm_response = assistant_message("完成")
        await middleware.after_model(context)

    await asyncio.gather(invoke(first_thread, 0.01), invoke(second_thread, 0))

    assert agent_stats.get_stats(first_thread).total_llm_calls == 1
    assert agent_stats.get_stats(second_thread).total_llm_calls == 1
    assert agent_stats.get_stats(first_thread).llm_records[0].latency_ms >= 5
