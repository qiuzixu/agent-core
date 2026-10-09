# handwritten-agent-core 代码评审与分析报告

- **评审对象**：`agent-core`（`handwritten-agent-core` v0.1.0 Alpha）
- **评审基线**：`main` @ `731faa8`（feat: 实现并发隔离的观测性、可返回失败实例的持久化工作流和工具审批边界）
- **评审方式**：4 路并行源码深读（执行引擎 / 模型与工具层 / 协议端口存储层 / 横切链与知识层），全部结论附文件+行号；核心文件（react.py、service.py、middleware/base.py、protocol/runtime.py、tools/executor.py、公共 API）经人工交叉复核；测试套件实测运行。
- **性质**：只读评审，未修改任何代码，不触发 `docs/ARCHITECTURE.md` 同步义务。
- **问题计数**：共 **115 项**（高危 26 / 中危 51 / 低危 38），完整清单见第 5 节。
- **整改后计数**：经三批整改后当前未修复 80 项（值得修复见 B 节，不修见 C 节），明细见 5.2-5.5 保留条目。

---

## 1. 执行摘要

`agent-core` 是一个零外部平台绑定的手写 Python Agent 运行时内核：核心包声明依赖为**空**，所有模型 SDK / MCP / 向量库 / asyncpg 均为 optional extras + 延迟导入。架构意识明显高于平均水准——依赖倒置、端口协议、失败降级、幂等键设计、文档纪律（宣称 vs 已实现）在同类项目中属上乘。

但实现收尾跟不上架构野心，问题呈三个系统性模式：

1. **单实现精修、多实现失 sync**——同一保护逻辑在 Memory/SQLite/PostgreSQL 三种后端三种做法（版本谓词只有 memory store 有；`FOR UPDATE` 只有 PG 有；WAL 只有 session store 设）；
2. **防御性吞异常与关键路径漏接并存**——流式坏参数静默置空 vs 审批回调异常冒泡 vs `JSONDecodeError` 穿透 fallback，故障行为不可预测；
3. **横切层自身缺纪律**——中间件链无异常隔离、`MODIFY` 短路使官方推荐的 Compaction+TokenLimit 组合存在缝隙、观测层无界增长。

**结论**：当前适合作为学习参考与单机/开发环境框架；按 CAPABILITIES.md 宣称的"生产多实例 + 多租户隔离"标准衡量**尚不可用**，需优先修复第 5.1 节 Top 10。

---

## 2. 项目规模与组成

| 部分 | 内容 | 规模 |
|---|---|---|
| `src/agent_core` | 21 个子模块（runtime/model/tools/mcp/skills/storage/protocol/ports/middleware/compaction/guardrails/hitl/observability/callbacks/prompts/documents/retrieval/checkpoint/workflow/acp/serialization/access） | 约 1.3 万行 |
| `packages/mineru` | 独立扩展包：自托管 MinerU HTTP 文档解析适配 | ~400 行 |
| `packages/embeddings` | 独立扩展包：OpenAI / Gemini / Ollama Embedding 适配 | ~450 行 |
| `packages/multi-agent` | 独立扩展包：Agent 注册 / 路由 / Supervisor handoff 编排 | ~1400 行 |
| `tests/` + 各包 tests | 54 个核心用例 + 3 个扩展包用例 | ~2500 行 |
| `docs/` | MkDocs 材料站，17 篇指南 + 架构 + 能力边界 | 完整 |

最大文件：`model/providers.py`（1061 行）、`storage/session.py`（742 行）、`runtime/react.py`（781 行）、`storage/runtime.py`（675 行）。公共 API 导出约 230 个符号。

工程配置：Python `>=3.13,<3.14`（PEP 695 泛型）、hatchling 构建、ruff（line-length 110）+ mypy strict（仅 src）、pytest-asyncio auto、uv.lock。

---

## 3. 架构总评

```
protocol/ports/access   ← 协议与依赖倒置端口（Message / RunContext / RunEvent / 8 个存储端口）
        ↑
runtime                 ← ReActAgent（纯循环，无持久化依赖）+ AgentRuntime（生命周期门面）
        ↑                       ↕ checkpoint_callback 反向驱动
middleware 扩展链        ← before/after_model、before/after_tool、handle_exception 五切面
        ↑
model / tools / mcp / skills   ← Provider 注册表 + 治理 + 统一工具调度 + 声明式 Skill
        ↑
storage                 ← 每端口三级实现：Memory（测试）/ SQLite（开发）/ PostgreSQL（生产）
```

