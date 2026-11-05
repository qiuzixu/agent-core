"""任务存储最小端口协议。

具体后端（Memory / SQLite / PostgreSQL）实现同一套生命周期方法：
create / claim / complete / fail / 租约续期与僵尸回收。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from agent_core.tasks.models import AgentTask

class AgentTaskStore(ABC):
    @abstractmethod
    async def create(self, task: AgentTask) -> AgentTask: ...

    @abstractmethod
    async def get(self, task_id: str) -> AgentTask | None: ...

    @abstractmethod
    async def get_by_idempotency_key(self, key: str) -> AgentTask | None: ...

    @abstractmethod
    async def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
        attempt_id: str | None = None,
    ) -> AgentTask | None:
        """将 queued/waiting_retry 状态原子转为 running，分配给一个 Worker。"""

    @abstractmethod
    async def set_attempt(
        self,
        task_id: str,
        attempt_id: str,
        *,
        retry_count: int | None = None,
    ) -> AgentTask | None:
        """原子发布 Worker 唯一可执行的 attempt_id。"""

    @abstractmethod
    async def set_waiting_retry(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        """传输层失败后将任务回退到 retry 调度。"""

    @abstractmethod
    async def heartbeat(
        self,
        task_id: str,
        *,
        worker_id: str,
        lease_seconds: float = 30.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def renew_leases(
        self,
        worker_id: str,
        *,
        lease_seconds: float = 30.0,
    ) -> int: ...

    @abstractmethod
    async def reclaim_stale(
        self,
        *,
        worker_id: str = "lease-reaper",
        limit: int = 100,
    ) -> list[AgentTask]: ...

    @abstractmethod
    async def complete(
        self,
        task_id: str,
        *,
        worker_id: str,
        result: dict[str, Any] | None,
        fencing_token: int | None = None,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def fail(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        retry_delay_seconds: float = 5.0,
        fencing_token: int | None = None,
    ) -> AgentTask | None:
        """递增重试计数；还有配额则进入 waiting_retry，否则标记失败。"""

    @abstractmethod
    async def mark_dead(
        self,
        task_id: str,
        *,
        worker_id: str,
        error: str,
        fencing_token: int | None = None,
    ) -> AgentTask | None: ...

    @abstractmethod
    async def mark_requeued(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> AgentTask | None:
        """将 dead 任务手动回放为 queued。"""

    @abstractmethod
    async def mark_discarded(
        self,
        task_id: str,
        *,
        worker_id: str,
        reason: str,
    ) -> AgentTask | None:
        """运维确认后将 dead 任务标记为 discarded。"""

    @abstractmethod
    async def reschedule(self, task_id: str, *, worker_id: str = "scheduler") -> AgentTask | None:
        """将 waiting_retry 原子转回 queued；只有一个调度器能成功。"""

    @abstractmethod
    async def list_by_status(
        self,
        status: str,
        *,
        limit: int = 100,
    ) -> list[AgentTask]: ...

    @abstractmethod
    async def list_tasks(
        self,
        *,
        status: str | None = None,
        intent: str | None = None,
        tenant_id: str | None = None,
        user_id: str | None = None,
        worker_id: str | None = None,
        limit: int = 100,
    ) -> list[AgentTask]:
        """按创建时间返回任务列表，支持可选过滤。"""
        ...



__all__ = ["AgentTaskStore"]
