# Lark Remote Claude

在电脑终端运行 Claude Code / Codex，同时通过飞书（Feishu / Lark）实时查看输出、发送指令、点击交互按钮（选项/权限确认）。

Feishu/Lark remote terminal bridge for Claude Code and Codex.

Keywords: remote claude lark, lark remote claude, feishu claude code.

## 这个项目解决什么问题

- 离开电脑时，仍可在手机飞书里继续操作同一个会话
- 多个终端 + 飞书可共享同一个会话
- 终端输出按流式卡片同步到飞书，支持交互按钮

## 版本更新记录

> 建议每次发版都在这里按版本号追加“新增能力 / 关键修复”。

### v1.0.3（当前）

**新增能力**
- 飞书群聊会话恢复与接管链路：支持离线态感知、恢复原会话、接管其他会话。
- 群聊滚动总结机制：支持每 80 条有效消息自动总结，也支持手动触发总结。
- 群聊恢复协议增强：基于群聊历史生成 role 化恢复上下文，提升宕机后的续聊连续性。
- 新增“查看本轮变更”入口：在卡片中可直接查看当前轮次产生的代码差异。

**关键修复**
- 修复“显示已连接但实际未投递”问题：恢复后增加就绪校验，避免假成功。
- 修复消息投递静默失败：发送超时/异常/返回失败时，飞书侧给出明确告警。
- 修复飞书 `post` 富文本长消息被识别为空的问题：增强文本提取与兜底逻辑。
- 修复 `Update(...)/Added/removed` 等输出在飞书被吞的问题（Claude/Codex 解析器对齐修复）。
- 修复全角 `！` 未被识别为 bash 命令前缀的问题。

## 环境要求

