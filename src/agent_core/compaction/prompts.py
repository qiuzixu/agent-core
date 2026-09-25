"""通用上下文摘要模板。

应用可以把自己的领域模板注入 ``CompactionMiddleware``，例如低空 Agent 的
飞行器、航线、审批章节。Core 的默认模板只要求保留任务、事实、决策、状态和待办。
"""

from __future__ import annotations

DEFAULT_SUMMARY_SECTIONS: tuple[str, ...] = (
    "任务目标",
    "关键事实与实体",
    "已执行动作及结果",
    "决策与约束",
    "当前状态",
    "待办与下一步",
)

DEFAULT_COMPACTION_INSTRUCTION = "\n".join(
    [
        "请把上面的早期对话压缩成结构化检查点，使用中文，输出且仅输出下列小节。",
        "",
        "要求：",
        "- 用精炼要点记录事实、用户约束、工具结果和未完成事项。",
        "- 保留精确编号、时间、数值、标识符和用户纠正，不要编造。",
        "- 空小节写 (none)，但一节都不能少。",
        "- 不要提及压缩过程。",
        "",
        *(f"## {section}" for section in DEFAULT_SUMMARY_SECTIONS),
    ]
)

__all__ = ["DEFAULT_COMPACTION_INSTRUCTION", "DEFAULT_SUMMARY_SECTIONS"]
