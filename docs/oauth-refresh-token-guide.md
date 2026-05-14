# 飞书 OAuth Refresh Token 配置指南

## 问题描述

当前用户授权后只能使用 2 小时（`access_token` 有效期），之后需要重新授权。这是因为飞书 API 没有返回 `refresh_token`。

## 根本原因

飞书 OAuth 2.0 API 是否返回 `refresh_token` 取决于：

### 1. 应用权限配置

在**飞书开放平台** > **凭证与基础信息** > **权限配置**中，需要确保：

- ✅ 启用了 **OAuth 2.0** 功能
- ✅ 配置了正确的**重定向 URL**（redirect_uri）
- ✅ 勾选了需要的权限范围（scopes）

### 2. 授权模式

必须使用 **authorization_code** 授权模式（我们当前使用的就是这个）。

### 3. 飞书应用类型

根据飞书文档，`refresh_token` 的返回取决于应用类型和权限配置：

- **企业自建应用**：通常会返回 refresh_token
- **应用商店应用**：需要特别配置

## 诊断步骤

### 步骤 1：查看当前 Token 数据

```bash
cat ~/.remote-claude/user_tokens.json | python3 -m json.tool
```

检查输出中是否包含 `refresh_token` 字段。

当前状态：
```json
{
  "ou_21f26af5e01688eee2ef152483ca1a2b": {
    "access_token": "u-hysd...",
    "expires_in": 6900,  // 约 1.9 小时
    "saved_at": 1774565893.208104,
    // ❌ 缺少 refresh_token
  }
}
```

### 步骤 2：检查飞书开放平台配置

1. 登录 [飞书开放平台](https://open.feishu.cn)
2. 进入你的应用
3. 查看 **凭证与基础信息** > **权限配置**
4. 确认以下设置：
   - **OAuth 2.0 回调地址**: `http://localhost:8080/oauth/callback`
   - **权限范围**: 确保勾选了需要的权限，例如：
     - `im:message` - 发送和接收消息
     - `im:chat` - 获取群聊信息
     - `im:message.group_msg` - 读取群消息

### 步骤 3：重新授权并查看日志

1. 撤销当前授权：
   ```
   /oauth revoke
   ```

2. 重新授权：
   ```
   /oauth
   ```

3. 完成授权后，查看服务端日志：
   ```bash
   tail -50 ~/.remote-claude/lark_client.log | grep -E "(token|refresh|换取)"
   ```

4. 查找日志中的警告信息：
   ```
   ⚠️ 飞书未返回 refresh_token！可能需要在开放平台配置权限范围
   ```

## 解决方案

### 方案 1：联系飞书应用管理员（推荐）

如果你不是应用管理员，请联系管理员：

1. 确认应用类型（企业自建应用 vs 应用商店应用）
2. 检查开放平台中的权限配置
3. 确保启用了长期授权（refresh_token）功能

### 方案 2：检查飞书官方文档

参考飞书官方文档：
- [OAuth 2.0 概述](https://open.feishu.cn/document/common-capabilities/sso/api/oauth)
- [获取 user_access_token](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/authen/access_token)

查看你的应用类型是否支持 refresh_token。

### 方案 3：临时解决方案（不推荐）

如果短期内无法配置 refresh_token，可以：

1. 将 token 有效期检查逻辑设置为更宽松
2. 用户每 2 小时需要重新授权一次
3. 在 token 即将过期时，提前提醒用户重新授权

## 验证 Refresh Token 功能

重新授权后，再次检查 token 存储：

```bash
cat ~/.remote-claude/user_tokens.json | jq 'to_entries[0].value | keys'
```

期望输出应包含：
```json
[
  "access_token",
  "avatar_big",
  "avatar_middle",
  "avatar_thumb",
  "avatar_url",
  "en_name",
  "expires_in",
  "name",
  "open_id",
  "refresh_expires_in",  // ✅ 新增
  "refresh_token",         // ✅ 新增
  "saved_at",
  "sid",
  "tenant_key",
  "token_type",
  "union_id",
  "user_id"
]
```

## 自动刷新机制验证

如果获取到了 refresh_token，系统会自动刷新 token。验证方式：

1. 等待 1.5 小时（token 剩余 5 分钟时触发刷新）
2. 查看日志：
   ```bash
   tail -f ~/.remote-claude/lark_client.log | grep -i "刷新"
   ```
3. 期望看到：
   ```
   正在刷新 token...
   token 刷新成功: expires_in=7200
   ```

## 代码已实现的功能

当前代码已经完整实现了 refresh_token 机制：

✅ **自动检测过期**（提前 5 分钟）
✅ **自动调用刷新接口**
✅ **自动更新本地 token**
✅ **错误处理和降级**

唯一的问题是：**飞书 API 没有返回 refresh_token**。

## 需要用户协助

请提供以下信息以帮助诊断：

1. 你的飞书应用类型（企业自建/应用商店）
2. 是否有开放平台管理权限
3. 重新授权后日志中的 refresh_token 相关信息
4. （如果可能）飞书开放平台中权限配置的截图

## 参考

- 飞书 OAuth 2.0 文档: https://open.feishu.cn/document/common-capabilities/sso/api/oauth
- 本项目的 OAuth 实现: `lark_client/oauth_service.py`
