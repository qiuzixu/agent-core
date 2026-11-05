# Agent Core 面试知识点与项目讲解

本文把 `zero-agent-core` 中值得在面试中展开的设计整理成可直接复述的知识点。
重点不是罗列功能，而是说明如何把不稳定的 LLM 调用封装成可恢复、可观测、可治理、支持多实例并发的 Agent Runtime。

## 一、项目的一分钟介绍

可以这样介绍：

> 我负责的是一个零外部平台绑定的 Python Agent Runtime Core。它通过 Protocol、Ports 和 Storage Adapter 解耦运行时与具体基础设施，支持 ReAct、Plan-Execute、Supervisor、Reflexion、Swarm 五种执行模式。项目重点解决了 Agent 在生产环境中的可靠性问题，包括工具幂等、Checkpoint 恢复、Worker Lease、Heartbeat、Fencing Token、事件 Outbox、超时、重试和多租户访问控制。核心目标不是让模型能回答，而是让 Agent 在并发、失败、重试和进程重启后仍然能够正确运行。

## 二、最值得掌握的知识点

### 1. 分层架构与依赖倒置

项目的核心分层如下：

| 层次 | 主要职责 |
| --- | --- |
| `protocol` | 定义 `RunContext`、`RunEvent`、`ToolResult` 等公共数据契约 |
| `ports` | 定义 `RunStore`、`EventSink`、`ToolOperationStore` 等可插拔接口 |
| `runtime` | 实现 Agent Loop、Run 生命周期和多 Agent 编排 |
| `storage` | 提供 Memory、SQLite、PostgreSQL 等默认实现 |
| `middleware` | 提供权限、审计、压缩、限流、超时和追踪等横切能力 |

面试表达：

> Core 不直接依赖数据库、HTTP 框架或具体模型厂商，而是依赖稳定的端口接口。这样可以替换存储后端，也方便用内存实现进行单元测试。

可能追问：为什么不直接在 Runtime 里写 SQL？

回答：直接写 SQL 会让执行引擎和基础设施耦合，难以测试、迁移和支持多种部署环境。接口与适配器分离后，业务执行语义保持稳定，后端差异由 Adapter 处理。

参考：[ARCHITECTURE.md](./ARCHITECTURE.md)

### 2. ReAct Loop 是一个状态机

ReAct 的基本执行过程是：

```text
用户输入
  -> 调用模型
  -> 判断是否产生 tool_calls
  -> 执行工具
  -> 将工具结果写回上下文
  -> 再次调用模型
  -> 得到最终答案
```

项目对循环设置了最大迭代次数、最大工具调用次数、超时和预算边界，并对空回答、模型失败和中断进行明确收口。

面试表达：

> Agent 不是一次模型调用，而是一个带外部副作用的状态机，因此必须有明确的状态迁移、终止条件和失败状态。

参考：[react.py](../src/agent_core/runtime/react.py)

### 3. `run()` 与 `stream()` 共享执行内核

普通调用和流式调用都复用 `_iterate()`：

- `_iterate()` 负责消息构造、Middleware、模型调用、工具执行和 Checkpoint；
- `run()` 只负责普通结果包装；
- `stream()` 负责文本增量转发；
- `_LoopMode` 只封装普通调用与流式调用的差异。

面试表达：

> 流式和非流式只是输出方式不同，执行语义应保持一致。共享内核可以避免两条路径长期演化后产生行为漂移。

这个设计还要考虑一个细节：流式文本一旦已经发送给客户端，就不能简单地把整个模型调用当作可重试操作，否则客户端会收到重复文本。

### 4. 工具系统是策略执行器

工具除了函数本身，还会声明：

- 参数 Schema；
- 风险等级；
- 是否需要审批；
- 是否幂等；
- 超时时间；
- 优先级和执行阶段。

执行器统一处理参数校验、并发限制、同步函数线程隔离、审批、超时、结构化错误和使用统计。

面试表达：

> Tool Registry 负责描述工具，Tool Executor 负责执行策略。这样可以把“能不能执行”和“如何执行”从业务函数中分离出来。

参考：[executor.py](../src/agent_core/tools/executor.py)

### 5. 幂等性与至少一次执行

非幂等工具必须提供稳定的 `operation_id`，并通过 `ToolOperationStore` 执行：

