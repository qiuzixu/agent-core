# 零依赖 Agent 运行时内核：架构与三个核心设计

## 一、这是什么

`zero-agent-core` 是一个 Python 3.13 的 Agent 运行时内核，定位是"零外部平台绑定"：不依赖任何托管平台或编排服务，核心包 `dependencies=[]`，模型 SDK、MCP、向量库等全部以 optional extras 形式提供、运行时延迟导入，并有专门的守护测试保证"不安装任何第三方 site-packages 也能完成 import"。这不是反依赖，而是把依赖变成可选项：装了哪个 SDK 就启用哪个能力，一个都不装也能把内核完整跑起来。

功能上覆盖一个 Agent 应用运行时的完整闭环：ReAct 循环、工具调用（Function Calling）、MCP 客户端、声明式 Skill、上下文压缩、持久化恢复、持久化工作流、HITL（人在回路审批）、多模型适配（OpenAI / Anthropic / Gemini / Ollama）、ACP 协议。README 列出 20 组主要能力，`docs/CAPABILITIES.md` 用 22 个模块的表格逐条核对"宣称 vs 已实现"，不允许规划中的能力写成已实现。

边界也很清楚：Core 不含任何业务，不含 HTTP/API 层、认证、业务提示词。它是一个内核，不是全家桶。

## 二、架构：分层与三级存储

架构上只有一条主轴：**依赖倒置，方向恒定向内**。

```
protocol / ports / access   ← 消息、RunContext、RunEvent、8 个存储端口
        ↑
runtime                     ← ReActAgent（纯循环，无持久化依赖）+ AgentRuntime（生命周期门面）
        ↑
middleware 扩展链            ← 审批、压缩、Guardrails、可观测性，五切面钩子
        ↑
model / tools / mcp / skills ← Provider 注册表 + 治理 + 统一工具调度 + 声明式 Skill
        ↑
storage                     ← 每个端口三级实现：Memory（测试）/ SQLite（开发）/ PostgreSQL（生产）
```

**三级存储是纪律，不是摆设。** 每个存储端口（Run、Session、Context、Memory、Approval、Workflow、Lease、ModelSelection）都强制提供三种实现：Memory 用于测试（快、无 IO、确定性），SQLite 用于本地开发，PostgreSQL 用于生产多实例。禁止反向依赖：`storage` 实现端口，`runtime` 只认端口。这意味着业务应用在测试里跑内存实现、在笔记本上跑 SQLite、上生产换连接串，代码零改动。

三级同时并存的另一个收益是测试矩阵：同一条业务路径可以在三种后端上重复验证，任何一个后端的实现漂移都会在持续集成里暴露，而不是等到生产环境。

## 三、三个核心设计

框架代码里大部分是体力活，真正有含量的是并发与崩溃恢复。以下三个设计是内核中含金量最高的部分，代码片段均取自当前源码。

### 3.1 工作流幂等键：跨恢复的序列号

持久化工作流最阴险的问题是：**节点重试和进程崩溃恢复，必须共用同一个幂等键，否则幂等失效**。如果幂等键里带 `attempt`（第几次尝试），重试没问题，但崩溃恢复后 attempt 计数从零开始，键就变了——外部副作用会执行第二次。

内核的做法是把键设计成 `{execution_id}:{已完成节点数+1}:{node_name}`，其中"已完成节点数"以恢复时刻的持久化快照为基准（`workflow/durable.py` L83-87）：

```python
completed_offset = execution.steps_completed   # 恢复时从存储读出的已完成步数

def idempotency_key(node_name: str, completed_nodes: int) -> str:
    sequence = completed_offset + completed_nodes + 1
    return f"{execution.execution_id}:{sequence}:{node_name}"
```

这样键只取决于**业务位置**（第几个节点），不取决于**尝试次数**。同一个节点重试三次，键不变；进程崩溃后 `resume` 回来接着做同一个节点，键还是不变。幂等键再通过 `ContextVar` 透传给节点函数（`state_machine.py` 的 `current_node_execution()`），业务代码在写外部系统时可以取到它，把"工作流层的幂等"一路带到"业务副作用的幂等"。

### 3.2 HITL：审批状态机的 CAS

审批有四个状态：`pending → approved / rejected / expired`。麻烦在于三方竞速：用户点批准、审批超时、Worker 恢复流程，可能同时发生。用"先读后写"会丢决定：超时扫描读到 pending，写 expired；与此同时用户的 approved 已经落库，被 expired 覆盖——用户批准了一个已经死掉的请求。

所以所有状态转换必须是**条件更新**（CAS）：只有当数据库里仍是 `pending` 时才允许转换，转换失败就去读真实终态。`hitl/core.py` L375-391：

