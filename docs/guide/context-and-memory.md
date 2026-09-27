# 上下文、会话与记忆

Agent 的“上下文”不是单一对象。把不同生命周期的数据分开保存，可以避免消息历史、长期记忆
和一次运行状态互相污染。

## 会话历史

`SessionStore` 保存按 `thread_id` 组织的 `Message`。`SessionManager` 提供读取、追加和清理会话
的上层入口。开发环境可用 SQLite，生产使用 PostgreSQL。

## 长期上下文

`ContextStore` 保存线程范围的键值数据，并支持：

- 用户和租户归属；
- 版本号与乐观并发检查；
- TTL 与过期清理；
- 字段来源；
- 更新时的冲突检测。

这些字段让应用能够判断记忆来自用户、工具还是系统，以及当前更新是否覆盖了其他进程刚写入
的版本。业务仍需决定哪些内容值得长期保存，Core 不自动把所有对话转成记忆。

`ContextStore` 的键值属于一个 Thread，适合保存当前会话持续使用的结构化字段。跨 Thread 的用户
偏好、事实和历史经验使用独立的 `MemoryStore`。

## 跨会话长期记忆

```python
from agent_core import AccessContext, MemoryRecord, create_memory_store

access = AccessContext(user_id="u-1", tenant_id="t-1")
memory = create_memory_store(
    "development",
    sqlite_path="./agent.db",
    require_access=True,
)

record = await memory.put(
    MemoryRecord(
        namespace="preferences",
        content="用户偏好中文回答",
        source="conversation",
        metadata={"kind": "preference"},
    ),
    access=access,
)
results = await memory.search(
    "preferences",
    query="中文",
    metadata={"kind": "preference"},
    access=access,
)
```

`MemoryRecord` 包含 namespace、来源、metadata、TTL、版本以及用户/租户归属。`put()` 和
`delete()` 可传 `expected_version` 做乐观并发检查。内存实现用于测试，SQLite 用于本地开发，
PostgreSQL 用于生产；PostgreSQL 实例需要先调用 `initialize()`，退出前调用 `close()`。

内置 Memory 检索是轻量关键词匹配，适合偏好和结构化事实。外部知识文档使用独立的
`Retriever`、`Embeddings` 和 `VectorStore` 协议；需要把长期记忆加入统一检索时，可以实现
`MemoryRetriever` 适配器，并保留相同的访问隔离、TTL 和版本语义。

## 上下文压缩

`CompactionMiddleware` 在模型调用前读取 `context_usage()`。达到窗口阈值时，它会：

1. 瘦身过长工具结果，必要时写入 `SpillStore`；
2. 选择不破坏 assistant/tool 调用配对的切分点；
3. 把旧消息压成结构化摘要 checkpoint；
4. 保留近期原始消息并继续 Agent Loop；
5. 模型真实返回上下文超长时，强制压缩后有限次重试。

```python
spill = SpillStore()
compaction = CompactionMiddleware(
    model,
    config=CompactionConfig(
        threshold_ratio=0.75,
        retain_ratio=0.20,
        summary_max_tokens=2048,
    ),
    tool_definitions=tool_definitions,
    spill_store=spill,
)

agent = ReActAgent(
    ...,
    middleware=[compaction, TokenLimitMiddleware(max_tokens=120_000)],
)
```

推荐把 `TokenLimitMiddleware` 放在压缩中间件之后，作为摘要失败或计数不可用时的硬截断兜底。

## Spill

Spill 将超长工具结果放入有容量限制的进程内存储，并在消息中保留引用。调用
`build_spill_tool(spill)` 可以给模型提供按 ID 读取原文的工具。当前 `SpillStore` 是进程内实现，
需要跨进程恢复时，应用应实现持久化替代方案，并制定内容过期和敏感数据策略。

## 记忆策略建议

- 当前 Thread 使用的结构化字段写入 Context，跨会话稳定偏好写入 Memory；
- 工具原始结果优先保留来源和时间，不直接视为事实永久保存；
- 临时推理、模型中间文本和可重新计算数据留在 Run 或 Checkpoint；
- 多租户应用始终启用 `require_access=True`；
- 对记忆写入增加业务级允许字段和审计，而不是允许模型写任意键。
