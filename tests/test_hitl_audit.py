"""HITL 审计与保留清理测试。

覆盖两项平台升级：
1. 决定人审计：approve/reject/超时收口都会把 decided_by/decided_at 落库，
   且跨队列实例（跨进程视角）可见；
2. 保留清理：终态记录超期可清理，pending 永不清理。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from agent_core.access import AccessContext
from agent_core.hitl import ApprovalQueue, ApprovalStatus
from agent_core.storage.runtime import MemoryRuntimeStore, SqliteRuntimeStore


async def test_approve_records_decided_by_and_at_persistently(tmp_path) -> None:
    """批准后 decided_by/decided_at 持久化到 SQLite，且跨队列实例可读回。"""
    store = SqliteRuntimeStore(tmp_path / "audit.db")
    waiting = ApprovalQueue(store, poll_interval_seconds=0.01)
    deciding = ApprovalQueue(store, poll_interval_seconds=0.01)

    # 匿名公共审批（不绑定归属）由管理员 bob 批准，审计应记录批准人。
    request = await waiting.create_request_async("thread", "dangerous_tool", {"v": 1})
    waiter = asyncio.create_task(
        waiting.wait_for_decision(request, timeout_seconds=5.0)
    )
    approved = await deciding.approve(
        request.request_id,
        access=AccessContext(user_id="bob", tenant_id="t1"),
    )
    status = await waiter

    assert approved is True
    assert status is ApprovalStatus.APPROVED
    # 跨队列实例（模拟跨进程）从库里读回终态，审计字段必须齐全。
    record = await store.load_approval(request.request_id)
    assert record is not None
    assert record.status == "approved"
    assert record.decided_by == "bob"
    assert record.decided_at is not None
    # 决定时间应是合法 ISO 串且晚于创建时间。
    decided = datetime.fromisoformat(record.decided_at or "")
    created = datetime.fromisoformat(record.created_at)
    assert decided >= created


async def test_reject_records_decided_by_and_reason() -> None:
    """拒绝时决定人与原因一并落库。"""
    store = MemoryRuntimeStore()
    queue = ApprovalQueue(store)
    request = await queue.create_request_async("thread", "tool", {})
    ok = await queue.reject(
        request.request_id,
        reason="风险过高",
        access=AccessContext(user_id="carol", tenant_id="t1"),
    )
    assert ok is True
    record = await store.load_approval(request.request_id)
    assert record is not None
    assert record.status == "rejected"
    assert record.reason == "风险过高"
    assert record.decided_by == "carol"
    assert record.decided_at is not None


async def test_timeout_records_decided_at_without_decider() -> None:
    """超时收口是系统决定：decided_at 必填，decided_by 为 None。"""
    store = MemoryRuntimeStore()
    queue = ApprovalQueue(store, poll_interval_seconds=0.01)
    request = await queue.create_request_async(
        "thread", "tool", {}, timeout_seconds=0.05
    )
    status = await queue.wait_for_decision(request, timeout_seconds=0.1)
    assert status is ApprovalStatus.TIMEOUT
    record = await store.load_approval(request.request_id)
    assert record is not None
    assert record.status == "expired"
    assert record.decided_at is not None
    assert record.decided_by is None


async def test_purge_terminal_approvals_keeps_pending(tmp_path) -> None:
    """保留清理只删超期终态；pending 与未超期终态必须保留。"""
    store = SqliteRuntimeStore(tmp_path / "purge.db")
    queue = ApprovalQueue(store)

    # 超期终态：approved / rejected
    stale_approved = await queue.create_request_async("t", "tool", {})
    await queue.approve(stale_approved.request_id)
    stale_rejected = await queue.create_request_async("t", "tool", {})
    await queue.reject(stale_rejected.request_id)
    # 未超期终态：刚批准
    fresh = await queue.create_request_async("t", "tool", {})
    await queue.approve(fresh.request_id)
    # pending：必须保留
    pending = await queue.create_request_async("t", "tool", {})

    # 把两条旧终态的 decided_at 改到 40 天前（模拟时间流逝）。
    cutoff_iso = (datetime.now(UTC) - timedelta(days=40)).isoformat()
    import sqlite3

    with sqlite3.connect(store._db_path) as conn:
        conn.execute(
            "UPDATE agent_approvals SET decided_at=? WHERE approval_id IN (?, ?)",
            (cutoff_iso, stale_approved.request_id, stale_rejected.request_id),
        )

    removed = await queue.purge_terminal_approvals(older_than_days=30.0)

    assert removed == 2
    assert await store.load_approval(stale_approved.request_id) is None
    assert await store.load_approval(stale_rejected.request_id) is None
    # 未超期终态与 pending 都在。
    assert await store.load_approval(fresh.request_id) is not None
    assert await store.load_approval(pending.request_id) is not None


async def test_purge_rejects_non_positive_retention() -> None:
    """保留天数必须为正，防御性校验。"""
    queue = ApprovalQueue(MemoryRuntimeStore())
    try:
        await queue.purge_terminal_approvals(older_than_days=0)
    except ValueError:
        pass
    else:
        raise AssertionError("older_than_days=0 应当抛出 ValueError")
