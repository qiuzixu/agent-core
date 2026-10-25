# Compaction（上下文压缩）模块走读

> 记录时间：2026-10-24。本文是模块设计讨论的沉淀，覆盖：为什么压缩、
> 滚动合并策略、摘要持久化（跨轮复用）、与 deepseek-harness 的对齐关系。

## 1. 模块在全局链路中的位置

```
用户第 N 轮提问
  ① SessionStore.load(thread_id)         ← 从 DB 读全部历史消息
  ② SessionManager._build_context        ← 历史压成 memory_context 字符串
  ③ ReActAgent.run(memory_context=...)   ← memory 折叠进首条 user 消息
  ④ ReAct 循环（每轮模型调用前）：
       CompactionMiddleware.before_model  ← 窗口占用达阈值则压缩
       TokenLimitMiddleware               ← 压缩失败时的硬截断兜底
  ⑤ 回复后 append 写回 DB
```

两层压缩职责不同：

| 层 | 谁负责 | 管什么 |
|---|---|---|
| 跨轮历史 | `SessionManager`（截断 + 摘要复用） | 多轮对话累积 |
| run 内膨胀 | `CompactionMiddleware` | 一次 run 内多轮工具调用 |

## 2. 压缩触发机制

`before_model` 每次模型调用前：

1. 计数（Provider 精确 tokenizer 优先，字符估算兜底）
2. 未达阈值（默认 75%）→ 放行
3. 达阈值 → 先瘦身超长工具输出（零成本），再压摘要
4. 模型真报"上下文超长" → `handle_exception` 无视阈值强制压一轮重试

任何一步失败都返回 CONTINUE 交给硬截断兜底，不阻断主流程。

## 3. 滚动合并（不是分段压缩）

**任何时刻只有一份有效摘要**，像滚雪球演化：

```
[摘要S₁] + [增量原文]
      ↓ 增量攒多了
[摘要S₂] = 合并(S₁, 增量)     ← S₂ 生成时能看到 S₁ 的内容
      ↓ 又攒多了
[摘要S₃] = 合并(S₂, 新增量)
```

对比分段压缩（多个摘要并行、模型看拼盘）的劣势：分段之间割裂，
跨段关联（第 5 轮和第 70 轮提到同一架飞机）容易丢。滚动合并把
旧摘要当作新摘要的输入，关联信息一直在链上传递。

## 4. 摘要整合策略（固定结构模板 + 全量重述）

摘要是"重述"不是"拼接"。`build_summarization_messages` 把三样东西
拼给摘要模型：

```
[system]                    ← 原 system prompt（角色不变）
[旧摘要 checkpoint]          ← 滚动基线，作为普通消息混在头部里
[被压的原文消息]              ← 上一轮摘要之后积累的新对话
[user] 压缩指令               ← 固定模板
```

### 结构模板（六节，一节都不能少）

```markdown
## 任务目标
## 关键事实与实体      ← 编号、名称、ID
## 已执行动作及结果    ← 调过什么工具、结果如何
## 决策与约束          ← 用户纠正、限制条件
## 当前状态
## 待办与下一步
```

`normalize_summary` 结构校验：
- 一节都没有 → 判定失败，拒绝写入（防无效摘要污染历史）
- 缺个别节 → 自动补 `(none)`，保证下次滚动合并结构稳定

### 保真约束（指令内置）

- 保留精确编号、时间、数值、标识符和用户纠正，不要编造
- 不要提及压缩过程（避免模型把"我被压缩过"写进摘要）

### 质量门禁

- **必须更小**：摘要 token ≥ 原文 token → 放弃，降级 prune-only
- **必须合规**：结构不合规 → CompactionError → 降级

### 领域可定制

模板可注入（`summary_instruction` / `summary_sections`）。
低空 Agent 可加"飞行器状态 / 航线 / 审批"专属小节；
Core 默认只保留通用六节。

## 5. 打标机制（影子化）

摘要靠两层标记运转：

### 消息级标记（影子边界）

