# 序列化概览

`agent_core.serialization` 提供安全、显式、版本化的 Core 对象 JSON 序列化。

## 设计原则

### 为什么不用 pickle

- pickle 反序列化时会执行任意 Python 代码，恶意 payload 可触发 RCE；
- pickle 与 Python 版本 / 类定义强耦合，跨服务、跨语言、长期归档不可靠；
- JSON 是唯一既人可读、又能被数据库 / Redis / HTTP 通道直接承载的格式。

### 白名单原则

反序列化**不会**根据输入中的模块路径或类路径动态导入。只有显式注册进
`SerializerRegistry` 的类型才能被恢复；未注册的 `type` 直接抛
`UnknownSerializedTypeError`。

## SerializedEnvelope 结构

```json
{
  "type": "agent_core.graph_entity",
  "schema_version": 1,
  "payload": { "entity_id": "e1", "...": "..." }
}
```

- `type`：全局唯一的注册类型 ID，格式约定 `agent_core.<对象名>`；
- `schema_version`：**序列化格式版本**，表示 payload 字段结构的演进版本；
- `payload`：对象字段的 JSON 字典。

### envelope schema vs protocol schema

| 版本号 | 含义 | 归属 |
| --- | --- | --- |
| 信封 `schema_version` | payload 字段结构的序列化格式版本 | `SerializerRegistry` 负责迁移 |
| `RunEvent` / `RunContext` 内部 `schema_version` | 运行协议对象的业务版本 | 属于 payload 内容的一部分 |

两者互不影响：协议对象内部版本升级到 2 时，信封格式仍可保持 1。

## 当前默认注册对象

| type_id | Python 类型 |
| --- | --- |
| `agent_core.blob` | `Blob`（含 sha256 checksum 校验） |
| `agent_core.document_locator` | `DocumentLocator` |
| `agent_core.document` | `Document` |
| `agent_core.document_chunk` | `DocumentChunk` |
| `agent_core.message` | `Message` |
| `agent_core.run_event` | `RunEvent` |
| `agent_core.run_context` | `RunContext` |
| `agent_core.approval_record` | `ApprovalRecord` |
| `agent_core.tool_result` | `ToolResult` |
| `agent_core.access_context` | `AccessContext`（严格校验 user_id / tenant_id / roles） |
| `agent_core.graph_entity` | `GraphEntity` |
| `agent_core.graph_relation` | `GraphRelation` |
| `agent_core.graph_subgraph` | `GraphSubgraph` |
| `agent_core.model_selection` | `ModelSelection` |
| `agent_core.alert_payload` | `AlertPayload` |
| `agent_core.dead_letter_record` | `DeadLetterRecord` |

## 版本迁移规则

注册时通过 `migrations={n: fn}` 声明"从版本 n 升级到 n+1"的迁移函数：

```python
registry.register(
    "agent_core.example",
    Example,
    schema_version=2,
    encoder=Example.to_dict,
    decoder=Example.from_dict,
    migrations={
        1: lambda payload: {**payload, "enabled": True},  # v1 → v2
    },
)
```

- 加载时自动按链式执行迁移，直到达到当前 `schema_version`；
- 缺少对应迁移时抛 `UnsupportedSchemaVersionError`；
- 迁移函数必须是纯函数，不产生副作用。

## Blob checksum 防篡改

`Blob` 序列化时携带 `sha256(data)` 的 `checksum`，反序列化时**严格校验**：

- checksum 缺失 → 拒绝恢复；
- checksum 不匹配 → 拒绝恢复（数据被篡改）。

旧数据如果存在没有 checksum 的 Blob，恢复时会失败，属于有意为之的安全策略。

## 何时应新增 codec

- Core 新增了需要持久化的数据契约对象（如新的 ports 值对象）；
- 该对象会跨进程传递（存入 Redis / 数据库 / 消息队列）；
- 需要恢复为完整的 Python 对象（而不是裸 dict）。

新增步骤：

1. 在 `serialization/registry.py` 写 encoder / decoder（或给对象补 `to_dict` / `from_dict`）；
2. 在 `_build_default_registry()` 注册 `agent_core.<名称>`；
3. 补 round-trip 测试；
4. 更新本文件的默认注册表清单。
