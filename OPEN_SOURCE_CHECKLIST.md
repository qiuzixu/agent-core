# 开源发布检查清单

本清单区分仓库内已经准备好的内容，以及首次公开发布前必须由维护者确认的外部事项。

## 仓库内容

- [x] Apache-2.0 许可证文件和包元数据。
- [x] README、VitePress 文档站、架构图、能力清单和示例。
- [x] 贡献指南、行为准则、安全策略、支持说明和变更日志。
- [x] Python 包构建配置、类型标记 `py.typed` 和可选依赖分组。
- [x] CI、文档构建、Issue 模板和合并请求模板。
- [x] 忽略 Python、Node、数据库与文档构建产物。
- [ ] 对公开仓库执行密钥与敏感数据扫描，并检查完整 Git 历史。
- [x] 在全新环境安装 wheel，运行最小示例并核对 wheel 内容。
- [ ] 补充 PostgreSQL 服务集成测试和主要并发场景测试。
- [ ] 清理严格 `mypy` 的历史问题；2026-09-26 检查仍有 44 个错误。

## 发布决策

- [ ] 确认版权主体和 Apache-2.0 许可证选择。
- [ ] 确认 `handwritten-agent-core` 的 PyPI 包名可用。
- [x] 将 `agent-core` 拆成独立仓库并保留相关历史；目录内的 `.github` 工作流在独立仓库生效。
- [x] 独立仓库地址确定后，更新 `pyproject.toml`、VitePress 编辑链接和 Issue 模板中的仓库 URL。
- [ ] 决定是否把 Python 支持范围扩展到 3.11/3.12；当前只声明 Python 3.13。
- [ ] 决定首个公开版本号、发布候选流程和兼容性承诺。
- [ ] 决定文档托管平台和正式 `DOCS_BASE`。
- [ ] 在 `SECURITY.md` 配置专用安全邮箱或私密漏洞报告入口。
- [ ] 配置 PyPI Trusted Publisher 或最小权限发布令牌。
- [ ] 在代码托管平台启用分支保护、强制 CI、依赖更新和秘密扫描。

## 每次发布

- [ ] 更新版本号和 `CHANGELOG.md`，清空或迁移 `Unreleased` 内容。
- [ ] 运行 `ruff check src tests examples`、`pytest` 和 `pnpm docs:build`，并审阅 `mypy src` 结果。
- [ ] 运行 `python -m build` 并用 `twine check dist/*` 校验元数据。
- [ ] 在隔离环境安装 wheel，验证导入、`py.typed` 和 examples。
- [ ] 创建签名或受保护的 Git tag，并从该 tag 构建发布产物。
- [ ] 发布 PyPI、文档站和发布说明，记录已知限制与升级步骤。
