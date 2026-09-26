# 贡献指南

感谢你参与 Handwritten Agent Core。提交改动前，请先确认问题属于框架通用能力，
还是某个应用 Agent 的业务能力。业务工具、HTTP 路由和产品页面应留在应用项目中。

## 开始开发

项目要求 Python 3.13。推荐使用 `uv`：

```bash
uv sync --extra dev --extra models --extra mcp --extra production
uv pip install build twine
python -m pip install mkdocs-material
```

也可以使用标准 `venv` 和 `pip`：

```bash
python -m venv .venv
python -m pip install -e ".[dev,models,mcp,production]"
python -m pip install build twine mkdocs-material
```

运行检查：

```bash
ruff check src tests examples
pytest
python -m mkdocs build
python -m build
```

严格类型检查可用 `mypy src` 运行。当前仓库仍有记录在
`OPEN_SOURCE_CHECKLIST.md` 的历史类型债务，因此它暂不作为合并阻塞项。

## 修改约束

- 公共 API 优先从 `agent_core` 顶层导出，并为行为变化补充测试。
- 修改 `src/agent_core/**` 后，检查并更新 `docs/ARCHITECTURE.md` 的架构图或同步记录。
- 新增公共能力时，同步更新 `docs/CAPABILITIES.md` 和对应使用指南。
- 注释和架构说明优先使用中文；公共类型名、协议名与行业术语保留英文。
- 不在提交中包含密钥、真实业务数据、数据库文件或构建产物。
- 兼容性破坏需要在变更说明中给出迁移方式，并按语义化版本提升主版本或次版本。

## 提交和合并请求

提交信息推荐使用 Conventional Commits，例如：

```text
feat: 增加自定义模型适配器注册能力
fix: 恢复中断运行时释放租约
docs: 补充 MCP 接入示例
```

合并请求应说明问题、最终行为、兼容性影响和验证命令。一个合并请求尽量只解决一个主题。
安全问题不要创建公开 Issue，请按 [安全策略](SECURITY.md) 报告。

提交贡献即表示你有权提交这些内容，并同意按项目的 Apache-2.0 许可证发布。