```python
Message(
    role="user",
    name="compaction_checkpoint",   ← 消息级"标"
    content="前言…<compacted-summary>摘要正文</compacted-summary>",
)
```

| 环节 | 用标做什么 |
|---|---|
| `_build_context` | 找最新标 → 从这里开始拼，之前原文影子化 |
| `max_history` 裁剪 | 裁剪不越过标，不丢压缩成果 |
| 再次压缩 | 认出旧摘要 → 滚动合并，不当待压原文 |
| 去重 | 同 run 内重复压缩不重复写库 |

### 内容级标记（正文包裹）

`<compacted-summary>` / `</compacted-summary>` 包裹摘要正文，
`_extract_summary_body` 靠它分离"前言说明"和"正文"——喂给模型
的是正文，不是整个包装。

## 6. 摘要持久化（2026-10-24 升级，对齐 deepseek-harness）

### 升级前的问题

压缩只发生在内存：run 结束摘要蒸发，下一轮全量重查重压，
同样内容反复付摘要 LLM 的钱。

### deepseek-harness 的参照

它的压缩是持久化事务，写进 session 事件流：

```
compaction/start    ← 加锁
compaction/summary  ← 摘要 + shadowedRange + 模型 + token 用量
user/message        ← 表面层替换：携带摘要的影子消息取代旧区间
compaction/end      ← 释放锁
```

摘要节点写回会话历史本身 → 下一轮从 ~60% 继续增长，摘要永不重算。

### 我们的实现

```
CompactionMiddleware(persist_summary=回调)   ← agent-core 侧新增参数
        │ 压缩成功后自动调用
        ▼
回调（deps.py 注入）→ store.append(thread_id, [checkpoint 消息])
        │
        ▼
下一轮 SessionManager._build_context 识别最新标 → "摘要 + 增量"
```

防护措施：

- 无摘要 / prune-only 不写
- 相同摘要文本去重（同 run 多次触发）
- 写库失败只记日志，不阻断模型调用
- `max_history` 裁剪永不越过最新摘要

### 上下文占用曲线变化

| | 升级前 | 升级后 |
|---|---|---|
| 每轮上下文 | 全量历史重新截断 | 最新摘要 + 增量 |
| 摘要 LLM 调用 | 每次触发都调 | 同一区间只调一次 |
| 曲线 | 每轮从全量爬升 | 90% → 60% → 61% → …（锯齿形） |

## 7. 数据不删原则

压缩/影子化永远不删 DB 原文：

- `sessions` 表保留完整流水（含退役旧摘要）
- 审计、回溯、前端历史展示随时可查
- "影子化"只是不进模型上下文，不是删除

## 8. 已知边界

- LangGraph 端（my-cesium-agent）暂未对齐摘要落库语义，可作后续任务
- 压缩明细（before/after tokens、cut）目前在日志/事件里，
  若要 deepseek-harness 级别的事件流审计，可把明细编码进消息
  或单独存压缩事件
- 跨轮历史的 memory_context 是字符串折叠（每条助手回复截断 300 字），
  与 run 内的消息数组压缩是两套粒度

## 9. 关键代码索引

| 文件 | 内容 |
|---|---|
| `compaction/middleware.py` | 触发判断、压缩主流程、溢出接管、持久化回调 |
| `compaction/region.py` | 切点选择（尾部预算 + 工具配对平衡）、checkpoint 构建 |
| `compaction/summarizer.py` | 摘要调用、结构校验（六节模板） |
| `compaction/prompts.py` | 默认指令与章节（可注入领域模板） |
| `compaction/pruner.py` | 工具输出瘦身 + spill 回读 |
| `compaction/spill.py` | 被截断工具输出的本地暂存 |
| `storage/session.py` | `_build_context` 摘要复用、裁剪保摘要 |
| `tests/test_compaction_core.py` | 切点/瘦身/中间件阈值测试 |
| `tests/test_compaction_persistence.py` | 持久化/复用/裁剪保标测试 |


## 10. 开源框架压缩机制调研（2026-10-25）

