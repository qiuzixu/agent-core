"""跨会话长期记忆的值对象和存储端口。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from agent_core.access import AccessContext


def memory_now() -> str:
    """返回可排序的 UTC ISO 时间。"""

    return datetime.now(UTC).isoformat()


@dataclass
class MemoryRecord:
    """一条可跨会话检索的长期记忆。"""

    namespace: str
    content: str
    memory_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    key: str | None = None
    user_id: str | None = None
    tenant_id: str | None = None
    source: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    expires_at: str | None = None
    version: int = 0
    created_at: str = field(default_factory=memory_now)
    updated_at: str = field(default_factory=memory_now)

    def __post_init__(self) -> None:
        if not self.namespace.strip():
            raise ValueError("记忆 namespace 不能为空")
        if not self.memory_id.strip():
            raise ValueError("memory_id 不能为空")

    def is_expired(self, *, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        return expires <= (now or datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "namespace": self.namespace,
            "key": self.key,
            "content": self.content,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "source": self.source,
            "metadata": self.metadata,
            "expires_at": self.expires_at,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> MemoryRecord:
        return cls(
            memory_id=str(value["memory_id"]),
            namespace=str(value["namespace"]),
            key=value.get("key"),
            content=str(value.get("content", "")),
            user_id=value.get("user_id"),
            tenant_id=value.get("tenant_id"),
            source=value.get("source"),
            metadata=dict(value.get("metadata") or {}),
            expires_at=value.get("expires_at"),
            version=int(value.get("version", 0)),
            created_at=str(value.get("created_at") or memory_now()),
            updated_at=str(value.get("updated_at") or memory_now()),
        )


@dataclass(frozen=True)
class MemorySearchResult:
    """长期记忆检索结果。"""

    record: MemoryRecord
    score: float


@runtime_checkable
class MemoryStore(Protocol):
    """跨会话记忆存储协议，语义向量适配器也实现该端口。"""

    async def put(
        self,
        record: MemoryRecord,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> MemoryRecord: ...

    async def get(
        self,
        memory_id: str,
        *,
        access: AccessContext | None = None,
    ) -> MemoryRecord | None: ...

    async def delete(
        self,
        memory_id: str,
        *,
        expected_version: int | None = None,
        access: AccessContext | None = None,
    ) -> bool: ...

    async def search(
        self,
        namespace: str,
        *,
        query: str = "",
        metadata: dict[str, Any] | None = None,
        limit: int = 20,
        access: AccessContext | None = None,
    ) -> list[MemorySearchResult]: ...


__all__ = ["MemoryRecord", "MemorySearchResult", "MemoryStore", "memory_now"]
