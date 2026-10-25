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
