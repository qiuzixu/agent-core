# Skill

Skill 把模型指令和工具声明打包成可复用目录。Core 负责校验、注册和激活，不会从清单中动态
导入 Python 代码；应用在激活时显式提供可信函数和 MCP 客户端。

## 目录结构

```text
skills/
  weather/
    skill.json
    SKILL.md
```

`skill.json`：

```json
{
  "name": "weather",
  "description": "查询天气并解释出行影响",
  "version": "1.0.0",
  "instructions_file": "SKILL.md",
  "tools": [
    {
      "name": "lookup_weather",
      "kind": "function",
      "target": "weather_lookup",
      "description": "查询指定城市的天气",
      "parameters": {
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"]
      },
      "risk_level": "read",
      "requires_approval": false,
      "timeout_seconds": 10,
      "idempotent": true
    }
  ],
  "metadata": {"owner": "example"}
}
```

`SKILL.md` 只写模型执行该能力所需的领域指令，不要写密钥、连接地址或面向开发者的安装说明。

## 加载和激活

```python
skills = SkillRegistry()
skills.load_directory("./skills")

tools = ToolRegistry()
activation = skills.activate(
    ["weather"],
    tool_registry=tools,
    functions={"weather_lookup": lookup_weather},
)

agent = ReActAgent(
    llm=model,
    tool_executor=ToolExecutor(tools),
    system_prompt=activation.compose_system_prompt("你是一个出行助手。"),
    tool_definitions=activation.tool_definitions,
)
```

`target` 是绑定键，可以与暴露给模型的 `name` 不同。激活是原子的：任何工具缺少绑定时，
不会向 `ToolRegistry` 留下部分注册结果。

## 声明 MCP 工具

```json
{
  "name": "list_entities",
  "kind": "mcp",
  "server": "cesium",
  "target": "entity_list",
  "description": "列出场景实体",
  "parameters": {"type": "object", "properties": {}}
}
```

激活时提供与 `server` 同名的客户端：

```python
activation = skills.activate(
    ["cesium-scene"],
    tool_registry=tools,
    mcp_clients={"cesium": cesium_client},
)
```

客户端只需满足 `McpToolCaller`：实现 `list_tools()` 和 `call_tool()`。

## 校验规则

- 清单拒绝未知字段、重复 Skill 和重复工具名；
- `instructions_file` 不能越过当前 Skill 目录；
- 工具名必须符合 Function Calling 命名规则；
- `parameters.type` 必须为 `object`；
- MCP 工具必须声明 `server`，Function 工具不能声明 `server`；
- 超时必须大于 0。

生产应用应只从受信目录加载 Skill，并在发布流程中评审 `SKILL.md` 和工具绑定。
