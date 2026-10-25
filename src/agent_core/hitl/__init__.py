"""通用人机协同（HITL）审批队列。

Core 只负责审批状态机和异步等待，不负责 HTTP 路由、认证或具体业务动作：
- FastAPI 路由（/approvals）由上层应用（vanilla 的 api/approvals.py）提供；
- 持久化由可选的 ``ApprovalStore``（Memory/SQLite/PostgreSQL）实现；
- ReAct 工具执行前的拦截由 ``HumanInTheLoopMiddleware`` 完成。

状态机::

    create_request ──→ PENDING ──approve()──→ APPROVED（终态）
                          │
                          ├───reject()───→ REJECTED（终态）
                          │
                          └──超时/过期───→ TIMEOUT（终态，持久化为 "expired"）

并发模型（为什么所有终态转换必须走 CAS）：

- 同进程：批准/拒绝通过 ``request._event.set()`` 立即唤醒等待者；
- 跨线程：SQLite 同步 IO 经 ``asyncio.to_thread`` 在线程池执行，
  多线程可能同时读写状态，靠 ``transition_approval`` 条件更新兜底；
- 跨进程：多 worker / 多副本各自持有独立的 ``_requests`` 字典，
  内存锁完全失效，只有数据库层的 CAS（WHERE status='pending'）
  能保证"批准 vs 拒绝 vs 过期"三者永远只有一个赢家。

模块布局：``models``（数据模型）、``queue``（队列核心）、
``middleware``（工具执行前拦截）。
"""

from agent_core.hitl.middleware import HumanInTheLoopMiddleware
from agent_core.hitl.models import ApprovalRequest, ApprovalStatus
from agent_core.hitl.queue import ApprovalQueue

__all__ = [
    "ApprovalQueue",
    "ApprovalRequest",
    "ApprovalStatus",
    "HumanInTheLoopMiddleware",
]
