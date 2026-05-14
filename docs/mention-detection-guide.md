# @消息检测功能使用指南

## 功能概述

Remote Claude 飞书客户端现已支持自动检测和通知未回复的@消息，帮助你及时处理重要消息，避免遗漏。

## 主要特性

- ✅ **自动检测** - 定时检查所有群聊和私聊中的@消息
- ✅ **话题回复支持** - 检测话题（Thread）中的@消息
- ✅ **智能过滤** - 支持黑名单和重点群配置
- ✅ **一键跳转** - 通知卡片带群聊链接，直接跳转到消息位置
- ✅ **批量检查** - 支持轮换检查大量群聊，避免 API 限流

## 前置条件

1. **启用用户授权功能**

   在 `~/.remote-claude/.env` 文件中设置：
   ```bash
   ENABLE_USER_AUTH=true
   OAUTH_REDIRECT_URI=http://localhost:8765/oauth/callback
   OAUTH_SERVER_PORT=8765
   ```

2. **完成 OAuth 授权**

   在飞书中向机器人发送：
   ```
   /oauth
   ```
   然后点击链接完成授权。

## 基本使用

### 1. 手动检查@消息

```
/check-mentions
```

检查最近的未回复@消息（最多检查50个群）。

如果需要全量检查（所有群），使用：
```
/check-mentions --all
```

### 2. 开启自动检查

```
/mentions-auto on
```

开启自动检查，默认每10分钟检查一次。

自定义检查间隔（5-60分钟）：
```
/mentions-auto on 15
```

### 3. 关闭自动检查

```
/mentions-auto off
```

### 4. 查看状态

```
/mentions-status
```

显示当前的自动检查状态、上次检查时间和未回复数量。

## 高级配置

### 配置黑名单

将某些群聊加入黑名单，不再检查这些群的@消息：

```
# 添加到黑名单
/mentions-config blacklist add oc_xxx

# 查看黑名单
/mentions-config blacklist list

# 从黑名单移除
/mentions-config blacklist remove oc_xxx
```

### 配置重点群

将重要群聊设为重点群，每次检查都会优先处理：

```
# 添加到重点群
/mentions-config priority add oc_xxx

# 查看重点群
/mentions-config priority list

# 从重点群移除
/mentions-config priority remove oc_xxx
```

### 统一配置管理

查看所有配置：
```
/config
```

修改单个配置项：
```
/config mention.check_interval_minutes 20
/config mention.notify_priority_only true
```

## 配置说明

### mention 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `auto_check_enabled` | bool | true | 是否开启自动检查 |
| `check_interval_minutes` | int | 10 | 检查间隔（5-60分钟） |
| `blacklist_chats` | list | [] | 黑名单群 chat_id 列表 |
| `priority_chats` | list | [] | 重点群 chat_id 列表 |
| `notify_priority_only` | bool | false | 是否只通知重点群的@消息 |

### notification 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `on_complete` | bool | true | 任务完成时提醒 |
| `on_error` | bool | true | 发生错误时提醒 |
| `urgent_at_mention` | bool | true | @消息紧急提醒 |

## 命令总览

查看所有可用命令：
```
/commands
```

## 工作原理

### 检测逻辑

1. **主消息流检测**
   - 获取群聊最近50条消息
   - 找出包含@当前用户的消息
   - 检查@消息之后是否有用户回复

2. **话题回复检测**
   - 识别包含话题（thread）的消息
   - 使用 `thread_id` 查询话题内的回复
   - 检查话题内@消息之后是否有用户回复

3. **回复判断原则**
   - 主消息中的@：只在主消息流检查回复
   - 话题中的@：只在该话题内检查回复
   - 不跨话题、不跨群检查回复

### 批量检查策略

当用户加入大量群聊（>50个）时：

1. **重点群优先**
   - 重点群每次都检查（不占用配额）

2. **普通群轮换**
   - 每次最多检查50个普通群
   - 从上次停止的位置继续
   - 检查完所有群后重置索引

3. **全量检查**
   - 使用 `--all` 参数可强制全量检查
   - 可能耗时较长，适合偶尔使用

## 故障排查

### 检查不到@消息

1. 确认已完成 OAuth 授权：`/oauth-status`
2. 检查是否在黑名单中：`/mentions-config blacklist list`
3. 查看最近检查时间：`/mentions-status`

### 自动检查未运行

1. 确认自动检查已开启：`/mentions-status`
2. 检查配置间隔是否合理（5-60分钟）
3. 查看日志：`~/.remote-claude/lark_client.log`

### API 限流

如果遇到飞书 API 限流（429 错误）：

1. 增加检查间隔：`/mentions-auto on 30`
2. 使用黑名单排除不重要的群
3. 减少重点群数量

## 安全说明

- OAuth 授权使用 User Access Token，权限仅限于读取消息
- 配置文件存储在本地 `~/.remote-claude/` 目录
- 可以随时撤销授权：`/oauth-revoke`

## 更多帮助

- 查看帮助：`/help`
- 查看菜单：`/menu`
- 查看所有命令：`/commands`
