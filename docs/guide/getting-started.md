# 快速开始

本页用一个离线模型运行最小 Agent，不需要 API Key 或外部服务。完成后再替换为真实模型。

## 环境要求

- Python `3.13.x`
- Git
- Node.js 仅在构建文档站时需要

## 安装

在 `agent-core` 目录执行：

```bash
python -m venv .venv
```

Windows PowerShell：

```powershell
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Linux 或 macOS：

```bash
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

也可以使用 `uv sync --extra dev`。

## 运行最小示例

```bash
python examples/basic_agent.py
```

示例完成四件事：

1. 实现最小 `ModelAdapter`；
2. 向 `ToolRegistry` 注册 Python 函数；
3. 用 `ToolExecutor` 和 `ReActAgent` 组装 Agent；
4. 模型先返回工具调用，再基于工具结果返回最终答案。

核心组装代码如下：

```python
registry = ToolRegistry()
registry.register(
    "add",
    add,
    "计算两个整数之和",
    parameters={
        "type": "object",
        "properties": {
            "a": {"type": "integer"},
            "b": {"type": "integer"},
        },
        "required": ["a", "b"],
    },
)

agent = ReActAgent(
    llm=DemoModel(),
    tool_executor=ToolExecutor(registry),
    system_prompt="你是一个计算助手。",
    tool_definitions=registry.build_tool_definitions(),
)
answer = await agent.run("计算 20 + 22")
```

## 换成真实模型

先安装对应 extra：

```bash
pip install "handwritten-agent-core[openai]"
```

应用配置可以是 dataclass、Pydantic Settings 或其他有同名属性的对象：

```python
from dataclasses import dataclass

from agent_core import create_model_provider


@dataclass
class Settings:
    llm_provider: str = "openai"
    model_name: str = "gpt-4o-mini"
    openai_api_key: str = "从安全配置读取"
    openai_base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.0
    max_tokens: int = 4096


model = create_model_provider(Settings())
```

不要把 API Key 写入源码或提交到仓库。环境变量如何映射成配置对象由应用负责。

## 接入应用 Runtime

`ReActAgent` 负责一次 Agent Loop；需要运行记录、事件、取消、恢复、租约和访问隔离时，
再包装 `AgentRuntime`：

```python
from agent_core import AgentRuntime, create_run_lease_store, create_runtime_store

runtime = AgentRuntime(
    agent,
    run_store=create_runtime_store("development", sqlite_path="./agent.db"),
    lease_store=create_run_lease_store("development", sqlite_path="./agent.db"),
)
context = await runtime.run("thread-1", "计算 20 + 22")
print(context.status, context.events[-1].payload.get("answer"))
```

生产环境和多租户设置请继续阅读[存储与生产部署](./storage-and-production.md)。
