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
)
from agent_core.model import ModelAdapter, StreamChunk
from agent_core.protocol.messages import (
    Message,
    assistant_message,
    system_message,
    tool_message,
    user_message,
)
from agent_core.protocol.runtime import RunContext, RunStatus
from agent_core.tools import ToolExecutor

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
    ) -> None:
        self._llm = llm
        self._tool_executor = tool_executor
        self._system_prompt = system_prompt
        self._tool_definitions = tool_definitions
        self._max_iterations = max_iterations
        self._max_tool_calls = max_tool_calls
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
        checkpoint = checkpoint_state or {}
        messages = (
            self._messages_from_checkpoint(checkpoint)
            if checkpoint.get("messages")
            else self._build_initial_messages(user_input, memory_context)
        )
        # 3.2 初始化运行变量
        last_answer = str(checkpoint.get("last_answer", ""))
        metadata = self._runtime_metadata(run_context, dict(run_metadata or {}))
        run_context.status = "running"
        # 恢复执行时沿用已有上下文的会话 ID，避免快照落到默认会话；
        # 流式与普通执行使用同一套会话隔离规则。
        checkpoint_thread = thread_id or run_context.thread_id

        if checkpoint.get("phase") == "completed":
            if last_answer:
                yield _LoopEvent(answer=last_answer)
                return
            raise ModelOutputValidationError("completed checkpoint 缺少最终回答")

        # 4. checkpoint 恢复阶段
        start_iteration = int(checkpoint.get("next_iteration", checkpoint.get("iteration", 0)))
        pending_tool_calls = checkpoint.get("pending_tool_calls")
        # 4.1 如果上次停在工具执行前，于是构造一个 middleware 上下文：
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

        tool_calls_used = int(checkpoint.get("tool_calls_used", 0))
        # 5. Agent 主循环
        for iteration in range(start_iteration, self._max_iterations):
            # 5.1 设置当前轮次
            run_context.iteration = iteration
            ctx = MiddlewareContext(
                messages=messages,
                iteration=iteration,
                metadata=metadata,
                emit=run_context.emit,
            )

            # before_model
            mw_result = await self._middleware_manager.execute_before_model(ctx)
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型调用被中间件阻止")
                )
            if mw_result.action == MiddlewareAction.MODIFY:
                messages = mw_result.data.get("messages", messages)

            # ── 策略点：一轮模型调用 ──────────────────────
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

            # 7. 保存模型响应
            messages.append(response)
            # 这一步非常关键，因为下一轮调用模型时，模型必须知道自己上一轮说了什么。
            run_context.emit(
                "model_finished",
                iteration=iteration,
                tool_calls=len(response.tool_calls),
            )
            ctx.llm_response = response

            # after_model
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

            if not response.tool_calls:
                if not response.content:
                    raise ModelOutputValidationError(mode.empty_final_error)
                logger.debug("ReAct %s第 %d 轮结束（无工具调用）", mode.label, iteration + 1)
                break
            # 11. 有工具调用：检查数量
            tool_calls_used = next_tool_calls_used
            run_context.tool_calls_used = tool_calls_used

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
        while True:
            text_parts.clear()
            tool_calls_by_key.clear()
            try:
                async for chunk in self._llm.stream_chat(
                    messages,
                    tools=self._tool_definitions or None,
                ):
                    if chunk.text:
                        text_parts.append(chunk.text)
                        yield chunk

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

        # 组装 assistant message
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

    @staticmethod
    # 这个方法负责把 JSON 字典恢复为内部消息对象。
    def _messages_from_checkpoint(state: dict[str, Any]) -> list[Message]:
        """从 checkpoint 恢复内部消息，兼容 OpenAI tool-call 格式。"""
        messages: list[Message] = []
        for item in state.get("messages", []):
            if not isinstance(item, dict):
                continue
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
        state = {
            "messages": [message.to_dict() for message in messages],
            "iteration": iteration,
            "next_iteration": iteration + 1 if phase in {"model", "completed"} else iteration,
            "last_answer": last_answer,
            "tool_calls_used": tool_calls_used,
            "phase": phase,
            "pending_tool_calls": pending_tool_calls,
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

    async def _finish_run(
        self,
        runtime_context: RunContext,
        status: RunStatus,
        event_type: str,
        **payload: Any,
    ) -> None:
        """设置终态并立即通知持久化回调，所有退出路径共用。"""
        if runtime_context.status != status:
            runtime_context.status = status
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
        """并发执行所有工具，把结果追加到 messages。"""

        async def execute_one(tc: dict[str, Any]) -> Message:
            # 每个并发任务使用独立上下文，避免工具名、参数和结果互相覆盖。
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
                if runtime_context is not None:
                    runtime_context.emit(
                        "tool_finished",
                        tool_name=tc["name"],
                        success=False,
                        result={"error_kind": "approval", "error": result},
                    )
                tool_ctx.metadata["tool_success"] = False
            else:
                # 审批通过后才真正执行副作用工具。
                structured_result = await self._tool_executor.execute_result(tc["name"], tc.get("args", {}))
                result = structured_result.to_text()
                tool_ctx.metadata["tool_success"] = structured_result.success
                if runtime_context is not None:
                    runtime_context.emit(
                        "tool_finished",
                        tool_name=tc["name"],
                        success=structured_result.success,
                        result=structured_result.to_dict(),
                    )

            # after_tool 中间件
            tool_ctx.tool_result = result
            await self._middleware_manager.execute_after_tool(tool_ctx)
            return tool_message(
                content=result,
                tool_call_id=tc["id"],
                name=tc["name"],
            )

        async def execute_indexed(index: int, tool_call: dict[str, Any]) -> tuple[int, Message | Exception]:
            try:
                return index, await execute_one(tool_call)
            except Exception as exc:
                return index, exc

        tasks = [
            asyncio.create_task(execute_indexed(index, tool_call))
            for index, tool_call in enumerate(tool_calls)
        ]
        completed_indices: set[int] = set()
        progress_messages: list[Message] = []
        ordered_messages: list[Message | None] = [None] * len(tool_calls)
        try:
            for completed in asyncio.as_completed(tasks):
                index, outcome = await completed
                tool_call = tool_calls[index]
                if isinstance(outcome, Exception):
                    error_text = str(outcome)
                    logger.exception(
                        "工具 %s 的执行链异常",
                        tool_call["name"],
                        exc_info=(type(outcome), outcome, outcome.__traceback__),
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
                ordered_messages[index] = outcome
                completed_indices.add(index)
                progress_messages.append(outcome)
                if runtime_context is not None:
                    pending = [
                        tool_call
                        for pending_index, tool_call in enumerate(tool_calls)
                        if pending_index not in completed_indices
                    ]
                    if pending:
                        # 只保存"仍有未完成工具"的阶段快照；批末的 phase=model 快照
                        # 由主循环统一保存，避免与最后一次进度快照重复全量持久化。
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

        # 最终消息按模型原始调用顺序追加；阶段性 checkpoint 允许按完成顺序恢复。
        messages.extend(message for message in ordered_messages if message is not None)
        return messages

    async def _persist_tool_progress(
        self,
        runtime_context: RunContext,
        messages: list[Message],
        pending_tool_calls: list[dict[str, Any]],
    ) -> None:
        """保存并发工具批次的阶段性进度，恢复时只重放尚未完成的调用。"""
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
