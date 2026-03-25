# @消息检测功能设计文档

**文档版本**: v1.0
**创建日期**: 2025-03-25
**作者**: Claude Opus 4.6
**状态**: 设计评审中

---

## 1. 概述

### 1.1 功能目标

为 Remote Claude 飞书客户端增加 OAuth 用户授权和@消息检测功能，让用户可以：

1. **查看未回复的@消息** - 手动或自动检查所有群和私聊中未回复的@消息
2. **跨群消息聚合** - 无论群聊、私聊还是话题回复，统一检测和展示
3. **智能过滤** - 支持黑名单、重点群等过滤策略
4. **一键跳转** - 通知卡片带群聊链接，直接跳转到对应消息位置

### 1.2 用户场景

**场景 1: 忙碌时错过@消息**
- 用户在会议/通勤中，飞书有几十个群的消息
- 无法逐个群检查是否有@自己的消息
- 使用 Remote Claude 发送 `/check-mentions`，快速查看所有未回复@

**场景 2: 定期检查习惯**
- 用户希望每10分钟自动检查一次
- 有新的未回复@时收到通知
- 避免漏掉重要消息

**场景 3: 话题回复检测**
- 用户在群里发起话题，其他人在话题中@了用户
- 主消息流看不到，容易漏掉
- 系统自动检测话题中的@消息

### 1.3 技术背景

- **OAuth 2.0 用户授权** - 使用 User Access Token 而非 Tenant Access Token
- **飞书开放平台 API** - 需要 `im:message:readonly` 权限
- **话题消息检测** - 使用 `thread_id` 查询话题回复（不是 `message_id`）

---

## 2. 架构设计

### 2.1 整体架构

```
Remote Claude (lark_client/)
├─ main.py                    # 启动 MentionPoller
├─ mention_poller.py          # @消息轮询器【新增】
├─ config_service.py          # 统一配置服务【新增】
├─ lark_handler.py            # 命令路由【修改】
├─ card_builder.py            # 卡片构建【修改】
├─ user_api.py                # 用户 API【已有】
└─ oauth_service.py           # OAuth 服务【已有】

配置文件
├─ ~/.remote-claude/config.json          # 统一配置
├─ ~/.remote-claude/user_tokens.json     # OAuth tokens
└─ ~/.remote-claude/mention_state.json   # @消息状态
```

### 2.2 模块职责

#### 2.2.1 mention_poller.py（新增）

**MentionPoller 类**

- **职责**: @消息自动检查轮询器
- **核心方法**:
  - `start()` - 启动轮询（如果 auto_check_enabled）
  - `stop()` - 停止轮询
  - `check_now(user_id, chat_id)` - 手动触发检查
  - `_run_check_loop()` - 异步轮询循环
  - `_check_mentions(user_id)` - 执行检查逻辑

**MentionState 类**

- **职责**: @消息状态跟踪
- **字段**:
  - `last_check_time: float` - 上次检查时间戳
  - `known_unreplied: Dict[str, MentionInfo]` - 已知未回复消息（message_id -> info）
- **方法**:
  - `load()` - 从文件加载状态
  - `save()` - 保存状态到文件
  - `get_new_mentions(current)` - 对比当前和已知，找出新增

**MentionInfo 数据结构**

```python
@dataclass
class MentionInfo:
    message_id: str
    chat_id: str
    chat_name: str
    time: float
    sender_id: str
    sender_name: str
    text: str
    location: str  # "主消息" 或 "话题回复（根消息: XX-XX XX:XX）"
    chat_link: str  # 飞书群聊链接
```

#### 2.2.2 config_service.py（新增）

**ConfigService 类**

- **职责**: 统一配置管理
- **配置结构**:

```python
{
  "version": "1.0",
  "mention": {
    "auto_check_enabled": bool,      # 自动检查开关
    "check_interval_minutes": int,   # 检查间隔（分钟）
    "blacklist_chats": List[str],    # 黑名单群 chat_id
    "priority_chats": List[str],     # 重点群 chat_id
    "notify_priority_only": bool     # 只通知重点群
  },
  "notification": {
    "on_complete": bool,             # 任务完成提醒
    "on_error": bool,                # 错误提醒
    "urgent_at_mention": bool        # @消息紧急提醒
  },
  "ui": {
    "message_mode": str,             # "card" 或 "text"
    "bypass_permission": bool        # 跳过权限确认
  }
}
```

