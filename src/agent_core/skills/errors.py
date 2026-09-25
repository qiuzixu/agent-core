"""Skill 加载、注册和绑定错误。"""

from agent_core.errors import ConfigurationError


class SkillError(ConfigurationError):
    """Skill 配置基础错误。"""


class SkillLoadError(SkillError):
    """Skill 清单或指令文件无法加载。"""


class SkillRegistrationError(SkillError):
    """Skill 名称或工具名称发生注册冲突。"""


class SkillBindingError(SkillError):
    """Skill 声明的 Function 或 MCP 客户端无法绑定。"""


__all__ = [
    "SkillBindingError",
    "SkillError",
    "SkillLoadError",
    "SkillRegistrationError",
]
