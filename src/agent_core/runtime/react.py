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
from agent_core.model import ModelAdapter
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
        metadata = dict(run_metadata or {})
        runtime_context = run_context or RunContext(
            thread_id=thread_id or self._thread_id,
            run_id=str(metadata.get("run_id") or uuid.uuid4()),
            metadata=metadata,
        )
        if run_context is None:
            runtime_context.emit("run_started", input=user_input)

        try:
            answer = await self._run_loop(
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_metadata=metadata,
                run_context=runtime_context,
                checkpoint_state=checkpoint_state,
            )
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

    async def _run_loop(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> str:
        """执行普通 Agent Loop；生命周期状态由 ``run`` 统一管理。"""
        checkpoint = checkpoint_state or {}
        messages = (
            self._messages_from_checkpoint(checkpoint)
            if checkpoint.get("messages")
            else self._build_initial_messages(user_input, memory_context)
        )
        # 3.2 初始化运行变量
        last_answer = str(checkpoint.get("last_answer", ""))
        metadata = dict(run_metadata or {})
        tool_calls_used = int(checkpoint.get("tool_calls_used", 0))
        # 3.3 创建 RunContext
        runtime_context = run_context or RunContext(
            thread_id=thread_id or self._thread_id,
            run_id=str(metadata.get("run_id") or uuid.uuid4()),
            metadata=metadata,
        )
        metadata = self._runtime_metadata(runtime_context, metadata)
        runtime_context.status = "running"
        # 恢复执行时沿用已有上下文的会话 ID，避免快照落到默认会话。
        checkpoint_thread = thread_id or runtime_context.thread_id

        # 4. checkpoint 恢复阶段
        start_iteration = int(checkpoint.get("next_iteration", checkpoint.get("iteration", 0)))
        pending_tool_calls = checkpoint.get("pending_tool_calls")
        # 4.1 如果上次停在工具执行前，于是构造一个 middleware 上下文：
        if checkpoint.get("phase") == "execute_tools" and isinstance(pending_tool_calls, list):
            restore_ctx = MiddlewareContext(
                messages=messages,
                iteration=int(checkpoint.get("iteration", start_iteration)),
                metadata=metadata,
                emit=runtime_context.emit,
            )
            # 然后补执行之前未完成的工具：
            messages = await self._execute_tools(pending_tool_calls, messages, restore_ctx, runtime_context)
            # 工具执行完成后，把阶段改回模型调用：
            start_iteration = int(checkpoint.get("iteration", 0)) + 1
            # 然后保存一次新的 checkpoint
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                start_iteration - 1,
                last_answer,
                tool_calls_used,
                runtime_context,
                phase="model",
                pending_tool_calls=[],
            )
        # 5. Agent 主循环
        for iteration in range(start_iteration, self._max_iterations):
            # 5.1 设置当前轮次
            runtime_context.iteration = iteration
            ctx = MiddlewareContext(
                messages=messages,
                iteration=iteration,
                metadata=metadata,
                emit=runtime_context.emit,
            )

            # before_model
            mw_result = await self._middleware_manager.execute_before_model(ctx)
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型调用被中间件阻止")
                )
            if mw_result.action == MiddlewareAction.MODIFY:
                messages = mw_result.data.get("messages", messages)

            # LLM 调用
            while True:
                try:
                    response = await self._llm.chat(
                        messages,
                        tools=self._tool_definitions or None,
                    )
                    break
                except Exception as exc:
                    if await self._middleware_manager.handle_exception(exc, ctx):
                        runtime_context.emit("model_retry", iteration=iteration, error=str(exc))
                        continue
                    raise ModelInvocationError(
                        f"ReAct agent 第 {iteration + 1} 轮 LLM 调用失败：{exc}"
                    ) from exc
            # 7. 保存模型响应
            messages.append(response)
            # 这一步非常关键，因为下一轮调用模型时，模型必须知道自己上一轮说了什么。
            runtime_context.emit(
                "model_finished",
                iteration=iteration,
                tool_calls=len(response.tool_calls),
            )
            ctx.llm_response = response

            # after_model
            mw_result = await self._middleware_manager.execute_after_model(ctx)
            if mw_result.action == MiddlewareAction.RETRY:
                messages.pop()
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
                runtime_context,
                phase="execute_tools" if response.tool_calls else "completed",
                pending_tool_calls=response.tool_calls,
                metadata={"iteration": iteration, "answer_preview": last_answer[:80], **metadata},
            )

            if not response.tool_calls:
                if not response.content:
                    raise ModelOutputValidationError(
                        "ReAct agent 最终响应为空，不能使用上一轮中间文本作为答案。"
                    )
                logger.debug("ReAct 第 %d 轮结束（无工具调用）", iteration + 1)
                break
            # 11. 有工具调用：检查数量
            tool_calls_used = next_tool_calls_used
            runtime_context.tool_calls_used = tool_calls_used

            # 执行工具调用
            messages = await self._execute_tools(response.tool_calls, messages, ctx, runtime_context)
            # 13. 工具执行后再次保存 checkpoint
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                iteration,
                last_answer,
                tool_calls_used,
                runtime_context,
                phase="model",
                pending_tool_calls=[],
                metadata={"iteration": iteration, "answer_preview": last_answer[:80], **metadata},
            )

        else:
            logger.warning("ReAct 达到最大迭代次数 %d", self._max_iterations)
            raise ModelOutputValidationError(
                f"ReAct agent 达到最大迭代次数（{self._max_iterations}），尚未生成不含工具调用的最终回答。"
            )

        if not last_answer:
            raise ModelOutputValidationError("ReAct agent 未能提取到有效回答。")

        return last_answer

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
        metadata = dict(run_metadata or {})
        runtime_context = run_context or RunContext(
            thread_id=thread_id or self._thread_id,
            run_id=str(metadata.get("run_id") or uuid.uuid4()),
            metadata=metadata,
        )
        if run_context is None:
            runtime_context.emit("run_started", input=user_input)

        try:
            async for chunk in self._stream_loop(
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_metadata=metadata,
                run_context=runtime_context,
                checkpoint_state=checkpoint_state,
            ):
                yield chunk
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

    async def _stream_loop(
        self,
        user_input: str,
        *,
        memory_context: str | None = None,
        thread_id: str | None = None,
        run_metadata: dict[str, Any] | None = None,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
    ) -> AsyncIterator[str]:
        """执行流式循环；生命周期状态由 ``stream`` 统一管理。

        模型文本到达后立即输出；工具调用本身不产生文本块。
        每次 yield 一个文本片段（str）。

        Yields:
            文本内容块（增量）。
        """
        checkpoint = checkpoint_state or {}
        messages = (
            self._messages_from_checkpoint(checkpoint)
            if checkpoint.get("messages")
            else self._build_initial_messages(user_input, memory_context)
        )
        last_answer = str(checkpoint.get("last_answer", ""))
        metadata = dict(run_metadata or {})
        tool_calls_used = int(checkpoint.get("tool_calls_used", 0))
        runtime_context = run_context or RunContext(
            thread_id=thread_id or self._thread_id,
            run_id=str(metadata.get("run_id") or uuid.uuid4()),
            metadata=metadata,
        )
        metadata = self._runtime_metadata(runtime_context, metadata)
        runtime_context.status = "running"
        # 流式执行与普通执行使用同一套会话隔离规则。
        checkpoint_thread = thread_id or runtime_context.thread_id

        start_iteration = int(checkpoint.get("next_iteration", checkpoint.get("iteration", 0)))
        pending_tool_calls = checkpoint.get("pending_tool_calls")
        if checkpoint.get("phase") == "execute_tools" and isinstance(pending_tool_calls, list):
            restore_ctx = MiddlewareContext(
                messages=messages,
                iteration=int(checkpoint.get("iteration", start_iteration)),
                metadata=metadata,
                emit=runtime_context.emit,
            )
            messages = await self._execute_tools(
                pending_tool_calls,
                messages,
                restore_ctx,
                runtime_context,
            )
            start_iteration = int(checkpoint.get("iteration", 0)) + 1
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                start_iteration - 1,
                last_answer,
                tool_calls_used,
                runtime_context,
                phase="model",
                pending_tool_calls=[],
            )

        for iteration in range(start_iteration, self._max_iterations):
            runtime_context.iteration = iteration
            ctx = MiddlewareContext(
                messages=messages,
                iteration=iteration,
                metadata=metadata,
                emit=runtime_context.emit,
            )

            # before_model
            mw_result = await self._middleware_manager.execute_before_model(ctx)
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型调用被中间件阻止")
                )
            if mw_result.action == MiddlewareAction.MODIFY:
                messages = mw_result.data.get("messages", messages)

            # 收集流式输出
            text_parts: list[str] = []
            final_tool_calls: list[dict[str, Any]] = []

            try:
                async for chunk in self._llm.stream_chat(
                    messages,
                    tools=self._tool_definitions or None,
                ):
                    if chunk.text:
                        text_parts.append(chunk.text)
                        # 真正逐块向上游发送；调用方可以立即转成 SSE 数据帧。
                        yield chunk.text

                    if chunk.is_tool_call and chunk.tool_calls:
                        final_tool_calls = chunk.tool_calls

            except Exception as exc:
                if await self._middleware_manager.handle_exception(exc, ctx):
                    runtime_context.emit("model_retry", iteration=iteration, error=str(exc))
                    continue
                raise ModelInvocationError(f"ReAct agent 第 {iteration + 1} 轮流式调用失败：{exc}") from exc

            # 组装 assistant message
            content = "".join(text_parts)
            response = assistant_message(content, tool_calls=final_tool_calls)
            messages.append(response)
            runtime_context.emit(
                "model_finished",
                iteration=iteration,
                tool_calls=len(final_tool_calls),
            )
            ctx.llm_response = response

            # after_model
            mw_result = await self._middleware_manager.execute_after_model(ctx)
            if mw_result.action == MiddlewareAction.RETRY:
                messages.pop()
                if text_parts:
                    raise ModelOutputValidationError("流式文本已经发送，after_model 不能安全重试当前响应。")
                continue
            if mw_result.action == MiddlewareAction.STOP:
                raise ModelOutputValidationError(
                    mw_result.error or mw_result.data.get("reason", "模型输出被中间件阻止")
                )

            if content:
                last_answer = content

            next_tool_calls_used = tool_calls_used + len(final_tool_calls)
            if next_tool_calls_used > self._max_tool_calls:
                raise ModelOutputValidationError(
                    f"ReAct agent 工具调用次数超过上限（{self._max_tool_calls}）"
                )

            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                iteration,
                last_answer,
                next_tool_calls_used,
                runtime_context,
                phase="execute_tools" if final_tool_calls else "completed",
                pending_tool_calls=final_tool_calls,
                metadata={"iteration": iteration, "answer_preview": last_answer[:80], **metadata},
            )

            # 无工具调用 → 结束
            if not final_tool_calls:
                if not content:
                    raise ModelOutputValidationError(
                        "ReAct agent 流式最终响应为空，不能使用上一轮中间文本作为答案。"
                    )
                logger.debug("ReAct 流式第 %d 轮结束（无工具调用）", iteration + 1)
                break

            tool_calls_used = next_tool_calls_used
            runtime_context.tool_calls_used = tool_calls_used

            # 有工具调用：静默执行，然后继续循环（下一轮仍会流式输出）
            logger.debug(
                "ReAct 流式第 %d 轮执行工具: %s",
                iteration + 1,
                [tc["name"] for tc in final_tool_calls],
            )
            messages = await self._execute_tools(final_tool_calls, messages, ctx, runtime_context)
            await self._save_checkpoint(
                checkpoint_thread,
                messages,
                iteration,
                last_answer,
                tool_calls_used,
                runtime_context,
                phase="model",
                pending_tool_calls=[],
                metadata={"iteration": iteration, "answer_preview": last_answer[:80], **metadata},
            )

        else:
            logger.warning("ReAct 流式达到最大迭代次数 %d", self._max_iterations)
            raise ModelOutputValidationError(
                f"ReAct agent 达到最大迭代次数（{self._max_iterations}），尚未生成不含工具调用的最终回答。"
            )

        if not last_answer:
            raise ModelOutputValidationError("ReAct agent 流式未能提取到有效回答。")

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
            else:
                # 审批通过后才真正执行副作用工具。
                structured_result = await self._tool_executor.execute_result(tc["name"], tc.get("args", {}))
                result = structured_result.to_text()
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

        outcomes = await asyncio.gather(
            *(execute_one(tool_call) for tool_call in tool_calls),
            return_exceptions=True,
        )
        tool_messages: list[Message] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
            tool_messages.append(outcome)

        # gather 的返回顺序与 tool_calls 一致，模型能按原调用 ID 匹配结果。
        messages.extend(tool_messages)
        return messages

    # ──────────────────────────────────────────────
    # 时间旅行接口
    # ──────────────────────────────────────────────

    async def get_checkpoint_history(self) -> list[dict[str, Any]]:
        """返回当前 thread 的 checkpoint 版本列表（需要 checkpointer）。"""
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
        return await loader(self._thread_id)

    async def rollback_to(self, version_id: str) -> bool:
        """回滚到指定 checkpoint 版本（需要 checkpointer）。"""
        if not self._checkpointer:
            return False
        rollback = getattr(self._checkpointer, "rollback", None)
        if rollback is None:
            return False
        rollback_version = cast(
            Callable[[str, str], Awaitable[bool]],
            rollback,
        )
        return await rollback_version(self._thread_id, version_id)
