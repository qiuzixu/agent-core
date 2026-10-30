# Skills（技能清单）模块总览

> 位置：`agent-core/src/agent_core/skills/` · 对外出口：`agent_core.skills`

## 一句话定位

Skill 是“说明书 + 接线图”，不是任意代码插件。它声明一个能力叫什么、模型该怎么理解、
能绑定哪些 Function 或 MCP 工具；真正的执行仍然由 `ToolRegistry` 和 MCP client 完成。

## 整体链路

```
skill 目录 / skill.json
   │
   ▼
SkillLoader（严格校验）
   · 名称 / 版本 / 描述校验
   · 未知字段直接拒绝
   · instructions_file 防路径穿越
   · 工具参数做最小 JSON Schema 校验
   · 同一 Skill 内重复工具名拒绝
   │
   ▼
SkillRegistry
   · load / load_directory / register / unregister
   · check_for_changes / reload_changed（可选热重载）
   │
   ▼
activate()
   ├─ function 工具 ──► functions[target]
   ├─ mcp 工具      ──► mcp_clients[server].call_tool(target)
   └─ 两阶段原子绑定：先检查全部冲突和依赖，再批量写入 ToolRegistry
       │
       ▼
SkillActivation
   ├─ instructions ──► compose_system_prompt() 拼进系统提示词
   └─ tool_definitions ──► OpenAI Function Calling 格式
```

## 关键设计

### 1. 只声明，不执行代码

Skill 清单不会动态导入 Skill 作者写的 Python 文件。它可以声明工具绑定，但不能自带
任意执行逻辑。这是最小攻击面设计：加载一个 Skill 不会等于执行一个插件。

### 2. 未知字段直接拒绝

`skill.json` 里的未知字段不会“忽略后继续”，而是直接抛出 `SkillLoadError`。
这样可以尽早发现拼写错误，也避免某个新字段被误当成安全开关。

### 3. 指令文件不能越过 Skill 目录

`instructions_file` 会先 resolve，再确认仍在 Skill 目录内，防止通过相对路径读取
Skill 目录外的文件。

### 4. 激活是两阶段原子操作

`activate()` 先收集所有绑定，检查：

- Skill 之间是否重复声明同名工具
- 工具是否和现有 ToolRegistry 冲突
- function target 是否存在且可调用
- MCP server 是否已提供

全部通过后才批量注册。因此缺一个绑定不会造成“一半工具已注册”的半成品状态。

### 5. 热重载不吞错

2026-10-30 升级后：

- `register()` / `unregister()` 同步维护 manifest mtime 基线；
- `check_for_changes()` 首次调用只建立基线，之后返回实际变化；
- `reload_changed()` 先加载候选 Skill，成功后才替换旧 Skill；
- 清单损坏时保留旧版本，并输出 `WARNING` 日志；
- 成功重载时记录版本变化，例如 `1.0.0 -> 2.0.0`。

## vanilla 接线

vanilla 应用中有两条 Skill 注入路径：

1. 启动时静态激活：`skill_runtime.activate_skills()` 读取 `skills_directory` 和
   `enabled_skills`，只激活配置里显式启用的 Skill。
2. 管理面板动态注入：`AdminState` 里的 SkillRegistry 和动态 MCP 连接池由
   `bind_runtime_tools()` 合并进当前 Agent 的 ToolRegistry，并拼接系统提示词。

同名覆盖白名单：Skill 想复用已有同名工具，必须满足 `kind == "function"` 且
`target == name`。否则拒绝，避免 Skill 静默替换业务工具或把 MCP 调用伪装成本地函数。

动态 MCP 同名冲突：管理面板 MCP 工具进入运行时注册表时使用 `replace=False`；
如果和本地 Function Calling、静态 Skill 或其他已有工具同名，会抛出
`ToolRegistrationError`，不会静默覆盖。

## 与 LangGraph 端的关系

LangGraph 端有独立的 `low_altitude_agent.skills` 实现，保持对外行为一致：清单加载、
激活、MCP 绑定、提示词注入和工具转换。热重载属于框架内部机制，LangGraph 端当前不暴露
该能力，因此属于可单侧演进的框架细节。

## 配置

vanilla 相关配置：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `skills_directory` | `../skills` | Skill 清单根目录 |
| `enabled_skills` | 空 | 逗号分隔；为空时启动阶段不激活任何 Skill |

管理面板动态注册的 Skill 会进入持久化表；SQLite / PostgreSQL 后端由环境自动选择，
重启后通过 `restore_from_persistence()` 恢复。
