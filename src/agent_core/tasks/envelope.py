"""任务信封契约：API 生产者和后台 Worker 共享的统一消息格式。

使用 dataclass 而非 pydantic，确保 agent-core 无 pydantic 依赖时也能导入。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class TaskEnvelope:
    """写入存储并发布到队列的统一信封。"""

    task_id: str = field(default_factory=lambda: str(uuid4()))
    intent: str = "workflow.run"
    thread_id: str | None = None
    user_id: str = "anonymous"
    tenant_id: str = "default"
    roles: list[str] = field(default_factory=lambda: ["user"])
    idempotency_key: str | None = None
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    retry_count: int = 0
    max_retries: int = 3
    requested_at: str = field(default_factory=_now_iso)
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def model_dump(self, *, mode: str = "python", **kwargs: Any) -> dict[str, Any]:
        """兼容 pydantic 风格的序列化接口。"""
        if mode == "json":
            import json
            return json.loads(json.dumps(self.to_dict(), ensure_ascii=False, default=str))
        return self.to_dict()

    def model_dump_json(self, **kwargs: Any) -> str:
        """兼容 pydantic 风格的 JSON 序列化。"""
        import json
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> "TaskEnvelope":
        """兼容 pydantic 风格的反序列化。"""
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def model_validate_json(cls, json_str: str) -> "TaskEnvelope":
        """兼容 pydantic 风格的 JSON 反序列化。"""
        import json
        return cls.model_validate(json.loads(json_str))


__all__ = ["TaskEnvelope"]