要点：

- **ReAct 循环与持久化解耦**：循环本身不依赖存储，经 `RunContext.emit`（事件）与 `checkpoint_callback`（快照）被 `AgentRuntime` 反向驱动；每轮 LLM 回复后与工具执行后各写一次 checkpoint，`phase=execute_tools/pending_tool_calls` 支持中断续跑。
- **每轮 checkpoint 双通道**：`checkpoint_callback`（→ RunStore 全量 persist）+ `Checkpointer.save`（state 内嵌完整 `run_context.to_dict()`）。
- **生命周期**：start（锁内幂等查重→save→claim 租约→后台 task） / resume（从 checkpoint 续跑） / cancel（仅进程内） / 流式（独立路径） / recover_expired_runs（过期租约扫描→标 interrupted→可选 auto_resume）。
- **工作流**：串行状态机 + `NodeExecutionPolicy`（重试强制幂等声明）+ `DurableWorkflowRunner`（`{execution_id}:{sequence}:{node_name}` 跨恢复幂等键、pending 先落盘）；无并行分支、无 reducer 合并语义、无子图。
- **多租户**：`AccessContext`（None=公开，admin 租户内越权）在 Session/Memory/Context 三家落地，Run/审批/Workflow 存储未落地（见问题 B-1）。

---

## 4. 做得好的部分（值得保持）

1. **零依赖纪律真实兑现**：核心 `dependencies=[]`，SDK 全部延迟导入，`test_core_imports_without_site_packages` 专门守护。
2. **工作流幂等键设计**（durable.py L85-87 + state_machine.py L75-84）：ContextVar 传递 + 跨恢复序列号拼接，重试与崩溃恢复共用同一幂等键——全库最精细的设计，验证正确。
3. **Compaction 核心不变式正确**：工具配对边界切割（防孤儿 tool 消息 400）、旧摘要排除比较、prune-only 独立采纳、真实计数收敛四件事都做对；失败降级链（摘要失败→prune-only→TokenLimit 兜底）显式设计。
4. **Skill 系统无代码执行路径**：纯 JSON 清单、路径逃逸拒绝（loader.py L130-135）、绑定式注入（不动态 import）、先检查后注册的原子激活。
5. **流式正确性意识**："文本已 yield 后不能安全 RETRY"显式拒绝（react.py L470-474）；治理层流式 fallback 防 chunk 重放（governance.py L265-266）。
6. **安全基础**：Prompt 模板白名单（拒属性/下标/位置参数/fmt-spec 嵌套）、pgvector 标识符正则白名单、序列化信封+版本迁移（排除 pickle/动态导入）、HITL"配置存储后强制异步创建"防丢记录守卫。
7. **租约三实现的原子 claim**：PG 条件 upsert + RETURNING、SQLite `BEGIN IMMEDIATE`、内存 asyncio.Lock。
8. **文档纪律**：ARCHITECTURE.md 维护待办 + 同步记录表、CAPABILITIES.md 只记已实现能力——同类项目罕见。

---

## 5. 问题清单（115 项）

### 5.1 交叉验证后的 Top 10 优先修复项（2026-10-08 更新：#1-#8 已修复删除，保留未修两项）

