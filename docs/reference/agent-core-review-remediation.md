# Agent Core 审查报告复核与整改评价

> 状态备注：本文记录**第一批**整改（提交 1428e93/1656585）。第二批（复核遗留修复：恢复期状态回填、时间旅行 thread_id、批末冗余快照、MODIFY 对账、索引迁移预检、HITL 幻影/过期收口、cancel 日志、治理预检+容量、TimeTravel 回滚）与第三批（`AgentRuntime.RunExecutor` 可插拔执行器及 vanilla 委托）见仓库根 `agent-core-review.md` 文末"修复状态备注"。

本文复核仓库根目录的 `agent-core-review.md`，并记录 2026-10-08 的整改结果。评价对象是整改前主仓
`470d44a` 及其后的本次修改。

## 总体评价

原报告找到了多项真实且优先级较高的问题，尤其是 PostgreSQL Workflow JSONB 恢复、HITL 终态竞态、
checkpoint 非原子写、SQLite/PostgreSQL 乐观并发、模型治理范围和 ReAct 工具恢复。这些问题有明确的
触发条件，也会影响生产恢复或并发正确性，整改价值很高。

报告的数量和最终定性不够严谨。“115 项”同时统计了确定 Bug、潜在风险、性能问题、测试缺口、
接口一致性和架构建议，不能等同为 115 个已复现缺陷。“当前仅适合学习、生产不可用”也缺少部署规模、
数据库配置、调用边界和故障模型作为前提。更准确的结论是：整改前不应直接宣称具备完备的多实例恢复和
并发保障；单机开发路径与部分生产能力已经可用，但仍需要真实 PostgreSQL、故障注入和负载测试证明边界。

## 基线与证据限制

原报告声明评审基线为 `731faa8`。该对象属于独立 `agent-core` GitHub 仓库的历史；顶层主仓使用另一套
commit SHA，因此不能直接在顶层历史中解析。获取独立仓库的最新远端历史后，该基线已经可以核对。
原报告没有保存完整测试命令、原始输出和运行环境，测试过程与“全部经交叉验证”的范围仍不能逐条复演。
本次复核以该源码快照、当前源码、现有测试、新增回归测试和两套 Git 历史为依据。

严重度也需要结合可达性判断。例如底层 Store 没有统一接收 `AccessContext` 是纵深防御缺口，但
`AgentRuntime`、`ApprovalQueue` 和 `DurableWorkflowRunner` 已在服务边界执行访问检查；它和“任意外部用户
可直接跨租户读取”不是同一个结论。

## 属实且已整改

| 问题组 | 复核结论 | 整改结果 |
| --- | --- | --- |
| Workflow JSONB | 属实 | SQLite 文本和 asyncpg JSONB 返回统一解析。 |
| Run/Workflow 并发写 | 属实 | Memory/SQLite/PostgreSQL 增加版本检查、事务和条件更新。 |
| 匿名作用域幂等 | 属实 | SQLite/PostgreSQL 索引用 `COALESCE` 归一化 NULL 作用域。 |
| HITL 超时竞态 | 属实 | deadline 持久化，批准、拒绝、过期使用 `pending` 条件状态转换。 |
| Checkpoint 文件损坏窗口 | 属实 | 临时文件写入、flush、fsync、原子替换；失败显式抛错并回滚内存版本。 |
| 模型治理范围 | 属实 | 一个逻辑请求只计一次本地配额，熔断按 provider 和 scope 隔离，默认调用超时 120 秒。 |
| OpenAI 工具参数与 JSON 输出 | 属实 | 坏参数统一为模型异常，消息深拷贝，schema 参数实际校验。 |
| Context 归属覆盖 | 属实 | `_access` 成为保留字段，SQLite 同事务更新，PostgreSQL 保留已认领归属。 |
| ACP 错误归类和审批选项 | 属实 | 会话不存在使用专用异常，未声明审批选项按拒绝处理。 |
| 中间件组合和观测内存 | 属实 | `MODIFY` 继续执行后续中间件，统计增加容量上限，失败工具记录失败状态。 |
| MCP 结果和子进程环境 | 属实 | 支持 text content 回退，子进程只继承必要系统变量和显式配置。 |
| Prompt/Context 损坏文件 | 属实 | 加载失败改为 fail closed，Prompt 写入使用原子替换。 |
| AgentRuntime 生命周期窗口 | 属实 | 修复取消异常泄漏、执行与心跳同时完成误判及事件游标释放时机。 |

## 部分属实或已降低风险

### ReAct 工具副作用重放

报告指出并发工具中部分成功后异常会导致恢复时重放全部工具，这一判断属实。本次修改在每个工具完成后
保存阶段 checkpoint，并只保留未完成的 `pending_tool_calls`；单个工具执行链异常会转换为失败 tool
message，通常不会再使整批直接失败。

