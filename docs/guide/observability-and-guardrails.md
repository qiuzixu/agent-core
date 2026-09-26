# 可观测性与 Guardrails

## 运行事件

`RunContext.emit()` 产生 `RunEvent`。`AgentRuntime` 可通过 `EventSink` 把事件发送到 WebSocket、
消息队列或审计平台。内置 `MemoryEventSink` 适合测试。

事件负载可能包含用户输入、模型输出和工具参数。接入外部系统前应先定义脱敏和保留策略。

## 调用统计

`ObservabilityMiddleware` 记录模型和工具调用耗时、消息数、工具调用数与完成原因。
默认写入进程内 `agent_stats`：

```python
middleware = [ObservabilityMiddleware(thread_id="thread-1")]
print(agent_stats.summary())
```

生产指标应通过事件或自定义中间件导出到共享系统，因为进程内统计会在重启后丢失。

## OpenTelemetry

`setup_tracing()` 会按需导入 OpenTelemetry SDK，可输出到控制台或 OTLP gRPC 端点。
相关 SDK 当前不是包的默认依赖，应用需自行安装匹配版本。

## Guardrails

内置规则包括：

- `BlockedKeywordsGuard`：拦截指定关键词；
- `LengthGuard`：限制输入和输出字符数；
- `PIIRedactionGuard`：检测或脱敏常见手机号、身份证、邮箱和银行卡号；
- `OutputFormatGuard`：拦截几类常见脚本和 SQL 注入文本。

```python
guardrails = GuardrailsMiddleware(
    input_guards=[LengthGuard(max_input_chars=5000)],
    output_guards=[OutputFormatGuard()],
    pii_redact=True,
)
```

这些是通用基础规则，不是完整内容安全系统。正则无法可靠识别所有敏感信息，也不能替代业务
授权、工具参数白名单、模型供应商安全能力和人工审核。

## 错误分类

Core 将模型超时、限流、不可用、输出验证失败、工具执行、Checkpoint、配置和状态机错误分开。
上层 API 应按类型映射重试、用户提示和 HTTP 状态，避免把所有失败都重试。详情见
[异常体系](../reference/exceptions.md)。
