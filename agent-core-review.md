# handwritten-agent-core 代码评审与分析报告

- **评审对象**：`agent-core`（`handwritten-agent-core` v0.1.0 Alpha）
- **评审基线**：`main` @ `731faa8`（feat: 实现并发隔离的观测性、可返回失败实例的持久化工作流和工具审批边界）
- **评审方式**：4 路并行源码深读（执行引擎 / 模型与工具层 / 协议端口存储层 / 横切链与知识层），全部结论附文件+行号；核心文件（react.py、service.py、middleware/base.py、protocol/runtime.py、tools/executor.py、公共 API）经人工交叉复核；测试套件实测运行。
- **性质**：只读评审，未修改任何代码，不触发 `docs/ARCHITECTURE.md` 同步义务。
- **问题计数**：共 **115 项**（高危 26 / 中危 51 / 低危 38），完整清单见第 5 节。

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

### 5.1 交叉验证后的 Top 10 优先修复项

| # | 严重度 | 问题 | 位置 |
|---|---|---|---|
| 1 | 高 | `PostgresWorkflowExecutionStore.load/list`：asyncpg 默认把 JSONB 返回为 str，`dict()` 直接 ValueError——**生产工作流恢复大概率是坏的**（同坑在 runtime/memory 已处理，唯独 workflow 漏） | storage/workflow.py L88-90, L317-333 |
| 2 | 高 | HITL 超时覆盖已批准决定：deadline 置 expired 后 finally 无条件落库，与并发 approve 构成 last-writer-wins（存储 upsert 无 CAS）；且 `expires_at` 从不设置/检查，崩溃后 pending 审批永不过期 | hitl/core.py L206-224; protocol/runtime.py L105 |
| 3 | 高 | checkpoint 非原子写（直接 `open("w")` 截断）+ TimeTravel 持久化失败静默吞掉——动摇"可恢复"核心卖点 | checkpoint/store.py L136-137, L522-525 |
| 4 | 高 | SQLite runtime/workflow store 跨进程丢失更新（版本检查与写入间无谓词，PG 已修未对齐）；幂等键唯一索引对 NULL 作用域失效（无 `NULLS NOT DISTINCT`），匿名并发重发产生重复 run | storage/runtime.py L287-333, L266-270; storage/workflow.py L184-201, L282-315 |
| 5 | 高 | 治理层把本地限流/并发排队超时记入远端熔断（`ModelRateLimitError` ⊂ `ModelInvocationError`），自我限流最终熔断所有 provider；流式全程占用并发名额且默认无超时 | governance.py L207-224, L245-264, L47 |
| 6 | 高 | OpenAI 适配器坏 tool arguments 抛裸 `JSONDecodeError` 穿透 fallback 与熔断；`parse_json` 浅拷贝共享可变 Message，永久污染调用方 system 消息 | providers.py L262, L527-529 |
| 7 | 高 | **访问隔离未在最核心处落地**：RunStore/审批/WorkflowExecutionStore 完全无 AccessContext 过滤（拿着 run_id/approval_id 即可跨租户读写）；`ContextStore.update` 允许覆写 `_access` 归属键 | storage/runtime.py L141-494; storage/workflow.py L64; storage/context.py L110-114 |
| 8 | 高 | ReAct 并发工具批任一失败即 raise，checkpoint 停在 `execute_tools`，恢复路径**重放全部工具（含已成功者）**且工具无幂等凭证——工作流有、ReAct 没有 | react.py L738-746, L168-176 |
| 9 | 中 | 全部 SQLite 存储在 async 方法内同步阻塞调用 sqlite3，无 `to_thread`——一次锁等待冻住整个事件循环 | storage/*.py 各处 |
| 10 | 中 | `run`/`stream` 两条循环约 250 行近乎复制 + `_check_access` 两份实现——同型 bug 双写（本次评审 #7/#16/#18 均为双写同型），建议先抽取去重再修 bug | react.py L132-302 vs L352-539 |

### 5.2 执行引擎（runtime / workflow / checkpoint）——28 项

**高危**

| # | 问题 | 位置 |
|---|---|---|
| 1 | 并发工具部分失败→副作用重放且无幂等键（见 Top10 #8） | react.py L738-746, L255-263, L168-176 |
| 2 | checkpoint 非原子写：`open("w")` 截断重写，写入中途崩溃产生截断 JSON，load 抛错且无备份降级 | store.py L136-137, L522-523 |
| 3 | 持久化失败静默吞掉：`save` 已返回 version_id 但落盘失败只打日志；与 FileCheckpointer 抛 `CheckpointError` 的策略自相矛盾 | store.py L524-525, L545-546 |
| 4 | 时间旅行接口用错 thread：checkpoint 写入用 `thread_id or runtime_context.thread_id`（运行时决定），`get_checkpoint_history`/`rollback_to` 固定用构造时 `self._thread_id` | react.py L756-781 vs L162/L387 |
| 5 | cancel 异常窗口：`task.done()` 检查与 `await task` 之间任务若因业务异常完成，异常抛给取消方（只捕获 CancelledError）；应用 `gather(..., return_exceptions=True)` | service.py L241-247 |
| 6 | 双 task 同时完成时误判租约丢失：`FIRST_COMPLETED` 后先判 heartbeat，会 cancel 已成功的执行并标 interrupted，成功结果被丢弃；应先判 execution | service.py L371-388 |

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 7 | LLM 重试无上限：`handle_exception` 返回 True 即 `continue`，无计数无退避（RetryMiddleware 存在时有界，但其缺席时依赖中间件策略） | react.py L211-224, L451-455 |
| 8 | 流式 tool_calls 覆盖式收集：`final_tool_calls = chunk.tool_calls` 直接覆盖，强假设 adapter 单 chunk 给完整列表，且无结构校验 | react.py L448-449 |
| 9 | 流式运行游离于生命周期管理：不进 `_tasks`（cancel 永远 False）、不走锁、无 idempotency 处理；resume 只能走非流式——生命周期双轨不对称 | service.py L251-303 |
| 10 | Callback 故障炸穿 checkpoint 链：`_callback_manager.dispatch` 无 try，一个坏 handler 使 `_persist` 失败→run 判 failed；与 EventSink 吞错策略不一致 | service.py L483 vs L485-489 |
| 11 | recover 与存活任务双写冲突：租约过期但任务仍在跑时，recover 的副本 persist（version+1）使原任务后续 persist 版本冲突，异常被 `_finish_run` 吞成日志，终态丢失 | service.py L311-318; store.py L26-27 |
| 12 | 文件名清洗不足：只替换 `/`、`\\`；`:` 使 Windows 写失败；`"a/b"` 与 `"a_b"` 同文件互覆 | store.py L117, L265 |
| 13 | version_id 32bit 截断（`uuid4()[:8]` 可碰撞）+ `datetime.now()` 非 UTC + 函数体内联 import 不可注入 | store.py L284-289 |
| 14 | falsy 判断空 state：合法空 dict 被换成 input_data；空 state 返回 None 与"不存在"混淆 | durable.py L121; store.py L83 |
| 15 | DurableWorkflowRunner.resume 无租约/锁、不校验 status==interrupted（failed 也能续）、无次数限制 | durable.py L60-78 |
| 16 | 已完成 run 可被 resume 并重调 LLM：completed checkpoint 的 `next_iteration=iteration+1`，resume 不从 `last_answer` 短路 | react.py L633; service.py L206 |
| 17 | 取消路径终态持久化不保证：`_finish_run` 的 `except Exception` 不覆盖 `CancelledError`（BaseException），二次取消导致状态与事实背离 | react.py L666-676 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 18 | metadata 后置展开覆盖快照键（`{"iteration":..., **metadata}`） | react.py L264/L290/L498/L529 |
| 19 | before_tool STOP 后仍走 after_tool，"阻止"与"失败"语义混杂 | react.py L730-731 |
| 20 | 状态机默认重试无退避（`backoff_initial_seconds=0` 时重试变立即风暴） | state_machine.py L290-295 |
| 21 | 同步节点绕过超时（`wait_for` 只包 awaitable，同步函数卡死事件循环且超时失效） | state_machine.py L279-283 |
| 22 | conditional targets 纯装饰：route 返回未注册节点要到下一轮才报错，Mermaid 静默丢弃 | state_machine.py L168, L349-354 |
| 23 | Mermaid label 不转义换行 | state_machine.py L339 |
| 24 | 事件与版本列表无限膨胀：每步全量 save、每版本全量 deepcopy、checkpoint 内嵌完整 run_context | durable.py L90-117; store.py L302-306; react.py L647 |
| 25 | `_event_cursors` 泄漏（只有 `_tasks` 有 done_callback 清理） | service.py L79, L490 |
| 26 | 异常类型不统一：裸 RuntimeError / KeyError / PermissionError vs 已有领域异常体系 | store.py L27; service.py L166, L425 |
| 27 | compat 契约不一致：`ThreadRecord.user_id="anonymous"` 默认值 vs RunContext 的 None 语义 | compat.py L39-40 |
| 28 | 幂等查询 O(n) 线性扫描 | store.py L43-50 |

### 5.3 模型与工具层（model / tools / mcp / skills）——29 项

**高危**

| # | 问题 | 位置 |
|---|---|---|
| 1 | `json.loads(tc.function.arguments)` 无保护：坏参数抛裸 JSONDecodeError，非 ModelInvocationError，fallback 不接、熔断不计；流式同况却静默置 `{}`——同层两种行为 | providers.py L262 vs L477-479 |
| 2 | `parse_json` 对 `messages.copy()` 首元素 `content +=`：浅拷贝共享可变 Message，调用方 system 消息被永久改写，重复调用不断叠加指令 | providers.py L527-529 |
| 3 | 本地限流/并发排队超时计入 provider 熔断失败数；fallback 每个候选重耗限流配额（N 倍窗口消耗） | governance.py L207-224, L245-264 |
| 4 | 审批回调在 try 外，异常冒泡；`execute_batch` gather 无 return_exceptions，整批失败且兄弟任务失控 | executor.py L211 vs L220, L287-288 |
| 5 | MCP `call_tool` 强制要求 `structuredContent` 为 dict：未返回该字段的 server 一律失败，不回退读 text | mcp/client.py L101-103 |

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 6 | `parse_json` 的 `schema` 参数从未使用，docstring 称"用于验证" | providers.py L509-553 |
| 7 | 多条 system：Anthropic 只留最后一条（静默丢弃），Gemini 拼接——跨 provider 不一致 | providers.py L654-655 vs L914-917 |
| 8 | Gemini 流式按函数名合并 tool_calls：同轮同名调用被并成一个，args.update 可能拼出键并集 | providers.py L1078-1090 |
| 9 | tool 消息 name None 时 function_response 兜底 `"tool"`，与 function_call 不匹配 | providers.py L950-953 |
| 10 | 流式全程占并发名额并整体计时；`call_timeout_seconds` 默认 None＝默认无超时治理 | governance.py L246-255, L47 |
| 11 | 熔断状态不分 scope（单租户故障拖垮全部）；token 估算 `len//4` 忽略 tool_calls 与工具定义，中文低估 2-3 倍 | governance.py L163, L166-168 |
| 12 | to_thread 同步工具超时后线程继续跑（副作用照常）；`iscoroutinefunction` 不识别 partial/`__call__` 对象 | executor.py L224-232 |
| 13 | schema 校验缺口：boolean/array/object 不查、未知参数不拒、TypeError 误归 execution | executor.py L255-273 |
| 14 | MCP 子进程全量 `os.environ` 透传，密钥泄漏面大 | mcp/client.py L147 |
| 15 | `list_tools` 只返回名字，丢 inputSchema/description，无法从 MCP 元数据构建工具定义 | mcp/client.py L76-86 |
| 16 | Skill `replace_tools=True` + ToolRegistry 覆盖仅 warning + `activate(None)` 默认全量激活＝无审计命名劫持通道 | skills/registry.py L103-104, L152-153; executor.py L61-62 |
| 17 | context_usage 向非标准 /tokenizer、/tokenize POST 完整会话+Bearer key：官方必 404（无负缓存反复白打），第三方网关收到会话内容——外泄面 | providers.py L300-339 |
| 18 | context window HTTP 兜底值含 None 实际不缓存，每次重付 8s GET | providers.py L355, L374-375 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 19 | 缺 SDK 错误类型不统一（ModelInvocationError vs 裸 ImportError） | providers.py L177-180 vs L576-581/L854-858 |
| 20 | finish_reason 仅 OpenAI 流式填充，StreamChunk 契约事实上只有 OpenAI 满足 | providers.py L503 vs L828-830/L897 |
| 21 | known_context_window 表极小 + `"qwen3.7-plus": 1_000_000` 疑似笔误 | providers.py L137-149 |
| 22 | `_model_name` 空回退 ""，直到调 API 才报错 | providers.py L1180-1182 |
| 23 | per-scope 限流 deque 只增不删 | governance.py L69-70 |
| 24 | 两套行为不一的 JSON 围栏解析出口（structured.py vs OpenAIProvider.parse_json） | structured.py L46-56 vs providers.py L534-541 |
| 25 | 结构化输出 temperature=0 重试确定性复现；tools 透传引发必然失败的尝试 | structured.py L171-176 |
| 26 | context_usage 完全绕过治理 | governance.py L271-277 |
| 27 | 默认目录模型名与 provider 默认值漂移（gemini-2.0-flash vs gemini-2.5-flash） | catalog.py L38-43 |
| 28 | `ModelProviderFactory = Callable[..., ModelAdapter]` 丢失 kwargs 契约 | core.py L72 |
| 29 | `risk_level` 自由字符串无枚举 | executor.py L26 |

### 5.4 协议、端口与存储层（protocol / ports / storage / hitl / acp / serialization）——31 项

**高危**

| # | 问题 | 位置 |
|---|---|---|
| 1 | PG workflow store JSONB str→ValueError（见 Top10 #1） | storage/workflow.py L88-90, L317-333 |
| 2 | 幂等键对 NULL 作用域失效：SQLite/PG 唯一索引中 NULL 互异，匿名并发重发产生重复 run；`FOR SHARE` 预检查锁不住不存在的行 | storage/runtime.py L266-270, L548-550, L583 |
| 3 | `SqliteRuntimeStore.save_run` 丢失更新：版本检查与写入间无锁无谓词（PG 版 `FOR UPDATE`+`WHERE version=$18` 已修，未对齐） | storage/runtime.py L287-333 vs L566-634 |
| 4 | WorkflowExecutionStore.save 同型盲写，SQLite 与 **PG 均无事务**，version 可重复/倒退 | storage/workflow.py L184-201, L282-315 |
| 5 | HITL 超时覆盖已批准决定（见 Top10 #2） | hitl/core.py L206-224; storage/runtime.py L448, L722 |
| 6 | `ContextStore.update` 允许覆写 `_access` 归属键——用户可把自己的 thread 改挂任意 user/tenant | storage/context.py L110-114, L214-224, L305-317 |
| 7 | `expires_at` 从不设置/检查，崩溃后 pending 审批永不过期、可事后批准作废动作 | protocol/runtime.py L105; hitl/core.py 全文 |
| 8 | ACP 权限应答不校验 optionId ∈ options——越权 client 可注入未声明选项 | acp/server.py L223-227 |
| 9 | ACP `KeyError → "会话不存在"`：backend 任意 KeyError（配置缺键）被误报 | acp/server.py L107-109 |

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 10 | 全部 SQLite 存储事件循环内同步阻塞 IO，无 to_thread（见 Top10 #9） | storage/*.py |
| 11 | SQLite 各存储 PRAGMA 不一致：仅 session 有 WAL、仅 lease 有 30s busy_timeout，同一 sessions.db 等待策略矛盾 | session.py L305-306; lease.py L184 |
| 12 | `SqliteSessionStore._check_access` claim 无 ON CONFLICT，并发首写抛裸 IntegrityError（PG 版有） | session.py L331-334 vs L517-523 |
| 13 | SQLite 幂等撞键抛裸 sqlite3.IntegrityError，无领域错误翻译 | storage/runtime.py L296-333 |
| 14 | `SessionManager.clear` 摘锁竞态：`not lock.locked()` 检查与 pop 无原子性，极端时序同 thread 双锁 | session.py L871-873 vs L754 |
| 15 | `save_run` 隐式第二写：pending_approval 在 run 事务外独立连接落 approvals——非原子 | storage/runtime.py L334-335, L635-636 |
| 16 | `mark_stale_runs` 先读后逐个改写，无事务，与在跑 worker 竞争；N+1 连接；重复调用累积重复 `run_interrupted` 事件 | storage/runtime.py L164-175, L406-420, L671-686 |
| 17 | RunContext.events 随 save 全量重写：长 run O(n²) 累计成本 | protocol/runtime.py L262; storage/runtime.py L331, L629 |
| 18 | MemoryStore.get 的过期删除发生在 `_can_access` **之前**——PermissionError vs None 构成存在性 oracle | storage/memory.py L294-307, L492-501 |
| 19 | SqliteContextStore 先读后写跨连接无事务；SQLite 整文档覆盖 vs PG `data ||` 字段级合并——dev/prod 合并语义不同 | storage/context.py L214-236, L219-224 vs L311-314 |
| 20 | Memory/Sqlite ModelSelectionStore version 读后写，跨进程可重复；仅 PG 原子自增 | storage/model_selection.py L77-87, L176-196 vs L288 |
| 21 | PG save_run 幂等预检查 `FOR SHARE` 对不存在的行无效 | storage/runtime.py L583 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 22 | RuntimeStore 抽象方法 docstring 复制粘贴错误 | storage/runtime.py L46-88 |
| 23 | RunStore Protocol 与 RuntimeStore ABC 双轨不一致；EventSink 无实现；`storage.context.ContextStore` 与端口同名冲突 | ports/storage.py; storage/runtime.py |
| 24 | 工厂返回未初始化的 Postgres 对象，忘 `await initialize()` 得到运行期 RuntimeError | 7 个工厂函数 |
| 25 | `ApprovalRecord.from_dict`/`Message.from_dict` 不校验 status/role 枚举 | protocol/runtime.py L132; messages.py L57 |
| 26 | `_ensure_columns` 历史数据已有重复 idempotency_key 时建唯一索引失败无处理，升级即不可用 | storage/runtime.py L266-270 |
| 27 | SqliteMemoryStore.search 不清理过期行（InMemory 版会清）；无租户谓词下推 | storage/memory.py L343-361 |
| 28 | Message tool_calls 键名契约无约束，不合规时静默产出 `arguments: "{}"` | messages.py L41-45, L61 |
| 29 | ACP 内部异常消息直通客户端、`_sessions` 永不清理、通知失败无日志 | acp/server.py L98-118 |
| 30 | 序列化：无完整性校验（无 HMAC，Blob checksum 从不比对）、payload 键不收敛、`_decode_access` 可从存储铸 admin、超深 JSON RecursionError 未包装、模块级可变单例 | serialization/registry.py L166-212, L290 |
| 31 | `SqliteModelSelectionStore.save` scope="tenant" 时静默丢弃 user_id | storage/model_selection.py L176 |

**租约专项**：三实现 claim 均原子（✔），但无 fencing token（旧 Worker 被接管后业务写路径不受租约拦截）、SQLite ISO 字符串时间比较靠巧合保序、租约表无清理、`heartbeat_at` 不参与判定。 | lease.py L46-47, L205-266

**序列化宣称对照**：防 RCE ✔（白名单+无 pickle+无动态导入）；防篡改 ✘（无签名/checksum 不校验）；域校验 ✘（role/status 枚举不校验）。

### 5.5 横切扩展链与知识层（middleware / compaction / guardrails / observability / callbacks / prompts / documents / retrieval / multi-agent）——27 项

**高危**

| # | 问题 | 位置 |
|---|---|---|
| 1 | `LengthGuard` 接口断裂：`check()` 用 `max_input or max_output` 选限制，`check_input/check_output` 从未被调用——配 max_output 实际按 max_input 执行，输入输出限制互相污染 | guardrails/core.py L59-63, L140-162 |
| 2 | PromptRegistry 静默清空 + 覆盖写：文件损坏时捕获一切异常只记日志→注册表变空→后续 register 用空数据**覆盖整个 JSON**，版本历史永久丢失；且 `_save` 非原子 | prompts/registry.py L205-206, L87, L162-181 |
| 3 | InMemoryVectorStore.delete 部分提交：循环内逐条"校验→删除"，遇无权记录抛异常时之前的已删（pgvector 事务内先全量校验、Chroma 先收集后删——三后端三种故障语义） | retrieval/memory.py L72-80 |
| 4 | Chroma search 全集合拉取：`n_results=count` 取回全部 embeddings+metadatas 再客户端过滤，O(N) 热路径，metadata 过滤零下推 | retrieval/chroma.py L121-130 |
| 5 | 工具失败不可观测：`after_tool` 恒传默认 `success=True`，`ToolRecord.success` 无写入 False 的通道 | observability/core.py L209-213 |
| 6 | 观测统计无界增长：records 列表与 `_stats` 字典无淘汰，模块级单例 `agent_stats` 长驻进程必然膨胀 | observability/core.py L51-126 |

**中危**

| # | 问题 | 位置 |
|---|---|---|
| 7 | 中间件链无异常隔离：钩子抛异常直接打断主流程（对比 CallbackManager 逐订阅者隔离——同一框架两套策略） | middleware/base.py L134-141 |
| 8 | 溢出恢复日志算术错误（多轮压缩/prune-only 时"压缩前条数"算错，仅日志失真） | compaction/middleware.py L258-264 |
| 9 | `_last_window` 实例字段跨并发会话串扰 | compaction/middleware.py L127, L230 |
| 10 | 溢出误判：`"context window"`、`"too many tokens"` 宽泛子串匹配会吞掉非溢出异常并强制压缩重试 | compaction/middleware.py L67-83 |
| 11 | TokenLimit 与 region 的 token 估算口径不一致（前者忽略 tool_calls）；`chars_per_token=4` 是英文口径，中文低估 2-3 倍 | compaction/token_limit.py L36-38; region.py L29-34 |
| 12 | rebuild 静默丢弃中途 system 消息 | compaction/region.py L143-144 |
| 13 | spill LRU 无存活绑定：被消息引用中的 spill_id 可被挤出，回读得到"不存在" | compaction/spill.py L59-62 |
| 14 | spill 回读无访问控制（与检索层 AccessContext 体系反差） | compaction/spill_tool.py L16-21 |
| 15 | emit 双签名（`ctx.emit(event, **payload)` vs `emit_hook(event, payload)`） | compaction/middleware.py L422-426 |
| 16 | 摘要输入不裁剪：会话逼近窗口时摘要调用自身可能溢出，只能靠 CompactionError→prune-only 兜底 | summarizer.py L33-36 |
| 17 | Retry 空响应判定过宽（合法空 assistant 回合也触发退避）；空响应与异常重试共享 `retry_count` 额度 | middleware/base.py L214-232, L253-269 |
| 18 | KeywordRetriever 的 namespace 藏在 metadata 里（与 VectorRecord.namespace 两套约定，漏写静默落 default） | retrieval/memory.py L205 |
| 19 | metadata 过滤语义跨后端不一致：InMemory/Chroma 逐 key 相等 vs pgvector JSONB `@>` 子树包含 | retrieval/_common.py L45-46 vs pgvector.py L194 |
| 20 | KeywordRetriever 评分不可比（命中次数/词数），与余弦分共用 `min_score` 语义 | retrieval/memory.py L213-218 |
| 21 | Callback 分发串行无超时：一个慢 handler 拖住整条事件路径 | callbacks/manager.py L86-105 |

**低危**

| # | 问题 | 位置 |
|---|---|---|
| 22 | OTel span 异常路径可能泄漏（无 try/finally） | observability/core.py L154-161, L194-198 |
| 23 | `setup_tracing` 全局 provider 不可重入 | observability/core.py L270 |
| 24 | embeddings gather 不取消在途批次 | packages/embeddings base.py L88 |
| 25 | Chroma payload 塞整份文档 JSON，长文档成倍放大存储 | chroma.py L194-200 |
| 26 | TextBlobParser 解码异常不包装（风格不一致） | documents/loaders.py L38 |
| 27 | Observability 在 before_model 未执行时记 0ms 假值 | observability/core.py L173-174 |

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
