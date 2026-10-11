# 发布流程备忘

本文只记录 `agent-core` 的 Python 包发布流程。普通开发推送不会发布，只有打并推送 `v*` tag 才会触发发布流程。

## 1. 每次更新前先确认

- 只改业务代码：走普通 `git push origin main` 即可，不会发布。
- 要发布新包：一定要先升版本号，否则 PyPI 会拒绝重复版本。
- 主要包名：
  - `zero-agent-core`
  - `zero-agent-core-mineru`
  - `zero-agent-core-embeddings`
  - `zero-agent-core-multi-agent`

## 2. 更新版本号

根据改动类型修改对应 `pyproject.toml` 里的 `version`：

```bash
agent-core/pyproject.toml
agent-core/packages/mineru/pyproject.toml
agent-core/packages/embeddings/pyproject.toml
agent-core/packages/multi-agent/pyproject.toml
```

建议：

- bug / 文案 / 元数据：patch，例如 `0.1.1 -> 0.1.2`
- 新功能：minor，例如 `0.1.1 -> 0.2.0`
- 破坏性改动：major，例如 `0.2.0 -> 1.0.0`

同时更新 `CHANGELOG.md`。

## 3. 测试

```bash
uv run pytest
```

如果有具体子包测试，也可以在对应包目录下运行。

## 4. 提交源码

```bash
git add .
git commit -m "feat: xxx"
git push origin main
```

## 5. 触发自动发布

推荐做法是打 tag 并推送：

```bash
git tag -a v0.2.1 -m "Release zero-agent-core 0.2.1"
git push origin v0.2.1
```

这会触发 GitHub Actions 中的 `publish.yml`。

你可以在下面页面看构建进度：

```text
https://github.com/qiuzixu/agent-core/actions
```

## 6. 自动发布失败时怎么办

如果 GitHub Actions 的 publish job 失败，可以先检查：

1. 是否 PyPI 的 Trusted Publisher 还没配置
2. 是否某个包的版本号没有升
3. 是否某个包构建失败

如果只是临时需要把新版本发出去，也可以本地构建后手动上传：

```bash
uv build
uv run --with twine -- twine check dist/*
uv run --with twine -- twine upload --non-interactive --disable-progress-bar dist/*
```

注意：

- token 不要写进聊天记录或提交里
- token 暴露后要去 PyPI 撤销
- 长期推荐用 PyPI Trusted Publisher，而不是反复手动 token

## 7. 验证发布成功

去 PyPI 项目页确认新版本是否存在，或者本地安装：

```bash
pip install "zero-agent-core==0.2.1"
```

如果只更新了某个扩展包，例如 mineru，则验证：

```bash
pip install "zero-agent-core-mineru==0.1.2"
```
