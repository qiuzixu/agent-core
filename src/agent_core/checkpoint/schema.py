"""Checkpoint schema 版本戳与链式迁移。

设计参考 CrewAI 的 crewai_version + _migrate() 模式：
- save 前统一打上当前版本戳（ensure_schema_version，幂等）；
- load 时发现旧版本，沿注册表逐步迁移到当前版本（migrate_state）；
- 版本号是单调递增整数，每步用 @register_migration(from, to) 注册；
- 缺少某一步迁移时抛 SchemaMigrationError（诚实报错，不猜测）。

Original summary: schema versioning and state migration.

Provides CHECKPOINT_SCHEMA_VERSION, a migration registry (register_migration),
and auto-migration helpers (ensure_schema_version / migrate_state) for use by
travel.py and sqlite.py to prevent old-format states from being silently broken.

Design reference: CrewAI's crewai_version + _migrate() pattern.
Each version bumps by one step; migration functions are chained until
the current version is reached. Versions are monotonically increasing
integers (no semver flexibility needed for checkpoint state shapes).
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "SchemaMigrationError",
    "ensure_schema_version",
    "get_schema_version",
    "migrate_state",
    "register_migration",
]

# 当前 schema 版本。state 结构不兼容变更时 +1。
CHECKPOINT_SCHEMA_VERSION = 1

# 迁移函数签名：(旧版本 state) -> (新版本 state)
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]

# 迁移注册表：{from_version: {to_version: 迁移函数}}
_migrations: dict[int, dict[int, MigrationFn]] = {}


class SchemaMigrationError(Exception):
    """schema 迁移失败（缺少迁移函数或版本回退）。"""


def register_migration(
    from_version: int,
    to_version: int,
) -> Callable[[MigrationFn], MigrationFn]:
    """装饰器：注册从 from_version 迁移到 to_version 的函数。"""


    if to_version <= from_version:
        raise ValueError(f"to_version ({to_version}) must be > from_version ({from_version})")

    def decorator(fn: MigrationFn) -> MigrationFn:
        _migrations.setdefault(from_version, {})[to_version] = fn
        return fn

    return decorator


def get_schema_version(state: dict[str, Any]) -> int:
    """从 state 提取 schema 版本号；没有版本戳的旧数据视为 v0。"""

    metadata = state.get("_checkpoint_metadata")
    if isinstance(metadata, dict) and "schema_version" in metadata:
        return int(metadata["schema_version"])
    return 0


def migrate_state(state: dict[str, Any], from_version: int) -> dict[str, Any]:
    """把 state 从 from_version 逐步链式迁移到当前版本。"""


    if from_version >= CHECKPOINT_SCHEMA_VERSION:
        return state

    if from_version < 0:
        raise SchemaMigrationError(f"Invalid schema version: {from_version}")

    migrated = dict(state)

    for step in range(from_version, CHECKPOINT_SCHEMA_VERSION):
        target = step + 1
        migration = _migrations.get(step, {}).get(target)
        if migration is None:
            raise SchemaMigrationError(
                f"Missing checkpoint schema migration: v{step} -> v{target}. "
                f"Register with @register_migration({step}, {target}) in schema.py."
            )
        migrated = migration(migrated)

    metadata = migrated.get("_checkpoint_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    metadata["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    migrated["_checkpoint_metadata"] = metadata

    return migrated


def ensure_schema_version(
    state: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """保存前把当前 schema 版本戳写入 state（幂等操作）。"""


    result = dict(state)
    checkpoint_metadata = result.get("_checkpoint_metadata")
    if not isinstance(checkpoint_metadata, dict):
        checkpoint_metadata = {}
    checkpoint_metadata["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    result["_checkpoint_metadata"] = checkpoint_metadata
    return result
