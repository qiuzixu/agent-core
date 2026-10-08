"""多 Agent 编排扩展错误。"""

from agent_core import AgentError


class MultiAgentError(AgentError):
    """多 Agent 扩展基础错误。"""


class AgentRegistrationError(MultiAgentError):
    """Agent 注册信息无效或发生名称冲突。"""


class AgentNotFoundError(MultiAgentError):
    """注册表中不存在目标 Agent。"""


class AgentAccessDeniedError(MultiAgentError):
    """当前访问身份无权调用目标 Agent。"""


class AgentRoutingError(MultiAgentError):
    """无法为任务选择唯一且可访问的 Agent。"""


class AgentInvocationError(MultiAgentError):
    """子 Agent 调用失败或返回了无效结果。"""


class CoordinationBudgetExceededError(MultiAgentError):
    """协调实例超过 handoff、调用次数、访问次数或使用量预算。"""


class CoordinationBusyError(MultiAgentError):
    """协调实例已经由其他 Worker 持有。"""


class CoordinationLeaseLostError(MultiAgentError):
    """执行过程中失去协调实例租约。"""
