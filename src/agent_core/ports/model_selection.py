"""模型选择值对象与存储端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

ModelSelectionScope = Literal["user", "tenant"]


@dataclass(frozen=True)
class ModelSelection:
    """一个按用户或租户保存的模型选择。"""

    provider: str
    model: str
    scope: ModelSelectionScope
    user_id: str | None = None
    tenant_id: str | None = None
    version: int = 1
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "scope": self.scope,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "version": self.version,
            "updated_at": self.updated_at,
        }


class ModelSelectionStore(Protocol):
    """按用户优先、租户其次保存和解析模型选择。"""

    async def resolve(
        self,
        *,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection | None: ...

    async def save(
        self,
        provider: str,
        model: str,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> ModelSelection: ...

    async def clear(
        self,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> None: ...


__all__ = ["ModelSelection", "ModelSelectionScope", "ModelSelectionStore"]
