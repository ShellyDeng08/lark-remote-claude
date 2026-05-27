#!/usr/bin/env python3
"""
Remote Claude 飞书客户端

通过飞书聊天控制 remote_claude 会话
"""

import asyncio
import json
import logging
import os
import signal
import sys
import urllib.request
from pathlib import Path


# 设置 sys.path 以导入 utils 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.session import USER_DATA_DIR


def _setup_logging():
    """配置 lark_client 日志：INFO → lark_client.log, DEBUG → lark_client.debug.log"""
    from .config import LARK_LOG_LEVEL

    log_dir = USER_DATA_DIR
    log_dir.mkdir(parents=True, exist_ok=True)

    # 日志格式（含毫秒级时间戳）
    log_format = "%(asctime)s.%(msecs)03d [%(name)s] %(levelname)s %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    # 根 logger 配置
    root_logger = logging.getLogger()
    root_logger.setLevel(LARK_LOG_LEVEL)

    # 清除默认 handler
    root_logger.handlers.clear()

    # 正常日志文件（INFO 及以上）
    info_handler = logging.FileHandler(log_dir / "lark_client.log", encoding="utf-8")
    info_handler.setLevel(logging.INFO)
    info_handler.setFormatter(formatter)
    root_logger.addHandler(info_handler)

    # 调试日志文件（DEBUG 及以上，仅当 LARK_LOG_LEVEL=DEBUG 时写入）
    if LARK_LOG_LEVEL == logging.DEBUG:
        debug_handler = logging.FileHandler(log_dir / "lark_client.debug.log", encoding="utf-8")
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(formatter)
        root_logger.addHandler(debug_handler)

    # 第三方库保持 INFO 级别
    for _noisy in ('urllib3', 'websockets', 'asyncio'):
        logging.getLogger(_noisy).setLevel(logging.INFO)

    # 控制台输出（仅在终端交互模式下启用，守护进程模式下 stderr 已重定向到日志文件）
    if sys.stderr.isatty():
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        root_logger.addHandler(console_handler)


# 在导入 lark SDK 之前配置日志
_setup_logging()

import lark_oapi as lark

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1, P2ImChatMemberBotAddedV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast
)

from . import config
from .lark_handler import handler
from .card_service import card_service


async def _graceful_shutdown() -> None:
    """优雅关闭：更新所有活跃流式卡片为已断开状态后退出"""
    try:
        await handler.disconnect_all_for_shutdown()
    except Exception as e:
        print(f"[Lark] graceful shutdown 异常: {e}")
    finally:
        import os
        os._exit(0)

def check_user_allowed(user_id: str) -> bool:
    """检查用户是否在白名单中"""
    if not config.ENABLE_USER_WHITELIST:
        return True
    return user_id in config.ALLOWED_USERS


async def _handle_image_message(handler, user_id: str, chat_id: str, message_id: str, image_key: str):
    """下载飞书图片并转发路径给 Claude"""
    from .card_service import card_service as _cs
    local_path = await _cs.download_image(message_id, image_key)
    if local_path:
        text = f"[用户发送了一张图片，本地路径：{local_path}]"
        await handler.forward_to_claude(user_id, chat_id, text)
    else:
        await _cs.send_text(chat_id, "图片下载失败，请重试")