调研对象：`D:/ttzx/open-agent/` 下五个开源 Agent 的压缩实现。
结论先行：五家全部是“LLM 摘要式压缩”，没有一家只做截断；差异集中在
触发校准、摘要整合策略、失败防御和持久化语义上。

### 10.1 各家机制速览

#### Codex（Rust，codex-rs/core/src/compact*.rs）

- 压缩 = 一个独立 turn：全量历史 + 摘要 prompt 作为一次请求
- 触发：AutoCompactWindow 窗口链（UUIDv7 串 first/previous/window_id，
  window_number 递增）+ 手动 /compact
- 摘要请求自身爆上下文 → 从头部删最老历史项重试（保住前缀缓存）
- 摘要整合：保留用户原话（事实记录），剔除旧摘要，只压缩 assistant/tool 内容
- pre-turn/manual 压缩后下一轮完整重注入初始上下文；mid-turn 则插到
  最后一条 user 消息上方
- PreCompact/PostCompact 钩子可中断；完整 CompactionEvent 埋点
  （trigger/reason/phase/status/前后 token/缓存 token）

#### Claude Code（TS，services/compact/）

- 触发：有效窗口 = 窗口 − min(maxOutput, 20K 摘要预留)；阈值再减 13K 缓冲
- 熔断器：连续失败 3 次停止自动压缩（注释记录了真实事故：
  1279 个会话每天浪费 25 万次 API 调用）
- 摘要请求 prompt too long → 截掉最老 API 轮次组重试（最多 3 次）
- fork 子 agent 生成摘要（共享 prompt cache，省缓存写入）
- microCompact 独立机制：老工具结果（Read/Bash/Grep/WebFetch 等）原位替换
  为 `[Old tool result content cleared]`，保留最近 5 条，零 LLM 成本
- 压缩后状态重建最重：重建文件附件、plan mode、skills、MCP 指令、
  延迟加载工具的广播

#### ZCode（TS，apps/zcode-cli/packages/core/src/compact/）

- 结构同源 Claude Code（常量一一对应），三处改良：
  1. token 决策来源可切换：`estimate`（兜底）/ `provider_usage`（优先，
     含 cache read/write 真实值）
  2. token 估算把工具入参 JSON 计入（大参数工具纯文本估算会漏掉）
  3. 压缩是正式 turn：TurnStarted/TurnComplete 事件流 + usage 观测 +
     boundary payload（summarized/kept 数量、preservedSegment、
     lastSummarizedMessageId）完整审计
- microcompact：阈值 = 自动压缩阈值 × 0.9 或 −2K 取小，最小节省 256 token

#### Zoo-Code（TS，src/core/context-management/ + condense/）

- 触发：contextPercent ≥ 配置阈值（5%~100%，按 profile 覆盖）或超
  90% 硬线（窗口 × 0.9 − 输出预留）
- LLM 浓缩失败 → 降级为滑动窗口截断（0.5 比例）
- 非破坏性设计（独家）：
  - 浓缩：旧消息全部打 `condenseParent` 标签（不删除），追加 `isSummary`
    摘要消息，生效历史 = 只有摘要（fresh start）
  - 截断：打 `truncationParent` 标签 + 插入截断标记，第一条消息必留
  - 用户 rewind 到压缩点之前时孤儿标签被清理，被压缩的消息可恢复
- 摘要前兼容处理：tool_use/tool_result 转纯文本（Bedrock 不带 tools
  参数也能摘要）、孤儿 toolCall 补合成结果、文件 tree-sitter 折叠

#### pi（open-code/pi，packages/coding-agent/src/core/compaction/）

- 触发：contextTokens > 窗口 − 16384；token 锚点机制：最后一条
  assistant 的 provider 真实 usage 做基数，之后的新消息才 chars/4 估算
  （图片按 4800 字符计）
- 滚动合并显式规则（与我们同款但更成熟）：有旧摘要时换 UPDATE prompt，
  规则写死——PRESERVE 所有已有信息、ADD 新进度、UPDATE Progress 分区、
  保留精确文件路径/函数名/错误信息；结构化六分区
  （Goal/Constraints/Progress/Key Decisions/Next Steps/Critical Context）
