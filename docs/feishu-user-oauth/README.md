# 飞书用户授权模式（User OAuth）实现

## 需求概述

为 Remote Claude 添加飞书用户授权模式（OAuth 2.0），使机器人能够以用户身份访问飞书资源，包括机器人不在的群聊、用户文档、日历等。

## 文档结构

```
docs/feishu-user-oauth/
├── README.md           # 本文件 - 需求概述和快速导航
├── task_plan.md        # 详细的 10 阶段实施计划
├── findings.md         # 技术调研和设计决策记录
└── progress.md         # 实施进度和会话日志
```

## 快速链接

- **[实施计划](task_plan.md)** - 查看 10 个阶段的详细任务和时间估算
- **[技术调研](findings.md)** - 查看 OAuth 流程、API 功能、安全方案等调研结果
- **[进度跟踪](progress.md)** - 查看当前进度和下一步行动

## 核心目标

### 当前模式（Tenant Access Token）
- ✅ 简单安全
- ❌ 只能访问机器人所在的群

### 目标模式（User Access Token）
- ✅ 可以访问用户的所有群（包括机器人不在的群）
- ✅ 以用户身份发消息和操作
- ⚠️ 需要用户主动授权

## 技术栈

- **OAuth 2.0 授权码模式**
- **aiohttp** - 异步 HTTP 服务器（OAuth 回调）
- **飞书 Open API** - 用户权限 API 调用
- **加密存储** - Token 安全管理

## 实施概览

| 阶段 | 内容 | 工时 | 状态 |
|------|------|------|------|
| Phase 1 | OAuth 服务模块 | 1.5h | pending |
| Phase 2 | OAuth 回调服务器 | 1.5h | pending |
| Phase 3 | 配置与开关 | 1h | pending |
| Phase 4 | 集成到飞书客户端 | 1.5h | pending |
| Phase 5 | 用户授权管理命令 | 1.5h | pending |
| Phase 6 | User Token API 调用 | 2h | pending |
| Phase 7 | 实验功能（跨群转发） | 2h | pending |
| Phase 8 | 安全加固 | 3h | pending |
| Phase 9 | 测试与文档 | 2h | pending |
| Phase 10 | 权限申请与发布 | 2h | pending |
| **总计** | | **18h** | **0% 完成** |

## 当前状态

- **进度**：0% - 规划完成，待开始实施
- **当前阶段**：Phase 1 - OAuth 服务模块
- **开始时间**：2026-03-24
- **负责人**：待定

## 风险提示

⚠️ **高风险项**：
- User Access Token 拥有用户完整权限，安全风险高
- 需要飞书开放平台权限审核，可能被拒
- 用户可能误操作转发敏感信息

✅ **缓解措施**：
- Token 加密存储、审计日志
- 操作前二次确认
- 最小权限原则
- 完善的用户教育文档

## 参考资料

- [飞书 OAuth 授权文档](https://open.feishu.cn/document/authentication-management/access-token/get-user-access-token)
- [飞书 API 权限列表](https://feishu.apifox.cn/doc-1939254)
- [OpenClaw 飞书集成](https://docs.openclaw.ai/zh-CN/channels/feishu)

---

**文档创建时间**：2026-03-24
**最后更新**：2026-03-24
**状态**：Active - 规划中
