# 飞书用户授权模式 - 使用指南

## 🎯 功能说明

用户授权模式允许你通过 OAuth 授权，让 Remote Claude 以**你的个人身份**访问飞书资源，包括：
- ✅ 读取你可见的所有群聊（包括机器人不在的群）
- ✅ 以你的名义发送消息到任意群
- ✅ 访问你的文档、日历等资源

## 📋 前置要求

### 1. 飞书开放平台配置

在 [飞书开放平台](https://open.feishu.cn/app) 完成以下配置：

#### A. 配置重定向 URL
```
路径：开发配置 → 安全设置 → 重定向 URL
添加：http://localhost:8080/oauth/callback
```

#### B. 申请用户权限（User Scopes）
```
路径：权限管理 → 用户权限
添加以下权限：
- im:message:send_as_user    # 以用户身份发消息
- im:chat:readonly            # 读取用户可见的群列表
- im:message:readonly         # 读取消息历史
- contact:user.base:readonly  # 读取用户基本信息
```

⚠️ **注意**：用户权限可能需要提交审核，请在申请时说明使用场景。

### 2. 本地环境配置

编辑 `~/.remote-claude/.env` 文件，添加：

```bash
# 启用用户授权模式
ENABLE_USER_AUTH=true

# OAuth 回调服务器端口（可选，默认 8080）
OAUTH_SERVER_PORT=8080

# OAuth 重定向 URI（可选，默认 http://localhost:{port}/oauth/callback）
OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback
```

## 🚀 快速开始

### 步骤 1：启动飞书客户端

```bash
# 重启飞书客户端以加载新配置
remote-claude lark restart

# 或者停止后启动
remote-claude lark stop
remote-claude lark start
```

启动日志应显示：
```
用户授权: 启用
OAuth 授权页面: http://localhost:8080/oauth/authorize
```

### 步骤 2：在飞书中授权

在飞书中发送命令：
```
/oauth
```

机器人会回复一个授权卡片，包含：
- 🔗 授权链接
- 📊 当前授权状态（未授权/已授权）

**方式 1：在手机/电脑浏览器中打开链接**
1. 点击或复制授权链接
2. 在浏览器中打开
3. 看到飞书授权页面，点击「同意授权」
4. 授权成功后会显示确认页面

**方式 2：直接访问授权页面**
```
http://localhost:8080/oauth/authorize
```

### 步骤 3：确认授权成功

在飞书中发送：
```
/oauth-status
```

应该显示：
- ✅ 授权状态：已授权
- 👤 用户信息：你的名字
- ⏰ 授权时间
- 📅 过期时间（约2小时后）
- 🔄 Refresh Token：有（支持自动刷新）

## 📱 飞书命令

### `/oauth` - 显示授权链接和状态
```
用法：/oauth

显示：
- 授权链接（点击或复制到浏览器）
- 当前授权状态
- 快速指引
```

### `/oauth-status` - 查看授权详情
```
用法：/oauth-status

显示：
- ✅/❌ 授权状态
- 👤 用户信息（姓名、Open ID）
- ⏰ 授权时间
- 📅 过期时间
- 🔑 Access Token 预览
- 🔄 Refresh Token 状态
```

### `/oauth-revoke` - 撤销授权
```
用法：/oauth-revoke

功能：
- 删除本地存储的 token
- 需要重新授权才能使用用户权限功能
```

## 🎯 使用场景

### 场景 1：向机器人不在的群发消息

当前 Remote Claude 机器人模式只能访问它所在的群。启用用户授权后，可以以你的身份向任意群发消息。

**实现方式**（Phase 7 待开发）：
```
/forward <群名> <消息内容>
```

### 场景 2：查看所有可见群列表

```python
# 使用 user_api.py
from lark_client.user_api import LarkUserApi

api = LarkUserApi(user_id="ou_xxx", oauth_service=oauth_service)
chats = await api.get_user_chats()
# 返回用户可见的所有群，包括机器人不在的群
```

### 场景 3：读取指定群的消息历史

```python
messages = await api.get_chat_messages(
    chat_id="oc_xxx",  # 可以是机器人不在的群
    page_size=20
)
```

## 🔒 安全说明

### Token 存储

当前版本（Phase 1-6）：
- Token 存储在 `~/.remote-claude/user_tokens.json`
- **明文存储**（Phase 8 将实现加密）

⚠️ **重要提示**：
- 不要将 `user_tokens.json` 文件分享给他人
- 不要提交到 git 仓库（已在 .gitignore）
- 定期检查并清理不用的 token

### Token 有效期

- **Access Token**：2 小时有效期
- **自动刷新**：系统会在 token 过期前自动刷新
- **Refresh Token**：长期有效（除非你主动撤销）

### 权限范围

用户授权后，Remote Claude 可以：
- ✅ 以你的身份发送消息
- ✅ 读取你可见的所有群
- ✅ 访问你有权限的飞书资源

⚠️ **请谨慎使用**，确保：
- 只在信任的环境中使用
- 定期检查授权状态
- 不需要时及时撤销授权

## 🐛 故障排查

### 问题 1：启动时没有显示 "用户授权: 启用"

**原因**：配置未生效

**解决**：
1. 检查 `~/.remote-claude/.env` 文件是否有 `ENABLE_USER_AUTH=true`
2. 重启飞书客户端：`remote-claude lark restart`
3. 检查日志：`tail -f ~/.remote-claude/lark_client.log`

### 问题 2：授权链接打不开

**原因**：OAuth 服务器未启动或端口被占用

**解决**：
1. 检查端口是否被占用：`lsof -i :8080`
2. 修改端口配置（如果 8080 被占用）：
   ```bash
   echo "OAUTH_SERVER_PORT=8081" >> ~/.remote-claude/.env
   echo "OAUTH_REDIRECT_URI=http://localhost:8081/oauth/callback" >> ~/.remote-claude/.env
   ```
3. 重启飞书客户端
4. **重要**：在飞书开放平台更新重定向 URL

### 问题 3：授权后显示 "token 失效"

**原因**：Token 已过期或被撤销

**解决**：
1. 重新授权：在飞书发送 `/oauth`
2. 或者手动删除旧 token：
   ```bash
   rm ~/.remote-claude/user_tokens.json
   ```

### 问题 4：授权时提示 "redirect_uri 不匹配"

**原因**：飞书开放平台配置的重定向 URL 与代码不一致

**解决**：
1. 检查 `.env` 中的 `OAUTH_REDIRECT_URI`
2. 确保飞书开放平台配置了相同的 URL
3. 注意 http/https、端口号必须完全一致

### 问题 5：权限审核被拒

**原因**：未说明使用场景或权限申请不合理

**解决**：
1. 在申请时详细说明使用场景：
   ```
   用于 Remote Claude 项目，允许用户授权后：
   - 以用户身份发送消息（跨群协作）
   - 读取用户可见的群列表（多群管理）
   - 自动化个人工作流

   安全措施：
   - Token 本地存储
   - 用户主动授权
   - 可随时撤销
   ```
2. 只申请必需的最小权限
3. 提供项目 GitHub 链接作为证明

## 📚 相关文档

- [技术调研](./findings.md) - OAuth 流程和 API 详解
- [实施计划](./task_plan.md) - 10 阶段开发计划
- [进度日志](./progress.md) - 开发过程记录

## 🤝 获取帮助

### 查看日志
```bash
# 飞书客户端日志
tail -f ~/.remote-claude/lark_client.log

# 调试日志（需要先设置 LARK_LOG_LEVEL=DEBUG）
tail -f ~/.remote-claude/lark_client.debug.log
```

### 检查服务状态
```bash
remote-claude lark status
```

### 反馈问题
GitHub Issues: https://github.com/ShellyDeng08/remote_claude/issues

---

**版本**：v1.0 (Phase 1-6)
**最后更新**：2026-03-25
**状态**：核心功能已完成，可用
