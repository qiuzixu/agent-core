# 发布流程

## 1. 准备版本

1. 确认目标版本和兼容性范围；
2. 更新 `pyproject.toml` 中的版本；
3. 把 `CHANGELOG.md` 的 `Unreleased` 内容迁入新版本并写发布日期；
4. 更新文档中的版本和迁移说明；
5. 检查 `OPEN_SOURCE_CHECKLIST.md`。

## 2. 运行验证

```bash
python -m pip install build twine
ruff check src tests examples
pytest
pnpm install --frozen-lockfile
pnpm docs:build
python -m build
twine check dist/*
```

另外运行 `mypy src` 并审阅结果。严格 mypy 在进入稳定版本前必须清零；Alpha 阶段的已知
历史问题记录在开源检查清单中。

在全新虚拟环境安装 `dist/*.whl`，验证：

```bash
python -c "import agent_core; print(agent_core.__file__)"
python examples/basic_agent.py
```

检查 wheel 包含 `agent_core/py.typed`，不包含测试数据库、密钥、缓存和文档构建产物。

## 3. 发布

1. 合并经过 CI 的发布提交；
2. 创建与版本一致的受保护 tag，例如 `v0.1.0`；
3. 由 PyPI Trusted Publisher 从该 tag 构建并发布；
4. 使用相同提交构建 VitePress 文档；
5. 创建发布说明，包含主要变化、升级方式、已知限制和产物校验信息。

首次发布前先上传 TestPyPI 验证元数据和安装流程。不要从开发机已有的 `dist/` 目录直接上传
未经校验的历史产物。

## 4. 回滚与修复

PyPI 版本不可覆盖。发布错误时撤下受影响版本、记录原因，并发布递增的修复版本。
如果变更影响持久化 Run、Checkpoint 或 WorkflowExecution，修复说明必须说明旧数据能否继续恢复。
