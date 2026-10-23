"""手写 ReAct 循环（tool-calling agent）。

执行模型：
1. LLM 收到用户问题 + 工具描述
2. LLM 决定调用哪些工具（或直接回答）
3. 若有 tool_calls → 执行工具 → 把结果追加到消息历史 → 再问 LLM
4. 若无 tool_calls → 输出最终文本答案
5. 超过 max_iterations 且仍有工具调用 → 明确失败，避免把中间文本当成最终回答

stream() 方法的工具调用处理：
- 模型文本到达时立即逐块 yield
- 工具调用收集完整后并发执行，工具结果写入消息，再进入下一轮模型调用

共享内核与策略点：
- ``run`` / ``stream`` 是两个薄包装，只负责 Run 生命周期收口（状态、事件、异常）。
- 消息构造、checkpoint 恢复、中间件 before/after、工具预算守卫、工具执行和
  checkpoint 保存只在私有异步生成器 ``_iterate`` 中实现一次。
- 两条路径的差异点集中在 ``_LoopMode``：非流式一轮调用 ``llm.chat``；
  流式一轮消费 ``llm.stream_chat``，文本增量经 ``_LoopEvent.text_delta``
  逐块向上游转发，tool_calls 增量按 ID 归并后组装成 assistant 消息。
  流式"已发文本不可重试"的守卫也由内核依据 text_delta 是否非空统一执行。

Checkpoint 集成：
- 提供 checkpointer 参数后，每轮 LLM 调用后自动保存快照
- 支持时间旅行：可通过 agent.get_history(thread_id) 查看历史版本
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_core.checkpoint import Checkpointer
from agent_core.errors import (
    ModelInvocationError,
    ModelOutputValidationError,
)
from agent_core.middleware import (
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareManager,
    ParallelToolPolicy,
)
from agent_core.compaction.region import estimate_input_tokens
from agent_core.model import ContextUsage, ModelAdapter, StreamChunk
from agent_core.protocol.messages import (
    Message,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from agent_core.protocol.runtime import RunContext, RunStatus
from agent_core.runtime.state import transition_run_status
from agent_core.tools import ToolExecutor, ToolResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _LoopMode:
    """run / stream 两条路径的差异点。

    共享内核 ``_iterate`` 只通过该模式对象感知两条路径的区别：
    - 模型一轮调用的实现方式（``streaming`` 分派到 ``_chat_round`` /
      ``_stream_round``）；
    - 用户可见的错误文案与日志标识（``label`` / ``invocation_failure``）。
    对外行为由 ``run`` / ``stream`` 两个薄包装保持不变。
    """

    streaming: bool
    # 日志文案中的路径标识：普通路径为空，流式路径为 "流式"。
    label: str
    # 模型调用失败文案中"轮"之后、冒号之前的片段，保持历史措辞逐字不变。
    invocation_failure: str

    @property
    def empty_final_error(self) -> str:
        """最终响应为空（无工具调用也没有文本）的失败文案。"""
        return f"ReAct agent {self.label}最终响应为空，不能使用上一轮中间文本作为答案。"

    @property
    def missing_answer_error(self) -> str:
        """循环结束仍未提取到有效回答的失败文案。"""
        return f"ReAct agent {self.label}未能提取到有效回答。"

    def invocation_error(self, iteration: int, exc: Exception) -> ModelInvocationError:
        """构造一轮模型调用的失败异常，保持历史措辞逐字不变。"""
        return ModelInvocationError(
            f"ReAct agent 第 {iteration + 1} 轮{self.invocation_failure}：{exc}"
        )


# 普通路径：一轮模型调用 = llm.chat，无文本增量。
_RUN_MODE = _LoopMode(streaming=False, label="", invocation_failure=" LLM 调用失败")
# 流式路径：一轮模型调用 = llm.stream_chat，文本增量逐块转发。
_STREAM_MODE = _LoopMode(streaming=True, label="流式", invocation_failure="流式调用失败")


@dataclass(frozen=True)
class _LoopEvent:
    """共享内核向薄包装暴露的单个事件。

    Attributes:
        text_delta: 流式文本增量；普通路径恒为空。
        answer: Agent Loop 正常结束时的最终回答；循环中不产出。
    """

    text_delta: str = ""
    answer: str | None = None


class ReActAgent:
    """手写 ReAct 循环的 tool-calling agent。"""

    def __init__(
        self,
        llm: ModelAdapter,
        tool_executor: ToolExecutor,
        *,
        system_prompt: str,
        tool_definitions: list[dict[str, Any]],
        max_iterations: int = 5,
        max_tool_calls: int = 20,
        middleware: list[Middleware] | None = None,
        checkpointer: Checkpointer | None = None,
        thread_id: str = "default",
        max_repeated_tool_calls: int = 3,
        parallel_policy: ParallelToolPolicy | None = None,
    ) -> None:
        self._llm = llm
        self._tool_executor = tool_executor
        self._system_prompt = system_prompt
        self._tool_definitions = tool_definitions
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
        self._max_repeated_tool_calls = max(0, int(max_repeated_tool_calls))
        self._parallel_policy = parallel_policy or ParallelToolPolicy.from_env()
        self._middleware_manager = MiddlewareManager(middleware)
        self._checkpointer = checkpointer
        self._thread_id = thread_id

    # ──────────────────────────────────────────────
    # 普通（非流式）执行
    # ──────────────────────────────────────────────

    async def run(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> str:
        """执行完整的 Agent Loop，并统一收口运行状态。"""
        runtime_context = self._prepare_context(
            user_input,
            thread_id=thread_id,
            run_metadata=run_metadata,
            run_context=run_context,
            checkpoint_state=checkpoint_state,
        )
        try:
            answer = ""
            async for event in self._iterate(
                _RUN_MODE,
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_metadata=run_metadata,
                run_context=runtime_context,
                checkpoint_state=checkpoint_state,
            ):
                if event.answer is not None:
                    answer = event.answer
        except asyncio.CancelledError:
            # 租约丢失引发的取消由 Runtime 收口为 interrupted，Agent 不抢落终态。
            if not runtime_context.metadata.get("_lease_lost", False):
                await self._finish_run(runtime_context, "cancelled", "run_cancelled")
            raise
        except Exception as exc:
            await self._finish_run(
                runtime_context,
                "failed",
                "run_failed",
                error=str(exc),
            )
            raise

        await self._finish_run(
            runtime_context,
            "completed",
            "run_completed",
            answer=answer,
        )
        return answer

    # ──────────────────────────────────────────────
    # 流式执行（完整支持工具调用）
    # ──────────────────────────────────────────────

    async def stream(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """逐块流式执行 Agent Loop，并统一收口运行状态。"""
        runtime_context = self._prepare_context(
            user_input,
            thread_id=thread_id,
            run_metadata=run_metadata,
            run_context=run_context,
            checkpoint_state=checkpoint_state,
        )
        try:
            async for event in self._iterate(
                _STREAM_MODE,
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_metadata=run_metadata,
                run_context=runtime_context,
                checkpoint_state=checkpoint_state,
            ):
                if event.text_delta:
                    yield event.text_delta
        except (asyncio.CancelledError, GeneratorExit):
            # 流式取消同理：lease_lost 标记存在时不抢先保存 cancelled。
            if not runtime_context.metadata.get("_lease_lost", False):
                await self._finish_run(runtime_context, "cancelled", "run_cancelled")
            raise
        except Exception as exc:
            await self._finish_run(
                runtime_context,
                "failed",
                "run_failed",
                error=str(exc),
            )
            raise

        await self._finish_run(runtime_context, "completed", "run_completed")

    # ──────────────────────────────────────────────
    # 共享 Agent Loop 内核
    # ──────────────────────────────────────────────

    def _prepare_context(
        self,
        user_input: str,
        *,
        thread_id: str | None,
        run_metadata: dict[str, Any] | None,
        run_context: RunContext | None,
        checkpoint_state: dict[str, Any] | None,
    ) -> RunContext:
        """构造或复用 RunContext，并回填 checkpoint 快照基底。

        直接 API 以 checkpoint_state 恢复时回填快照基底；否则恢复批次期间
        ``_persist_tool_progress`` 会以空 dict 为基底，丢失
        iteration/last_answer/tool_calls_used。
        """
        metadata = dict(run_metadata or {})
        runtime_context = run_context or RunContext(
            thread_id=thread_id or self._thread_id,
            run_id=str(metadata.get("run_id") or uuid.uuid4()),
            metadata=metadata,
        )
        if run_context is None:
            runtime_context.emit("run_started", input=user_input)
        if checkpoint_state and not runtime_context.checkpoint:
            runtime_context.checkpoint = checkpoint_state
        return runtime_context

    # 主循环：消息构造 -> checkpoint 恢复 -> before_model -> LLM -> after_model
    # -> 工具执行 -> checkpoint 保存 -> 下一轮模型调用。
    async def _iterate(
        self,
        mode: _LoopMode,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
        run_context: RunContext,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> AsyncIterator[_LoopEvent]:
        """run / stream 共享的 Agent Loop 内核。

        生命周期状态由 ``run`` / ``stream`` 统一管理；本方法只驱动循环本身。
        流式路径每次 yield 一个文本增量事件；循环正常结束时统一产出
        ``_LoopEvent.answer``（含 completed checkpoint 直接恢复的场景）。

        Yields:
            ``_LoopEvent``：text_delta 为流式文本增量，answer 为最终回答。
        """
        # 1. 恢复或新建消息历史。
        #    有 checkpoint.messages 时继续使用完整历史；否则由 system + memory + 用户输入构造。
        checkpoint = checkpoint_state or {}
        messages = (
            self._messages_from_checkpoint(checkpoint)
            if checkpoint.get("messages")
            else self._build_initial_messages(user_input, memory_context)
        )
        # 2. 初始化恢复所需的运行变量。
        #    last_answer 只保留最近一次有效文本；metadata 会透传给中间件。
        last_answer = str(checkpoint.get("last_answer", ""))
        metadata = self._runtime_metadata(run_context, dict(run_metadata or {}))
        transition_run_status(run_context, "running")
        # 恢复执行时沿用已有上下文的会话 ID，避免快照落到默认会话；
        # 流式与普通执行使用同一套会话隔离规则。
        checkpoint_thread = thread_id or run_context.thread_id

        # 3. 已完成快照不用重跑，直接返回最终回答。
        if checkpoint.get("phase") == "completed":
            if last_answer:
                yield _LoopEvent(answer=last_answer)
                return
            raise ModelOutputValidationError("completed checkpoint 缺少最终回答")

        # 4. 断点恢复阶段：优先重放上次还没执行完的工具。
        #    phase=execute_tools 表示 LLM 已产出工具调用，但部分工具尚未完成。
        start_iteration = int(checkpoint.get("next_iteration", checkpoint.get("iteration", 0)))
        pending_tool_calls = checkpoint.get("pending_tool_calls")
        if checkpoint.get("phase") == "execute_tools" and isinstance(pending_tool_calls, list):
            restore_ctx = MiddlewareContext(
                messages=messages,
                iteration=int(checkpoint.get("iteration", start_iteration)),
                metadata=metadata,
                emit=run_context.emit,
            )
            # 然后补执行之前未完成的工具（只重放 pending 集合，已完成的不重放）：
            messages = await self._execute_tools(pending_tool_calls, messages, restore_ctx, run_context)
            # 工具执行完成后，把阶段改回模型调用：
            start_iteration = int(checkpoint.get("iteration", 0)) + 1
            # 然后保存一次新的 checkpoint
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                start_iteration - 1,
                last_answer,
                int(checkpoint.get("tool_calls_used", 0)),
                run_context,
                phase="model",
                pending_tool_calls=[],
            )

        # 5. 普通主循环：最多 max_iterations 轮；每轮都可能“模型回答”或“模型调工具”。
        tool_calls_used = int(checkpoint.get("tool_calls_used", 0))
        for iteration in range(start_iteration, self._max_iterations):
            # 每轮使用独立 MiddlewareContext，避免上一轮的临时状态泄漏到下一轮。
            run_context.iteration = iteration
            ctx = MiddlewareContext(
                messages=messages,
                iteration=iteration,
                metadata=metadata,
                emit=run_context.emit,
            )

            # 5.1 before_model：中间件可以拦截请求，或压缩/改写消息历史。
            mw_result = await self._middleware_manager.execute_before_model(ctx)
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型调用被中间件阻止")
                )
            if mw_result.action == MiddlewareAction.MODIFY:
                messages = mw_result.data.get("messages", messages)

            # 5.2 策略点：一轮模型调用。
            # 非流式：llm.chat 返回完整响应；流式：消费 stream_chat，
            # 文本增量逐块转发给调用方，tool_calls 增量按 ID 归并。
            response: Message | None = None
            text_parts: list[str] = []
            async for item in self._model_round(mode, messages, iteration, ctx, run_context):
                if isinstance(item, Message):
                    response = item
                elif item.text:
                    text_parts.append(item.text)
                    # 真正逐块向上游发送；调用方可以立即转成 SSE 数据帧。
                    yield _LoopEvent(text_delta=item.text)
            assert response is not None

            # 5.3 保存模型响应。
            messages.append(response)
            # 这一步非常关键：下一轮模型必须看到自己上一轮说的话和工具调用，
            # 否则 tool_calls 与后续 tool_message 会失去配对关系。
            run_context.emit(
                "model_finished",
                iteration=iteration,
                tool_calls=len(response.tool_calls),
            )
            ctx.llm_response = response

            # 5.4 after_model：中间件可校验、改写、要求重试或阻断模型输出。
            mw_result = await self._middleware_manager.execute_after_model(ctx)
            if mw_result.action == MiddlewareAction.RETRY:
                messages.pop()
                if text_parts:
                    # 流式路径特有：文本已经发给上游，无法安全重试当前响应。
                    raise ModelOutputValidationError("流式文本已经发送，after_model 不能安全重试当前响应。")
                continue
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型输出被中间件阻止")
                )

            if response.content:
                last_answer = response.content

            # 5.5 全局工具预算守卫：防止模型在多轮循环中耗尽外部资源。
            next_tool_calls_used = tool_calls_used + len(response.tool_calls)
            if next_tool_calls_used > self._max_tool_calls:
                raise ModelOutputValidationError(
                    f"ReAct agent 工具调用次数超过上限（{self._max_tool_calls}）"
                )

            # ── Checkpoint：每轮 LLM 回复后自动保存快照 ──────────
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                iteration,
                last_answer,
                next_tool_calls_used,
                run_context,
                phase="execute_tools" if response.tool_calls else "completed",
                pending_tool_calls=response.tool_calls,
                metadata={**metadata, "iteration": iteration, "answer_preview": last_answer[:80]},
            )

            # 5.6 没有工具调用时，本轮文本就是最终答案。
            if not response.tool_calls:
                if not response.content:
                    raise ModelOutputValidationError(mode.empty_final_error)
                logger.debug("ReAct %s第 %d 轮结束（无工具调用）", mode.label, iteration + 1)
                break
            # 11. 有工具调用：检查数量
            tool_calls_used = next_tool_calls_used
            run_context.tool_calls_used = tool_calls_used

            # 5.7 死循环守卫：必须放在工具真正执行前，避免继续产生副作用。
            self._assert_no_tool_loop(messages, run_context)

            if mode.streaming:
                logger.debug(
                    "ReAct 流式第 %d 轮执行工具: %s",
                    iteration + 1,
                    [tc["name"] for tc in response.tool_calls],
                )
            # 执行工具调用
            messages = await self._execute_tools(response.tool_calls, messages, ctx, run_context)
            # 13. 工具执行后再次保存 checkpoint
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                iteration,
                last_answer,
                tool_calls_used,
                run_context,
                phase="model",
                pending_tool_calls=[],
                metadata={**metadata, "iteration": iteration, "answer_preview": last_answer[:80]},
            )

        else:
            logger.warning("ReAct %s达到最大迭代次数 %d", mode.label, self._max_iterations)
            raise ModelOutputValidationError(
                f"ReAct agent 达到最大迭代次数（{self._max_iterations}），尚未生成不含工具调用的最终回答。"
            )

        # 6. 循环正常结束时，必须有可返回的最终回答。
        if not last_answer:
            raise ModelOutputValidationError(mode.missing_answer_error)

        yield _LoopEvent(answer=last_answer)

    async def _model_round(
        self,
        mode: _LoopMode,
        messages: list[Message],
        iteration: int,
        ctx: MiddlewareContext,
        runtime_context: RunContext,
    ) -> AsyncIterator[Message | StreamChunk]:
        """按路径策略执行一轮模型调用。

        非流式策略产出单个 ``Message``；流式策略把 ``StreamChunk`` 原样
        转发（文本增量由内核继续向上游 yield），最后产出组装好的
        assistant ``Message``。异常重试由策略内部闭环。
        """
        if mode.streaming:
            async for item in self._stream_round(mode, messages, iteration, ctx, runtime_context):
                yield item
        else:
            async for item in self._chat_round(mode, messages, iteration, ctx, runtime_context):
                yield item

    async def _chat_round(
        self,
        mode: _LoopMode,
        messages: list[Message],
        iteration: int,
        ctx: MiddlewareContext,
        runtime_context: RunContext,
    ) -> AsyncIterator[Message]:
        """非流式策略：一轮 ``llm.chat``，中间件允许时重试后返回完整响应。"""
        while True:
            try:
                yield await self._llm.chat(
                    messages,
                    tools=self._tool_definitions or None,
                )
                return
            except Exception as exc:
                if await self._middleware_manager.handle_exception(exc, ctx):
                    runtime_context.emit("model_retry", iteration=iteration, error=str(exc))
                    continue
                raise mode.invocation_error(iteration, exc) from exc

    async def _stream_round(
        self,
        mode: _LoopMode,
        messages: list[Message],
        iteration: int,
        ctx: MiddlewareContext,
        runtime_context: RunContext,
    ) -> AsyncIterator[Message | StreamChunk]:
        """流式策略：消费 ``stream_chat`` 并归并增量。

        文本增量原样转发；工具调用增量按调用 ID（缺省按位置）归并，
        args 字典逐块合并；重试时文本与工具调用增量都从头重新收集
        （已转发给上游的文本无法撤回，与历史行为一致）。
        """
        text_parts: list[str] = []
        tool_calls_by_key: dict[str, dict[str, Any]] = {}
        # 同一轮内允许中间件决定重试；每次重试都重新收集本轮增量。
        while True:
            text_parts.clear()
            tool_calls_by_key.clear()
            try:
                async for chunk in self._llm.stream_chat(
                    messages,
                    tools=self._tool_definitions or None,
                ):
                    # 文本增量立即向上游转发，调用方可以转成 SSE/WebSocket。
                    if chunk.text:
                        text_parts.append(chunk.text)
                        yield chunk

                    # 工具调用增量不是一次到达；必须按 ID/位置归并成完整调用。
                    if chunk.is_tool_call and chunk.tool_calls:
                        for call_index, tool_call in enumerate(chunk.tool_calls):
                            key = str(tool_call.get("id") or f"index:{call_index}")
                            current = tool_calls_by_key.setdefault(key, {})
                            current.update(
                                {
                                    field: value
                                    for field, value in tool_call.items()
                                    if field != "args" and value is not None and value != ""
                                }
                            )
                            args = tool_call.get("args")
                            if isinstance(args, dict):
                                current_args = current.setdefault("args", {})
                                if isinstance(current_args, dict):
                                    current_args.update(args)
                                else:
                                    current["args"] = dict(args)

            except Exception as exc:
                if await self._middleware_manager.handle_exception(exc, ctx):
                    runtime_context.emit("model_retry", iteration=iteration, error=str(exc))
                    continue
                raise mode.invocation_error(iteration, exc) from exc
            break

        # 把所有流式增量组装成一条 assistant 消息，供后续循环/持久化使用。
        yield assistant_message("".join(text_parts), tool_calls=list(tool_calls_by_key.values()))

    # ──────────────────────────────────────────────
    # 内部辅助方法
    # ──────────────────────────────────────────────

    @staticmethod
    def _runtime_metadata(
        runtime_context: RunContext,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        """把 Run 身份写入中间件元数据，供审批和审计中间件使用。"""
        result = {**runtime_context.metadata, **metadata}
        result["run_id"] = runtime_context.run_id
        result["thread_id"] = runtime_context.thread_id
        if runtime_context.user_id is not None:
            result["user_id"] = runtime_context.user_id
        if runtime_context.tenant_id is not None:
            result["tenant_id"] = runtime_context.tenant_id
        return result

    def _build_initial_messages(
        self,
        user_input: str,
        memory_context: str | None,
    ) -> list[Message]:
        """构造初始消息列表。"""
        msgs: list[Message] = [system_message(self._system_prompt)]
        user_content = (
            f"会话相关记忆：\n{memory_context}\n\n用户问题：{user_input}" if memory_context else user_input
        )
        msgs.append(user_message(user_content))
        return msgs

    async def context_usage(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
    ) -> ContextUsage:
        """统计下一次模型调用的输入 token 与窗口上限。

        精确优先：Provider 提供 tokenizer 时直接返回其计数。
        估算兜底：Provider 没有 tokenizer（例如标准 OpenAI 兼容端点）时，
        按“消息 + 工具 schema”的字符数估算，并用 exact=False 明确标记，
        供 /context/usage 前端区分“精确值”与“估算值”。
        """
        messages = self._build_initial_messages(user_input, memory_context)
        usage = await self._llm.context_usage(messages, tools=self._tool_definitions)
        if usage.input_tokens is not None:
            return usage
        return ContextUsage(
            input_tokens=estimate_input_tokens(
                messages,
                tool_definitions=self._tool_definitions,
            ),
            context_window_tokens=usage.context_window_tokens,
            exact=False,
            source=f"{usage.source}+char_estimate",
        )

    @staticmethod
    def _messages_from_checkpoint(state: dict[str, Any]) -> list[Message]:
        """从 checkpoint 恢复内部消息，兼容 OpenAI tool-call 格式。"""
        messages: list[Message] = []
        # checkpoint 里存的是 message.to_dict()；这里反向恢复为内部 Message 对象。
        for item in state.get("messages", []):
            if not isinstance(item, dict):
                continue
            # 兼容两种历史格式：新的 Core 扁平格式和旧的 OpenAI function.call 格式。
            tool_calls: list[dict[str, Any]] = []
            for call in item.get("tool_calls", []) or []:
                if not isinstance(call, dict):
                    continue
                function = call.get("function", {})
                if isinstance(function, dict) and function:
                    raw_args = function.get("arguments", "{}")
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    name = function.get("name", call.get("name", ""))
                else:
                    # 新 checkpoint 使用 Core 内部扁平格式；兼容旧 OpenAI 快照。
                    args = call.get("args", {})
                    name = call.get("name", "")
                tool_calls.append(
                    {
                        "id": call.get("id", ""),
                        "type": "function",
                        "name": name,
                        "args": args if isinstance(args, dict) else {},
                    }
                )
            messages.append(
                Message(
                    role=item.get("role", "user"),
                    content=str(item.get("content") or ""),
                    name=item.get("name"),
                    tool_call_id=item.get("tool_call_id"),
                    tool_calls=tool_calls,
                )
            )
        return messages

    async def _save_checkpoint(
        self,
        thread_id: str,
        messages: list[Message],
        iteration: int,
        last_answer: str,
        tool_calls_used: int,
        runtime_context: RunContext,
        *,
        phase: str,
        pending_tool_calls: list[dict[str, Any]],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """保存可恢复的完整循环状态，并通知 RuntimeStore。"""
        # 快照完整保存：消息、下一轮起点、阶段、未完成工具、预算和业务 state。
        # phase 含义：execute_tools=挂起等待工具执行；model=下一轮从 LLM 开始；completed=结束。
        state = {
            "messages": [message.to_dict() for message in messages],
            "iteration": iteration,
            "next_iteration": iteration + 1 if phase in {"model", "completed"} else iteration,
            "last_answer": last_answer,
            "tool_calls_used": tool_calls_used,
            "phase": phase,
            "pending_tool_calls": pending_tool_calls,
            "state": dict(runtime_context.state),
        }
        runtime_context.checkpoint = state
        if runtime_context.checkpoint_callback is not None:
            result = runtime_context.checkpoint_callback(state)
            if hasattr(result, "__await__"):
                await result
        if self._checkpointer:
            await self._checkpointer.save(
                thread_id,
                {**state, "run_context": runtime_context.to_dict()},
                metadata=metadata or {"iteration": iteration},
            )

    @staticmethod
    def _tool_call_signature(call: dict[str, Any]) -> str:
        """归一化工具签名，防止模型用等价 JSON 绕过死循环检测。"""
        name = str(call.get("name", ""))
        args = call.get("args", {})
        try:
            args_text = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            args_text = repr(args)
        return f"{name}:{args_text}"

    @staticmethod
    def _tool_call_signatures(messages: list[Message]) -> list[str]:
        """按消息顺序提取完整工具调用签名序列。"""
        return [
            ReActAgent._tool_call_signature(call)
            for message in messages
            for call in message.tool_calls
        ]

    def _assert_no_tool_loop(self, messages: list[Message], runtime_context: RunContext) -> None:
        """同一工具同一参数连续调用超过阈值时，直接终止本轮 Agent Loop。"""
        if self._max_repeated_tool_calls <= 0:
            return
        signatures = self._tool_call_signatures(messages)
        tail = signatures[-self._max_repeated_tool_calls :]
        if len(tail) == self._max_repeated_tool_calls and len(set(tail)) == 1:
            last_call = next(
                (call for message in reversed(messages) for call in reversed(message.tool_calls)),
                None,
            )
            tool_name = str(last_call.get("name", "")) if last_call else ""
            runtime_context.emit(
                "loop_detected",
                tool_name=tool_name,
                repeat_count=len(tail),
            )
            raise ModelOutputValidationError(
                f"ReAct agent 检测到同一工具同一参数连续调用 {self._max_repeated_tool_calls} 次，已强制终止。"
            )

    @staticmethod
    def _maybe_suspend_on_retryable_result(
        runtime_context: RunContext,
        tool_call: dict[str, Any],
        result: ToolResult,
    ) -> bool:
        """业务级挂起：工具显式返回 retryable/suspend 时，暂停 run 并保留上下文。"""
        if result.success or not result.retryable:
            return False
        if result.metadata.get("suspend") is False:
            return False

        previous = runtime_context.state.get("pending_retry") or {}
        retry_count = int(previous.get("retry_count", 0)) + 1
        delay_seconds = max(0.0, float(result.metadata.get("retry_after_seconds", 1.0)))
        next_retry_at = (
            datetime.now(UTC) + timedelta(seconds=delay_seconds)
        ).isoformat()

        runtime_context.state["pending_retry"] = {
            "tool_name": str(tool_call.get("name", "")),
            "arguments": dict(tool_call.get("args") or {}),
            "error_kind": result.error_kind,
            "error": result.error,
            "retry_count": retry_count,
            "retry_after_seconds": delay_seconds,
            "next_retry_at": next_retry_at,
            "metadata": dict(result.metadata),
        }
        transition_run_status(runtime_context, "waiting_retry")
        runtime_context.emit(
            "run_waiting_retry",
            tool_name=str(tool_call.get("name", "")),
            retry_count=retry_count,
            next_retry_at=next_retry_at,
            error_kind=result.error_kind,
            error=result.error,
        )
        return True

    async def _finish_run(
        self,
        runtime_context: RunContext,
        status: RunStatus,
        event_type: str,
        **payload: Any,
    ) -> None:
        """设置终态并立即通知持久化回调，所有退出路径共用。"""
        if (
            runtime_context.status == "waiting_retry"
            and status in {"completed", "failed"}
        ):
            # 业务级挂起是可恢复状态，不应被普通终态覆盖。
            return
        if runtime_context.status != status:
            transition_run_status(runtime_context, status)
            runtime_context.emit(event_type, **payload)

        callback = runtime_context.checkpoint_callback
        if callback is None:
            return
        try:
            result = callback(runtime_context.checkpoint)
            if hasattr(result, "__await__"):
                await result
        except Exception:
            # 状态收口不能被二次持久化异常覆盖；RuntimeCompat 的 finally 还会再保存一次。
            logger.exception(
                "持久化 Agent run 终态失败: %s/%s",
                runtime_context.thread_id,
                runtime_context.run_id,
            )

    # 工具执行细节
    async def _execute_tools(
        self,
        tool_calls: list[dict[str, Any]],
        messages: list[Message],
        ctx: MiddlewareContext,
        runtime_context: RunContext | None = None,
    ) -> list[Message]:
        """按优先级分批执行工具；高优先级失败会阻断更低优先级工具。"""

        # 并发工具的执行结果必须按原始调用顺序回填，不能按完成先后顺序回填。
        success_by_index: dict[int, bool] = {}
        ordered_results: dict[int, Message] = {}
        completed_indices: set[int] = set()
        progress_messages: list[Message] = []

        # 单个工具的完整执行链：事件 -> before_tool -> 执行 -> after_tool。
        async def execute_one(index: int, tc: dict[str, Any]) -> Message:
            tool_ctx = MiddlewareContext(
                messages=list(messages),
                iteration=ctx.iteration,
                tool_name=tc["name"],
                tool_args=tc.get("args", {}),
                llm_response=ctx.llm_response,
                metadata=dict(ctx.metadata),
                emit=runtime_context.emit if runtime_context is not None else ctx.emit,
            )
            if runtime_context is not None:
                runtime_context.emit(
                    "tool_started",
                    tool_name=tc["name"],
                    arguments=tool_ctx.tool_args,
                )

            mw_result = await self._middleware_manager.execute_before_tool(tool_ctx)
            if mw_result.action == MiddlewareAction.STOP:
                logger.warning("中间件阻止工具 %s 执行", tc["name"])
                result = f"[工具 {tc['name']} 被阻止执行]"
                tool_ctx.metadata["tool_success"] = False
                success_by_index[index] = False
                if runtime_context is not None:
                    runtime_context.emit(
                        "tool_finished",
                        tool_name=tc["name"],
                        success=False,
                        result={"error_kind": "approval", "error": result},
                    )
            else:
                structured_result = await self._tool_executor.execute_result(
                    tc["name"],
                    tc.get("args", {}),
                )
                result = structured_result.to_text()
                tool_ctx.metadata["tool_success"] = structured_result.success
                success_by_index[index] = structured_result.success
                if runtime_context is not None:
                    runtime_context.emit(
                        "tool_finished",
                        tool_name=tc["name"],
                        success=structured_result.success,
                        result=structured_result.to_dict(),
                    )
                    self._maybe_suspend_on_retryable_result(
                        runtime_context,
                        tc,
                        structured_result,
                    )

            tool_ctx.tool_result = result
            await self._middleware_manager.execute_after_tool(tool_ctx)
            return tool_message(
                content=result,
                tool_call_id=tc["id"],
                name=tc["name"],
            )

        # 工具异常不直接打断整批并发；转成失败 tool_message 让模型看到错误。
        async def execute_indexed(index: int, tool_call: dict[str, Any]) -> tuple[int, Message]:
            try:
                return index, await execute_one(index, tool_call)
            except Exception as exc:
                success_by_index[index] = False
                error_text = str(exc)
                logger.exception(
                    "工具 %s 的执行链异常",
                    tool_call["name"],
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                content = f"[工具 {tool_call['name']} 执行异常] {error_text}"
                outcome = tool_message(
                    content=content,
                    tool_call_id=tool_call["id"],
                    name=tool_call["name"],
                )
                if runtime_context is not None:
                    runtime_context.emit(
                        "tool_finished",
                        tool_name=tool_call["name"],
                        success=False,
                        result={"error_kind": "execution", "error": error_text},
                    )
                return index, outcome

        # 一个可并发批次；每个子批次完成后立即持久化未完成工具，便于断点恢复。
        async def run_batch(batch: list[tuple[int, dict[str, Any]]]) -> None:
            tasks = [
                asyncio.create_task(execute_indexed(index, tool_call))
                for index, tool_call in batch
            ]
            try:
                for completed in asyncio.as_completed(tasks):
                    index, outcome = await completed
                    ordered_results[index] = outcome
                    progress_messages.append(outcome)
                    completed_indices.add(index)
                    if runtime_context is not None:
                        pending = [
                            tool_call
                            for pending_index, tool_call in enumerate(tool_calls)
                            if pending_index not in completed_indices
                        ]
                        if pending:
                            await self._persist_tool_progress(
                                runtime_context,
                                [*messages, *progress_messages],
                                pending,
                            )
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

        def priority_of(index: int, tool_call: dict[str, Any]) -> int:
            spec_for = getattr(self._tool_executor, "spec_for", None)
            spec = spec_for(tool_call["name"]) if callable(spec_for) else None
            return spec.priority if spec is not None else 100

        # 先按工具 spec.priority 分层；数字小的优先级更高。
        priority_entries = [
            (priority_of(index, tool_call), index, tool_call)
            for index, tool_call in enumerate(tool_calls)
        ]
        priorities = sorted({priority for priority, _, _ in priority_entries})

        # 逐层执行。只要更高优先级层有失败，后续所有低优先级工具都短路为阻断结果。
        for priority in priorities:
            batch = [
                (index, tool_call)
                for entry_priority, index, tool_call in priority_entries
                if entry_priority == priority
            ]
            blocked = any(success is False for success in success_by_index.values())
            if blocked:
                for index, tool_call in batch:
                    content = f"[工具 {tool_call['name']} 被更高优先级工具失败阻断]"
                    ordered_results[index] = tool_message(
                        content=content,
                        tool_call_id=tool_call["id"],
                        name=tool_call["name"],
                    )
                    success_by_index[index] = False
                    progress_messages.append(ordered_results[index])
                    completed_indices.add(index)
                    if runtime_context is not None:
                        runtime_context.emit(
                            "tool_finished",
                            tool_name=tool_call["name"],
                            success=False,
                            result={
                                "error_kind": "priority_gate",
                                "error": content,
                            },
                        )
                continue

            # 并行策略再把同一优先级拆成多个子批次，控制最大并发数。
            for sub_batch in self._parallel_policy.split(batch):
                await run_batch(sub_batch)

        messages.extend(ordered_results[index] for index in range(len(tool_calls)))
        return messages

    async def _persist_tool_progress(
        self,
        runtime_context: RunContext,
        messages: list[Message],
        pending_tool_calls: list[dict[str, Any]],
    ) -> None:
        """保存并发工具批次的阶段性进度，恢复时只重放尚未完成的调用。"""
        # 并发批次每完成一个工具就保存一次：恢复时 pending 里的才重放，
        # completed_indices 里的工具不会重复产生副作用。
        state = {
            **runtime_context.checkpoint,
            "messages": [message.to_dict() for message in messages],
            "phase": "execute_tools" if pending_tool_calls else "model",
            "pending_tool_calls": pending_tool_calls,
        }
        if not pending_tool_calls:
            state["next_iteration"] = int(state.get("iteration", 0)) + 1
        runtime_context.checkpoint = state
        callback = runtime_context.checkpoint_callback
        if callback is not None:
            result = callback(state)
            if hasattr(result, "__await__"):
                await result
        if self._checkpointer is not None:
            await self._checkpointer.save(
                runtime_context.thread_id,
                {**state, "run_context": runtime_context.to_dict()},
                metadata={"iteration": int(state.get("iteration", 0)), "phase": state["phase"]},
            )

    # ──────────────────────────────────────────────
    # 时间旅行接口
    # ──────────────────────────────────────────────

    async def get_checkpoint_history(self, thread_id: str | None = None) -> list[dict[str, Any]]:
        """返回指定 thread 的 checkpoint 版本列表（需要 checkpointer）。

        checkpoint 保存使用的是每次运行传入的 ``thread_id``；当运行时会话与构造
        Agent 时的默认会话不同（例如经由 AgentRuntime 运行），必须显式传入。
        """
        if not self._checkpointer:
            return []
        list_versions = getattr(self._checkpointer, "list_versions", None)
        if list_versions is None:
            # 内存和文件快照只有当前状态，没有时间旅行版本列表。
            return []
        loader = cast(
            Callable[[str], Awaitable[list[dict[str, Any]]]],
            list_versions,
        )
        return await loader(thread_id or self._thread_id)

    async def rollback_to(self, version_id: str, *, thread_id: str | None = None) -> bool:
        """回滚指定 thread 的 checkpoint 版本（需要 checkpointer）。"""
        if not self._checkpointer:
            return False
        rollback = getattr(self._checkpointer, "rollback", None)
        if rollback is None:
            return False
        rollback_version = cast(
            Callable[[str, str], Awaitable[bool]],
            rollback,
        )
        return await rollback_version(thread_id or self._thread_id, version_id)
