"""Agent 中间件。"""

# 兼容旧导入路径；能力实现分别归属 hitl 和 compaction 领域模块。
from agent_core.compaction.token_limit import TokenLimitMiddleware
from agent_core.hitl.core import HumanInTheLoopMiddleware
from agent_core.middleware.audit import AuditConfig, AuditMiddleware, AuditRecord
from agent_core.middleware.auth import (
    AuthConfig,
    AuthMiddleware,
    AuthMode,
    jwt_decode,
    jwt_encode,
)
from agent_core.middleware.base import (
    LoggingMiddleware,
    Middleware,
    MiddlewareAction,
    MiddlewareContext,
    MiddlewareManager,
    MiddlewareResult,
    RetryMiddleware,
)
from agent_core.middleware.circuit_breaker import (
    CircuitBreakerConfig,
    CircuitBreakerMiddleware,
    CircuitBreakerRegistry,
    CircuitState,
)
from agent_core.middleware.cost_budget import (
    CostBudgetConfig,
    CostBudgetMiddleware,
    CostRecord,
    UserCostTracker,
)
from agent_core.middleware.feedback import (
    FeedbackConfig,
    FeedbackMiddleware,
    FeedbackRecord,
    FeedbackStore,
)
from agent_core.middleware.memory_decay import (
    MemoryDecayConfig,
    MemoryDecayService,
    decay_score,
    select_forgetting,
)
from agent_core.middleware.parallel_policy import ParallelToolPolicy
from agent_core.middleware.resource_rbac import (
    ResourceRbacConfig,
    ResourceRbacMiddleware,
)
from agent_core.middleware.retention import (
    RetentionSweeper,
    RetentionPolicy,
    apply_retention,
)
from agent_core.middleware.tracing import (
    TraceConfig,
    TraceMiddleware,
    extract_trace_id,
    format_traceparent,
    generate_trace_id,
)
from agent_core.middleware.idempotency import (
    IdempotencyConfig,
    IdempotencyMiddleware,
)
from agent_core.middleware.injection_guard import (
    InjectionGuardConfig,
    InjectionGuardMiddleware,
)
from agent_core.middleware.semantic_cache import (
    SemanticCacheConfig,
    SemanticCacheMiddleware,
)
from agent_core.middleware.alerting import (
    AlertingConfig,
    AlertingMiddleware,
    LoggingAlertSink,
)
from agent_core.middleware.dlq import DLQConfig, DLQMiddleware, JsonlDeadLetterSink
from agent_core.middleware.dynamic_config import FileConfigCenter
from agent_core.middleware.entity_extraction import (
    EntityExtractionConfig,
    EntityExtractionMiddleware,
)
from agent_core.middleware.fallback import FallbackConfig, FallbackMiddleware
from agent_core.middleware.signature import (
    NonceStore,
    SignatureConfig,
    SignatureMiddleware,
    compute_signature,
    generate_nonce,
    verify_signature,
)
from agent_core.middleware.semantic_cache import (
    SemanticCacheConfig,
    SemanticCacheMiddleware,
)
from agent_core.middleware.timeout_budget import (
    TimeoutBudgetConfig,
    TimeoutBudgetMiddleware,
)
from agent_core.middleware.tool_cache import (
    InMemoryToolCacheStore,
    ToolCacheConfig,
    ToolCacheMiddleware,
)
from agent_core.middleware.secrets import (
    ChainedSecretsProvider,
    EnvSecretsProvider,
    FileSecretsProvider,
    SecretsProvider,
    resolve_config,
    resolve_secret,
)
from agent_core.middleware.a2a import (
    A2A_TOOL_PREFIX,
    A2AMessage,
    A2AAgentRegistry,
    A2AMiddleware,
    AgentCard,
)
from agent_core.middleware.self_check import (
    SelfCheckConfig,
    SelfCheckMiddleware,
    check_answer,
)
from agent_core.middleware.canary import CanaryConfig, CanaryRouter
from agent_core.middleware.tool_validation import (
    ToolValidationConfig,
    ToolValidationMiddleware,
)

__all__ = [
    "AuditConfig",
    "AuditMiddleware",
    "AuditRecord",
    "AuthConfig",
    "AuthMiddleware",
    "AuthMode",
    "CostBudgetConfig",
    "CostBudgetMiddleware",
    "CostRecord",
    "CircuitBreakerConfig",
    "CircuitBreakerMiddleware",
    "CircuitBreakerRegistry",
    "CircuitState",
    "FeedbackConfig",
    "FeedbackMiddleware",
    "FeedbackRecord",
    "FeedbackStore",
    "HumanInTheLoopMiddleware",
    "IdempotencyConfig",
    "IdempotencyMiddleware",
    "InjectionGuardConfig",
    "InjectionGuardMiddleware",
    "MemoryDecayConfig",
    "MemoryDecayService",
    "decay_score",
    "select_forgetting",
    "LoggingMiddleware",
    "Middleware",
    "MiddlewareAction",
    "MiddlewareContext",
    "MiddlewareManager",
    "MiddlewareResult",
    "ParallelToolPolicy",
    "RetryMiddleware",
    "ResourceRbacConfig",
    "ResourceRbacMiddleware",
    "RetentionPolicy",
    "RetentionSweeper",
    "apply_retention",
    "TraceMiddleware",
    "TraceConfig",
    "extract_trace_id",
    "format_traceparent",
    "generate_trace_id",
    "SemanticCacheConfig",
    "SemanticCacheMiddleware",
    "NonceStore",
    "SignatureConfig",
    "SignatureMiddleware",
    "compute_signature",
    "generate_nonce",
    "verify_signature",
    "TokenLimitMiddleware",
    "ToolValidationConfig",
    "ToolValidationMiddleware",
    "UserCostTracker",
    "AlertingConfig",
    "AlertingMiddleware",
    "LoggingAlertSink",
    "DLQConfig",
    "DLQMiddleware",
    "JsonlDeadLetterSink",
    "FileConfigCenter",
    "EntityExtractionConfig",
    "EntityExtractionMiddleware",
    "FallbackConfig",
    "FallbackMiddleware",
    "TimeoutBudgetConfig",
    "TimeoutBudgetMiddleware",
    "InMemoryToolCacheStore",
    "ToolCacheConfig",
    "ToolCacheMiddleware",
    "jwt_decode",
    "jwt_encode",
    "A2A_TOOL_PREFIX",
    "A2AMessage",
    "A2AAgentRegistry",
    "A2AMiddleware",
    "AgentCard",
    "SelfCheckConfig",
    "SelfCheckMiddleware",
    "check_answer",
    "CanaryConfig",
    "CanaryRouter",
]
