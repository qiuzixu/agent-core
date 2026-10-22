"""Checkpoint schema versioning and state migration.

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

# Current schema version. Bump +1 on incompatible state structure changes.
CHECKPOINT_SCHEMA_VERSION = 1

# Migration function signature: (old_version_state) -> (new_version_state)
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]

# Migration registry: {from_version: {to_version: migration_fn}}
_migrations: dict[int, dict[int, MigrationFn]] = {}


class SchemaMigrationError(Exception):
    """Checkpoint state migration failed (missing fn or version regression)."""


def register_migration(
    from_version: int,
    to_version: int,
) -> Callable[[MigrationFn], MigrationFn]:
    """Decorator: register a migration function from from_version to to_version."""

    if to_version <= from_version:
        raise ValueError(f"to_version ({to_version}) must be > from_version ({from_version})")

    def decorator(fn: MigrationFn) -> MigrationFn:
        _migrations.setdefault(from_version, {})[to_version] = fn
        return fn

    return decorator


def get_schema_version(state: dict[str, Any]) -> int:
    """Extract schema version from state. States without version metadata are v0."""
    metadata = state.get("_checkpoint_metadata")
    if isinstance(metadata, dict) and "schema_version" in metadata:
        return int(metadata["schema_version"])
    return 0


def migrate_state(state: dict[str, Any], from_version: int) -> dict[str, Any]:
    """Chain-migrate state from from_version to the current schema version."""

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
    """Stamp the current schema version onto state before saving (idempotent)."""

    result = dict(state)
    checkpoint_metadata = result.get("_checkpoint_metadata")
    if not isinstance(checkpoint_metadata, dict):
        checkpoint_metadata = {}
    checkpoint_metadata["schema_version"] = CHECKPOINT_SCHEMA_VERSION
    result["_checkpoint_metadata"] = checkpoint_metadata
    return result