- **方法**:
  - `load()` - 加载配置（带默认值）
  - `save()` - 保存配置
  - `get(key, default)` - 获取配置项
  - `set(key, value)` - 设置配置项
  - `validate()` - 验证配置合法性

#### 2.2.3 lark_handler.py（修改）

**新增命令**:

| 命令 | 功能 | 返回 |
|------|------|------|
| `/check-mentions` | 立即检查未回复@消息 | @消息列表卡片 |
| `/mentions-auto on\|off [interval]` | 开启/关闭自动检查 | 状态确认卡片 |
| `/mentions-config` | 配置黑名单/重点群 | 配置管理卡片 |
| `/mentions-status` | 查看@消息检查状态 | 状态信息卡片 |
| `/commands` | 命令总览 | 所有命令列表卡片 |
| `/config` | 统一配置入口 | 配置管理卡片 |

**整合命令**:

- OAuth 命令保持不变，但在 `/commands` 中统一展示

#### 2.2.4 card_builder.py（修改）

**新增卡片构建方法**:

1. **build_mentions_card(mentions: List[MentionInfo])**
   - 展示未回复@消息列表
   - 每条消息包含：群名、时间、发送者、内容预览、跳转链接
   - 支持分页（超过10条）

2. **build_commands_card()**
   - 展示所有可用命令
   - 分类：会话管理、@消息检测、配置管理、其他
   - 每个命令包含：名称、描述、示例

3. **build_config_card(config: dict)**
   - 展示当前配置
   - 支持交互式修改（按钮切换开关）

4. **build_mention_status_card(status: dict)**
   - 显示自动检查状态
   - 上次检查时间、下次检查时间
   - 当前未回复数量

---

## 3. 数据流设计

### 3.1 自动检查流程

```
[定时器 10分钟]
  → MentionPoller._run_check_loop()
  → ConfigService.get("mention.auto_check_enabled")
  → [enabled] MentionPoller._check_mentions(user_id)

      [检查所有群]
      → UserApi.get_user_chats(user_id, page_token)
      → 过滤黑名单（ConfigService）
      → 优先处理重点群

      [遍历每个群]
      → UserApi.get_chat_messages(user_id, chat_id, 50)
      → 检查主消息流中的 @mentions
      → 找出根消息（thread_id + no parent_id）
      → UserApi.get_thread_messages(user_id, thread_id, 50)
      → 检查话题回复中的 @mentions

      [判断是否回复]
      → 检查 @消息时间之后是否有自己的回复
      → 记录未回复消息

  → MentionState.get_new_mentions(current_unreplied)
  → [有新增] CardBuilder.build_mentions_card()
  → CardService.send_card(user_chat_id, card)
  → MentionState.save()
```

### 3.2 手动检查流程

```
[用户] 发送 /check-mentions
  → LarkHandler._cmd_check_mentions(user_id, chat_id)
  → MentionPoller.check_now(user_id, chat_id)
  → [执行检查逻辑 - 同自动检查]
  → 返回所有未回复@（不仅新增）
  → CardService.send_card(chat_id, mentions_card)
```

### 3.3 配置修改流程

```
[用户] 发送 /mentions-auto on 15
  → LarkHandler._cmd_mentions_auto(user_id, chat_id, "on 15")
  → ConfigService.set("mention.auto_check_enabled", True)
  → ConfigService.set("mention.check_interval_minutes", 15)
  → ConfigService.save()
  → MentionPoller.restart_with_new_interval(15)
  → CardService.send_card(chat_id, status_card)
```

---

## 4. 错误处理与边界情况

### 4.1 OAuth 未授权

**场景**: 用户未完成 OAuth 授权

**检测**:
```python
if not oauth_service.has_valid_token(user_id):
    # 发送授权引导卡片
```

**处理**:
1. 发送授权引导卡片
2. 卡片包含"立即授权"按钮
3. 自动检查暂停（每次检查时都会检测授权状态）
4. 授权完成后自动恢复

### 4.2 API 限流

**场景**: 飞书 API 返回 429 Too Many Requests

**检测**:
```python
try:
    result = await user_api._request(...)
except UserApiError as e:
    if e.code == 429:
        # 触发限流处理
```

