# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 语言与交互

- 与用户沟通、代码注释、变更说明统一使用**简体中文**。

## 项目定位（先理解这个）

Remote Claude 用 PTY + Unix Socket 把一个 Claude/Codex CLI 会话共享给多个终端和飞书客户端。

核心目标不是“消息中转”，而是：
- 服务端持续维护**终端真实状态**（含 ANSI、滚动、blink）
- 飞书端基于共享内存快照做**流式展示**

## 常用开发命令

### 环境与安装

```bash
# 安装 Python 依赖
uv sync

# 首次本地初始化（安装快捷命令、检查 tmux/uv/cli）
./init.sh
```

### 启动与会话管理

```bash
# 启动新会话（默认 Claude）
uv run python3 remote_claude.py start <session_name>

# 启动 Codex 会话
uv run python3 remote_claude.py start <session_name> --cli codex

# 连接已有会话
uv run python3 remote_claude.py attach <session_name>

# 列出/终止会话
uv run python3 remote_claude.py list
uv run python3 remote_claude.py kill <session_name>

# 查看会话状态
uv run python3 remote_claude.py status <session_name>
```

### 飞书客户端管理

```bash
uv run python3 remote_claude.py lark start
uv run python3 remote_claude.py lark stop
uv run python3 remote_claude.py lark restart
uv run python3 remote_claude.py lark status
```

### 快捷命令（init.sh 安装后）

```bash
cla   # Claude + 飞书客户端
cl    # Claude + 跳过权限确认
cx    # Codex + 飞书客户端（跳过权限）
cdx   # Codex + 需确认权限
```

### 测试命令

> 本仓库当前没有统一的 `lint` / `build` 命令；以脚本测试和功能验证为主。

```bash
# 单测（可单独运行）
uv run python3 tests/test_format_unit.py
uv run python3 tests/test_stream_poller.py
uv run python3 tests/test_renderer.py
uv run python3 tests/test_output_clean.py
uv run python3 tests/lark_client/test_mock_output.py
uv run python3 tests/lark_client/test_cjk_width.py
uv run python3 tests/lark_client/test_full_simulation.py

# 运行“单个测试文件”的通用方式
uv run python3 tests/<test_file>.py

# 需活跃会话的集成测试（先 start 一个会话）
uv run python3 tests/test_integration.py
uv run python3 tests/test_session.py
uv run python3 tests/test_real.py
uv run python3 tests/test_e2e.py
uv run python3 tests/test_mock_conversation.py
```

### Docker 包完整性验证（npm 发布前）

```bash
docker-compose -f docker/docker-compose.test.yml build
docker-compose -f docker/docker-compose.test.yml run npm-test /project/docker/scripts/docker-test.sh
```

## 高层架构（Big Picture）

```text
Terminal/Lark Input
      │
      ▼
remote_claude.py + client/session_bridge
      │ Unix Socket
      ▼
server/server.py (PTY 代理)
  ├─ 启动 Claude/Codex CLI
  ├─ 解析终端屏幕（server/parsers/*）
  ├─ 广播给 attach 客户端
  └─ 写入共享内存 .mq 快照（server/shared_state.py）
      │
      ▼
lark_client/shared_memory_poller.py
  ├─ 轮询 .mq
  ├─ 按 block diff 更新/冻结卡片
  └─ 调用 card_builder.py + card_service.py 渲染飞书卡片
```

## 必须遵守的职责边界

- `server/` 负责**内容正确性**（ANSI、布局、状态识别、结构化）。
- `lark_client/` 只负责**展示与交互**，不要在这里做“文本修复补丁”。
- 如果飞书显示内容异常，优先修 `server/parsers/*` 或 `server/server.py`，不是在卡片层做字符串修补。

## 关键数据模型（理解后再改代码）

### 1) ClaudeWindow：全量快照模型

服务端每次 flush 产出并覆盖写入共享内存。
核心字段：
- `blocks`：累积历史（OutputBlock/UserInput/PlanBlock/SystemBlock）
- `status_line` / `bottom_bar` / `agent_panel` / `option_block`：状态型组件（每帧覆盖）
- `layout_mode`：`normal | option | detail | agent_list | agent_detail`

### 2) “累积型” vs “状态型”

- 累积型：会保留历史，进入 `blocks`
- 状态型：全局唯一，不进 `blocks`，只反映当前帧状态

重点：`OptionBlock` 是状态型，不是历史消息。

### 3) 流式卡片窗口

`SharedMemoryPoller` 维护 `StreamTracker` + `CardSlice`：
- 首次 attach：最近 `INITIAL_WINDOW=30` 个 blocks
- 超限冻结：单卡超过 `MAX_CARD_BLOCKS` 后冻结旧卡并开新卡

## 解析器设计要点

### Claude 解析器（`server/parsers/claude_parser.py`）

- 通过分割线划分输出区/输入区/底部栏
- 支持 `normal / option / detail / agent_list / agent_detail`
- `PlanBlock`（box-drawing）与 `SystemBlock`（非 blink 星号）独立分类
- `OptionBlock(permission/option)` 从输入区域解析，作为状态型组件输出

### Codex 解析器（`server/parsers/codex_parser.py`）

- 不依赖 Claude 的分割线逻辑，重点靠**背景色区域**定位输入区
- 使用 `_find_bg_region` + `_determine_input_mode` 区分 normal/option
- Codex 中圆点 + blink 用于区分 StatusLine 与 OutputBlock

## 关键文件（排查问题优先级）

1. `remote_claude.py`：CLI 入口、start/attach/lark/stats/update 命令
2. `server/server.py`：PTY 生命周期、flush、快照生产
3. `server/parsers/claude_parser.py` / `server/parsers/codex_parser.py`：终端语义解析
4. `server/shared_state.py`：`.mq` mmap 快照写入
5. `lark_client/shared_memory_poller.py`：流式卡片窗口推进/冻结策略
6. `lark_client/card_builder.py`：卡片四层布局与 header 状态逻辑
7. `lark_client/lark_handler.py`：飞书命令路由与 chat_id 会话绑定

## 调试与日志

### Server 侧

- `/tmp/remote-claude/<name>_messages.log`：ClaudeWindow 快照调试
- `/tmp/remote-claude/<name>_screen.log`：原始 screen 快照（需 `--debug-screen`）
- `/tmp/remote-claude/<name>_debug.log`：server debug 日志（配合 `SERVER_LOG_LEVEL=DEBUG`）

### Lark 侧

- `~/.remote-claude/lark_client.log`
- `~/.remote-claude/lark_client.debug.log`
- `lark status` 同时看 PID、运行时长、最近日志

### 启动调试参数

```bash
uv run python3 remote_claude.py start <session_name> --debug-screen
uv run python3 remote_claude.py start <session_name> --debug-verbose
uv run python3 remote_claude.py start <session_name> --cli codex
```

## 环境与依赖约束

- OS：macOS / Linux
- 必需：`uv`、`tmux`、`claude` CLI
- 可选：`codex` CLI、飞书应用凭证（`~/.remote-claude/.env`）

## 仓库规则补充

- 当前仓库未发现 `.cursorrules`、`.cursor/rules/`、`.github/copilot-instructions.md`。
- 若后续新增上述规则文件，应把关键约束同步到本文件。