这仍然是 at-least-once 恢复语义。外部副作用已经成功、但进程在阶段 checkpoint 落盘前崩溃时，恢复仍
可能重放该工具。生产写工具必须接收稳定幂等键，或自行实现去重和事务。

### 访问隔离

Context 归属篡改已经修复，Run、审批和 Durable Workflow 的公开服务路径可以启用严格访问模式。
底层 `RunStore`、`ApprovalStore` 和 `WorkflowExecutionStore` 仍没有统一的 `AccessContext` 参数；绕过服务层
直接使用 Store 的内部代码需要自行保证调用边界。它是需要继续收紧的端口设计问题。

### Chroma 查询性能

无业务 metadata filter 时已经只请求 `limit` 条候选。存在任意 metadata filter 时，为保持当前精确过滤
语义仍会读取全量候选；大数据集应把可表达的过滤条件下推给 Chroma，或明确采用扩大候选集后的近似语义。

## 仍待处理

以下问题在本次整改中没有完成，不应在能力文档中宣称已经解决：

- SQLite Store 的 async 方法仍直接调用同步 `sqlite3`，锁等待可能阻塞事件循环；
- `ReActAgent.run()` 与 `stream()` 仍有较多重复逻辑，后续修改存在双路径漂移风险；
- 同步工具放入线程后，async timeout 只能停止等待，不能强制终止底层线程和已经发生的副作用；
- Callback 暂无单个 handler 的 timeout，慢回调仍会拖慢事件发布；
- TimeTravel 文件名清洗、版本数量和事件历史仍需要容量与碰撞策略；
- Compaction 区域边界、恢复故障注入和并发 Store 还需要更多独立测试；
- PostgreSQL、pgvector 和真实 Chroma 的集成测试尚未在本次环境中完成。

`run/stream` 去重、SQLite IO 线程化和 Store 端口访问上下文会改变较多内部边界，适合分成后续独立变更，
并配套迁移说明和并发测试。

## 设计建议，不应计为确定 Bug

报告中的子图、并行分支、reducer、更多 Provider 一致性、统一 OCR、Qdrant/FAISS、完整 JSON Schema、
事件归档和数据库行级安全等内容属于能力扩展或架构选择。是否实现应由目标用户、部署方式和维护成本决定，
不能仅凭与大型框架功能数量对照就判定为缺陷。

同样，Core 保持零基础依赖、模型/MCP/向量库使用 optional extras，是当前项目的明确边界，不属于遗漏。

## 本次验证

- Ruff：`src` 与 `tests` 全部通过；
- 回归测试：新增 14 项，覆盖并发版本、事务失败版本回写、审批 CAS、原子 checkpoint、模型治理、
  Context 归属、向量删除和工具阶段 checkpoint；
- Python 3.12 兼容运行：pytest 66 项通过，pgvector 集成测试因没有 DSN 跳过，真实 Chroma 测试因
  3.12/3.13 二进制依赖不兼容主动排除；
- MkDocs strict build、`git diff --check` 与修改文件 `py_compile` 通过。

项目正式要求 Python 3.13。当前 `.venv` 指向的 Python 3.13 可执行文件已丢失，网络环境也阻止重新下载，
因此本次无法在声明版本上执行 pytest 和 mypy。文档构建及除二进制扩展外的 pytest 使用 bundled
Python 3.12 完成；用 3.12 加载 3.13 的 site-packages 时，Chroma/NumPy 二进制扩展不兼容，不能作为
Chroma 实现失败的证据。合并或发布前仍应在正常 Python 3.13 环境运行完整 CI，并连接临时 PostgreSQL
做并发与恢复集成测试。

## 结论

原报告适合作为风险发现清单，但不适合作为精确缺陷计数或最终生产评级。高价值的确定性问题已经完成一轮
整改，并增加了对应回归测试；剩余风险主要集中在同步 SQLite、端口层纵深访问控制、不可强杀的同步工具、
双循环维护成本和真实基础设施验证。下一轮应围绕这些可验证目标推进，而不是继续追求报告问题总数归零。

### 勘误（第三批复核后补充）

- "批准、拒绝、过期使用 pending 条件状态转换"仅对内置 Memory/SQLite/PostgreSQL Store 原子成立；
  未实现 `transition_approval` 的外部 Store 经 `_transition` 回退为 load→check→save，跨进程仍存在
  last-writer-wins 窗口（CAPABILITIES.md 已同步声明）。
- "审批 CAS"回归测试仅覆盖 MemoryRuntimeStore；SQLite/PostgreSQL 的 `transition_approval` 当时无测试。
- 复核遗留的高危项"时间旅行接口使用构造时 thread_id"当时既未修复也未列入"仍待处理"，已在第二批修复
  （`get_checkpoint_history`/`rollback_to` 增加显式 `thread_id` 参数）。
