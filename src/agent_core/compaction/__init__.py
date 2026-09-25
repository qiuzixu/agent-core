"""通用上下文压缩能力。"""

from agent_core.compaction.middleware import OVERFLOW_RETRY_KEY, CompactionMiddleware, is_context_overflow_error
from agent_core.compaction.pruner import PRUNE_MARKER, PruneConfig, prune_text, prune_tool_results
from agent_core.compaction.prompts import DEFAULT_COMPACTION_INSTRUCTION, DEFAULT_SUMMARY_SECTIONS
from agent_core.compaction.region import (
    balanced_cut,
    build_checkpoint,
    estimate_message_tokens,
    estimate_tokens,
    extract_previous_summary,
    rebuild,
    select_cut,
)
from agent_core.compaction.spill import SpillStore
from agent_core.compaction.types import (
    SUMMARY_CLOSE,
    SUMMARY_OPEN,
    SUMMARY_SOURCE,
    CompactionConfig,
    CompactionError,
    CompactionResult,
)
from agent_core.compaction.token_limit import TokenLimitMiddleware

__all__ = [
    "DEFAULT_COMPACTION_INSTRUCTION",
    "DEFAULT_SUMMARY_SECTIONS",
    "OVERFLOW_RETRY_KEY",
    "PRUNE_MARKER",
    "SUMMARY_CLOSE",
    "SUMMARY_OPEN",
    "SUMMARY_SOURCE",
    "CompactionConfig",
    "CompactionError",
    "CompactionMiddleware",
    "CompactionResult",
    "TokenLimitMiddleware",
    "PruneConfig",
    "SpillStore",
    "balanced_cut",
    "build_checkpoint",
    "estimate_message_tokens",
    "estimate_tokens",
    "extract_previous_summary",
    "is_context_overflow_error",
    "prune_text",
    "prune_tool_results",
    "rebuild",
    "select_cut",
]
