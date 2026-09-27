# 存储与生产部署

Core 对会话、上下文、跨会话长期记忆、Run/审批、租约、工作流和模型选择提供三类实现。

| 环境参数 | 实现 | 使用场景 |
| --- | --- | --- |
| `test` / `testing` | Memory | 测试、临时脚本 |
| 其他值 | SQLite | 单机开发 |
| `production` / `prod` | PostgreSQL | 多进程、生产服务 |

## 统一创建

```python
stores = {
    "sessions": create_session_store(
        env,
        sqlite_path="./agent.db",
        postgres_url=database_url,
        require_access=True,
    ),
    "contexts": create_context_store(
        env,
        sqlite_path="./agent.db",
        postgres_url=database_url,
        require_access=True,
    ),
    "memory": create_memory_store(
        env,
        sqlite_path="./agent.db",
        postgres_url=database_url,
        require_access=True,
    ),
    "runs": create_runtime_store(
        env,
        sqlite_path="./agent.db",
        postgres_url=database_url,
    ),
    "leases": create_run_lease_store(
        env,
        sqlite_path="./agent.db",
        postgres_url=database_url,
    ),
}
```

生产环境缺少 `postgres_url` 时工厂会立即报错，不会静默降级到 SQLite。

向量数据使用独立工厂：测试为 `InMemoryVectorStore`，开发为 `ChromaVectorStore`，生产为
`PgVectorStore`。它不复用 SQLite Store，因为普通 SQLite 不具备向量距离索引；开发环境需要
完整的文档、metadata 和本地持久化语义，因此使用 Chroma，而不是只提供索引算法的 FAISS。

## PostgreSQL 生命周期

安装 `handwritten-agent-core[production,pgvector]`。每个 PostgreSQL Store 需要在服务启动时调用
`initialize()` 创建连接池并幂等建表，在关闭时调用 `close()`。应用可以集中管理这些生命周期，
不要每次请求创建连接池。

## 并发和幂等

- Runtime Store 使用版本/条件更新检测并发冲突；
- Run Lease Store 用认领、心跳、释放和过期时间保证同一 Run 的 Worker 互斥；
- `idempotency_key` 用于重复 API 请求命中同一 Run；
- Context Store 使用版本检测更新冲突；
- Memory Store 使用 `expected_version` 检测并发覆盖；
- Workflow Store 在节点执行前保存 pending 事件，完成后保存状态和下一节点；
- 工作流节点可把 `current_node_execution().idempotency_key` 传给外部写操作。

这些机制只覆盖 Core 数据。调用第三方 API 的业务工具仍需自己的幂等键和事务策略。

## 身份与租户隔离

应用完成认证后构造：

```python
access = AccessContext(
    user_id="user-1",
    tenant_id="tenant-1",
    roles=frozenset({"user"}),
)
```

Session、Context、Memory、Approval、Runtime 和 Durable Workflow 的访问路径应传入同一身份。
新生产项目建议启用所有可用的 `require_access=True`。Core 的访问检查是纵深防御，数据库账号、
网络策略和应用鉴权仍需在部署层完成。

## 上线检查

- 所有实例使用同一个 PostgreSQL 和一致的模型/工具配置；
- 设置服务优雅关闭，停止接收请求后释放资源；
- Worker 心跳和租约时间覆盖预期的模型最长响应时间；
- 敏感字段进入日志、事件和 checkpoint 前完成脱敏；
- 定义会话、审批、Run、上下文和长期记忆的保留与清理周期；
- 备份数据库，并实际演练恢复；
- 监控模型失败率、工具耗时、审批积压和过期租约数量。
