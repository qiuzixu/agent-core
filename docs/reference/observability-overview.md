# Observability（可观测性）模块总览

> 位置：`agent-core/src/agent_core/observability/` · 对外出口：`agent_core.observability`

## 一句话定位

回答两个非常实际的问题：

1. **"我的 Agent 慢在哪？"** —— 每次模型调用、每次工具调用的耗时与成败，按会话累计成统计表。
2. **"我的依赖还活着吗？"** —— 把所有外部依赖（存储、网关、MCP、配置中心、任务队列）并发探测一次，给出三态结论。

## 整体链路

```
用户请求
   │
   ▼
ObservabilityMiddleware（开关：observability_enabled）
   │
   ├─ before_model ──► 记开始时间 + 开 LLM span（ContextVar 按异步任务隔离）
   ├─ after_model  ──► 算耗时 ──► agent_stats.record_llm_call()
   ├─ before_tool  ──► 开工具 span（tool.call.<名字>）
   ├─ after_tool   ──► 算耗时 ──► agent_stats.record_tool_call() + 收口 span
   └─ handle_exception ──► 只关失败 span，不决定重试（职责分离：只看不判）

AgentStats（进程内单例）
   · 轻量 LRU：dict 插入序 + 重新插入刷新，最多 1000 会话
   · 每会话明细截断 1000 条
   · guard_events：护栏拦截/脱敏计数（与 guardrails / injection_guard 的 _emit 同源）
   · sink：add_sink() 实时外发，单个 sink 异常只记日志
        │
        ▼
GET /observability/stats（vanilla 与 LangGraph 端结构对齐）

HealthRegistry（另一条独立线）
   ├─ session_store / cesium_gateway / geolocation_mcp（常驻）
   ├─ dynamic_config（启用 Apollo / 动态配置时自动纳入）
   └─ task_queue（task_queue_backend=rabbitmq 时自动纳入）
        │
        ▼
GET /health/deps ──► 并发探测（单项超时默认 3s）
   全过 = healthy │ 部分挂 = degraded │ 全挂 = unhealthy
```

## 核心设计点（面试要点）

### 1. ContextVar 并发隔离

一个 Agent 服务并发处理多会话，"开始时间"绝不能跨会话串线。
`threading.local` 对协程切换感知不正确；`ContextVar` 是 asyncio 官方方案，
每个异步任务有独立副本，天然隔离。

### 2. dict 手搓轻量 LRU

`next(iter(dict))` 取最老键删除；老会话访问时 `pop` 后重新插入刷新新鲜度。
容量上限防止长跑进程内存无限增长——不引入 OrderedDict / cachetools 依赖。

### 3. 三态健康检查

K8s liveness（`/health`）只回答"进程活着吗"；`/health/deps` 回答"依赖还通吗"。
**degraded 是关键设计**：数据库挂了但主对话还能跑时，报 degraded（该修但不用紧急重启），
而不是一刀切 unhealthy 触发重启。单项检查超时不影响其他项，且有防死循环兜底。

### 4. 零依赖哲学

`opentelemetry-sdk` 未安装时自动降级：span 变 None，统计退化为纯日志 + 内存表。
装上后 `setup_tracing()` 支持 console 导出（调试）或 OTLP gRPC 导出（Jaeger/Tempo）。

### 5. 只看不判

`handle_exception` 返回 False——观测中间件只负责关 span、记失败，
是否重试由 RetryMiddleware / 熔断器决定。观测层不掺和业务决策。

## 2026-10-29 升级记录

| 升级项 | 内容 | 状态 |
| --- | --- | --- |
| guard 计数接入统计 | `record_guard_event`；GuardrailsMiddleware / InjectionGuardMiddleware 的 `_emit` 自动登记，summary 输出 `guard_events` | ✅ 双端对齐 |
| 工具调用 OTel span | `before_tool` 开 `tool.call.<名字>` span，`after_tool` 收口并记录 latency / success | ✅ |
| vanilla 补 `/observability/stats` | 与 LangGraph 端输出结构对齐（threads → llm_calls / tool_calls / avg_latency_ms / total_latency_ms / guard_events） | ✅ |
| 健康检查覆盖面扩充 | 动态配置中心（Apollo/JSON 启用时）与 RabbitMQ 任务队列（`ping()`）自动纳入 `/health/deps` | ✅ |
| AgentStats 外发 sink | `add_sink()`：每条记录实时推给外部系统，sink 异常只记日志不影响主流程 | ✅（机制就绪，默认无 sink） |

## 已知边界

- AgentStats 是进程内存：重启清零、多实例不聚合。真实生产监控把 sink 指向
  Prometheus pushgateway / OTel collector 即可，机制已预留。
- LangGraph 端的分布式追踪走 LangSmith / LangChain callbacks
  （settings.langsmith_*），本地统计只做轻量口径（见 `observability/stats.py` docstring）。
- LangGraph 端工具调用无独立 span（工具执行在图节点内），链路追踪由 LangSmith 承担。

## 配置开关

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `observability_enabled`（vanilla）/ `core_observability_enabled`（LangGraph） | true | 是否启用调用观测 |
| `health_timeout_seconds` | - | /health/deps 单项检查超时 |
| `apollo_enabled` / `dynamic_config_enabled` | false | 启用后配置中心纳入健康体检 |
| `task_queue_backend` | memory | 设为 rabbitmq 后任务队列纳入健康体检 |
