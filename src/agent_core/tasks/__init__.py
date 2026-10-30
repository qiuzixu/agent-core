"""Agent Core 任务编排模块。

提供持久化任务队列、Worker 消费循环、重试调度与死信对账，
不绑定任何具体消息中间件（Memory / RabbitMQ 双实现）。
"""

from agent_core.tasks.envelope import TaskEnvelope
from agent_core.tasks.events import access_from_envelope, event_recorder, task_summary
from agent_core.tasks.factory import (
    create_task_queue_from_settings,
    get_task_queue,
    worker_queue_names,
)
from agent_core.tasks.metrics import TaskMetrics, default_task_metrics
from agent_core.tasks.queue import MemoryTaskQueue, RabbitMQTaskQueue, TaskQueue
from agent_core.tasks.registry import (
    PermanentTaskError,
    TaskHandler,
    TaskTimeoutError,
    UnknownTaskError,
    make_task_router,
)
from agent_core.tasks.scheduler import AgentTaskRetryScheduler
from agent_core.tasks.store import (
    AgentTask,
    AgentTaskStore,
    MemoryAgentTaskStore,
    PostgresAgentTaskStore,
    SqliteAgentTaskStore,
    create_agent_task_store,
)
from agent_core.tasks.worker import AgentTaskWorker

__all__ = [
    "AgentTask",
    "AgentTaskRetryScheduler",
    "AgentTaskStore",
    "AgentTaskWorker",
    "MemoryAgentTaskStore",
    "MemoryTaskQueue",
    "PermanentTaskError",
    "PostgresAgentTaskStore",
    "RabbitMQTaskQueue",
    "SqliteAgentTaskStore",
    "TaskEnvelope",
    "TaskMetrics",
    "TaskQueue",
    "TaskHandler",
    "TaskTimeoutError",
    "UnknownTaskError",
    "access_from_envelope",
    "create_agent_task_store",
    "create_task_queue_from_settings",
    "default_task_metrics",
    "event_recorder",
    "get_task_queue",
    "make_task_router",
    "task_summary",
    "worker_queue_names",
]