```text
claim(operation_id)
  -> 已完成：返回缓存结果
  -> 正在执行：返回可重试状态
  -> 当前 Worker 获得所有权：执行工具
save(operation_id, result)
```

面试中可以这样说：

> Agent 和任务队列通常只能保证至少一次执行，因此写操作不能只依赖重试。我们通过 operation_id 对副作用操作去重，并缓存成功结果。

如果被问到 exactly-once：

> Runtime 可以做到幂等提交和重复结果去重，但外部系统能否 exactly-once 还取决于下游是否支持幂等键、事务或去重机制。

参考：[outbox.py](../src/agent_core/storage/outbox.py)

### 6. Worker Lease、Heartbeat 与 Fencing Token

任务 Worker 的执行链路是：

```text
claim 任务
  -> 获得 lease 和 fencing_token
  -> 定期 heartbeat
  -> 租约过期后允许其他 Worker reclaim
  -> 旧 Worker 的完成、失败和重试写入被拒绝
```

Lease 解决的是“谁可以执行”，Fencing Token 解决的是“旧执行者还能不能写入”。

典型故障场景：

1. Worker A 获得任务；
2. A 因网络阻塞没有及时续租；
3. Worker B 接管任务并完成；
4. A 恢复后尝试提交旧结果。

如果只有 Lease，A 可能覆盖 B 的结果；如果每次认领都递增 fencing token，存储层就可以拒绝 A 的迟到写入。项目在租约丢失后还会取消当前 Handler，减少无效执行。

参考：[worker.py](../src/agent_core/tasks/worker.py)

### 7. Checkpoint、恢复与时间旅行

Agent 每轮模型和工具调用都会产生状态变化。项目支持：

- Run 状态持久化；
- 每轮 Checkpoint；
- 中断后恢复；
- 历史版本查看；
- 全局事件序号；
- 事件历史裁剪。

面试表达：

> 长流程 Agent 不能把所有状态只放在内存里，否则进程重启、网络断开或人工暂停后都无法继续执行。Checkpoint 是 Agent 可恢复性的基础。

### 8. EventSink、Callback 与 Outbox 的边界

三者职责不同：

- `Callback`：进程内观察生命周期；
- `EventSink`：向外部传递运行事件；
- `EventOutbox`：暂存待投递事件；
- `RetryingEventSink`：处理临时投递失败。

面试表达：

> Callback 只观察，不应该修改 Agent 的执行流程；Outbox 用来把事件投递从主执行链中隔离出来，避免外部通知失败拖垮 Agent。

事件通常是 at-least-once 投递，所以消费者仍需要根据事件 ID 或序号做幂等处理。

### 9. 五种 Agent 执行模式

当前模式定义在 [modes.py](../src/agent_core/runtime/modes.py)：

| 模式 | 适用场景 | 主要特点 |
| --- | --- | --- |
| ReAct | 普通问答、工具调用 | 边思考边调用工具 |
| Plan-Execute | 长流程、多步骤任务 | 先生成计划，再按依赖执行 |
| Supervisor | 多 Agent 路由 | 中央控制器负责选择子 Agent |
| Reflexion | 质量敏感任务 | 生成、评审、修订 |
| Swarm | Agent 之间移交 | 基于白名单进行 handoff |

这些模式通过统一的 `SupportsRun` 和 `AgentOutput` 组合，因此一个模式可以嵌套另一个模式。

面试中不要只说“支持五种模式”，还要说明选择依据：任务是否需要全局规划、是否需要集中路由、是否需要自我批评，以及 Agent 之间是否需要自治移交。

### 10. Plan-Execute 与 DAG 并发

Plan-Execute 使用结构化输出生成计划，然后校验：

- 步骤 ID 是否重复；
- 依赖是否存在；
- 指令是否为空；
- 依赖是否可以满足；
- 是否超过最大步骤数。

执行阶段会找到所有依赖已完成的步骤，按 `max_parallel_steps` 批量并发执行，并把上一步结果限制在最大上下文长度内。

面试表达：

> Plan-Execute 的核心不是简单地把步骤串起来，而是把计划建模成依赖图，根据拓扑可执行性进行并发调度。

代价是规划本身增加了模型调用和延迟，而且计划可能过时。复杂场景需要增加重新规划或动态修订机制。

