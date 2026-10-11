"""zero-agent-core 单文件最小完整示例（离线可运行，无需 API Key）。

本示例演示一个完整 Agent 应用的四个核心组装件：

1. 工具注册（含一个 ``requires_approval`` 写工具，演示审批回调）；
2. 中间件挂载（``LoggingMiddleware``）；
3. Checkpointer（``FileCheckpointer``，跨进程可恢复）与 ``run`` / ``stream`` 两种调用；
4. 全程离线：使用仿照 ``examples/basic_agent.py`` 的 ``DemoModel``，不访问任何网络。

换真实模型时改哪里？——见文件末尾 ``build_llm()`` 的注释：
只需把 ``DemoModel()`` 换成 ``create_model_provider(config)``，其余代码零改动。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from agent_core import (
    ContextUsage,
    FileCheckpointer,
    LoggingMiddleware,
    Message,
    ReActAgent,
    StreamChunk,
    ToolExecutor,
    ToolRegistry,
    ToolSpec,
    assistant_message,
)

# 中间件日志走 logging，打开 INFO 级别即可在控制台看到 [Log] 前缀的轨迹。
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# Checkpointer 存储目录：每个 thread 一个 JSON 文件，进程重启后可从中恢复。
# 可用环境变量 AGENT_STARTER_CHECKPOINT_DIR 覆盖（如无写权限的只读部署场景）。
CHECKPOINT_DIR = Path(
    os.environ.get("AGENT_STARTER_CHECKPOINT_DIR", Path(__file__).parent / ".starter_checkpoints")
)


# ──────────────────────────────────────────────
# 1. 离线 DemoModel：不需要 API Key 的"假模型"
# ──────────────────────────────────────────────
class DemoModel:
    """按脚本演出的模型：第一轮挑一个工具调用，第二轮根据工具结果作答。

    它实现的是 ``ModelAdapter`` 的最小协议（chat / stream_chat / context_usage），
    与真实适配器（OpenAIAdapter 等）在 ``ReActAgent`` 眼中完全等价。
    """

    async def chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Message:
        tool_result = next((m for m in reversed(messages) if m.role == "tool"), None)
        if tool_result is None:
            # 第一轮：根据用户输入选一个工具调用（真实模型在这里自主决策）。
            user_text = next(
                (m.content for m in reversed(messages) if m.role == "user"), ""
            )
            if "记录" in user_text or "笔记" in user_text:
                return assistant_message(
                    "",
                    tool_calls=[
                        {
                            "id": "call-save-note-1",
                            "name": "save_note",
                            "args": {"title": "示例笔记", "content": user_text},
                        }
                    ],
                )
            return assistant_message(
                "",
                tool_calls=[
                    {
                        "id": "call-add-1",
                        "name": "add",
                        "args": {"a": 20, "b": 22},
                    }
                ],
            )
        # 第二轮：拿到工具结果后给出最终回答。
        return assistant_message(f"计算结果是 {tool_result.content}。")

    async def stream_chat(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # 流式只是把 chat 的结果切成增量：真实模型会在这里逐 token yield。
        response = await self.chat(messages, tools=tools, temperature=temperature, max_tokens=max_tokens)
        if response.tool_calls:
            yield StreamChunk(
                tool_calls=response.tool_calls,
                is_tool_call=True,
                finish_reason="tool_calls",
            )
        if response.content:
            for piece in (response.content[i : i + 4] for i in range(0, len(response.content), 4)):
                yield StreamChunk(text=piece, finish_reason=None)
        if not response.tool_calls:
            yield StreamChunk(text="", finish_reason="stop")

    async def context_usage(
        self,
        messages: list[Message],
        *,
        tools: list[dict] | None = None,
    ) -> ContextUsage:
        return ContextUsage(
            input_tokens=sum(len(m.content) for m in messages),
            context_window_tokens=4096,
            exact=False,
            source="demo",
        )


# ──────────────────────────────────────────────
# 2. 示例工具：2 个只读 + 1 个需要审批的写工具
# ──────────────────────────────────────────────
def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b


def lookup_city(name: str) -> str:
    """返回示例城市信息（演示普通只读工具）。"""
    return {"北京": "中国首都，人口约 2100 万", "上海": "中国经济中心，人口约 2400 万"}.get(
        name, f"未收录城市：{name}"
    )


def save_note(title: str, content: str) -> str:
    """模拟写操作（演示 requires_approval 工具）。真实场景这里会落库/发请求。"""
    return f"已保存笔记《{title}》：{content[:40]}"


def build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    # 普通只读工具：risk_level 默认 "read"，无需审批。
    registry.register(
        "add",
        add,
        "计算两个整数之和",
        parameters={
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    registry.register(
        "lookup_city",
        lookup_city,
        "查询示例城市简介",
        parameters={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    )
    # 写工具：requires_approval=True 后，每次调用都会先走 ToolExecutor 注入的审批回调。
    registry.register(
        "save_note",
        save_note,
        "保存一条笔记（写操作，需要人工审批）",
        parameters={
            "type": "object",
            "properties": {"title": {"type": "string"}, "content": {"type": "string"}},
            "required": ["title", "content"],
        },
        risk_level="write",
        requires_approval=True,
        idempotent=False,
    )
    return registry


# ──────────────────────────────────────────────
# 3. 审批回调：ToolExecutor 在执行 requires_approval 工具前调用它
# ──────────────────────────────────────────────
async def console_approval_callback(spec: ToolSpec, arguments: dict) -> bool:
    """演示用审批回调：真实应用在这里弹 UI / 写审批表 / 等人工确认。"""
    print(f"\n[审批请求] 工具 {spec.name}（risk={spec.risk_level}）参数：{arguments}")
    approved = True  # 演示自动批准；生产中应接入人工确认（HITL）流程。
    print(f"[审批结果] {'批准' if approved else '拒绝'}")
    return approved


# ──────────────────────────────────────────────
# 4. 组装 Agent：middleware + checkpointer 一次挂好
# ──────────────────────────────────────────────
def build_agent() -> ReActAgent:
    registry = build_registry()
    return ReActAgent(
        llm=DemoModel(),  # ← 换真实模型时只改这一处（见 build_llm 注释）
        tool_executor=ToolExecutor(registry, approval_callback=console_approval_callback),
        system_prompt="你是一个演示助手。",
        tool_definitions=registry.build_tool_definitions(),
        middleware=[LoggingMiddleware()],  # 中间件挂载：记录每轮 LLM/工具调用
        checkpointer=FileCheckpointer(CHECKPOINT_DIR),  # 会话状态持久化到文件
        thread_id="starter-demo",
    )


def build_llm_real_model_placeholder() -> None:
    """换真实模型时改哪里？

    ① 安装对应 extras（PyPI 包名 zero-agent-core，导入名 agent_core 不变）::

        pip install "zero-agent-core[openai]"      # OpenAI / 兼容 API
        pip install "zero-agent-core[models,mcp]"  # 一次装齐各家 SDK + MCP

    ② 用一个配置对象描述模型，然后 ``create_model_provider`` 会按
       ``config.llm_provider`` 选择适配器并校验密钥::

        from types import SimpleNamespace
        from agent_core import create_model_provider

        config = SimpleNamespace(
            llm_provider="openai",          # openai / anthropic / gemini / ollama
            openai_api_key="sk-...",        # 密钥由对应 Provider 工厂校验
            openai_base_url="https://api.openai.com/v1",
            model_name="gpt-4o-mini",
            temperature=0.0,
            max_tokens=4096,
        )
        llm = create_model_provider(config)

    ③ 把 ``build_agent()`` 里的 ``DemoModel()`` 换成上面得到的 ``llm`` 即可，
       工具、中间件、checkpointer、run/stream 全部保持不变。
    """


# ──────────────────────────────────────────────
# 5. 两种调用方式：run（一次拿全量）与 stream（逐块增量）
# ──────────────────────────────────────────────
async def demo_run(agent: ReActAgent) -> None:
    print("=" * 60)
    print("① agent.run()：阻塞式执行，返回最终回答")
    answer = await agent.run("帮我计算 20 + 22", thread_id="starter-run")
    print(f"最终回答：{answer}\n")


async def demo_stream(agent: ReActAgent) -> None:
    print("=" * 60)
    print("② agent.stream()：流式执行，逐块打印增量")
    print("增量输出：", end="", flush=True)
    async for delta in agent.stream("帮我计算 20 + 22", thread_id="starter-stream"):
        print(delta, end="", flush=True)
    print("\n")


async def demo_approval(agent: ReActAgent) -> None:
    print("=" * 60)
    print("③ 写工具审批：save_note 标记 requires_approval，执行前先走审批回调")
    answer = await agent.run("请记录一条笔记：明天上午十点评审", thread_id="starter-approval")
    print(f"最终回答：{answer}\n")


async def main() -> None:
    agent = build_agent()
    await demo_run(agent)
    await demo_stream(agent)
    await demo_approval(agent)
    print("=" * 60)
    print(f"checkpoint 已写入 {CHECKPOINT_DIR}（thread 级 JSON 文件，可用于恢复）")


if __name__ == "__main__":
    asyncio.run(main())