| # | 严重度 | 问题 | 位置 |
|---|---|---|---|
| 9 | 中 | 全部 SQLite 存储在 async 方法内同步阻塞调用 sqlite3，无 `to_thread`——一次锁等待冻住整个事件循环 | storage/*.py 各处 |
| 10 | 中 | `run`/`stream` 两条循环约 250 行近乎复制 + `_check_access` 两份实现——同型 bug 双写（#7/#16/#18 均为双写同型），建议先抽取去重再修 bug | react.py L136-314 vs L367-579（`_check_access` 双份已随整改收敛为 service.py `AgentRuntime._check_access`/`_require_owner`，L446-464） |

> 已修复删除：原 #1（PG Workflow JSONB）、#2（HITL 超时覆盖/expires_at）、#3（checkpoint 非原子写/静默失败）、#4（跨进程丢失更新/幂等 NULL 作用域）、#5（治理记账/默认超时）、#6（providers 坏参数/浅拷贝污染）、#7（访问隔离之 ContextStore `_access`）、#8（ReAct 工具重放无幂等）。详见文末"修复状态备注"。

### 5.2 执行引擎（runtime / workflow / checkpoint）——原 28 项，保留 19 项

> 已修复项已删除，归档见文末 A 节。（本节原高危 #1-#6 全部已修；其中 #5/#6 与中危 #13/#25 系随批次顺带修复、A 节未逐条列出，已经代码核实后一并删除。）

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 7 | LLM 重试无上限：`handle_exception` 返回 True 即 `continue`，无计数无退避（RetryMiddleware 存在时有界，但其缺席时依赖中间件策略） | react.py L222-236, L461-494 |
| 8 | 流式 tool_calls 覆盖式收集：`final_tool_calls = chunk.tool_calls` 直接覆盖，强假设 adapter 单 chunk 给完整列表，且无结构校验（注：覆盖式写法已改为按 id 增量合并，跨 chunk 结构校验仍缺） | react.py L471-488, L498 |
| 9 | 流式运行游离于生命周期管理：不进 `_tasks`（cancel 永远 False）、不走锁、无 idempotency 处理；resume 只能走非流式——生命周期双轨不对称（注：租约+心跳已补） | service.py L274-327 |
| 10 | Callback 故障炸穿 checkpoint 链：`_callback_manager.dispatch` 无 try，一个坏 handler 使 `_persist` 失败→run 判 failed；与 EventSink 吞错策略不一致 | service.py L543 vs L544-549 |
| 11 | recover 与存活任务双写冲突：租约过期但任务仍在跑时，recover 的副本 persist（version+1）使原任务后续 persist 版本冲突，异常被 `_finish_run` 吞成日志，终态丢失 | service.py L329-350; runtime/store.py L26-27 |
| 12 | 文件名清洗不足：只替换 `/`、`\\`；`:` 使 Windows 写失败；`"a/b"` 与 `"a_b"` 同文件互覆 | checkpoint/store.py L143, L290 |
| 14 | falsy 判断空 state：合法空 dict 被换成 input_data；空 state 返回 None 与"不存在"混淆（注：原 durable.py L121 的 falsy 写法已随 runner 重构消失，同类 falsy 判断现存于 service.py resume） | service.py L226-227 |
| 15 | DurableWorkflowRunner.resume 无租约/锁、不校验 status==interrupted（failed 也能续）、无次数限制 | durable.py L60-78 |
| 17 | 取消路径终态持久化不保证：`_finish_run` 的 `except Exception` 不覆盖 `CancelledError`（BaseException），二次取消导致状态与事实背离 | react.py L706-716 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 18 | metadata 后置展开覆盖快照键（`{"iteration":..., **metadata}`） | react.py L276/L302/L538/L569 |
| 19 | before_tool STOP 后仍走 after_tool，"阻止"与"失败"语义混杂 | react.py L747-757, L771-773 |
| 20 | 状态机默认重试无退避（`backoff_initial_seconds=0` 时重试变立即风暴） | state_machine.py L264-295 |
| 21 | 同步节点绕过超时（`wait_for` 只包 awaitable，同步函数卡死事件循环且超时失效） | state_machine.py L277-284 |
| 22 | conditional targets 纯装饰：route 返回未注册节点要到下一轮才报错，Mermaid 静默丢弃 | state_machine.py L168, L349-358 |
| 23 | Mermaid label 不转义换行 | state_machine.py L338-339 |
| 24 | 事件与版本列表无限膨胀：每步全量 save、每版本全量 deepcopy、checkpoint 内嵌完整 run_context | durable.py L89-117; checkpoint/store.py L316, L550-555; react.py L687, L868 |
| 26 | 异常类型不统一：裸 RuntimeError / KeyError / PermissionError vs 已有领域异常体系 | runtime/store.py L27; service.py L186, L207, L452-456 |
| 27 | compat 契约不一致：`ThreadRecord.user_id="anonymous"` 默认值 vs RunContext 的 None 语义 | compat.py L39-40 |
| 28 | 幂等查询 O(n) 线性扫描 | runtime/store.py L43-50 |

### 5.3 模型与工具层（model / tools / mcp / skills）——原 29 项，保留 19 项

> 已修复项已删除，归档见文末 A 节。（本节高危 #1-#5 全部已修；另 #7/#8/#23 经代码核实已修、A 节未逐条列出，一并删除；#10-#13/#15-#17/#19 的部分修复边界见行内注。）

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 9 | tool 消息 name None 时 function_response 兜底 `"tool"`，与 function_call 不匹配 | providers/gemini.py L121-133 |
| 10 | 流式全程占并发名额并整体计时（注：`call_timeout_seconds` 默认已改 120 秒） | governance.py L272, L279-287 |
| 11 | token 估算 `len//4` 忽略 tool_calls 与工具定义，中文低估 2-3 倍（注：熔断已按 (adapter, scope) 分桶） | governance.py L182-184 |
| 12 | to_thread 同步工具超时后线程继续跑（副作用照常）；async `__call__` 对象不被 `iscoroutinefunction` 识别（注：partial 已由标准库解包） | tools/executor.py L233-242 |
| 13 | schema 校验缺口：未知参数不拒、TypeError 误归 execution（注：boolean/array/object 校验已补齐） | tools/executor.py L255-263, L265-291 |
| 15 | `list_tools` 只返回名字，丢 inputSchema/description，无法从 MCP 元数据构建工具定义 | mcp/client.py L90-100 |
| 16 | ToolRegistry 覆盖仅 warning + `replace_tools=True` 显式旁路 + `activate(None)` 默认全量激活（注：activate 默认路径的工具名冲突已改 raise） | skills/registry.py L103-104, L151-153; tools/executor.py L61-63 |
| 17 | context_usage 向非标准 /tokenizer、/tokenize POST 完整会话+Bearer key：官方必 404（无负缓存反复白打），第三方网关收到会话内容——外泄面（注：qwen 系已优先本地 tokenizer、dashscope 域名优先原生端点） | providers/openai_compatible.py L259-298 |
| 18 | context window HTTP 兜底值含 None 实际不缓存，每次重付 8s GET | providers/openai_compatible.py L314, L320, L333-334 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 19 | 缺 SDK 错误类型不统一（ModelInvocationError vs 裸 ImportError）（注：OpenAI 侧已统一抛 ModelInvocationError，Anthropic/Gemini 仍裸 ImportError） | providers/openai_compatible.py L128-131; providers/anthropic.py L30-35; providers/gemini.py L31-35 |
| 20 | finish_reason 仅 OpenAI 流式填充，StreamChunk 契约事实上只有 OpenAI 满足 | providers/openai_compatible.py L459-466; providers/anthropic.py L283, L285; providers/gemini.py L271, L273 |
| 21 | known_context_window 表极小 + `"qwen3.7-plus": 1_000_000` 疑似笔误 | providers/common.py L19-27 |
| 22 | `_model_name` 空回退 ""，直到调 API 才报错 | providers/factory.py L23-25 |
| 24 | 两套行为不一的 JSON 围栏解析出口（structured.py vs OpenAIProvider.parse_json） | model/structured.py L15, L46-56 vs providers/openai_compatible.py L498-505 |
| 25 | 结构化输出 temperature=0 重试确定性复现；tools 透传引发必然失败的尝试 | model/structured.py L170-176 |
| 26 | context_usage 完全绕过治理 | governance.py L303-309 |
| 27 | 默认目录模型名与 provider 默认值漂移（gemini-2.0-flash vs gemini-2.5-flash） | model/catalog.py L41 vs providers/gemini.py L26 |
| 28 | `ModelProviderFactory = Callable[..., ModelAdapter]` 丢失 kwargs 契约 | model/core.py L72 |
| 29 | `risk_level` 自由字符串无枚举 | tools/executor.py L26 |

### 5.4 协议、端口与存储层（protocol / ports / storage / hitl / acp / serialization）——原 31 项，保留 22 项

> 已修复项已删除，归档见文末 A 节。（本节已删：高危 #1、#3-#9 与低危 #26；保留的 #2/#19/#23/#24 为部分修复、注记见行内；#25 经复核两处 from_dict 均仍未校验枚举，维持保留。）

**高危**

| # | 问题 | 位置 |
|---|---|---|
| 2 | 幂等键对 NULL 作用域失效：SQLite/PG 唯一索引中 NULL 互异，匿名并发重发产生重复 run；`FOR SHARE` 预检查锁不住不存在的行（注：NULL 作用域半边已用 COALESCE 表达式唯一索引修复） | storage/runtime.py L294-310, L618-633, L660-673 |

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 10 | 全部 SQLite 存储事件循环内同步阻塞 IO，无 to_thread（见 Top10 #9） | storage/runtime.py L324-326; storage/memory.py L343-348 |
| 11 | SQLite 各存储 PRAGMA 不一致：仅 session 有 WAL、仅 lease 有 30s busy_timeout，同一 sessions.db 等待策略矛盾 | storage/session.py L305-306; storage/lease.py L184 |
| 12 | `SqliteSessionStore._check_access` claim 无 ON CONFLICT，并发首写抛裸 IntegrityError（PG 版有） | storage/session.py L331-334 vs L517-523 |
| 13 | SQLite 幂等撞键抛裸 sqlite3.IntegrityError，无领域错误翻译（注：memory.py L274-275 已有 IntegrityError→MemoryConflictError 翻译可参照） | storage/runtime.py L324-380 |
| 14 | `SessionManager.clear` 摘锁竞态：`not lock.locked()` 检查与 pop 无原子性，极端时序同 thread 双锁 | storage/session.py L863-873 |
| 15 | `save_run` 隐式第二写：pending_approval 在 run 事务外独立连接落 approvals——非原子 | storage/runtime.py L379-380, L718-719 |
| 16 | `mark_stale_runs` 先读后逐个改写，无事务，与在跑 worker 竞争；N+1 连接；重复调用累积重复 `run_interrupted` 事件 | storage/runtime.py L174-185, L451-465, L754-769 |
| 17 | RunContext.events 随 save 全量重写：长 run O(n²) 累计成本 | protocol/runtime.py L262; storage/runtime.py L353/L372, L692/L711 |
| 18 | MemoryStore.get 的过期删除发生在 `_can_access` **之前**——PermissionError vs None 构成存在性 oracle | storage/memory.py L107-117, L287-308, L484-502 |
| 19 | SqliteContextStore 先读后写跨连接无事务；SQLite 整文档覆盖 vs PG `data ||` 字段级合并——dev/prod 合并语义不同（注：SQLite 已改 BEGIN IMMEDIATE 同事务，合并语义差异仍在） | storage/context.py L221-237 vs L326-329 |
| 20 | Memory/Sqlite ModelSelectionStore version 读后写，跨进程可重复；仅 PG 原子自增 | storage/model_selection.py L75-88, L177-195 vs L288 |
| 21 | PG save_run 幂等预检查 `FOR SHARE` 对不存在的行无效 | storage/runtime.py L660-673 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 22 | RuntimeStore 抽象方法 docstring 复制粘贴错误 | storage/runtime.py L46-70 |
| 23 | RunStore Protocol 与 RuntimeStore ABC 双轨不一致；EventSink 无实现；`storage.context.ContextStore` 与端口同名冲突（注：EventSink 已有 MemoryEventSink 实现，双轨与同名冲突仍在） | ports/storage.py L34-56, L87; storage/runtime.py L37-93; runtime/service.py L46-53 |
| 24 | 工厂返回未初始化的 Postgres 对象，忘 `await initialize()` 得到运行期 RuntimeError（注：仅 SessionManager.setup() 为 session 存储补了显式初始化，7 个工厂本身未变） | storage/{runtime,session,context,memory,model_selection,lease,workflow}.py 各 create_*_store 工厂 |
| 25 | `ApprovalRecord.from_dict`/`Message.from_dict` 不校验 status/role 枚举（注：经复核两处均仍未校验） | protocol/runtime.py L132; protocol/messages.py L57 |
| 27 | SqliteMemoryStore.search 不清理过期行（InMemory 版会清）；无租户谓词下推 | storage/memory.py L331-361 |
| 28 | Message tool_calls 键名契约无约束，不合规时静默产出 `arguments: "{}"` | protocol/messages.py L38-49, L61 |
| 29 | ACP 内部异常消息直通客户端、`_sessions` 永不清理、通知失败无日志 | acp/server.py L121-123, L49/L171/L178, L331-338 |
| 30 | 序列化：无完整性校验（无 HMAC，Blob checksum 从不比对）、payload 键不收敛、`_decode_access` 可从存储铸 admin、超深 JSON RecursionError 未包装、模块级可变单例 | serialization/registry.py L166-173, L187-200, L207-212, L290 |
| 31 | `SqliteModelSelectionStore.save` scope="tenant" 时静默丢弃 user_id | storage/model_selection.py L176, L184 |

**租约专项**：三实现 claim 均原子（✔），但无 fencing token（旧 Worker 被接管后业务写路径不受租约拦截）、SQLite ISO 字符串时间比较靠巧合保序、租约表无清理、`heartbeat_at` 不参与判定。 | lease.py L35-47, L205-266

**序列化宣称对照**：防 RCE ✔（白名单+无 pickle+无动态导入）；防篡改 ✘（无签名/checksum 不校验）；域校验 ✘（role/status 枚举不校验）。

### 5.5 横切扩展链与知识层（middleware / compaction / guardrails / observability / callbacks / prompts / documents / retrieval / multi-agent）——原 27 项，保留 20 项

> 已修复项已删除，归档见文末 A 节。（本节高危 #1-#6 与中危 #12 已删，其中 #5/#6 系整改记录"中间件组合和观测内存"一行覆盖；#22 为部分修复、注记见行内。）

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 7 | 中间件链无异常隔离：钩子抛异常直接打断主流程（对比 CallbackManager 逐订阅者隔离——同一框架两套策略） | middleware/base.py L134-154 |
| 8 | 溢出恢复日志算术错误（多轮压缩/prune-only 时"压缩前条数"算错，仅日志失真） | compaction/middleware.py L258-264 |
| 9 | `_last_window` 实例字段跨并发会话串扰 | compaction/middleware.py L127, L144, L230 |
| 10 | 溢出误判：`"context window"`、`"too many tokens"` 宽泛子串匹配会吞掉非溢出异常并强制压缩重试 | compaction/middleware.py L67-83 |
| 11 | TokenLimit 与 region 的 token 估算口径不一致（前者忽略 tool_calls）；`chars_per_token=4` 是英文口径，中文低估 2-3 倍 | compaction/token_limit.py L36-38; compaction/region.py L29-34 |
| 13 | spill LRU 无存活绑定：被消息引用中的 spill_id 可被挤出，回读得到"不存在" | compaction/spill.py L58-62 |
| 14 | spill 回读无访问控制（与检索层 AccessContext 体系反差） | compaction/spill_tool.py L16-21 |
| 15 | emit 双签名（`ctx.emit(event, **payload)` vs `emit_hook(event, payload)`） | compaction/middleware.py L416-428 |
| 16 | 摘要输入不裁剪：会话逼近窗口时摘要调用自身可能溢出，只能靠 CompactionError→prune-only 兜底 | compaction/summarizer.py L33-36 |
| 17 | Retry 空响应判定过宽（合法空 assistant 回合也触发退避）；空响应与异常重试共享 `retry_count` 额度 | middleware/base.py L227-249, L253-282 |
| 18 | KeywordRetriever 的 namespace 藏在 metadata 里（与 VectorRecord.namespace 两套约定，漏写静默落 default） | retrieval/memory.py L208 |
| 19 | metadata 过滤语义跨后端不一致：InMemory/Chroma 逐 key 相等 vs pgvector JSONB `@>` 子树包含 | retrieval/_common.py L45-46 vs retrieval/pgvector.py L194 |
| 20 | KeywordRetriever 评分不可比（命中次数/词数），与余弦分共用 `min_score` 语义 | retrieval/memory.py L216-221 |
| 21 | Callback 分发串行无超时：一个慢 handler 拖住整条事件路径 | callbacks/manager.py L86-105 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 22 | OTel span 异常路径可能泄漏（无 try/finally）（注：LLM 调用异常路径已加 handle_exception 收口，其余中间件异常路径仍可能泄漏） | observability/core.py L215-227; runtime/react.py L231, L491 |
| 23 | `setup_tracing` 全局 provider 不可重入 | observability/core.py L265-300 |
| 24 | embeddings gather 不取消在途批次 | packages/embeddings/src/agent_core_embeddings/base.py L88 |
| 25 | Chroma payload 塞整份文档 JSON，长文档成倍放大存储 | retrieval/chroma.py L196-203 |
| 26 | TextBlobParser 解码异常不包装（风格不一致） | documents/loaders.py L38 |
| 27 | Observability 在 before_model 未执行时记 0ms 假值 | observability/core.py L186-192 |

**Guardrails 补充局限**：只查最后一条 user 消息（历史与工具注入内容不复查）；PII 脱敏只在输出侧；tool_calls 参数完全不设防；`OutputFormatGuard` 对解释性 SQL 回答误杀。

**Multi-Agent（SupervisorAgent）概述**：带恢复能力的串行 handoff 状态机（非并行编排）——task_id 幂等、租约+心跳、预算体系（calls/tokens/cost/handoffs/visits）、checkpoint 复用已落库终态、取消防复活；局限：一次只挂起一个子 Agent、跨进程取消依赖 store 状态。

---

## 6. 测试现状

- **核心套件实测**：54 用例 → **40 通过 / 2 跳过 / 12 失败**。
- **12 个失败全部是环境限制，非代码缺陷**：本沙箱拒绝 `tempfile` 清理时的 `chmod`（WinError 5），失败样本堆栈均落在 `tempfile._resetperms`；换正常机器应全绿。复现命令（本机正常环境）：
  ```bash
  cd agent-core && .venv/Scripts/python -m pytest tests -q
  ```
- **扩展包测试未运行**：三个包未安装进 venv 导致收集失败；需先
  ```bash
  pip install -e packages/mineru -e packages/embeddings -e packages/multi-agent
  ```
- **覆盖盲区（与高危问题分布重合）**：
  - `compaction` 子系统（约 900 行）**零测试**；
  - guardrails 无测试（LengthGuard 断裂因此未被发现）；
  - providers 网络层无契约测试（finish_reason、多 system、工具增量合并等跨 provider 不一致因此未暴露）；
  - HITL 超时竞态、SQLite 跨进程并发、PG workflow 回读无测试。
- **测试风格**：行为级覆盖质量不错——租约竞速、恢复、严格访问模式、ACP stdio、序列化防动态导入、检索隔离均有专项；全部重依赖协议注入、内存实现齐备。短板在时间与随机性不可注入（`datetime.now()`/`uuid4()` 内联，如 store.py L284-289）。

---

## 7. 宣称 vs 实测对照

| 宣称能力 | 实测结论 |
|---|---|
| 三级存储（Memory/SQLite/PostgreSQL） | ✔ 全真实现（PG 非 stub，完整 DDL + 连接池） |
| 幂等键防重复创建 | △ 仅非 NULL 作用域成立；跨进程 SQLite 丢失更新 |
| Worker 租约多 Worker 互斥 | ✔ claim 原子；△ 接管缺 fencing token，旧 Worker 写路径不受租约拦截 |
| 安全 JSON 序列化（不用 pickle/动态导入） | ✔ 防 RCE 成立；✘ 防篡改与域校验不成立 |
| 用户/租户/namespace 隔离 | ✘ 仅 Session/Memory/Context 落地；Run/审批/Workflow 未落地 |
| HITL 持久化审批 | ✔/△ 持久化成立；状态机有超时竞态与 expires_at 空洞 |
| ACP JSON-RPC/stdio | ✔ 核心会话流程可用；方法覆盖与错误映射为部分实现 |
| 模型治理（限流/并发/超时/熔断/fallback） | △ 算法正确；记账语义失真（本地排队计入远端熔断）、默认无超时 |
| 结构化输出 JSON Schema | △ 自研子集校验；未用任何 provider 原生通道；两套解析出口行为不一 |
| 上下文压缩（摘要/裁剪/spill） | ✔ 核心不变式正确；⚠ 零测试覆盖 |
| Skill 声明式加载（不导入代码） | ✔ 属实；△ replace_tools 命名劫持通道 |

---

## 8. 修复路线建议

**第一批（数据正确性，1-2 天量级）**
1. `PostgresWorkflowExecutionStore` JSONB str→json 解析（照抄 runtime.py L95-117 的处理）；
2. HITL：deadline 分支改为"落 expired 前 CAS 校验仍 pending"，approve/reject 用条件 upsert；`expires_at` 落库并在加载时校验；
3. Checkpointer 全部写路径改 tmp+rename 原子写；TimeTravel 持久化失败改为抛 `CheckpointError`；
4. SQLite runtime/workflow store 的 UPDATE 加 `AND version=?` 谓词（照抄 SqliteMemoryStore L277-284）；幂等索引补 `NULLS NOT DISTINCT`（PG）。

**第二批（语义修正，2-3 天量级）**
5. 治理层：`acquire`/`slot` 的 `ModelRateLimitError` 移出 `_failure()` 记账；`call_timeout_seconds` 给默认值；熔断按 scope 分桶；
6. providers：tool arguments 解析包 try 转 `ModelInvocationError`；`parse_json` 改深拷贝；统一缺 SDK 错误与 finish_reason 契约；
7. ReAct 工具重放：checkpoint 记录已完成的 tool_call id，恢复时跳过（或给工具传幂等键，复用工作流的 ContextVar 机制）；
8. `run`/`stream` 循环抽取共用内核（先去重再修 #7/#16/#18 同型问题）。

**第三批（架构收敛）**
9. 访问隔离：RunStore/审批/Workflow 端口签名加 `access` 必选参数（或提供 `require_access` 严格实现）；封死 `ContextStore.update` 的 `_access` 覆写；
10. SQLite 统一连接层（共享 WAL + busy_timeout + `to_thread` 包装）；跨后端共享行映射 + 契约测试（同一测试矩阵跑三种后端）；
11. 补 compaction / guardrails / providers 契约 / HITL 竞态测试；时间与时钟注入（消除内联 `datetime.now()`/`uuid4()`）。

---

## 9. 总体结论

这是一个**明显经过多轮认真迭代、架构意识高于平均水准**的手写框架：依赖倒置、失败降级、幂等键设计、流式正确性判断、中文注释质量都体现真实功底；文档纪律（宣称 vs 已实现）在同类项目中罕见地诚实。

当前最大短板集中在四处：**(a) 持久层非原子写 + 静默失败**；**(b) 失败重放的副作用幂等只覆盖工作流、未覆盖 ReAct 工具**；**(c) 流式路径与访问隔离在核心处是二等公民**；**(d) "单实现精修、多实现失 sync"** 导致跨后端行为不可预测。

适用定位：学习参考、单机开发、原型验证。要兑现 CAPABILITIES.md 的生产多实例多租户承诺，需按第 8 节路线完成至少前两批修复，并补齐 compaction/guardrails/providers 契约测试。

---

## 附：修复状态备注（2026-10-08 三批整改后更新）

### A. 已修复（从跟踪清单删除）

三批整改已消化的条目不再逐条维护，明细见 git 历史与 `docs/reference/agent-core-review-remediation.md`：

- **第一批（Codex，1428e93/1656585）**：5.1 原 #1-#8；5.3 #4/#13；5.4 #1-#8、#24-25 部分；5.5 #1-#4、#12；guardrails LengthGuard、ACP/MCP/prompts/observability 约 13 组。
- **第二批（复核后修复）**：5.2 #1/#2/#3/#16；5.4 #2 之 COALESCE 预检、#5/#6/#7/#8/#9；5.5 #2/#3；middleware MODIFY 对账、cancel 异常日志、治理预检+容量、TimeTravel 按身份回滚。

### B. 值得修复（建议保留跟踪，按优先级）

1. **SQLite 同步 IO 线程化**（Top10 #9）：生产前必做，统一连接层 + `to_thread`。
2. **run/stream 循环抽取去重**（Top10 #10）：修后续任何循环 bug 前先做，避免双写。
3. **Store 端口级统一 AccessContext**：服务边界已覆盖，属纵深防御；多租户生产上线前收紧端口签名。
4. **外部 Store 的 `transition_approval` 升级**：内置已原子，未升级的外部实现仍是 LWW（文档已声明）。
5. **TimeTravel 文件名清洗/版本容量策略**：跨会话互覆与无限膨胀风险。
6. **租约启用**：vanilla 已具备零改动路径，多实例部署时传 `RunLeaseStore` 并在 `setup()` 调 `recover_expired_runs`。
7. **测试补强**：compaction 仍零测试；providers 流式契约（finish_reason/多 system/工具合并）；恢复重放断言。

### C. 不修（有意取舍，接受现状）

- **子图/并行分支/reducer/Qdrant/FAISS/统一 OCR/完整 JSON Schema/事件归档/行级安全**：能力扩展或架构选择，不算缺陷（整改意见 #4 成立）。
- **零依赖边界**：optional extras 是明确设计，不是遗漏。
- **spill LRU 容量淘汰、`_circuits` 淘汰策略、事件 O(n²) 写放大**：当前规模可接受，容量触顶再议。
- **resume_run 不映射 `AgentRuntime.resume`**：语义不同（新建 run vs 续跑），保留自研是正确决定。
- **既有 20 个 E501 长行**：历史格式债务，重排无行为收益。
- 各域低危清单（5.2#18-28、5.3#19-29、5.4#22-31、5.5#22-27）中未列入上面 B 节者：均为备忘性质，暂不修复。
