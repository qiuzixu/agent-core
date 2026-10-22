"""Checkpoint 包：会话状态的持久化、版本管理与谱系查询。

包结构（依赖方向自上而下，无循环）：

    naming.py    底层工具：thread_id 文件名清洗 / 原子写入（无包内依赖）
    store.py     Checkpointer 协议 + MemoryCheckpointer（测试用）
    travel.py    TimeTravelCheckpointer：多版本 / 回滚 / fork 分支
    schema.py    schema 版本戳与链式迁移（被 travel / sqlite / postgres 复用）
    sqlite.py    SqliteCheckpointer（开发环境，落盘 checkpoints.db）
    postgres.py  PostgresCheckpointer（生产环境，asyncpg 连接池）
    factory.py   create_checkpointer()：按环境自动选后端
    trace.py     trace_tree()：按 thread_id 查询全部 namespace 的版本树

对外常用入口：

    from agent_core.checkpoint import create_checkpointer   # 工厂（推荐）
    from agent_core.checkpoint import trace_tree            # 谱系查询

条件导入说明：asyncpg 是可选依赖（agent-core[postgresql]），
未安装时 PostgresCheckpointer 不可用，但 SQLite / Memory 后端照常工作——
所以用 try/except 逐级降级 __all__，而不是硬性导入失败。
"""

from agent_core.checkpoint.factory import create_checkpointer
from agent_core.checkpoint.schema import CHECKPOINT_SCHEMA_VERSION
from agent_core.checkpoint.store import (
    Checkpointer,
    MemoryCheckpointer,
)
from agent_core.checkpoint.trace import trace_tree
from agent_core.checkpoint.travel import (
    CheckpointVersion,
    TimeTravelCheckpointer,
)

try:
    # 全量模式：asyncpg 已安装，五个后端全部可用
    from agent_core.checkpoint.sqlite import SqliteCheckpointer
    from agent_core.checkpoint.postgres import PostgresCheckpointer
    __all__ = [
        "CHECKPOINT_SCHEMA_VERSION",
        "CheckpointVersion",
        "Checkpointer",
        "MemoryCheckpointer",
        "PostgresCheckpointer",
        "SqliteCheckpointer",
        "TimeTravelCheckpointer",
        "create_checkpointer",
        "trace_tree",
    ]
except ImportError:
    try:
        # 降级模式一：无 asyncpg，Postgres 不可用，SQLite 仍可用
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        __all__ = [
            "CHECKPOINT_SCHEMA_VERSION", "CheckpointVersion", "Checkpointer",
            "MemoryCheckpointer", "SqliteCheckpointer",
            "TimeTravelCheckpointer", "create_checkpointer", "trace_tree",
        ]
    except ImportError:
        # 降级模式二：连 sqlite3 也不可用（极罕见），只剩内存与时间旅行
        __all__ = [
            "CHECKPOINT_SCHEMA_VERSION", "CheckpointVersion", "Checkpointer",
            "MemoryCheckpointer",
            "TimeTravelCheckpointer", "create_checkpointer", "trace_tree",
        ]
