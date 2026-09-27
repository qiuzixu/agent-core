# 异常体系

所有框架级异常以 `AgentError` 为根。应用应按是否可重试、是否需要用户操作和是否属于配置错误
分类处理，不要对所有异常做无上限重试。

| 异常 | 含义 | 常见处理 |
| --- | --- | --- |
| `ConfigurationError` | 配置缺失或不合法 | 启动失败，修正配置 |
| `ModelInvocationError` | 模型调用失败 | 查看 cause，有限重试或降级 |
| `ModelTimeoutError` | 模型超时 | 有限重试、切换模型 |
| `ModelRateLimitError` | 模型限流 | 指数退避、配额告警 |
| `ModelUnavailableError` | 模型服务不可用 | 熔断或切换 Provider |
| `ModelOutputValidationError` | 最终输出、轮数或工具调用不合规 | 记录 Run 失败，调整提示或上限 |
| `ToolExecutionError` | 工具执行失败 | 根据工具幂等性决定重试 |
| `CheckpointError` | 快照读写失败 | 阻止不可靠恢复并告警 |
| `MiddlewareError` | 中间件链失败 | 检查自定义策略和返回动作 |
| `DocumentError` | 文档加载或解析失败 | 检查文件、服务配置和响应 |
| `DocumentParsingError` | 文件或解析响应无法转换 | 拒绝当前文档并记录来源 |
| `DocumentServiceError` | 外部文档解析服务失败 | 有限重试或检查 MinerU 服务 |
| `MultiAgentError` | 多 Agent 扩展基础错误 | 检查路由、注册、访问或预算配置 |
| `AgentRoutingError` | 没有唯一且可访问的目标 Agent | 指定目标/能力或配置默认/模型路由 |
| `CoordinationBudgetExceededError` | handoff、调用、访问或使用量超限 | 调整编排逻辑或显式提高预算 |
| `CoordinationBusyError` | 协调实例已被其他 Worker 持有 | 等待当前租约释放或过期后恢复 |
| `CoordinationLeaseLostError` | 执行中失去 Worker 租约 | 中断副作用并从最新快照恢复 |
| `StateMachineError` | 节点、路由或最大步数错误 | 修正工作流定义或节点实现 |
| `MemoryConflictError` | 长期记忆版本已被其他写入者修改 | 重新读取记录并合并或重试 |
| `SerializationError` | 信封、JSON 或对象编码不合法 | 拒绝输入并检查注册 Codec |
| `UnknownSerializedTypeError` | 输入类型没有进入白名单 | 显式注册可信类型，不动态导入 |
| `UnsupportedSchemaVersionError` | 版本过新或缺少迁移函数 | 升级 Core 或补齐逐版本迁移 |

## MCP 异常

`McpError` 派生出：

- `McpConnectionError`：Server 启动或连接失败；
- `McpTimeoutError`：列表或工具调用超时；
- `McpToolError`：远端工具返回错误。

## Skill 异常

- `SkillLoadError`：清单、指令路径或 schema 不合法；
- `SkillRegistrationError`：重名或注册冲突；
- `SkillBindingError`：激活时找不到 Function 或 MCP 客户端。

这些异常通常是部署配置错误，应在启动或加载阶段暴露，不应等到用户请求时静默忽略。

## 并发与权限

`RuntimeConcurrencyError` 表示 Run 的版本或状态已被其他执行者修改。重新加载最新 Run 后决定
是否继续，不要直接覆盖。缺少身份或跨租户访问会抛出 `PermissionError`，API 层应映射为适当的
认证或授权响应，并避免泄露目标记录是否存在。

## 保留根因

Provider 和 Runtime 会使用异常链保留底层错误。记录日志时保留完整 traceback，但返回客户端时
只暴露稳定错误码和经过脱敏的说明。
