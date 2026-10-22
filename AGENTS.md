# Agent Core 维护与抽离规则

本文件约束 `agent-core`、`my-cesium-agent-vanilla` 与上层 Agent 之间的模块归属。改代码前先判断能力属于哪一层，不能因为“代码在 vanilla 里写着”就直接搬进 Core。

## 1. 能力抽离准入

只有同时满足以下条件，能力才允许进入 `agent-core`：

1. 与具体业务域无关，任何新 Agent 都可能复用。
2. 不包含低空、航线、空域、无人机、Cesium、Nominatim、审批业务规则等业务语义。
3. 不包含 HTTP/FastAPI 路由、CLI 入口、前端页面或项目专属配置。
4. 不读取项目 `.env`，不依赖 `get_config()` 或某个 Agent 的 Settings。
5. 能给出稳定的通用数据契约和至少一个默认实现。
6. 不反向依赖上层应用；只依赖 `protocol`、标准库和明确允许的可选依赖。

只满足“现在 vanilla 用得到”还不够；必须回答“另一个 Agent/另一个业务项目是否也应该用”。

## 2. 目录职责

| 目录 | 职责 | 允许放 | 禁止放 |
| --- | --- | --- | --- |
| `protocol/` | Core 与上层共享的数据契约 | Message、RunContext、RunEvent、ToolResult、GraphEntity、GraphRelation | HTTP schema、业务 DTO、可替换服务接口 |
| `ports/` | 可插拔能力契约 | Store、Sink、Queue、ConfigCenter 等 Protocol | 具体实现、业务规则、FastAPI、项目配置 |
| `storage/` | 内置存储与默认实现 | Memory / SQLite / PostgreSQL 等实现 | 业务实体投影、HTTP API、某个 Agent 专属流程 |
| `middleware/` | 通用执行横切能力 | 缓存、限流、审计、超时、追踪 | 低空安全规则、业务校验、地图控制 |
| `runtime/` | 通用 Agent 执行内核 | ReAct、Run、事件、checkpoint 调度 | 某个 Agent 的意图解析、业务路由 |

`protocol` 定义“数据长什么样”；`ports` 定义“Core 需要什么能力”；`storage` 提供“默认怎么实现”。

## 3. 明确禁止进 Core 的内容

以下内容必须留在具体 Agent 项目：

1. `route`、`vehicle`、`airspace`、`flight_plan`、`landing_site` 等业务实体。
2. `HAS_START`、`EXECUTES`、`CROSSES_AIRSPACE` 等业务关系。
3. `/graph/*`、`/memory/*`、`/tasks/*`、`/approvals/*` 等 HTTP 路由。
4. 项目配置解析、`.env` 读取、租户默认值、角色默认值。
5. 业务事件到 Core 数据结构的投影，例如“一次航线查询 → route/place/vehicle 实体关系”。
6. UI、SDK、部署、运维脚本和 Agent 品牌逻辑。

Core 可以保存通用的实体和关系，但不能知道某个实体在业务上意味着什么。

## 4. 抽离流程

新增或迁移一个 Core 能力时按以下顺序执行：

1. 写出能力边界：哪些字段属于通用数据，哪些字段属于业务投影。
2. 先在 `ports/` 定义 Protocol 和数据契约。
3. 再在 `storage/` 或对应目录提供至少一个默认实现，优先 `InMemory*`。
4. 为 Core 能力补单元测试，覆盖正向、边界和失败路径。
5. 更新 `docs/CAPABILITIES.md`；涉及模块边界时更新 `docs/ARCHITECTURE.md`。
6. 上层 Agent 只保留适配层、业务投影、配置和 HTTP API。
7. 保持上层既有导入路径和对外 API 行为兼容；如需兼容层，必须注明“仅兼容旧导入路径”。
8. 如果该平台能力也要求 LangGraph 端具备，则在 `my-cesium-agent` 中独立实现同等对外行为，不得让它依赖 `zero-agent-core`。

## 5. 依赖边界

1. `agent-core` 不得依赖 LangChain、LangGraph、FastAPI、Cesium 或低空业务模型。
2. `my-cesium-agent` 不得依赖 `zero-agent-core`。
3. `my-cesium-agent-vanilla` 不得引入 LangChain / LangGraph。
4. 可选第三方依赖必须放在对应 extra 中，并在实现里延迟导入。
5. 通用接口不得因某个外部系统特化；外部系统差异通过 adapter 解决。

## 6. 提交与验证要求

修改 Core 时必须完成：

1. `ruff check`、`mypy` 和相关 `pytest` 通过。
2. 新增公共能力有 Core 测试，并有上层接入测试。
3. 公共能力更新 `docs/CAPABILITIES.md`。
4. 模块边界、公共接口、依赖方向、存储对象或协议变化时更新 `docs/ARCHITECTURE.md`。
5. 提交说明写清“通用能力”和“业务接入”分别在哪里。

不满足以上要求的 Core 改动应先停止，回到业务层实现。


## 注释与文档语言规范

1. 模块 docstring、方法/类 docstring、行内注释、日志消息、错误消息：**一律使用中文**。
2. 以下内容保持英文：标识符（变量/函数/类名）、类型注解、import 路径、URL、SQL 关键字。
3. 兼容原因需要保留英文原文时，放在中文说明之后，以 "Original:" 标注。
4. 新增代码不允许出现英文 docstring 或英文注释（标准库/第三方装饰器参数除外）。