**处理**:
1. 记录限流事件
2. 延长检查间隔（10分钟 → 30分钟）
3. 向用户发送提醒卡片
4. 连续3次成功后恢复正常间隔

### 4.3 群数量过多

**场景**: 用户加入 200+ 个群

**策略**:
1. **分批检查**: 每次最多检查 50 个群
2. **轮换策略**: 记录上次检查到的位置，下次从该位置继续
3. **优先级**: 重点群每次都检查，其他群轮换
4. **手动全量**: 用户可以发送 `/check-mentions --all` 强制全量检查

**实现**:
```python
# mention_state.json
{
  "last_batch_page_token": "xxx",
  "priority_last_check": 1234567890,
  "rotation_last_check": 1234567800
}
```

### 4.4 网络异常

**场景**: 网络断开或飞书 API 不可达

**处理**:
1. 捕获网络异常
2. 记录失败次数
3. 使用指数退避重试（1分钟、2分钟、4分钟）
4. 连续失败 5 次后暂停自动检查并通知用户

### 4.5 话题消息获取失败

**场景**: 大量 "invalid container_id" 错误

**原因**: 不是所有消息都有话题

**处理**:
1. 只对有 `thread_id` 的根消息查询话题
2. 捕获并忽略 "invalid container_id" 错误
3. 记录成功率，如果低于 20% 则记录警告日志

---

## 5. 配置与菜单

### 5.1 飞书机器人菜单配置

**当前菜单** (5个):
```
会话列表 | 菜单 | 创建群组 | 帮助 | 当前状态
```

**优化后菜单** (3个):
```
会话列表 | 检查@消息 | 命令总览
```

**菜单配置步骤**（在飞书开放平台）:

1. 登录 https://open.feishu.cn/
2. 进入你的应用 → 应用功能 → 机器人
3. 找到"菜单配置"
4. 删除现有菜单
5. 添加新菜单：

| 菜单名称 | 发送内容 | 类型 |
|---------|---------|------|
| 会话列表 | /list | 发送文字消息 |
| 检查@消息 | /check-mentions | 发送文字消息 |
| 命令总览 | /commands | 发送文字消息 |

6. 保存并发布

### 5.2 默认配置

```json
{
  "version": "1.0",
  "mention": {
    "auto_check_enabled": true,
    "check_interval_minutes": 10,
    "blacklist_chats": [],
    "priority_chats": [],
    "notify_priority_only": false
  },
  "notification": {
    "on_complete": true,
    "on_error": true,
    "urgent_at_mention": true
  },
  "ui": {
    "message_mode": "text",
    "bypass_permission": false
  }
}
```

**配置说明**:

- `auto_check_enabled: true` - 用户完成 OAuth 授权后自动开启
- `check_interval_minutes: 10` - 默认 10 分钟检查一次
- 通知方式：只通知"新增"未回复@（相比上次检查）

---

## 6. 测试策略

### 6.1 单元测试

**tests/lark_client/test_mention_poller.py**

```python
class TestMentionPoller:
    def test_check_main_messages():
        """测试主消息流@检测"""

    def test_check_thread_messages():
        """测试话题回复@检测（使用 thread_id）"""

    def test_detect_new_mentions():
        """测试新增@检测逻辑"""

    def test_blacklist_filter():
        """测试黑名单过滤"""

    def test_priority_chats():
        """测试重点群优先检查"""

    def test_batch_rotation():
        """测试大量群的轮换检查"""
```

**tests/lark_client/test_config_service.py**

```python
class TestConfigService:
    def test_load_config():
        """测试配置加载（含默认值）"""

    def test_save_config():
        """测试配置保存"""

    def test_config_validation():
        """测试配置验证（间隔范围、chat_id 格式）"""
```

### 6.2 集成测试

**tests/integration/test_mention_integration.py**

```python
@pytest.mark.integration
class TestMentionIntegration:
    def test_oauth_flow():
        """测试完整 OAuth 流程"""

    def test_check_real_chats():
        """测试真实群消息检查（需要测试 token）"""

    def test_auto_check_loop():
        """测试自动检查循环（短间隔验证）"""

    def test_notification_send():
        """测试通知卡片发送"""
```

### 6.3 测试数据

**Mock 数据结构**:

```python
MOCK_CHATS = [
    {"chat_id": "oc_test1", "name": "测试群1"},
    {"chat_id": "oc_test2", "name": "测试群2"}
]

MOCK_MESSAGES = [
    {
        "message_id": "om_xxx1",
        "create_time": "1234567890000",
        "sender": {"id": "ou_sender1"},
        "mentions": [{"id": "ou_testuser"}],
        "body": {"content": '{"text": "test"}'},
        "msg_type": "text"
    }
]

MOCK_THREAD_MESSAGES = [...]
```

---

## 7. 实施计划

### 7.1 阶段划分

**阶段 1: 核心功能开发** (预计 2 天)
- 实现 `mention_poller.py`
- 实现 `config_service.py`
- 手动检查命令 `/check-mentions`

**阶段 2: 自动检查** (预计 1 天)
- 自动轮询逻辑
- 状态持久化
- 新增@检测

**阶段 3: 配置与优化** (预计 1 天)
- 配置管理命令
- 黑名单/重点群
- 批量检查优化

**阶段 4: 卡片与命令** (预计 1 天)
- 新增所有卡片类型
- `/commands` 命令
- 菜单配置文档

**阶段 5: 测试与清理** (预计 1 天)
- 单元测试
- 集成测试
- 删除临时 test_*.py 文件
- 代码审查

### 7.2 文件清单

**新增文件**:
- `lark_client/mention_poller.py` (~500 行)
- `lark_client/config_service.py` (~200 行)
- `tests/lark_client/test_mention_poller.py` (~300 行)
- `tests/lark_client/test_config_service.py` (~150 行)
- `tests/integration/test_mention_integration.py` (~200 行)

**修改文件**:
- `lark_client/lark_handler.py` (+300 行)
- `lark_client/card_builder.py` (+400 行)
- `lark_client/main.py` (+50 行)

**删除文件**:
- `test_*.py` (根目录下所有临时测试脚本，约 14 个文件)

**总代码量**: 约 +2300 行新增，-1000 行删除

---

## 8. 风险与注意事项

### 8.1 API 调用量

**风险**: 用户加入大量群时，API 调用量可能很大

**缓解**:
- 分批检查，默认每次最多 50 个群
- 重点群优先，其他群轮换
- 用户可配置检查间隔（最小 5 分钟）

### 8.2 隐私与权限

**风险**: 用户 OAuth 授权后，系统可以读取所有群消息

**缓解**:
- 明确的授权说明：只读权限，不发送消息
- 配置保存在本地 `~/.remote-claude/`
- 用户可随时撤销授权

### 8.3 性能影响

**风险**: 轮询逻辑可能影响主进程性能

**缓解**:
- 使用异步 I/O（asyncio）
- 检查逻辑在独立任务中运行
- 可配置关闭自动检查

### 8.4 话题消息检测准确性

**风险**: 话题消息结构复杂，可能漏检

**缓解**:
- 记录详细日志（检查了多少话题，成功/失败比例）
- 用户可手动触发全量检查
- 提供反馈入口

---

## 9. 后续优化方向

### 9.1 功能增强

1. **消息摘要** - 对长消息自动生成摘要
2. **按群分类** - 按群或项目分组展示@消息
3. **快速回复** - 直接在通知卡片中回复
4. **已读标记** - 标记已处理但未回复的@

### 9.2 性能优化

1. **增量更新** - 只检查有新消息的群
2. **缓存** - 缓存群列表，减少 API 调用
3. **并发检查** - 多个群并发检查（控制并发度）

### 9.3 体验优化

1. **通知分级** - 区分普通@和紧急@（根据发送者）
2. **免打扰时段** - 配置免打扰时间，不发送通知
3. **统计报表** - 每周/每月@消息统计

---

## 10. 总结

本设计方案为 Remote Claude 飞书客户端增加了完整的@消息检测功能，包括：

- ✅ OAuth 用户授权机制
- ✅ 手动和自动@消息检查
- ✅ 话题回复检测（使用正确的 thread_id）
- ✅ 黑名单和重点群过滤
- ✅ 统一配置管理
- ✅ 完整的错误处理
- ✅ 菜单优化和命令总览

架构遵循项目现有模式（Poller 模式），代码职责清晰，易于测试和维护。

---

**批准签名**: _____________
**批准日期**: _____________
