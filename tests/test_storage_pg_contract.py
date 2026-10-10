"""存储契约测试：Memory / SQLite / PostgreSQL 三后端行为一致性矩阵。

背景：此前评审发现"单实现精修、多实现失 sync"——同一契约在不同后端上行为不一致。
本文件用同一组用例在三种存储后端（agent_core.storage）上断言相同结果：

- RuntimeStore：save_run/load_run 版本冲突、版本自增、幂等键查询与重复拒绝、
  thread 隔离与归属（save_thread_owner）
- 审批：save_approval / transition_approval 的 CAS 语义（pending 才可迁移、
  决议后不可被 save_approval 覆盖、重放返回 False）
- SessionStore：消息往返、覆盖保存、tool_calls 往返、访问隔离（require_access）
- ContextStore：按 thread 合并更新、_access 保留字段、访问隔离
- ModelSelectionStore：user 覆盖 tenant 的解析优先级、版本自增、clear
- WorkflowExecutionStore：执行实例往返、按 definition/tenant 过滤、版本冲突

PostgreSQL 通过 asyncpg 连接专用测试实例（默认 127.0.0.1:15432，可用环境变量
AGENT_CORE_PG_TEST_DSN 覆盖）；实例不可达时整组 PG 用例 skip，不误报。
所有测试数据使用每用例唯一前缀，测试结束后清理 PG 表数据并删除 SQLite 文件。

已知三后端不一致（以 xfail 如实记录，详见评审报告）：
- 重复幂等键：PostgreSQL 抛 RuntimeConcurrencyError；SQLite 抛 sqlite3.IntegrityError；
  Memory 静默接受重复（不拒绝、可覆盖 find_run_by_idempotency 的首条语义）。
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from agent_core.access import AccessContext
from agent_core.protocol.messages import Message, assistant_message, user_message
from agent_core.protocol.runtime import ApprovalRecord, RunContext
from agent_core.storage import (
    InMemoryMemoryStore,
    MemoryContextStore,
    MemoryModelSelectionStore,
    MemoryRuntimeStore,
    MemorySessionStore,
    MemoryWorkflowExecutionStore,
    PostgresContextStore,
    PostgresMemoryStore,
    PostgresModelSelectionStore,
    PostgresRuntimeStore,
    PostgresSessionStore,
    PostgresWorkflowExecutionStore,
    RuntimeConcurrencyError,
    SqliteContextStore,
    SqliteMemoryStore,
    SqliteModelSelectionStore,
    SqliteRuntimeStore,
    SqliteSessionStore,
    SqliteWorkflowExecutionStore,
    WorkflowConcurrencyError,
    WorkflowExecution,
)

PG_DSN = os.environ.get(
    "AGENT_CORE_PG_TEST_DSN", "postgresql://postgres:agentcore@127.0.0.1:15432/agentcore"
)
PG_HOST, PG_PORT = "127.0.0.1", 15432
# 沙箱对 tempfile/部分 sqlite 路径不友好，这里显式使用固定目录；
# 个别受限环境可用 AGENT_CORE_CONTRACT_SQLITE_DIR 覆盖（如工作区根目录）。
SQLITE_DIR = Path(
    os.environ.get(
        "AGENT_CORE_CONTRACT_SQLITE_DIR",
        str(Path(__file__).resolve().parent.parent / ".tmp_pg_contract"),
    )
)


def _postgres_reachable() -> bool:
    """快速探测 PG 端口，避免 asyncpg 的长超时把不可达场景拖成挂起。"""
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1.5):
            return True
    except OSError:
        return False


@dataclass
class StoreBundle:
    """一个后端下的全部存储实例，契约用例在该束上运行。"""

    backend: str
    prefix: str
    runtime: MemoryRuntimeStore | SqliteRuntimeStore | PostgresRuntimeStore
    session: MemorySessionStore | SqliteSessionStore | PostgresSessionStore
    session_strict: Any  # require_access=True 的 SessionStore
    memory: InMemoryMemoryStore | SqliteMemoryStore | PostgresMemoryStore
    context: Any  # ContextStore
    context_strict: Any  # require_access=True 的 ContextStore
    model_selection: Any  # ModelSelectionStore
    workflow: Any  # WorkflowExecutionStore
    _sqlite_path: Path | None = None

    async def cleanup(self) -> None:
        if self.backend == "postgres":
            await self._cleanup_postgres()
        elif self._sqlite_path is not None:
            await asyncio.to_thread(self._unlink_sqlite_files)

    def _unlink_sqlite_files(self) -> None:
        assert self._sqlite_path is not None
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(self._sqlite_path) + suffix)
            if path.exists():
                path.unlink()

    async def _cleanup_postgres(self) -> None:
        """删除本用例前缀写入的全部表数据，再关闭连接池。"""
        import asyncpg

        like = f"{self.prefix}%"
        targets = [
            ("agent_runs", "thread_id"),
            ("agent_approvals", "thread_id"),
            ("agent_threads", "thread_id"),
            ("sessions", "thread_id"),
            ("session_threads", "thread_id"),
            ("agent_memories", "memory_id"),
            ("agent_context", "thread_id"),
            ("agent_model_selections", "tenant_id"),
            ("workflow_executions", "execution_id"),
        ]
        conn = await asyncpg.connect(PG_DSN)
        try:
            for table, column in targets:
                await conn.execute(
                    f"DELETE FROM {table} WHERE {column} LIKE $1",
                    like,
                )
        finally:
            await conn.close()
        for store in (
            self.runtime,
            self.session,
            self.session_strict,
            self.memory,
            self.context,
            self.context_strict,
            self.model_selection,
            self.workflow,
        ):
            close = getattr(store, "close", None)
            if close is not None:
                await close()


def _memory_bundle(prefix: str) -> StoreBundle:
    return StoreBundle(
        backend="memory",
        prefix=prefix,
        runtime=MemoryRuntimeStore(),
        session=MemorySessionStore(),
        session_strict=MemorySessionStore(require_access=True),
        memory=InMemoryMemoryStore(),
        context=MemoryContextStore(),
        context_strict=MemoryContextStore(require_access=True),
        model_selection=MemoryModelSelectionStore(),
        workflow=MemoryWorkflowExecutionStore(),
    )


def _sqlite_bundle(prefix: str) -> StoreBundle:
    SQLITE_DIR.mkdir(parents=True, exist_ok=True)
    db_path = SQLITE_DIR / f"contract-{prefix}.db"
    return StoreBundle(
        backend="sqlite",
        prefix=prefix,
        runtime=SqliteRuntimeStore(db_path),
        session=SqliteSessionStore(db_path),
        session_strict=SqliteSessionStore(db_path, require_access=True),
        memory=SqliteMemoryStore(db_path),
        context=SqliteContextStore(db_path),
        context_strict=SqliteContextStore(db_path, require_access=True),
        model_selection=SqliteModelSelectionStore(db_path),
        workflow=SqliteWorkflowExecutionStore(db_path),
        _sqlite_path=db_path,
    )


async def _postgres_bundle(prefix: str) -> StoreBundle:
    bundle = StoreBundle(
        backend="postgres",
        prefix=prefix,
        runtime=PostgresRuntimeStore(PG_DSN),
        session=PostgresSessionStore(PG_DSN),
        session_strict=PostgresSessionStore(PG_DSN, require_access=True),
        memory=PostgresMemoryStore(PG_DSN),
        context=PostgresContextStore(PG_DSN),
        context_strict=PostgresContextStore(PG_DSN, require_access=True),
        model_selection=PostgresModelSelectionStore(PG_DSN),
        workflow=PostgresWorkflowExecutionStore(PG_DSN),
    )
    stores = [
        bundle.runtime,
        bundle.session,
        bundle.session_strict,
        bundle.memory,
        bundle.context,
        bundle.context_strict,
        bundle.model_selection,
        bundle.workflow,
    ]
    try:
        for store in stores:
            await store.initialize()
    except Exception as exc:  # pragma: no cover - 仅在 PG 环境异常时触发
        for store in stores:
            close = getattr(store, "close", None)
            if close is not None:
                await close()
        pytest.skip(f"PostgreSQL 测试实例不可达或初始化失败，跳过 PG 契约用例：{exc!r}")
    return bundle


@pytest.fixture(params=["memory", "sqlite", "postgres"])
async def stores(request: pytest.FixtureRequest) -> Any:
    """按参数化后端构造一套契约存储束；PG 不可达时 skip 该后端。"""
    if request.param == "postgres":
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            pytest.skip("未安装 asyncpg，跳过 PostgreSQL 契约用例")
        if not _postgres_reachable():
            pytest.skip(f"PostgreSQL 测试实例不可达（{PG_HOST}:{PG_PORT}），跳过 PG 契约用例")
    prefix = f"ct{uuid.uuid4().hex[:10]}"
    if request.param == "memory":
        bundle = _memory_bundle(prefix)
    elif request.param == "sqlite":
        bundle = _sqlite_bundle(prefix)
    else:
        bundle = await _postgres_bundle(prefix)
    try:
        yield bundle
    finally:
        await bundle.cleanup()


# ————————————————————————————————————————————————#
# RuntimeStore 契约
# ————————————————————————————————————————————————#


async def test_run_roundtrip_preserves_all_fields(stores: StoreBundle) -> None:
    """save_run/load_run 往返必须无损保留全部领域字段（含事件与审批）。"""
    ctx = RunContext(
        thread_id=f"{stores.prefix}-thread",
        run_id=f"{stores.prefix}-run-1",
        user_id="user-a",
        tenant_id="tenant-1",
        tags=["alpha", "中文标签"],
        status="running",
        idempotency_key=f"{stores.prefix}-idem-1",
        iteration=2,
        tool_calls_used=3,
        pending_clarification={"question": "需要确认执行时间吗？"},
        state={"step": "tool", "count": 1},
        metadata={"origin": "contract"},
        checkpoint={"messages": [{"role": "user", "content": "hi"}]},
    )
    ctx.pending_approval = ApprovalRecord(
        thread_id=ctx.thread_id,
        run_id=ctx.run_id,
        action="browser.click",
        arguments={"css": "#submit"},
        user_id="user-a",
        tenant_id="tenant-1",
    )
    ctx.emit("run_started", component="react")
    ctx.emit("tool_started", tool_name="browser.click")

    await stores.runtime.save_run(ctx)
    loaded = await stores.runtime.load_run(ctx.thread_id, ctx.run_id)

    assert loaded is not None
    expected = ctx.to_dict()
    actual = loaded.to_dict()
    assert actual == expected, f"{stores.backend}: 往返字段不一致"
    assert loaded.version == 1
    assert loaded.pending_approval is not None
    assert loaded.pending_approval.arguments == {"css": "#submit"}


async def test_run_version_conflict_rejected(stores: StoreBundle) -> None:
    """过期版本保存必须抛 RuntimeConcurrencyError，且存储内容不被覆盖。"""
    ctx = RunContext(thread_id=f"{stores.prefix}-t", run_id=f"{stores.prefix}-r")
    await stores.runtime.save_run(ctx)

    first = await stores.runtime.load_run(ctx.thread_id, ctx.run_id)
    stale = await stores.runtime.load_run(ctx.thread_id, ctx.run_id)
    assert first is not None and stale is not None

    first.status = "completed"
    await stores.runtime.save_run(first)

    stale.status = "failed"
    with pytest.raises(RuntimeConcurrencyError):
        await stores.runtime.save_run(stale)

    current = await stores.runtime.load_run(ctx.thread_id, ctx.run_id)
    assert current is not None
    assert current.status == "completed", f"{stores.backend}: 冲突保存覆盖了已存储状态"
    assert current.version == 2


async def test_run_version_increments_on_each_save(stores: StoreBundle) -> None:
    """连续保存同一 run 时版本单调递增，调用方无需手动维护版本号。"""
    ctx = RunContext(thread_id=f"{stores.prefix}-t", run_id=f"{stores.prefix}-r")
    await stores.runtime.save_run(ctx)
    assert ctx.version == 1
    ctx.status = "running"
    await stores.runtime.save_run(ctx)
    assert ctx.version == 2

    loaded = await stores.runtime.load_run(ctx.thread_id, ctx.run_id)
    assert loaded is not None and loaded.version == 2


async def test_run_idempotency_lookup_respects_scope(stores: StoreBundle) -> None:
    """find_run_by_idempotency 按 (tenant, user, key) 精确匹配，匿名作用域互不串扰。"""
    ctx = RunContext(
        thread_id=f"{stores.prefix}-t1",
        run_id=f"{stores.prefix}-r1",
        user_id="u1",
        tenant_id="t1",
        idempotency_key=f"{stores.prefix}-k1",
    )
    await stores.runtime.save_run(ctx)
    anonymous = RunContext(
        thread_id=f"{stores.prefix}-t2",
        run_id=f"{stores.prefix}-r2",
        idempotency_key=f"{stores.prefix}-k2",
    )
    await stores.runtime.save_run(anonymous)

    found = await stores.runtime.find_run_by_idempotency("t1", "u1", f"{stores.prefix}-k1")
    assert found is not None and found.run_id == f"{stores.prefix}-r1"

    assert await stores.runtime.find_run_by_idempotency("t1", "u1", "other-key") is None
    assert await stores.runtime.find_run_by_idempotency("t2", "u1", f"{stores.prefix}-k1") is None
    assert await stores.runtime.find_run_by_idempotency(None, None, f"{stores.prefix}-k1") is None

    anonymous_found = await stores.runtime.find_run_by_idempotency(None, None, f"{stores.prefix}-k2")
    assert anonymous_found is not None and anonymous_found.run_id == f"{stores.prefix}-r2"


async def test_run_duplicate_idempotency_key_rejected(stores: StoreBundle) -> None:
    """第二个 run 复用同 scope 的幂等键必须被拒绝，且不覆盖首个 run。

    已知不一致：PostgreSQL 抛 RuntimeConcurrencyError；SQLite 抛
    sqlite3.IntegrityError；Memory 静默接受。此用例把差异以 xfail 记录。
    """
    key = f"{stores.prefix}-dup"
    first = RunContext(
        thread_id=f"{stores.prefix}-t",
        run_id=f"{stores.prefix}-r1",
        user_id="u1",
        tenant_id="t1",
        idempotency_key=key,
    )
    await stores.runtime.save_run(first)
    duplicate = RunContext(
        thread_id=f"{stores.prefix}-t",
        run_id=f"{stores.prefix}-r2",
        user_id="u1",
        tenant_id="t1",
        idempotency_key=key,
    )

    rejected: str | None = None
    try:
        await stores.runtime.save_run(duplicate)
    except RuntimeConcurrencyError:
        rejected = "domain"
    except Exception as exc:  # 故意捕获以比较错误类型
        rejected = f"other:{type(exc).__name__}"

    if stores.backend == "memory":
        pytest.xfail(
            f"三后端不一致：Memory 后端不拒绝重复幂等键（实际行为：{rejected or '静默接受'}）"
        )
    if stores.backend == "sqlite" and rejected != "domain":
        pytest.xfail(
            f"三后端不一致：SQLite 重复幂等键抛 {rejected} 而非 RuntimeConcurrencyError"
        )
    assert rejected == "domain", f"{stores.backend}: 重复幂等键未被领域错误拒绝（{rejected}）"

    found = await stores.runtime.find_run_by_idempotency("t1", "u1", key)
    assert found is not None and found.run_id == f"{stores.prefix}-r1"


async def test_run_thread_isolation_and_owner(stores: StoreBundle) -> None:
    """thread 之间互相隔离；thread 归属首次绑定后不可被其他身份改写。"""
    thread_a = f"{stores.prefix}-ta"
    thread_b = f"{stores.prefix}-tb"
    run_a = RunContext(thread_id=thread_a, run_id=f"{stores.prefix}-ra", status="running")
    run_b = RunContext(thread_id=thread_b, run_id=f"{stores.prefix}-rb", status="running")
    await stores.runtime.save_run(run_a)
    await stores.runtime.save_run(run_b)

    assert await stores.runtime.load_run(thread_b, run_a.run_id) is None
    runs_a = await stores.runtime.list_runs(thread_a)
    assert {run.run_id for run in runs_a} == {run_a.run_id}

    await stores.runtime.save_thread_owner(thread_a, "u1", "t1")
    assert await stores.runtime.get_thread_owner(thread_a) == ("u1", "t1")
    with pytest.raises(RuntimeConcurrencyError):
        await stores.runtime.save_thread_owner(thread_a, "u2", "t1")
    assert await stores.runtime.get_thread_owner(thread_a) == ("u1", "t1")


# ————————————————————————————————————————————————#
# 审批 CAS 契约
# ————————————————————————————————————————————————#


def _approval(stores: StoreBundle, **overrides: Any) -> ApprovalRecord:
    value = ApprovalRecord(
        thread_id=f"{stores.prefix}-t",
        run_id=f"{stores.prefix}-r",
        action="shell.exec",
        arguments={"cmd": "deploy.sh"},
        user_id="u1",
        tenant_id="t1",
    )
    return ApprovalRecord.from_dict({**value.to_dict(), **overrides})


async def test_approval_transition_cas_semantics(stores: StoreBundle) -> None:
    """审批状态迁移：pending 才可迁移、重放与错误期望状态都返回 False。"""
    approval = _approval(stores)
    await stores.runtime.save_approval(approval)

    loaded = await stores.runtime.load_approval(approval.approval_id)
    assert loaded is not None
    assert loaded.status == "pending"
    assert loaded.arguments == {"cmd": "deploy.sh"}
    assert loaded.action == "shell.exec"

    decided = ApprovalRecord.from_dict({**approval.to_dict(), "status": "approved", "reason": "通过"})
    assert await stores.runtime.transition_approval(decided, expected_status="pending") is True

    after = await stores.runtime.load_approval(approval.approval_id)
    assert after is not None and after.status == "approved" and after.reason == "通过"

    # 重放：pending 已不存在，CAS 必须拒绝。
    assert await stores.runtime.transition_approval(decided, expected_status="pending") is False
    # 错误的期望状态。
    rejected = ApprovalRecord.from_dict({**approval.to_dict(), "status": "rejected"})
    assert await stores.runtime.transition_approval(rejected, expected_status="expired") is False
    # 不存在的审批。
    missing = _approval(stores)
    assert await stores.runtime.transition_approval(missing, expected_status="pending") is False


async def test_approval_decision_cannot_be_overwritten(stores: StoreBundle) -> None:
    """决议（非 pending）之后，save_approval 不得覆盖既有结论。"""
    approval = _approval(stores)
    await stores.runtime.save_approval(approval)
    decided = ApprovalRecord.from_dict({**approval.to_dict(), "status": "rejected", "reason": "拒绝"})
    assert await stores.runtime.transition_approval(decided, expected_status="pending") is True

    resubmit = ApprovalRecord.from_dict({**approval.to_dict(), "status": "pending", "reason": "再次提交"})
    await stores.runtime.save_approval(resubmit)

    loaded = await stores.runtime.load_approval(approval.approval_id)
    assert loaded is not None
    assert loaded.status == "rejected", f"{stores.backend}: 决议后被 save_approval 覆盖"
    assert loaded.reason == "拒绝"


# ————————————————————————————————————————————————#
# SessionStore 契约
# ————————————————————————————————————————————————#


async def test_session_roundtrip_overwrite_and_delete(stores: StoreBundle) -> None:
    """消息追加/覆盖/删除语义一致，tool_calls 与中文内容无损往返。"""
    thread = f"{stores.prefix}-session"
    tool_msg = Message(
        role="tool",
        content="42",
        name="calculator",
        tool_call_id="call-1",
        tool_calls=[{"id": "call-1", "name": "calculator", "arguments": {"expr": "6*7"}}],
    )
    await stores.session.append(thread, [user_message("你好"), assistant_message("你好！")])
    await stores.session.append(thread, [tool_msg, user_message("再见")])

    history = await stores.session.load(thread)
    assert [msg.role for msg in history] == ["user", "assistant", "tool", "user"]
    assert history[2].content == "42" and history[2].name == "calculator"
    assert history[2].tool_calls == [{"id": "call-1", "name": "calculator", "arguments": {"expr": "6*7"}}]
    assert await stores.session.count(thread) == 4

    await stores.session.save(thread, [user_message("覆盖后的消息")])
    overwritten = await stores.session.load(thread)
    assert len(overwritten) == 1 and overwritten[0].content == "覆盖后的消息"
    assert await stores.session.count(thread) == 1

    threads = await stores.session.list_threads()
    assert thread in threads

    await stores.session.delete(thread)
    assert await stores.session.load(thread) == []
    assert await stores.session.count(thread) == 0


async def test_session_access_isolation(stores: StoreBundle) -> None:
    """require_access 的 SessionStore 必须按 (user, tenant) 隔离会话历史。"""
    thread = f"{stores.prefix}-shared"
    alice = AccessContext(user_id="alice", tenant_id="t1")
    bob = AccessContext(user_id="bob", tenant_id="t1")
    admin = AccessContext(user_id="root", tenant_id="t1", roles=frozenset({"admin"}))

    await stores.session_strict.save(thread, [user_message("alice 的数据")], access=alice)

    assert (await stores.session_strict.load(thread, access=alice))[0].content == "alice 的数据"
    with pytest.raises(PermissionError):
        await stores.session_strict.load(thread, access=bob)
    with pytest.raises(PermissionError):
        await stores.session_strict.append(thread, [user_message("越权写入")], access=bob)
    with pytest.raises(PermissionError):
        await stores.session_strict.load(thread, access=None)

    # 同租户管理员可见；其他用户不可见。
    assert thread in await stores.session_strict.list_threads(access=admin)
    assert thread not in await stores.session_strict.list_threads(access=bob)

    # 越权删除同样被拒绝，数据保持完整。
    with pytest.raises(PermissionError):
        await stores.session_strict.delete(thread, access=bob)
    assert await stores.session_strict.count(thread, access=alice) == 1


# ————————————————————————————————————————————————#
# ContextStore 契约
# ————————————————————————————————————————————————#


async def test_context_merge_update_and_clear(stores: StoreBundle) -> None:
    """update 按键合并而非整体替换；_access 是保留字段；clear 清空该 thread。"""
    thread = f"{stores.prefix}-ctx"
    await stores.context.update(thread, {"preference": {"theme": "dark"}, "refs": ["a"]})
    await stores.context.update(thread, {"refs": ["a", "b"]})

    value = await stores.context.get(thread)
    assert value["preference"] == {"theme": "dark"}, f"{stores.backend}: update 覆盖了既有键"
    assert value["refs"] == ["a", "b"]

    with pytest.raises(ValueError):
        await stores.context.update(thread, {"_access": {"user_id": "evil"}})

    await stores.context.clear(thread)
    assert await stores.context.get(thread) == {}
    # clear 后写入再读，互不影响其他 thread。
    other = f"{stores.prefix}-ctx-other"
    await stores.context.update(other, {"k": "v"})
    assert await stores.context.get(other) == {"k": "v"}


async def test_context_access_isolation(stores: StoreBundle) -> None:
    """require_access 的 ContextStore 按归属隔离，匿名访问一律拒绝。"""
    thread = f"{stores.prefix}-ctx"
    alice = AccessContext(user_id="alice", tenant_id="t1")
    bob = AccessContext(user_id="bob", tenant_id="t1")

    await stores.context_strict.update(thread, {"k": "alice"}, access=alice)

    bound = await stores.context_strict.get(thread, access=alice)
    assert bound["k"] == "alice"
    # 绑定所有者后 get 会带回 _access 元数据（三后端一致），校验归属未被越权改写。
    assert bound["_access"]["user_id"] == "alice" and bound["_access"]["tenant_id"] == "t1"
    with pytest.raises(PermissionError):
        await stores.context_strict.get(thread, access=bob)
    with pytest.raises(PermissionError):
        await stores.context_strict.update(thread, {"k": "bob"}, access=bob)
    with pytest.raises(PermissionError):
        await stores.context_strict.get(thread, access=None)

    # 更新未被越权修改。
    assert (await stores.context_strict.get(thread, access=alice))["k"] == "alice"

# ————————————————————————————————————————————————#
# ModelSelectionStore 契约
# ————————————————————————————————————————————————#


async def test_model_selection_user_overrides_tenant(stores: StoreBundle) -> None:
    """user 级选择优先于 tenant 级；重复保存版本递增；clear 后回落。"""
    store = stores.model_selection

    tenant_first = await store.save(
        "OpenAI-Compatible", " qwen-max ", scope="tenant", user_id=None, tenant_id="t1"
    )
    assert tenant_first.provider == "openai-compatible" and tenant_first.model == "qwen-max"
    assert tenant_first.version == 1 and tenant_first.scope == "tenant"

    fallback = await store.resolve(user_id="u1", tenant_id="t1")
    assert fallback is not None and fallback.scope == "tenant"

    user_sel = await store.save("Anthropic", "claude-x", scope="user", user_id="u1", tenant_id="t1")
    assert user_sel.version == 1 and user_sel.user_id == "u1"

    resolved = await store.resolve(user_id="u1", tenant_id="t1")
    assert resolved is not None
    assert (resolved.provider, resolved.model, resolved.scope) == ("anthropic", "claude-x", "user")

    tenant_second = await store.save(
        "openai-compatible", "qwen-plus", scope="tenant", user_id=None, tenant_id="t1"
    )
    assert tenant_second.version == 2, f"{stores.backend}: tenant 版本未递增"

    tenant_only = await store.resolve(user_id=None, tenant_id="t1")
    assert tenant_only is not None and tenant_only.model == "qwen-plus"

    await store.clear(scope="user", user_id="u1", tenant_id="t1")
    back_to_tenant = await store.resolve(user_id="u1", tenant_id="t1")
    assert back_to_tenant is not None and back_to_tenant.scope == "tenant"

    await store.clear(scope="tenant", user_id=None, tenant_id="t1")
    assert await store.resolve(user_id=None, tenant_id="t1") is None

    with pytest.raises(ValueError):
        await store.save("openai", "m", scope="user", user_id=None, tenant_id="t1")


# ————————————————————————————————————————————————#
# WorkflowExecutionStore 契约
# ————————————————————————————————————————————————#


async def test_workflow_roundtrip_and_list_filter(stores: StoreBundle) -> None:
    """执行实例往返无损；list 按 definition_id 与 tenant 过滤。"""
    execution = WorkflowExecution(
        definition_id=f"{stores.prefix}-def",
        execution_type="macro",
        user_id="u1",
        tenant_id="t1",
        status="running",
        current_step="step-1",
        steps_completed=1,
        total_steps=3,
        input_data={"city": "北京"},
        events=[{"type": "started"}],
    )
    await stores.workflow.save(execution)
    assert execution.version == 1

    loaded = await stores.workflow.load(execution.execution_id)
    assert loaded is not None
    expected = execution.to_dict()
    actual = loaded.to_dict()
    for field in (
        "execution_id",
        "definition_id",
        "execution_type",
        "user_id",
        "tenant_id",
        "status",
        "current_step",
        "steps_completed",
        "total_steps",
        "input_data",
        "events",
        "version",
    ):
        assert actual[field] == expected[field], f"{stores.backend}: 字段 {field} 往返不一致"

    loaded.status = "completed"
    loaded.result_data = {"ok": True}
    await stores.workflow.save(loaded)
    assert loaded.version == 2

    assert {item.execution_id for item in await stores.workflow.list(f"{stores.prefix}-def")} == {
        execution.execution_id
    }
    assert len(await stores.workflow.list(f"{stores.prefix}-def", tenant_id="t1")) == 1
    assert await stores.workflow.list(f"{stores.prefix}-def", tenant_id="other") == []
    assert await stores.workflow.list(f"{stores.prefix}-other-def") == []


async def test_workflow_version_conflict_rejected(stores: StoreBundle) -> None:
    """过期版本的工作流保存必须抛 WorkflowConcurrencyError 且不覆盖。"""
    execution = WorkflowExecution(definition_id=f"{stores.prefix}-def", tenant_id="t1")
    await stores.workflow.save(execution)

    first = await stores.workflow.load(execution.execution_id)
    stale = await stores.workflow.load(execution.execution_id)
    assert first is not None and stale is not None

    first.status = "completed"
    await stores.workflow.save(first)

    stale.status = "failed"
    with pytest.raises(WorkflowConcurrencyError):
        await stores.workflow.save(stale)

    current = await stores.workflow.load(execution.execution_id)
    assert current is not None
    assert current.status == "completed" and current.version == 2