参考：[plan_execute.py](../src/agent_core/runtime/plan_execute.py)

### 11. Middleware 与横切能力

Middleware 在以下阶段介入：

- `before_model`；
- `after_model`；
- `before_tool`；
- `after_tool`。

可以实现权限校验、Prompt Injection 防护、审计、上下文压缩、超时预算、工具结果校验、缓存和 Tracing。

中间件通过 `CONTINUE`、`MODIFY`、`STOP`、`RETRY` 表达控制结果。

面试表达：

> Agent Loop 只负责执行语义，安全、审计和治理属于横切能力，应该通过 Middleware 插入，而不是堆进主循环。

### 12. 上下文压缩与成本控制

项目使用最大消息历史、字符上限、Token 估算、摘要加尾部保留、工具结果裁剪等策略，防止上下文无限增长。

面试追问：压缩会不会丢信息？

回答：

> 会，所以结构化状态不能只放在自然语言上下文中。关键状态应放到 Memory、Checkpoint 或结构化协议里；上下文压缩主要保留任务摘要和最近交互。

### 13. 模型治理与结构化输出

模型调用还需要统一治理：

- 超时；
- 重试；
- 并发限制；
- 熔断；
- 降级；
- JSON Schema 校验；
- 结构化输出失败后的修复重试。

Plan-Execute 的计划、Reflexion 的评审结果、Swarm 的 handoff 决策都适合采用结构化输出，而不是依赖脆弱的文本解析。

### 14. 安全与多租户隔离

`AccessContext` 用于约束用户、租户、角色和资源范围，并贯穿 Session、Memory、Approval 和检索存储。

项目还强调：

- 不使用不受信任的 `pickle` 恢复对象；
- 通过类型白名单进行序列化恢复；
- 工具执行前做权限与审批判断；
- 业务层负责业务权限，Core 负责通用访问边界。

面试表达：

> Agent 的安全边界不能只看模型输出，还要验证当前用户是否有权限执行对应工具和访问对应上下文。

## 三、面试高频追问与短答案

### 为什么需要 Fencing Token？

Lease 过期后旧 Worker 可能恢复并迟到写入。Fencing Token 让存储层识别新旧执行者，拒绝旧 Worker 的结果提交。

### 为什么不把所有逻辑写进 ReAct？

这样会导致权限、审计、压缩、重试和业务执行强耦合。通过 Middleware 和 Ports 拆分，可以独立测试和替换。

### 为什么工具要区分幂等和非幂等？

因为失败重试可能重复产生外部副作用。幂等工具可以直接重试，非幂等工具必须有稳定的 operation_id 和结果去重。

### Plan-Execute 一定比 ReAct 好吗？

不一定。Plan-Execute 适合依赖清晰的复杂任务，ReAct 更适合动态探索。前者规划成本更高，也可能出现计划过时。

### Callback 和 Middleware 有什么区别？

Callback 主要观察事件；Middleware 可以修改输入、阻止执行或要求重试。前者是观察者，后者是执行链的一部分。

### Agent 能做到 exactly-once 吗？

运行时可以通过幂等键、操作记录和 fencing token 降低重复副作用，但外部系统是否 exactly-once 取决于下游协议和存储能力。

### 如何验证可靠性设计？

重点测试租约丢失、旧 Worker 写入、重复 operation_id、工具超时、事件投递失败、Checkpoint 恢复和并发边界，而不是只测试模型正常返回。

## 四、项目验证事实

最近一次可靠性升级后的验证结果：

- 定向可靠性测试：`77 passed`；
- 全量测试：`479 passed`、`23 skipped`；
- 另有 `2` 个既有 FAISS 测试失败，与本轮可靠性升级无关；
- `python -m compileall` 通过。

面试时建议如实说明失败范围和归因，不要简单表述为“全部测试通过”。

## 五、准备优先级

如果面试时间有限，优先准备以下五个主题：

1. ReAct Loop 与状态机；
2. 工具幂等和至少一次执行；
3. Worker Lease 与 Fencing Token；
4. Ports and Adapters 分层架构；
5. Plan-Execute DAG 与多 Agent 编排。

这五个主题能够覆盖 Agent 原理、工程架构、分布式一致性、可靠性和系统设计，最能体现项目深度。