def _record_dm_chat(user_id: str, chat_id: str) -> None:
    """记录私聊chat_id，用于后续私聊消息检测"""
    try:
        dm_file = USER_DATA_DIR / "dm_chats.json"

        # 读取现有数据
        if dm_file.exists():
            with open(dm_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            data = {"dm_chats": []}

        # 检查是否已存在
        dm_chats = data.get("dm_chats", [])
        chat_info = {"user_id": user_id, "chat_id": chat_id}

        # 如果不存在则添加
        if not any(dm.get("chat_id") == chat_id for dm in dm_chats):
            dm_chats.append(chat_info)
            data["dm_chats"] = dm_chats

            # 保存
            with open(dm_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)

            print(f"[DM] 记录私聊: {user_id[:8]}... -> {chat_id[:8]}...")
    except Exception as e:
        print(f"[DM] 记录私聊失败: {e}")


def _collect_post_text(node) -> str:
    """递归提取 post 富文本中的可见文本。"""
    if isinstance(node, str):
        return node

    if isinstance(node, list):
        parts = [(_collect_post_text(item) or "").strip() for item in node]
        return "\n".join([p for p in parts if p]).strip()

    if not isinstance(node, dict):
        return ""

    tag = node.get("tag")
    if tag == "at":
        name = node.get("name") or node.get("user_name") or ""
        if name:
            return f"@{name}"

    # 常见字段优先
    direct_parts = []
    for key in ("text", "content", "title", "name"):
        val = node.get(key)
        if isinstance(val, str) and val.strip():
            direct_parts.append(val.strip())

    nested_parts = []
    for key, val in node.items():
        if key in ("text", "title", "name", "tag"):
            continue
        # 过滤结构化标识字段，避免把 image_key/open_id 之类机器字段当用户文本
        if key.endswith("_key") or key in ("id", "open_id", "union_id", "user_id", "chat_id", "message_id"):
            continue
        got = (_collect_post_text(val) or "").strip()
        if got:
            nested_parts.append(got)

    merged = "\n".join([p for p in (direct_parts + nested_parts) if p]).strip()
    return merged


def _extract_message_text(message_type: str, content_raw: str) -> str:
    """统一提取飞书消息文本内容（text / post）。"""
    try:
        content = json.loads(content_raw)
    except Exception:
        return ""

    if message_type == "text":
        return (content.get("text", "") or "").strip()

    if message_type == "post":
        # 优先从 locale payload 中提取
        locale_payload = None
        for v in content.values():
            if isinstance(v, dict) and "content" in v:
                locale_payload = v
                break

        if isinstance(locale_payload, dict):
            lines = []
            title = (locale_payload.get("title") or "").strip()
            if title:
                lines.append(title)

            body = _collect_post_text(locale_payload.get("content") or [])
            if body:
                lines.append(body)

            merged = "\n".join([ln for ln in lines if ln]).strip()
            if merged:
                return merged

        # 兜底：直接递归整个 JSON
        return _collect_post_text(content).strip()

    return ""


def handle_message_receive(data: P2ImMessageReceiveV1) -> None:
    """处理收到的消息"""
    try:
        print(f"[DEBUG] handle_message_receive 被调用")

        # 首次收到消息时，尝试启动mention_poller（此时事件循环已运行）
        if hasattr(handler, 'mention_poller') and handler.mention_poller and not handler.mention_poller._running:
            try:
                loop = asyncio.get_running_loop()
                if loop and not loop.is_closed():
                    handler.mention_poller._running = True
                    handler.mention_poller._task = loop.create_task(handler.mention_poller._run_check_loop())
                    print("[MentionPoller] 自动检查已启动（事件循环就绪）")
            except Exception as e:
                print(f"[MentionPoller] 启动失败: {e}")

        event = data.event
        message = event.message
        sender = event.sender

        # 获取基本信息
        sender_id = sender.sender_id.open_id  # 发送者ID
        chat_id = message.chat_id
        message_type = message.message_type
        chat_type = message.chat_type

        # 如果是私聊，传递给 mention_poller 进行实时推送
        if chat_type == "p2p":
            # 从 dm_chats.json 反向查找接收者（owner）的 user_id
            recipient_user_id = None
            try:
                dm_chats_file = USER_DATA_DIR / "dm_chats.json"
                if dm_chats_file.exists():
                    with open(dm_chats_file, 'r', encoding='utf-8') as f:
                        data = json.load(f)
                        for item in data.get("dm_chats", []):
                            if item.get("chat_id") == chat_id:
                                recipient_user_id = item.get("user_id")
                                break
            except Exception as e:
                print(f"[DM] 查找接收者失败: {e}")

            # 如果找到了接收者，记录这个映射（如果还没记录）
            if recipient_user_id:
                _record_dm_chat(recipient_user_id, chat_id)
            else:
                # 如果找不到接收者，说明这是新的私聊，需要用户授权后才能处理
                print(f"[DM] 收到私聊消息但无法确定接收者，chat_id={chat_id[:10]}...")
                # 不处理这条消息，因为我们不知道是给谁的
                recipient_user_id = sender_id  # 暂时用发送者ID作为占位符

            # 传递给 mention_poller 进行实时推送
            if hasattr(handler, 'mention_poller') and handler.mention_poller and recipient_user_id:
                try:
                    # 构造私聊消息数据
                    dm_message_data = {
                        "message_id": message.message_id,
                        "chat_id": chat_id,
                        "sender_id": sender_id,  # 发送者ID
                        "recipient_id": recipient_user_id,  # 接收者ID（新增）
                        "content": message.content,
                        "create_time": message.create_time,
                    }
                    handler.mention_poller.add_dm_message(dm_message_data)
                except Exception as e:
                    print(f"[DM] 传递私聊消息给 mention_poller 失败: {e}")

        # 对于白名单检查和后续处理，user_id 指的是发送者
        user_id = sender_id

        # 检查用户白名单
        if not check_user_allowed(user_id):
            print(f"[Lark] 用户 {user_id} 不在白名单中")
            return

        message_id = message.message_id

        # 处理图片消息
        if message_type == "image":
            content = json.loads(message.content)
            image_key = content.get("image_key", "")
            if image_key:
                print(f"[Lark] 收到图片消息: {user_id[:8]}..., image_key={image_key}")
                asyncio.create_task(_handle_image_message(handler, user_id, chat_id, message_id, image_key))
            return

        # text / post 都尝试提取文本；其余类型继续忽略
        if message_type not in ("text", "post"):
            print(f"[Lark] 忽略非文本消息: {message_type}")
            return

        text = _extract_message_text(message_type, message.content)

        # 移除 @ 提及
        if message.mentions:
            for mention in message.mentions:
                text = text.replace(f"@_{mention.key}", "").strip()
                text = text.replace(mention.key, "").strip()

        if not text:
            if chat_type != "p2p":
                # 仅在 text + @ 场景下才视作“只@无文本”入口
                if message_type == "text" and message.mentions:
                    asyncio.create_task(handler.handle_message(user_id, chat_id, "", chat_type=chat_type))
                else:
                    print(
                        f"[Lark] 文本提取为空: type={message_type}, chat={chat_id[:8]}..., "
                        f"content={message.content[:120]!r}"
                    )
                    asyncio.create_task(
                        card_service.send_text(
                            chat_id,
                            "⚠️ 未识别到可解析文本，请直接发送纯文本消息后重试。"
                        )
                    )
            return

        print(f"[Lark] 收到消息: {user_id[:8]}... -> {text[:50]}...")

        # 异步处理消息（传入 chat_type 以支持群聊路由）
        asyncio.create_task(handler.handle_message(user_id, chat_id, text, chat_type=chat_type))

    except Exception as e:
        print(f"[Lark] 处理消息异常: {e}")
        import traceback
        traceback.print_exc()


def handle_chat_member_bot_added(data: P2ImChatMemberBotAddedV1) -> None:
    """机器人被拉入群时主动推送入口卡片（仅专属群）。"""
    try:
        event = data.event
        chat_id = event.chat_id
        operator_id = event.operator_id.open_id if event.operator_id else ""
        if not chat_id:
            return

        print(f"[Lark] bot added event: chat={chat_id[:8]}..., operator={operator_id[:8] if operator_id else 'N/A'}...")
        asyncio.create_task(handler._show_group_entry_card(operator_id or "system", chat_id))
    except Exception as e:
        print(f"[Lark] 处理 bot added 事件异常: {e}")


_ACTION_HINTS = {
    "list_attach": "执行指令：/attach",
    "list_detach": "执行指令：/detach",
    "list_new_group": "执行指令：/new-group",
    "list_disband_group": "执行指令：/disband-group",
    "list_kill": "执行指令：/kill",
    "dir_browse": "执行指令：/ls",
    "menu_page": "执行指令：/menu",
    "dir_page": "执行指令：/ls",
    "dir_start": "执行指令：/start",
    "dir_new_group": "执行指令：/start + /new-group",
    "menu_detach": "执行指令：/detach",
    "menu_list": "执行指令：/list",
    "menu_help": "执行指令：/help",
    "menu_ls": "执行指令：/ls",
    "menu_tree": "执行指令：/tree",
    "menu_status": "执行指令：/status",
    "stream_detach": "执行指令：/detach",
    "stream_reconnect": "执行指令：/attach",
    "send_key": "执行指令：快捷键",
    "menu_toggle_notify": "执行指令：notify 开关",
    "menu_toggle_urgent": "执行指令：urgent 开关",
    "menu_toggle_bypass": "执行指令：bypass 开关",
    "group_show_recovery": "执行指令：恢复会话",
    "group_reconnect_original": "执行指令：恢复原会话",
    "group_choose_takeover": "执行指令：选择接管会话",
    "group_takeover_session": "执行指令：接管会话",
    "group_summarize_now": "执行指令：/summarize-now",
    "view_round_diff": "执行指令：/diff",
    "menu_open": "执行指令：/menu",
    "select_option": "执行指令：选择选项",
}


async def _visualize_action(chat_id: str, action_type: str, action_value: dict) -> None:
    try:
        hint = _ACTION_HINTS.get(action_type, f"执行动作：{action_type}")
        extra = ""
        if action_type in ("list_attach", "list_new_group", "list_disband_group", "list_kill", "group_takeover_session", "stream_reconnect", "stream_detach"):
            session = str((action_value or {}).get("session", "") or "").strip()
            if session:
                extra = f" `{session}`"
        elif action_type in ("dir_browse", "dir_page"):
            path = str((action_value or {}).get("path", "") or "").strip()
            if path:
                extra = f" `{path}`"
        elif action_type == "send_key":
            key = str((action_value or {}).get("key", "") or "").strip()
            if key:
                extra = f" `{key}`"
        elif action_type == "select_option":
            val = str((action_value or {}).get("value", "") or "").strip()
            if val:
                extra = f" `{val}`"

        await card_service.send_text(chat_id, f"✅ {hint}{extra}")
    except Exception as e:
        print(f"[Lark] 可视化动作提示发送失败: {e}")


def handle_card_action(event: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    """处理卡片按钮点击"""
    try:
        action = event.event.action
        operator = event.event.operator
        context = event.event.context

        user_id = operator.open_id
        chat_id = context.open_chat_id
        message_id = context.open_message_id  # 原始卡片 message_id，用于就地更新
        action_value = action.value or {}

        print(f"[Lark] 收到卡片动作: user={user_id[:8]}..., action={action_value}")

        # 检查用户白名单
        if not check_user_allowed(user_id):
            print(f"[Lark] 用户 {user_id} 不在白名单中")
            toast = CallBackToast()
            toast.type = "error"
            toast.content = "您没有权限操作"
            response = P2CardActionTriggerResponse()
            response.toast = toast
            return response

        # 检测 form 提交（输入框 Enter ↵ 按钮）
        form_value = getattr(action, 'form_value', None)
        if form_value is not None:
            command_text = (form_value.get("command") or "").strip()
            print(f"[Lark] form 提交: user={user_id[:8]}..., command={command_text!r}")
            if command_text:
                # 有输入内容：检查是否有活跃选项
                if handler.has_active_option(chat_id):
                    asyncio.create_task(handler.handle_option_input(user_id, chat_id, command_text))
                else:
                    asyncio.create_task(handler.forward_to_claude(user_id, chat_id, command_text))
            else:
                # 空输入 → 发送原始 Enter 键（用于确认默认选项等场景）
                asyncio.create_task(handler.send_raw_key(user_id, chat_id, "enter"))
            return None

        action_type = action_value.get("action", "")
        if action_type:
            asyncio.create_task(_visualize_action(chat_id, action_type, action_value))

        # 处理选项选择动作
        if action_type == "select_option":
            option_value = action_value.get("value", "")
            option_total = int(action_value.get("total", "0"))
            needs_input = action_value.get("needs_input", False)
            print(f"[Lark] 用户选择了选项: {option_value} (total={option_total}, needs_input={needs_input})")
            asyncio.create_task(handler.handle_option_select(user_id, chat_id, option_value, option_total, needs_input=needs_input))

            # 立即反馈，避免用户感觉“点击无响应”
            toast = CallBackToast()
            toast.type = "info"
            toast.content = f"已选择 {option_value}，正在处理..."
            response = P2CardActionTriggerResponse()
            response.toast = toast
            return response

        # 列表卡片：进入会话
        if action_type == "list_attach":
            session_name = action_value.get("session", "")
            print(f"[Lark] list_attach: session={session_name}")
            asyncio.create_task(handler._cmd_attach(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 列表卡片：断开连接
        if action_type == "list_detach":
            print(f"[Lark] list_detach: chat={chat_id[:8]}...")
            asyncio.create_task(handler._handle_list_detach(user_id, chat_id, message_id=message_id))
            return None

        # 列表卡片：创建群聊
        if action_type == "list_new_group":
            session_name = action_value.get("session", "")
            print(f"[Lark] list_new_group: session={session_name}")
            asyncio.create_task(handler._cmd_new_group(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 列表卡片：解散群聊
        if action_type == "list_disband_group":
            session_name = action_value.get("session", "")
            print(f"[Lark] list_disband_group: session={session_name}")
            asyncio.create_task(handler._cmd_disband_group(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 列表卡片：关闭会话
        if action_type == "list_kill":
            session_name = action_value.get("session", "")
            print(f"[Lark] list_kill: session={session_name}")
            asyncio.create_task(handler._cmd_kill(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 目录卡片：进入子目录（继续浏览，就地更新原卡片）
        if action_type == "dir_browse":
            path = action_value.get("path", "")
            print(f"[Lark] dir_browse: path={path}")
            asyncio.create_task(handler._cmd_ls(user_id, chat_id, path, message_id=message_id))
            return None

        # 菜单卡片：会话列表翻页
        if action_type == "menu_page":
            page = int(action_value.get("page", 0))
            print(f"[Lark] menu_page: page={page}")
            asyncio.create_task(handler._cmd_menu(user_id, chat_id, message_id=message_id, page=page))
            return None

        # 目录卡片：翻页
        if action_type == "dir_page":
            path = action_value.get("path", "")
            page = int(action_value.get("page", 0))
            print(f"[Lark] dir_page: path={path}, page={page}")
            asyncio.create_task(handler._cmd_ls(user_id, chat_id, path, message_id=message_id, page=page))
            return None

        # 目录卡片：在该目录创建新 Claude 会话
        if action_type == "dir_start":
            path = action_value.get("path", "")
            session_name = action_value.get("session_name", "")
            print(f"[Lark] dir_start: path={path}, session={session_name}")
            asyncio.create_task(handler._cmd_start(user_id, chat_id, f"{session_name} {path}"))
            return None

        # 目录卡片：在该目录启动会话并创建专属群聊
        if action_type == "dir_new_group":
            path = action_value.get("path", "")
            session_name = action_value.get("session_name", "")
            print(f"[Lark] dir_new_group: path={path}, session={session_name}")
            asyncio.create_task(handler._cmd_start_and_new_group(user_id, chat_id, session_name, path))
            return None

        # /menu 卡片按钮
        if action_type == "menu_detach":
            asyncio.create_task(handler._cmd_detach(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "menu_list":
            asyncio.create_task(handler._cmd_list(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "menu_help":
            asyncio.create_task(handler._cmd_help(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "menu_ls":
            asyncio.create_task(handler._cmd_ls(user_id, chat_id, "", message_id=message_id))
            return None

        if action_type == "menu_tree":
            asyncio.create_task(handler._cmd_ls(user_id, chat_id, "", tree=True, message_id=message_id))
            return None

        if action_type == "menu_status":
            asyncio.create_task(handler._cmd_status(user_id, chat_id, message_id=message_id))
            return None

        # 流式卡片：断开连接
        if action_type == "stream_detach":
            session_name = action_value.get("session", "")
            print(f"[Lark] stream_detach: session={session_name}")
            asyncio.create_task(handler._handle_stream_detach(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 流式卡片：重新连接
        if action_type == "stream_reconnect":
            session_name = action_value.get("session", "")
            print(f"[Lark] stream_reconnect: session={session_name}")
            asyncio.create_task(handler._handle_stream_reconnect(user_id, chat_id, session_name, message_id=message_id))
            return None

        # 快捷键按钮（callback 模式）
        if action_type == "send_key":
            key_name = action_value.get("key", "")
            times = action_value.get("times", 1)
            print(f"[Lark] send_key: key={key_name}" + (f" ×{times}" if times > 1 else ""))
            async def _multi_send(k=key_name, t=times):
                for _ in range(t):
                    await handler.send_raw_key(user_id, chat_id, k)
                    await asyncio.sleep(0.15)
            asyncio.create_task(_multi_send())
            return None

        if action_type == "menu_toggle_notify":
            asyncio.create_task(handler._cmd_toggle_notify(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "menu_toggle_urgent":
            asyncio.create_task(handler._cmd_toggle_urgent(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "menu_toggle_bypass":
            asyncio.create_task(handler._cmd_toggle_bypass(user_id, chat_id, message_id=message_id))
            return None

        # 群聊离线恢复入口
        if action_type == "group_show_recovery":
            asyncio.create_task(handler._cmd_group_show_recovery(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "group_reconnect_original":
            asyncio.create_task(handler._cmd_group_reconnect_original(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "group_choose_takeover":
            asyncio.create_task(handler._cmd_group_choose_takeover(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "group_takeover_session":
            session_name = action_value.get("session", "")
            asyncio.create_task(handler._cmd_group_takeover_session(user_id, chat_id, session_name, message_id=message_id))
            return None

        if action_type == "group_summarize_now":
            asyncio.create_task(handler._cmd_summarize_now(user_id, chat_id, message_id=message_id))
            return None

        if action_type == "view_round_diff":
            asyncio.create_task(handler._cmd_view_round_diff(user_id, chat_id, message_id=message_id))
            return None

        # 各卡片底部菜单按钮：辅助卡片就地→菜单，流式卡片降级新卡
        if action_type == "menu_open":
            asyncio.create_task(handler._cmd_menu(user_id, chat_id, message_id=message_id))
            return None

        # 默认响应
        return P2CardActionTriggerResponse()

    except Exception as e:
        print(f"[Lark] 处理卡片动作异常: {e}")
        import traceback
        traceback.print_exc()
        return P2CardActionTriggerResponse()


class LarkBot:
    """飞书机器人"""

    def __init__(self):
        self.ws_client = None
        self.running = False
        self.oauth_service = None
        self.oauth_server = None
        self._oauth_loop = None
        self._oauth_thread = None
        self.config_service = None
        self.mention_poller = None
        self.monitor_poller = None
        self.session_manager = None
        self.message_analyzer = None

    def _init_config(self) -> None:
        """初始化配置服务"""
        from .config_service import ConfigService

        self.config_service = ConfigService()
        handler.config_service = self.config_service
        print("配置服务已初始化")

    def _init_oauth(self) -> None:
        """按需初始化 OAuth 服务和回调服务器（仅 ENABLE_USER_AUTH=true 时）。"""
        if not config.ENABLE_USER_AUTH:
            return

        from .oauth_service import LarkOAuthService
        from .oauth_server import OAuthCallbackServer

        self.oauth_service = LarkOAuthService(
            app_id=config.FEISHU_APP_ID,
            app_secret=config.FEISHU_APP_SECRET,
            redirect_uri=config.OAUTH_REDIRECT_URI,
        )
        self.oauth_server = OAuthCallbackServer(
            oauth_service=self.oauth_service,
            host="0.0.0.0",
            port=config.OAUTH_SERVER_PORT,
        )
        # 挂到 handler 上，供命令路由使用（如 /auth 命令）
        handler.oauth_service = self.oauth_service
        print(f"用户授权: 启用 (端口 {config.OAUTH_SERVER_PORT})")

    def _init_mention_poller(self) -> None:
        """初始化@消息轮询器（需要先初始化 OAuth 和 ConfigService）"""
        if not config.ENABLE_USER_AUTH:
            print("@消息检测: 未启用（需要启用用户授权）")
            return

        if not self.oauth_service or not self.config_service:
            print("@消息检测: 初始化失败（依赖 OAuth 和配置服务）")
            return

        from .mention_poller import MentionPoller
        from .user_api import LarkUserApi
        from .card_service import card_service

        user_api = LarkUserApi(self.oauth_service)
        self.mention_poller = MentionPoller(
            config_service=self.config_service,
            user_api=user_api,
            card_sender=card_service,
            oauth_service=self.oauth_service
        )

        # 挂到 handler 上，供命令路由使用
        handler.mention_poller = self.mention_poller

        print("私聊消息: 通过主事件订阅接收")

        # 如果配置了自动检查，则启动轮询
        auto_enabled = self.config_service.get("mention.auto_check_enabled", False)
        if auto_enabled:
            self.mention_poller.start()
            interval = self.config_service.get("mention.check_interval_minutes", 10)
            print(f"@消息检测: 启用（自动检查间隔: {interval} 分钟）")
        else:
            print("@消息检测: 已初始化（自动检查未启用）")

    def _init_monitor_poller(self) -> None:
        """初始化监听轮询器（需要先初始化 OAuth）"""
        if not config.ENABLE_USER_AUTH:
            print("监听功能: 未启用（需要启用用户授权）")
            return

        if not self.oauth_service:
            print("监听功能: 初始化失败（依赖 OAuth）")
            return

        from .monitor_poller import MonitorPoller
        from .monitor_config import MonitorConfigService
        from .session_manager import SessionManager
        from .message_analyzer import MessageAnalyzer
        from .user_api import LarkUserApi
        from .card_service import card_service
        import os

        # 创建 SessionManager
        self.session_manager = SessionManager()

        # 创建 MessageAnalyzer（可选配置 API Key）
        api_key = os.getenv('ANTHROPIC_API_KEY')
        self.message_analyzer = MessageAnalyzer(
            session_manager=self.session_manager,
            api_key=api_key
        )

        # 创建 MonitorConfigService
        monitor_config = MonitorConfigService()

        # 创建 UserApi
        user_api = LarkUserApi(self.oauth_service)

        # 注入到 handler（供命令使用）
        handler.user_api = user_api

        # 创建 MonitorPoller
        self.monitor_poller = MonitorPoller(
            monitor_config=monitor_config,
            user_api=user_api,
            oauth_service=self.oauth_service,
            card_service=card_service,
            message_analyzer=self.message_analyzer
        )

        print("监听功能: 已初始化")

    def _stop_mention_poller(self) -> None:
        """停止@消息轮询器"""
        if self.mention_poller:
            self.mention_poller.stop()
            print("@消息检测: 已停止")

    def _stop_monitor_poller(self) -> None:
        """停止监听轮询器"""
        if self.monitor_poller:
            self.monitor_poller.stop()
            print("监听功能: 已停止")

    def _start_oauth_server(self) -> None:
        """在独立线程中启动 OAuth 回调服务器，不阻塞主 WebSocket。"""
        if not self.oauth_server:
            return

        import threading

        def _run_oauth():
            self._oauth_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._oauth_loop)
            try:
                self._oauth_loop.run_until_complete(self.oauth_server.start())
                self._oauth_loop.run_forever()
            except Exception as e:
                print(f"[OAuth] 服务器异常退出: {e}")
            finally:
                self._oauth_loop.close()

        self._oauth_thread = threading.Thread(
            target=_run_oauth, name="oauth-server", daemon=True
        )
        self._oauth_thread.start()
        print(f"OAuth 授权页面: http://localhost:{config.OAUTH_SERVER_PORT}/oauth/authorize")

    def _stop_oauth_server(self) -> None:
        """停止 OAuth 回调服务器。"""
        if self._oauth_loop and self.oauth_server:
            try:
                asyncio.run_coroutine_threadsafe(
                    self.oauth_server.stop(), self._oauth_loop
                ).result(timeout=5)
            except Exception as e:
                print(f"[OAuth] 停止服务器异常: {e}")
            finally:
                self._oauth_loop.call_soon_threadsafe(self._oauth_loop.stop)

    def start(self):
        """启动机器人"""
        # 检查配置
        if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
            print("错误: 请配置 FEISHU_APP_ID 和 FEISHU_APP_SECRET")
            print("在 ~/.remote-claude/.env 文件中添加:")
            print("  FEISHU_APP_ID=your_app_id")
            print("  FEISHU_APP_SECRET=your_app_secret")
            return

        print("=" * 50)
        print("Remote Claude 飞书客户端")
        print("=" * 50)
        print(f"App ID: {config.FEISHU_APP_ID[:8]}...")
        print(f"白名单: {'启用' if config.ENABLE_USER_WHITELIST else '禁用'}")

        # 初始化配置服务
        self._init_config()

        # 初始化 OAuth（按配置决定）
        self._init_oauth()
        if not config.ENABLE_USER_AUTH:
            print("用户授权: 禁用")

        # 初始化@消息轮询器（依赖 OAuth 和配置服务）
        self._init_mention_poller()

        # 初始化监听轮询器（依赖 OAuth）
        self._init_monitor_poller()

        print("=" * 50)

        # 设置信号处理
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        # 创建事件处理器
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(handle_message_receive) \
            .register_p2_im_chat_member_bot_added_v1(handle_chat_member_bot_added) \
            .register_p2_card_action_trigger(handle_card_action) \
            .build()

        # 创建 WebSocket 客户端
        self.ws_client = lark.ws.Client(
            config.FEISHU_APP_ID,
            config.FEISHU_APP_SECRET,
            event_handler=event_handler,
            log_level=lark.LogLevel.INFO,
        )

        # 代理兼容：检测 SOCKS 代理，按配置决定是否绕过
        proxy_info = urllib.request.getproxies()
        socks_proxy = (proxy_info.get('socks') or proxy_info.get('all')
                       or proxy_info.get('https') or proxy_info.get('http'))
        if socks_proxy and 'socks' in socks_proxy.lower():
            if config.LARK_NO_PROXY:
                # 用户选择绕过代理 → 清除代理环境变量
                for var in ('ALL_PROXY', 'all_proxy', 'HTTPS_PROXY', 'https_proxy',
                            'HTTP_PROXY', 'http_proxy', 'SOCKS_PROXY', 'socks_proxy'):
                    os.environ.pop(var, None)
                print(f"检测到 SOCKS 代理 ({socks_proxy})，已按 LARK_NO_PROXY=1 绕过")
            else:
                print(f"检测到 SOCKS 代理 ({socks_proxy})，将通过代理连接")
                print("  如连接失败，可在 .env 中设置 LARK_NO_PROXY=1 绕过代理")

        # 启动 OAuth 回调服务器（独立线程，不阻塞）
        self._start_oauth_server()

        self.running = True
        print("\n机器人已启动，等待消息...")
        print("在飞书中发送 /help 查看使用说明\n")

        # 延迟启动轮询器（给 WebSocket 事件循环 2 秒启动时间）
        if self.mention_poller:
            import threading
            def delayed_start_poller():
                import time
                time.sleep(2)
                try:
                    loop = asyncio.get_event_loop()
                    if loop and loop.is_running() and not self.mention_poller._running:
                        self.mention_poller._running = True
                        future = asyncio.run_coroutine_threadsafe(
                            self.mention_poller._run_check_loop(),
                            loop
                        )
                        self.mention_poller._task = future
                        print("[MentionPoller] 自动检查已启动（延迟启动成功）")
                except Exception as e:
                    print(f"[MentionPoller] 延迟启动失败: {e}")

            threading.Thread(target=delayed_start_poller, daemon=True).start()

        # 延迟启动监听轮询器（给 WebSocket 事件循环 2 秒启动时间）
        if self.monitor_poller:
            import threading
            def delayed_start_monitor():
                import time
                time.sleep(2)
                try:
                    loop = asyncio.get_event_loop()
                    if loop and loop.is_running():
                        self.monitor_poller.start()
                        print("[MonitorPoller] 定时监听已启动")

                        # 每小时清理空闲会话
                        async def cleanup_loop():
                            while self.running:
                                await asyncio.sleep(3600)  # 1 小时
                                if self.session_manager:
                                    await self.session_manager.cleanup_idle_sessions()
                                    print("[SessionManager] 🧹 清理空闲会话")

                        asyncio.run_coroutine_threadsafe(cleanup_loop(), loop)
                except Exception as e:
                    print(f"[MonitorPoller] 延迟启动失败: {e}")

            threading.Thread(target=delayed_start_monitor, daemon=True).start()

        # 启动 WebSocket（阻塞）
        self.ws_client.start()

    def _signal_handler(self, signum, frame):
        """处理退出信号（SIGTERM / SIGINT）"""
        print("\n正在关闭...")
        self.running = False
        # 停止@消息轮询器
        self._stop_mention_poller()
        # 停止监听轮询器
        self._stop_monitor_poller()
        # 停止 OAuth 服务器
        self._stop_oauth_server()
        # 调度异步清理（更新所有活跃卡片为已断开状态后退出）
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(_graceful_shutdown())
                )
                return
        except Exception:
            pass
        sys.exit(0)


def main():
    """入口函数"""
    bot = LarkBot()
    bot.start()


if __name__ == "__main__":
    main()