- 摘要防御性校验（五家最严格）：
  - `stopReason: length` → 拒绝落库（截断摘要是不完整摘要，宁败不落脏数据）
  - 摘要响应含 toolCall → 报错（模型试图继续干活而不是总结）
  - LLM 调用套统一 retry policy
- 切点算法（独家）：保留最近 20K token，切点只能落在 user/assistant/
  turn 开始处，永不切在 toolResult 上；切在 turn 中间时拆成
  “前缀摘要 + 后缀保留”两段（split turn），前缀单独小预算摘要后拼进主摘要
- 文件操作追踪：从工具调用提取 readFiles/modifiedFiles（累积跨压缩），
  追加到摘要尾部——压缩后模型立刻知道文件状态
- 分支摘要：session 树状结构，切换分支时给被离开的分支生成摘要
- 缓存策略：摘要请求 cacheRetention="none"，多次摘要 usage 合并统计

### 10.2 五方对比表

| 维度 | Codex | Claude Code | ZCode | Zoo-Code | pi | 本模块 |
|---|---|---|---|---|---|---|
| 触发 | 窗口链 | 窗口−20K−13K | 同CC+usage校准 | 百分比阈值 | usage锚点+尾部估算 | 阈值比例 |
| 摘要整合 | 保用户消息 | 全量重摘 | 全量重摘 | fresh start | 滚动合并+结构化 | 滚动合并+六节模板 |
| 切点保护 | 删最老重试 | 截轮次重试 | 保留段 | 第一条必留 | turn完整性+split-turn | 尾部预算+工具配对 |
| 失败防御 | hook可中断 | 3次熔断 | 3次熔断 | 降级截断 | length拒绝+toolCall校验 | 结构校验+硬截断兜底 |
| 工具结果 | — | microCompact | microCompact+入参计token | 转text进摘要 | — | 瘦身+spill分页 |
| 持久化 | rollout | transcript | session事件 | condenseParent打标 | CompactionEntry+指针 | compaction_checkpoint落库 |

### 10.3 借鉴升级点（按优先级）

1. **P1 摘要防御性校验**（学 pi）：`length` 截断的摘要拒绝落库；
   摘要响应含 toolCall 视为失败重试。目前我们只有结构校验（六节模板），
   这两条是增量，成本低收益高
2. **P1 token 锚点**（学 pi）：最后一条 assistant 真实 usage 做基数 +
   尾部估算，替代全量估算，决策更准
3. **P1 压缩失败熔断**（学 Claude Code/ZCode）：连续 3 次失败停止自动
   压缩，防止每轮都对着爆掉的上下文撞墙
4. **P2 token 决策用 provider usage 校准**（学 ZCode）：estimate 为兜底、
   usage 优先的 override 模式
5. **P2 microCompact**（学 Claude Code/ZCode）：老工具结果原位替换占位符，
   与 spill 互补（spill 管“可找回”，microCompact 管“零成本瘦身”）
6. **P2 文件/实体操作清单入摘要**（学 pi）：摘要尾部附“读过/改过哪些文件”，
   低空场景可类比为“查过哪些航线/实体”
7. **P3 压缩埋点**（学 Codex）：trigger/前后 token/耗时的事件记录

### 10.4 结论修正

- 滚动合并并非本模块独家：pi 是同款且更成熟（显式 UPDATE 规则 +
  防御校验 + 结构化分区），但“摘要落库复用 + 裁剪从摘要起点构建上下文”
  的完整链路已对齐 pi，差距在防御校验和 token 锚点
- 我们的独家优势：spill 分页回读（五家都没有按页取回被截断工具输出的机制）
- 本模块已对齐的：六节结构模板（≈pi 的结构化分区）、结构校验拒写
  （≈pi 的防御思想）、尾部预算切点（≈pi 的切点算法，但缺 turn 完整性约束）
