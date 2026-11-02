"""模型选择值对象与存储端口。"""

from __future__ import annotations

from collections.abc import Mapping
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

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelSelection":
        """从字典恢复模型选择对象，并校验必要字段。"""

        scope = str(value["scope"])
        if scope not in ("user", "tenant"):
            raise ValueError(f"模型选择 scope 无效：{scope}")
        provider = str(value["provider"])
        model = str(value["model"])
        if not provider or not model:
            raise ValueError("provider 和 model 不能为空")
        version = int(value.get("version", 1))
        if version < 1:
            raise ValueError("version 必须大于等于 1")
        return cls(
            provider=provider,
            model=model,
            scope=scope,  # type: ignore[arg-type]
            user_id=value.get("user_id") or None,
            tenant_id=value.get("tenant_id") or None,
            version=version,
            updated_at=str(value.get("updated_at", "")),
        )


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
        expected_version: int | None = None,
    ) -> ModelSelection:
        """保存模型选择。

        ``expected_version`` 为 ``None`` 时保持兼容的 last-writer-wins 语义；
        非 ``None`` 时表示调用方已读取的版本：记录不存在应传 0，
        版本不匹配时实现应抛出并发冲突错误。
        """
        ...

    async def clear(
        self,
        *,
        scope: ModelSelectionScope,
        user_id: str | None,
        tenant_id: str | None,
    ) -> None: ...


__all__ = ["ModelSelection", "ModelSelectionScope", "ModelSelectionStore"]
