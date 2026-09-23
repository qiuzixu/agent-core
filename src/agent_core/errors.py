"""Agent Core 公共错误定义。"""

from __future__ import annotations


class AgentError(Exception):
    """Agent 基础错误。"""


class ConfigurationError(AgentError):
    """配置错误。"""


class ModelInvocationError(AgentError):
    """模型调用错误。"""


class ModelRateLimitError(ModelInvocationError):
    """模型服务限流，适合按退避策略重试。"""


class ModelTimeoutError(ModelInvocationError):
    """模型调用超时，适合有限次数重试。"""


class ModelUnavailableError(ModelInvocationError):
    """模型服务暂时不可用，适合按退避策略重试。"""


class ModelOutputValidationError(AgentError):
    """模型输出验证错误。"""


class ToolExecutionError(AgentError):
    """工具执行错误。"""


class StateMachineError(AgentError):
    """状态机错误。"""


class CheckpointError(AgentError):
    """Checkpoint 持久化错误。"""


class MiddlewareError(AgentError):
    """中间件错误。"""
