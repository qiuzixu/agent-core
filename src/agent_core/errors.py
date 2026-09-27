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


class DocumentError(AgentError):
    """文档加载或解析基础错误。"""


class DocumentParsingError(DocumentError):
    """文档内容或解析服务响应无法转换成 Core Document。"""


class DocumentServiceError(DocumentError):
    """外部文档解析服务调用失败。"""


class MemoryConflictError(AgentError):
    """长期记忆发生乐观并发冲突。"""

    def __init__(self, memory_id: str, expected_version: int, actual_version: int) -> None:
        super().__init__(f"记忆 {memory_id} 版本冲突：期望 {expected_version}，实际 {actual_version}")
        self.memory_id = memory_id
        self.expected_version = expected_version
        self.actual_version = actual_version


class SerializationError(AgentError):
    """序列化或反序列化失败。"""


class UnknownSerializedTypeError(SerializationError):
    """序列化注册表中不存在指定类型。"""


class UnsupportedSchemaVersionError(SerializationError):
    """对象版本高于当前版本，或缺少必要迁移函数。"""
