"""面向业务的 SessionManager：组合模型、历史裁剪与会话存储。"""

from __future__ import annotations

import asyncio
from typing import Any, cast

from agent_core.access import AccessContext
from agent_core.compaction.types import SUMMARY_SOURCE
from agent_core.model import ContextUsage
from agent_core.protocol.messages import Message, assistant_message, user_message
from agent_core.protocol.runtime import RunContext
from agent_core.storage.session.base import BaseSessionStore, _extract_summary_body
from agent_core.storage.session.factory import create_session_store
from agent_core.storage.session.postgres import PostgresSessionStore


class SessionManager:
    """带多轮记忆的对话管理器。

    使用示例：
        # 开发环境（自动用 SQLite）
        mgr = SessionManager.from_config(env="development")

        # 生产环境（自动用 PostgreSQL）
        mgr = SessionManager.from_config(
            env="production",
            postgres_url="postgresql://user:pass@host/db",
        )

        # 每轮对话
        answer = await mgr.chat(agent, thread_id="user-123", user_input="你好")
        answer = await mgr.chat(agent, thread_id="user-123", user_input="我叫什么")
    """

    def __init__(
        self,
        store: BaseSessionStore,
        *,
        max_history_messages: int = 50,
    ) -> None:
        self._store = store
        self._max_history = max_history_messages
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_config(
        cls,
        env: str = "development",
        *,
        sqlite_path: str = "./sessions.db",
        postgres_url: str | None = None,
        max_history_messages: int = 50,
        require_access: bool = False,
    ) -> SessionManager:
        """从配置创建 SessionManager（自动选择存储后端）。"""
        store = create_session_store(
            env,
            sqlite_path=sqlite_path,
            postgres_url=postgres_url,
            require_access=require_access,
        )
        return cls(store, max_history_messages=max_history_messages)

    async def setup(self) -> None:
        """初始化存储（PostgreSQL 需要调用建连接池和建表）。"""
        if isinstance(self._store, PostgresSessionStore):
            await self._store.initialize()

    async def teardown(self) -> None:
        """关闭存储（服务停止时调用）。"""
        if isinstance(self._store, PostgresSessionStore):
            await self._store.close()

    async def chat(
        self,
        agent: Any,
        thread_id: str,
        user_input: str,
        *,
        append_user: bool = True,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        access: AccessContext | None = None,
    ) -> str:
        """串行化同一 thread 的对话，避免并发请求覆盖历史。"""
        lock = self._locks.setdefault(thread_id, asyncio.Lock())
        async with lock:
            return await self._chat_unlocked(
                agent,
                thread_id,
                user_input,
                append_user=append_user,
                run_context=run_context,
                checkpoint_state=checkpoint_state,
                access=access,
            )

    async def _chat_unlocked(
        self,
        agent: Any,
        thread_id: str,
        user_input: str,
        *,
        append_user: bool = True,
        run_context: RunContext | None = None,
        checkpoint_state: dict[str, Any] | None = None,
        access: AccessContext | None = None,
    ) -> str:
        """带记忆的多轮对话。

        Args:
            agent: ReActAgent 实例。
            thread_id: 会话 ID。
            user_input: 用户输入。

        Returns:
            Agent 回答文本。
        """
        if access is None and run_context is not None:
            if run_context.user_id is not None and run_context.tenant_id is not None:
                access = AccessContext(
                    user_id=run_context.user_id,
                    tenant_id=run_context.tenant_id,
                )

        # 1. 加载历史，构建 memory_context
        history = await self._store.load(thread_id, access=access)
        memory_context = self._build_context(history) if history else None

        # 2. 裁剪过长历史（仅在内存中裁剪，下一步 append 写入的是新消息）。
        #    裁剪不能越过最新摘要：摘要已 shadow 它之前的全部原文，
        #    越过会把摘要裁掉、迫使下一轮回到全量重压。
        if len(history) > self._max_history:
            start = len(history) - self._max_history
            latest_summary = None
            for index in range(len(history) - 1, -1, -1):
                if history[index].name == SUMMARY_SOURCE:
                    latest_summary = index
                    break
            if latest_summary is not None and latest_summary < start:
                start = latest_summary
            history = history[start:]
            await self._store.save(thread_id, history, access=access)

        # 3. 调用 Agent
        answer = cast(
            str,
            await agent.run(
                user_input,
                memory_context=memory_context,
                thread_id=thread_id,
                run_context=run_context,
                checkpoint_state=checkpoint_state,
            ),
        )

        # 4. 追加本轮对话（append 比 save 更高效，只写新增的行）。
        # 暂停的 run 在等待浏览器审批前已经保存过用户消息，因此恢复时不重复追加。
        messages_to_append = [assistant_message(answer)]
        if append_user:
            messages_to_append.insert(0, user_message(user_input))
        await self._store.append(thread_id, messages_to_append, access=access)

        return answer

    def _build_context(self, history: list[Message]) -> str:
        """把历史消息转成字符串摘要注入 system prompt。

        历史中若存在压缩摘要 checkpoint（name=compaction_checkpoint），
        以最新一条摘要为起点：摘要之前的原文已被"影子化"（仍在库中，
        但不再进入模型上下文），之后只拼接摘要 + 增量消息。
        """
        lines: list[str] = []
        start = 0
        # 从后往前找最新摘要；找到则以"摘要 + 增量"替代全量重建
        for index in range(len(history) - 1, -1, -1):
            if history[index].name == SUMMARY_SOURCE:
                lines.append(f"（早期会话摘要）\n{_extract_summary_body(history[index].content)}")
                start = index + 1
                break
        for msg in history[start:]:
            if msg.role == "user":
                lines.append(f"用户: {msg.content}")
            elif msg.role == "assistant":
                # 限制每条摘要长度，避免 context 过长
                lines.append(f"助手: {msg.content[:300]}")
        return "\n".join(lines)

    async def get_history(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> list[Message]:
        """获取会话历史（用于查询接口）。"""
        return await self._store.load(thread_id, access=access)

    # ──────────────────────────────────────────────
    # 上下文使用
    # ──────────────────────────────────────────────
    async def context_usage(
        self,
        agent: Any,
        thread_id: str | None,
        user_input: str,
        *,
        access: AccessContext | None = None,
    ) -> ContextUsage:
        """按真实会话记忆构造下一次模型输入并统计 token。"""
        history = await self._store.load(thread_id, access=access) if thread_id else []
        memory_context = self._build_context(history) if history else None
        return cast(
            ContextUsage,
            await agent.context_usage(user_input, memory_context=memory_context),
        )

    async def clear(
        self,
        thread_id: str,
        *,
        access: AccessContext | None = None,
    ) -> None:
        """清空会话历史。"""
        await self._store.delete(thread_id, access=access)
        lock = self._locks.get(thread_id)
        if lock is not None and not lock.locked():
            self._locks.pop(thread_id, None)

    async def list_threads(
        self,
        *,
        access: AccessContext | None = None,
    ) -> list[str]:
        """列出所有会话 ID。"""
        return await self._store.list_threads(access=access)

    @property
    def store(self) -> BaseSessionStore:
        """暴露底层存储（测试用）。"""
        return self._store
