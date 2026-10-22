"""上下文压缩的数据结构与常量。

本模块只放"值对象"，不含任何算法与 IO，便于 region / summarizer /
middleware 三层共享，也便于单测直接构造。
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_core.protocol.messages import Message

# ──────────────────────────────────────────────
# 标签与来源标记
# ──────────────────────────────────────────────

# 摘要正文在 checkpoint 消息中的包裹标签；下一轮压缩时靠它识别"旧摘要"
SUMMARY_OPEN = "<compacted-summary>"
SUMMARY_CLOSE = "</compacted-summary>"

# 写入 Message.name，便于日志、前端展示与排查时识别自动生成的检查点
SUMMARY_SOURCE = "compaction_checkpoint"


@dataclass(frozen=True)
class CompactionConfig:
    """上下文压缩配置。

    Attributes:
        threshold_ratio: 窗口占用达到该比例即触发压缩。
        retain_ratio: 压缩后保留的尾部原文比例（占窗口，必须小于阈值）。
        summary_max_tokens: 摘要生成本身的输出上限。
        max_retries: 压完仍超阈值的额外压缩轮数（0 表示只压一轮）。
        max_overflow_retries: 模型报"上下文超长"时的强制压缩重试上限
            （0 表示不接管该错误，交给上层抛出）。
        enabled: 总开关，关闭后中间件直接放行。
        chars_per_token: 无精确计数时的字符/token 估算比例（用于尾部预算
            与"摘要是否更小"的判断）。
        reserved_output_tokens: 为本次模型输出预留的 token 数；0 表示自动
            从模型适配器的 max_tokens 读取（窗口按输入+输出共享）。
        estimate_when_unavailable: 精确计数不可用时是否回退到字符估算；
            关闭后只能依赖模型侧的"上下文超长"报错兜底。
    """

    threshold_ratio: float = 0.75
    retain_ratio: float = 0.20
    summary_max_tokens: int = 2048
    max_retries: int = 1
    max_overflow_retries: int = 1
    enabled: bool = True
    chars_per_token: int = 4
    reserved_output_tokens: int = 0
    """为模型输出预留的 token 数。

    窗口按“输入 + 输出”共享计算：请求里还要放本次生成的内容，
    因此可用输入上限是“窗口 - 预留输出”。设为 0 表示自动从模型
    适配器的 max_tokens 读取；读取不到则按 0 处理。
    """
    estimate_when_unavailable: bool = True
    """精确计数不可用时是否回退到字符估算。

    Provider 没有 tokenizer 接口（例如标准 OpenAI 兼容端点）时，
    context_usage 会返回 input_tokens=None。开启后按
    “字符数 / chars_per_token”粗估，保证主动压缩仍能触发；
    关闭则只能依赖模型侧的“上下文超长”报错兜底。
    """

    def __post_init__(self) -> None:
        if not 0 < self.threshold_ratio <= 1:
            raise ValueError(f"compaction threshold_ratio 必须在 (0, 1] 区间，当前 {self.threshold_ratio}")
        if not 0 < self.retain_ratio < self.threshold_ratio:
            raise ValueError(
                "compaction retain_ratio 必须大于 0 且小于 threshold_ratio，"
                f"当前 retain={self.retain_ratio}, threshold={self.threshold_ratio}"
            )
        if self.summary_max_tokens <= 0:
            raise ValueError("compaction summary_max_tokens 必须为正整数")
        if self.max_retries < 0:
            raise ValueError("compaction max_retries 不能为负数")
        if self.max_overflow_retries < 0:
            raise ValueError("compaction max_overflow_retries 不能为负数")
        if self.chars_per_token <= 0:
            raise ValueError("compaction chars_per_token 必须为正整数")
        if self.reserved_output_tokens < 0:
            raise ValueError("compaction reserved_output_tokens 不能为负数")


@dataclass
class CompactionResult:
    """一次压缩的产出。

    Attributes:
        compacted: 是否真的替换了消息（False 时 messages 为原列表）。
        messages: 压缩后的消息列表。
        before_tokens: 压缩前窗口占用（来自 Provider 的真实计数）。
        after_tokens: 压缩后窗口占用（按字符估算的缩减量推算）。
        cut: 被压成摘要的头部条数（压缩边界）。
        reason: 未压缩或失败原因，供日志与降级事件使用。
    """

    compacted: bool
    messages: list[Message]
    before_tokens: int = 0
    after_tokens: int = 0
    cut: int = 0
    reason: str = ""


class CompactionError(RuntimeError):
    """压缩过程中的可预期失败（摘要调用失败 / 结构不合规 / 未变小）。

    中间件捕获它后返回 CONTINUE，把消息交给后续的硬截断中间件兜底，
    因此不需要中断主流程。
    """
