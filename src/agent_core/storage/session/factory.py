"""按运行环境创建会话存储后端。"""

from __future__ import annotations

import logging

from agent_core.storage.session.base import BaseSessionStore
from agent_core.storage.session.memory import MemorySessionStore
from agent_core.storage.session.postgres import PostgresSessionStore
from agent_core.storage.session.sqlite import SqliteSessionStore

logger = logging.getLogger(__name__)


def create_session_store(
    env: str = "development",
    *,
    sqlite_path: str = "./sessions.db",
    postgres_url: str | None = None,
    require_access: bool = False,
) -> BaseSessionStore:
    """根据运行环境创建合适的会话存储后端。

    对应关系：
        development → SqliteSessionStore   ≈ SqliteSaver
        production  → PostgresSessionStore ≈ PostgresSaver

    Args:
        env: 运行环境，"development"（默认）或 "production"。
        sqlite_path: SQLite 数据库文件路径。
        postgres_url: PostgreSQL 连接 URL（生产环境必填）。
        require_access: 是否强制所有调用携带 ``AccessContext``。

    Returns:
        对应的存储后端实例。

    Raises:
        ValueError: 生产环境未配置 postgres_url。
    """
    normalized = env.lower()
    if normalized in ("test", "testing"):
        logger.info("Session store: memory (test)")
        return MemorySessionStore(require_access=require_access)

    is_prod = normalized in ("production", "prod")

    if is_prod:
        if not postgres_url:
            raise ValueError(
                "生产环境需要配置 AGENT_SESSION_POSTGRES_URL\n"
                "例如：postgresql://user:pass@localhost:5432/agent_db"
            )
        logger.info("Session store: PostgreSQL (production)")
        return PostgresSessionStore(postgres_url, require_access=require_access)

    logger.info("Session store: SQLite (development) → %s", sqlite_path)
    return SqliteSessionStore(sqlite_path, require_access=require_access)


# ──────────────────────────────────────────────
# 5. 会话管理器（组合 Agent + Store）
# ──────────────────────────────────────────────