- macOS 或 Linux
- [uv](https://docs.astral.sh/uv/)
- [tmux](https://github.com/tmux/tmux)
- [Claude Code CLI](https://claude.ai/code) 或 [Codex CLI](https://github.com/openai/codex)

## 安装

### 方式 1：npm（推荐）

```bash
npm install -g lark-remote-claude
```

### 方式 2：源码安装

```bash
git clone https://github.com/ShellyDeng08/lark-remote-claude.git
cd lark-remote-claude
./init.sh
```

安装后请重启终端，使快捷命令生效。

## 用户入口（先看这个）

- 普通用户完整手册：
  https://github.com/ShellyDeng08/lark-remote-claude/blob/main/docs/USER_GUIDE.md
- 飞书客户端运维与排障：
  https://github.com/ShellyDeng08/lark-remote-claude/blob/main/docs/LARK_CLIENT_GUIDE.md

## 3 分钟快速开始（推荐）

```bash
# 1) 安装（全局）
npm install -g lark-remote-claude

# 2) 在你的项目目录启动（会自动启动飞书客户端）
cl

# 3) 若飞书侧无响应，执行一次重连
remote-claude lark restart

# 4) 在飞书里给机器人发 /menu，选择会话后直接开始聊天
```

> 说明：`remote-claude lark restart` 不需要每次都执行；首次安装后、修改飞书凭证后，或连接异常时执行一次即可。

你也可以使用以下快捷命令：

```bash
cla   # 启动 Claude（默认会话名为当前目录）
cl    # 启动 Claude（跳过权限确认）
cx    # 启动 Codex（跳过权限确认）
cdx   # 启动 Codex（需要确认权限）
```

常用管理命令：

```bash
remote-claude list
remote-claude attach <会话名>
remote-claude kill <会话名>
remote-claude status <会话名>
```

## 启用飞书（需要一次管理员配置）

> 如果你只在终端使用，可跳过本节。

### 1) 在飞书开放平台创建企业自建应用

- 地址：https://open.feishu.cn/
- 获取 `App ID` 和 `App Secret`
- 添加应用能力：**机器人**

### 2) 配置事件与回调（长连接）

- 事件配置：使用长连接接收事件，并添加：
  - `im.message.receive_v1`（接收消息 v2.0）
- 回调配置：使用长连接接收回调，并添加：
  - `card.action.trigger`（卡片回传交互）

### 3) 配置权限（最小必需）

在「权限管理 -> 批量导入/导出权限」导入以下 tenant scopes：

```json
{
  "scopes": {
    "tenant": [
      "cardkit:card:write",
      "contact:contact.base:readonly",
      "contact:user.id:readonly",
      "im:chat.members:read",
      "im:chat.members:write_only",
      "im:chat:create",
      "im:chat:read",
      "im:chat:update",
      "im:message.group_at_msg:readonly",
      "im:message.group_msg",
      "im:message.p2p_msg:readonly",
      "im:message.urgent",
      "im:message:readonly",
      "im:message:send_as_bot",
      "im:message:update",
      "im:resource"
    ]
  }
}
```

### 4) 发布应用

在飞书开放平台创建版本并发布到线上。

### 5) 在本机写入飞书凭证

首次运行 `cla` / `cl` 会提示填写；或手动编辑 `~/.remote-claude/.env`：

```bash
FEISHU_APP_ID=cli_xxxxx
FEISHU_APP_SECRET=xxxxx
```

## 飞书端怎么用

### 启动/检查飞书客户端

```bash
remote-claude lark start
remote-claude lark status
remote-claude lark restart
remote-claude lark stop
```

### 在飞书中操作

1. 搜索并打开你的机器人
2. 发送 `/menu`
3. 在菜单卡片里连接会话
4. 直接发消息给 Claude/Codex

常用命令：

- `/menu` 打开主菜单
- `/list` 列出可用会话
- `/attach <会话名>` 连接会话
- `/detach` 断开当前连接
- `/help` 查看帮助

## 可选功能

### 自定义环境变量（`~/.remote-claude/.env`）

除了 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`，还支持以下常用自定义配置：

```bash
# 用户白名单
ENABLE_USER_WHITELIST=true
ALLOWED_USERS=ou_xxx,ou_yyy

# 群聊名显示前缀
GROUP_NAME_PREFIX=【Remote-Claude】

# 自定义 Claude CLI 命令
CLAUDE_COMMAND=claude

# 流式卡片单卡 block 上限
MAX_CARD_BLOCKS=50

# 日志级别
LARK_LOG_LEVEL=INFO
SERVER_LOG_LEVEL=INFO

# SOCKS 代理环境下绕过飞书请求代理
LARK_NO_PROXY=1

# 用户 OAuth（高级能力）
ENABLE_USER_AUTH=true
OAUTH_SERVER_PORT=8080
# OAUTH_REDIRECT_URI=http://localhost:8080/oauth/callback

# 统计（可选）
# MIXPANEL_TOKEN=your_mixpanel_token

# AI 分析降级备用（可选）
# ANTHROPIC_API_KEY=sk-ant-xxx
```

完整变量清单见仓库根目录 `.env.example`。

### 用户 OAuth（高级能力，可选）

仅当你需要“以用户身份访问更多飞书资源”时启用，详细见：`docs/feishu-user-oauth/USER_GUIDE.md`

## 常见问题

### 1) 飞书里搜不到机器人

- 检查应用是否已发布到线上
- 确认你和机器人在同一飞书组织

### 2) 飞书发消息没反应

- 先看 `remote-claude lark status`
- 检查 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 是否正确
- 检查开放平台事件/回调是否按上文配置

### 3) 终端与飞书显示不一致

先查看 `docs/LARK_CLIENT_GUIDE.md` 的排障部分。

## 关键词（便于搜索）

Claude Code, Codex, Feishu bot, Lark bot, remote terminal, shared CLI session, AI coding assistant, remote-claude, lark-remote-claude.

## 文档

- `docs/USER_GUIDE.md`：面向使用者的完整操作手册
- `docs/LARK_CLIENT_GUIDE.md`：飞书客户端运维与排障
- `docs/feishu-user-oauth/USER_GUIDE.md`：用户 OAuth 指南
- `CLAUDE.md`：项目架构与开发说明
