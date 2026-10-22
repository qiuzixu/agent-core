"""Checkpoint and versioned state management."""

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
        from agent_core.checkpoint.sqlite import SqliteCheckpointer
        __all__ = [
            "CHECKPOINT_SCHEMA_VERSION", "CheckpointVersion", "Checkpointer",
            "MemoryCheckpointer", "SqliteCheckpointer",
            "TimeTravelCheckpointer", "create_checkpointer", "trace_tree",
        ]
    except ImportError:
        __all__ = [
            "CHECKPOINT_SCHEMA_VERSION", "CheckpointVersion", "Checkpointer",
            "MemoryCheckpointer",
            "TimeTravelCheckpointer", "create_checkpointer", "trace_tree",
        ]
