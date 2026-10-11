# PyPI 发布问题排查与解决总结

> 记录时间：2026-10-11
> 项目：zero-agent-core（agent-core）
> 状态：已解决

---

## 一、问题回顾

### 问题 1：GitHub Actions 发布失败（invalid-publisher）

**报错信息：**
```
invalid-publisher: valid token, but no corresponding publisher
```

**发生时间：** 推送 tag `v0.2.1` 后，GitHub Actions workflow `Publish #2`（run id 38149649684）

**原因：**
工作流使用的是 **Trusted Publisher（OIDC）** 方式发布，但 PyPI 上没有配置对应的 Trusted Publisher 信息。PyPI 收到了 GitHub 的 OIDC 令牌，但找不到信任记录，所以拒绝了发布。

### 问题 2：解决方式

最终选择 **API Token 方式** 替代 Trusted Publisher，因为配置更简单。

---

## 二、PyPI 发布的两种认证方式

### 方式 A：Trusted Publisher（OIDC）

**原理：** PyPI 信任你的某个 GitHub 仓库 + 工作流文件，不需要任何 Token。

**优点：**
- 不存在可泄露的密钥
- 不需要在 GitHub 存 Secret
- 更安全

**缺点：**
- 需要在 PyPI 每个项目页面手动配置一次
- 配置必须精确匹配（Owner / Repository / Workflow / Environment），错一个字就不行

**配置步骤（如果以后想切换回来）：**
1. 登录 PyPI，进入每个项目的 **Manage → Publishing** 页面
2. 添加 GitHub Trusted Publisher：
   ```
   Owner:       qiuzixu
   Repository:  agent-core
   Workflow:    publish.yml
   Environment: pypi
   ```
3. 4 个项目都要配：zero-agent-core、zero-agent-core-mineru、zero-agent-core-embeddings、zero-agent-core-multi-agent

### 方式 B：API Token（当前使用 ✅）

**原理：** 在 PyPI 生成一串密钥，存到 GitHub Secrets，工作流读取它来认证。

**优点：**
- 配置简单，只需要一个 Secret
- 不需要在 PyPI 项目页面额外配置

**缺点：**
- Token 有泄露风险（所以之前暴露在聊天里的 Token 应该撤销重建）

**当前配置：**
- GitHub Secret 名称：`PYPI_API_TOKEN`
- 位置：https://github.com/qiuzixu/agent-core/settings/secrets/actions

---

## 三、本次实际操作步骤

1. **手动发布了 0.2.0 版本**（4 个包），使用 `uv publish` + 用户 Token
2. **版本号升级到下一版**：core → 0.2.1，子包 → 0.1.2
3. **提交并推送** `main` 分支
4. **打 tag `v0.2.1`** 并推送，触发了自动发布工作流
5. **工作流失败**（invalid-publisher）
6. **修改工作流** 从 Trusted Publisher 切换到 API Token 方式
7. **添加 GitHub Secret** `PYPI_API_TOKEN`
8. **提交并推送**工作流改动
9. **重跑失败的 Actions 任务**

---

## 四、以后发布新版本的步骤

详见 `RELEASE.md`，简要流程：

```bash
# 1. 改版本号（pyproject.toml 里的 version）
# 2. 更新 CHANGELOG.md
# 3. 提交代码
git add -A
git commit -m "chore: release zero-agent-core x.y.z"

# 4. 打 tag
git tag -a vX.Y.Z -m "Release zero-agent-core X.Y.Z"

# 5. 推送 tag（这才会触发自动发布）
git push origin main
git push origin vX.Y.Z
```

**注意：** 普通推送 `git push origin main` 只提交代码，不会触发 PyPI 发布。只有推送 `v*` 格式的 tag 才会触发工作流。

---

## 五、踩过的坑

| 坑 | 原因 | 解决 |
|---|---|---|
| invalid-publisher | PyPI 上没配 Trusted Publisher | 改用 API Token 方式 |
| Token 暴露 | 聊天中直接发送了 Token | 建议去 PyPI 撤销旧 Token，生成新 Token |
| SSH 推送失败 | Windows 下 ssh 命令不可用 | 暂时用 HTTPS 推送（后来 SSH 恢复了） |
| HTTPS 推送失败 | schannel 凭据问题 | 改回 SSH 推送 |
