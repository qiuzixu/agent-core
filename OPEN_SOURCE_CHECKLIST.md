# 开源发布检查清单

本清单区分仓库内已经准备好的内容，以及首次公开发布前必须由维护者确认的外部事项。

## 仓库内容

- [x] Apache-2.0 许可证文件和包元数据。
- [x] README、MkDocs 文档站、架构图、能力清单和示例。
- [x] 贡献指南、行为准则、安全策略、支持说明和变更日志。
- [x] Core、MinerU、Embedding 与 Multi-Agent 扩展包的构建配置、类型标记和依赖分组。
- [x] CI、文档构建、Issue 模板和合并请求模板。
- [x] 忽略 Python、Node、数据库与文档构建产物。
- [ ] 对公开仓库执行密钥与敏感数据扫描，并检查完整 Git 历史。
- [x] 在全新环境安装 wheel，运行最小示例并核对 wheel 内容。
- [x] 主要并发场景测试：租约认领/心跳丢失/过期恢复、三级存储乐观并发与事务冲突、Run 生命周期委托（RunExecutor）、HITL 条件状态转换、模型治理限流/熔断、ReAct 工具阶段 checkpoint 恢复（2026-10-10 补充）。
- [ ] PostgreSQL 服务集成测试（连接临时 PostgreSQL 实例，覆盖三级存储跨后端契约矩阵与崩溃恢复；当前仅有真实 pgvector 的 round-trip 测试）。
- [x] Core、MinerU、Embedding 和 Multi-Agent 扩展包通过 strict `mypy`。

## 发布决策

- [x] 确认版权主体（个人：qiuzixu）和 Apache-2.0 许可证选择；包元数据已补 `authors`。
- [ ] 确认 Core、MinerU、Embedding 和 Multi-Agent 四个 PyPI 包名可用。
- [x] 将 `agent-core` 拆成独立仓库并保留相关历史；目录内的 `.github` 工作流在独立仓库生效。
- [x] 独立仓库地址确定后，更新包元数据、MkDocs 配置和 Issue 模板中的仓库 URL。
- [x] Python 支持范围维持仅 3.13：核心使用 PEP 695 泛型等 3.13 语法，降级需代码改造；待真实需求出现再评估。
- [x] 首个公开版本号定为 `0.2.0`（`0.1.0` 为内部 Alpha，未发布）；兼容性承诺：0.x 阶段补丁与小版本不破坏公开 API，弃用先警告后移除；发布候选流程沿用"CI 全绿 + tag 构建"。
- [ ] 决定文档托管平台和正式 `DOCS_BASE`。
- [ ] 在 `SECURITY.md` 配置专用安全邮箱或私密漏洞报告入口。
- [ ] 配置 PyPI Trusted Publisher 或最小权限发布令牌。
- [ ] 在代码托管平台启用分支保护、强制 CI、依赖更新和秘密扫描。

## 每次发布

- [ ] 更新版本号和 `CHANGELOG.md`，清空或迁移 `Unreleased` 内容。
- [ ] 按 `CONTRIBUTING.md` 运行 Core 与扩展包的 Ruff、strict mypy、pytest 和 MkDocs 检查。
- [ ] 分别构建 Core、`packages/mineru`、`packages/embeddings` 与 `packages/multi-agent`，
  并用 `twine check` 校验四组发布产物。
- [ ] 在隔离环境安装四个 wheel，验证导入、`py.typed`、依赖方向和 examples。
- [ ] 创建签名或受保护的 Git tag，并从该 tag 构建发布产物。
- [ ] 发布 PyPI、文档站和发布说明，记录已知限制与升级步骤。