```python
async def _transition(self, record: ApprovalRecord) -> bool:
    """执行审批 CAS；兼容尚未升级条件更新接口的外部 Store。

    内置 Memory/SQLite/PostgreSQL Store 的 ``transition_approval`` 是原子条件更新。
    未实现该接口的外部 Store 会退化为 load→check→save，跨进程并发下存在
    last-writer-wins 窗口；生产环境应让外部 Store 实现 ``transition_approval``。
    """
    if self._approval_store is None:
        return False
    transition = getattr(self._approval_store, "transition_approval", None)
    if transition is not None:
        return bool(await transition(record, expected_status="pending"))
    current = await self._approval_store.load_approval(record.approval_id)
    if current is None or current.status != "pending":
        return False
    await self._approval_store.save_approval(record)
    return True
```

两个细节：其一，`wait_for_decision` 里连"持久化 deadline"这种看似无害的写（pending → pending）也走 CAS——否则它会覆盖掉刚好落库的用户决定；其二，超时收口时如果 CAS 失败，**以持久化终态为准**，而不是硬报 timeout。用户决定和 deadline 撞在同一个瞬间，赢的必须是用户的真实意愿。

### 3.3 租约：三种后端，三种原子做法

多 Worker 部署时，同一个 Run 必须同一时间只被一个 Worker 执行。租约端口的三个实现各用了各自后端最地道的原子原语（`storage/lease.py`）：

- PostgreSQL：条件 upsert + `RETURNING`，一条语句完成"认领或失败"（L335-344）：

```sql
INSERT INTO agent_run_leases
(thread_id,run_id,worker_id,lease_expires_at,heartbeat_at)
VALUES ($1,$2,$3,NOW()+($4::double precision * INTERVAL '1 second'),NOW())
ON CONFLICT(thread_id,run_id) DO UPDATE SET
worker_id=EXCLUDED.worker_id,
lease_expires_at=EXCLUDED.lease_expires_at,
heartbeat_at=EXCLUDED.heartbeat_at
WHERE agent_run_leases.lease_expires_at<=NOW()
   OR agent_run_leases.worker_id=EXCLUDED.worker_id
RETURNING thread_id
```

- SQLite：`BEGIN IMMEDIATE` 写锁内读-判-写；
- 内存实现：`asyncio.Lock` 包住整个 check-and-set。

续租更严格：更新条件是当前 `worker_id` 匹配、且 `lease_expires_at>NOW()`——只有当前所有者、且租约尚未过期才能续。过期 Worker 的续租静默失败，它应该自己停下来；其他 Worker 通过 `list_expired()` 扫到过期租约后接管。配一个后台心跳线程定期 `renew()`，Worker 崩溃后租约自然过期，Run 就能被自动恢复——不需要人工清理。

## 四、安装与快速上手

```bash
pip install zero-agent-core
```

要求 Python 3.13。不装任何 extras 即可离线运行；要接真实模型时按需安装，例如 `pip install "zero-agent-core[openai]"`（安装包名是 `zero-agent-core`，导入名保持 `agent_core` 不变）。

最小示例只需要三样东西：一个模型适配器、一个工具注册表、一个 ReAct 循环：

```python
import asyncio
from agent_core import ReActAgent, ToolExecutor, ToolRegistry

def add(a: int, b: int) -> int:
    """返回两个整数之和。"""
    return a + b

registry = ToolRegistry()
registry.register(
    "add", add, "计算两个整数之和",
    parameters={
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    },
)

agent = ReActAgent(
    llm=DemoModel(),  # 或 create_model_provider(config) 接入真实模型
    tool_executor=ToolExecutor(registry),
    system_prompt="你是一个演示助手。",
    tool_definitions=registry.build_tool_definitions(),
)

asyncio.run(agent.run("帮我计算 20 + 22", thread_id="demo"))
```

完整的单文件示例见 `examples/starter/starter_agent.py`：离线可运行、不需要任何 API Key，依次演示 `run`（阻塞式）、`stream`（流式）和一个 `requires_approval=True` 写工具的审批回调，并演示了如何用 `FileCheckpointer` 把会话状态落盘、跨进程恢复。换真实模型只需把 `DemoModel()` 换成 `create_model_provider(config)`，其余代码零改动。更多示例（自定义模型、持久化工作流）在 `examples/` 目录，系统文档在 `docs/guide/`。

## 五、当前限制

项目处于 0.x 阶段，以下是当前的客观限制清单：

- **租约没有 fencing token**：旧 Worker 被接管后，业务写路径不受租约拦截，极端时序下可能双写。多实例上线前必须补上。
- **PostgreSQL 集成测试待补**："有 PostgreSQL 代码"不等于"通过真实数据库联调"，存储层的 PG 实现尚未覆盖端到端测试。
- **部分子系统测试待加强**：如 compaction 子系统目前测试覆盖接近于零。
- **外部 Store 的 CAS 退化**：未实现 `transition_approval` 接口的外部审批 Store 会退化为 load→check→save，跨进程并发下存在 last-writer-wins 窗口（见 3.2 源码注释），生产环境应实现该接口。
- **边界**：Core 不含业务逻辑、HTTP/API 层、认证、业务提示词。

## 仓库

- GitHub：<https://github.com/qiuzixu/agent-core>
- PyPI：

```bash
pip install zero-agent-core
```

欢迎 issue 和 PR，尤其是跨后端契约测试矩阵方向的贡献。
