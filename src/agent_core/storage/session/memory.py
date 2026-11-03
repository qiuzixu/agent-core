"""进程内会话存储，主要用于测试与最小部署。"""

from __future__ import annotations

from typing import Any

from agent_core.access import AccessContext
from agent_core.protocol.messages import Message
from agent_core.storage.session.base import (
    BaseSessionStore,
    _msg_to_row,
    _row_to_msg,
)


class MemorySessionStore(BaseSessionStore):
    """进程内存，重启丢失。仅用于单元测试。"""

    def __init__(self, *, require_access: bool = False) -> None:
        # thread_id → list[Message]
        self._data: dict[str, list[Message]] = {}
        self._owners: dict[str, tuple[str, str]] = {}
        self._require_access = require_access

    def _check(self, thread_id: str, access: AccessContext | None, *, claim: bool) -> None:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        owner = self._owners.get(thread_id)
        if owner is None and access is not None and claim:
            self._owners[thread_id] = (access.user_id, access.tenant_id)
            return
        if owner is None and self._require_access and thread_id in self._data:
            raise PermissionError("该 thread 的历史数据尚未绑定所有者")
        if owner is not None and access is not None and not access.can_access(*owner):
            raise PermissionError("无权访问该 thread 的会话历史")

    async def load(self, thread_id: str, *, access: AccessContext | None = None) -> list[Message]:
        self._check(thread_id, access, claim=False)
        return list(self._data.get(thread_id, []))

    async def save(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check(thread_id, access, claim=True)
        self._data[thread_id] = list(messages)

    async def append(
        self,
        thread_id: str,
        messages: list[Message],
        *,
        access: AccessContext | None = None,
    ) -> None:
        self._check(thread_id, access, claim=True)
        self._data.setdefault(thread_id, []).extend(messages)

    async def delete(self, thread_id: str, *, access: AccessContext | None = None) -> None:
        self._check(thread_id, access, claim=False)
        self._data.pop(thread_id, None)
        self._owners.pop(thread_id, None)

    async def list_threads(self, *, access: AccessContext | None = None) -> list[str]:
        if self._require_access and access is None:
            raise PermissionError("该 SessionStore 要求提供 AccessContext")
        if access is None:
            return list(self._data.keys())
        return [
            thread_id
            for thread_id in self._data
            if (owner := self._owners.get(thread_id)) is not None and access.can_access(*owner)
        ]

    async def count(self, thread_id: str, *, access: AccessContext | None = None) -> int:
        self._check(thread_id, access, claim=False)
        return len(self._data.get(thread_id, []))


# ──────────────────────────────────────────────
# 2. SQLite 后端（开发环境）
# ──────────────────────────────────────────────
