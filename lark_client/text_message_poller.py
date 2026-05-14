"""
文本消息轮询器 - 发送普通文本消息而非卡片

用于普通聊天模式，Claude 的回复作为普通文本消息发送。

跟踪策略：用数字索引（next_idx）跟踪已处理的位置，而非 block_id。
因为 block_id 基于内容生成，streaming 中和完成后 block_id 可能不同，
用 block_id 匹配会导致旧内容被重复发送。
"""

import asyncio
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger('TextMessagePoller')

# 添加 server/ 目录到路径
_root = Path(__file__).parent.parent
sys.path.insert(0, str(_root / "server"))
sys.path.insert(0, str(_root))

from utils.session import ensure_user_data_dir, USER_DATA_DIR

POLL_INTERVAL = 1.0  # 轮询间隔（秒）
RAPID_INTERVAL = 0.3  # 快速轮询间隔
RAPID_DURATION = 3.0  # 快速轮询持续时间


@dataclass
class TextTracker:
    """单个 chat_id 的文本消息跟踪状态"""
    chat_id: str
    session_name: str
    display_name: str = ""     # 卡片标题显示名（项目目录名）
    reader: Optional[Any] = None
    next_idx: int = 0          # 下一个待处理的 block 索引
    pending_idx: int = -1      # 正在 streaming 的 block 索引（等它完成后发送），-1 表示无
    is_group: bool = False
    last_option_block_id: str = ""  # 已发送的 option_block 的 block_id，避免重复发送
    sent_block_ids: set = field(default_factory=set)  # 已发送的 block_id 集合，防止骤降重置后重复发送


class TextMessagePoller:
    """
    文本消息轮询器

    监听共享内存中的输出变化，发送普通文本消息。
    与卡片模式互斥，用户可以选择使用哪种模式。
    """

    def __init__(self, card_service: Any):
        self._card_service = card_service
        self._trackers: Dict[str, TextTracker] = {}
        self._tasks: Dict[str, asyncio.Task] = {}
        self._kick_events: Dict[str, asyncio.Event] = {}
        self._rapid_until: Dict[str, float] = {}

    def start(self, chat_id: str, session_name: str, is_group: bool = False,
              display_name: str = "") -> None:
        """启动轮询"""
        self.stop(chat_id)

        tracker = TextTracker(
            chat_id=chat_id,
            session_name=session_name,
            display_name=display_name or "Claude",
            is_group=is_group
        )

        # 跳过共享内存中已有的 blocks，只发送启动后的新内容
        try:
            from shared_state import get_mq_path, SharedStateReader
            mq_path = get_mq_path(session_name)
            if mq_path.exists():
                reader = SharedStateReader(session_name)
                state = reader.read()
                blocks = state.get("blocks", [])
                # 将 next_idx 设为当前 blocks 数量，跳过所有历史
                tracker.next_idx = len(blocks)
                # 将历史 blocks 的 block_id 加入已发送集合，防止骤降后重发
                for b in blocks:
                    bid = b.get("block_id", "")
                    if bid:
                        tracker.sent_block_ids.add(bid)
                tracker.reader = reader  # 复用已创建的 reader
                logger.info(f"跳过 {len(blocks)} 个已有 blocks")
        except Exception as e:
            logger.warning(f"初始化跳过历史 blocks 失败: {e}")

        self._trackers[chat_id] = tracker
        self._kick_events[chat_id] = asyncio.Event()

        task = asyncio.create_task(self._poll_loop(chat_id))
        task.add_done_callback(lambda t: self._on_task_done(t, chat_id))
        self._tasks[chat_id] = task
        logger.info(f"文本消息轮询器启动: chat_id={chat_id[:8]}..., session={session_name}")

    def stop(self, chat_id: str) -> None:
        """停止轮询"""
        task = self._tasks.pop(chat_id, None)
        if task:
            task.cancel()

        self._kick_events.pop(chat_id, None)
        self._rapid_until.pop(chat_id, None)

        tracker = self._trackers.pop(chat_id, None)
        if tracker and tracker.reader:
            try:
                tracker.reader.close()
            except Exception:
                pass
        logger.info(f"文本消息轮询器停止: chat_id={chat_id[:8]}...")

    def kick(self, chat_id: str) -> None:
        """触发立即轮询"""
        self._rapid_until[chat_id] = time.time() + RAPID_DURATION
        ev = self._kick_events.get(chat_id)
        if ev:
            ev.set()

    def _on_task_done(self, task: asyncio.Task, chat_id: str) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc:
            logger.error(f"文本轮询 Task 异常: chat_id={chat_id[:8]}..., {exc}", exc_info=exc)

    async def _poll_loop(self, chat_id: str) -> None:
        """轮询循环"""
        while True:
            try:
                rapid_until = self._rapid_until.get(chat_id, 0)
                interval = RAPID_INTERVAL if time.time() < rapid_until else POLL_INTERVAL

                kick_event = self._kick_events.get(chat_id)
                if kick_event:
                    try:
                        await asyncio.wait_for(kick_event.wait(), timeout=interval)
                        kick_event.clear()
                        self._rapid_until[chat_id] = time.time() + RAPID_DURATION
                    except asyncio.TimeoutError:
                        pass
                else:
                    await asyncio.sleep(interval)

                tracker = self._trackers.get(chat_id)
                if not tracker:
                    break
                await self._poll_once(tracker)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"_poll_once 异常: {e}", exc_info=True)

    async def _poll_once(self, tracker: TextTracker) -> None:
        """单次轮询：读取共享内存 → 检测新内容 → 发送文本消息"""
        # 延迟初始化 Reader
        if tracker.reader is None:
            try:
                from shared_state import get_mq_path, SharedStateReader
                mq_path = get_mq_path(tracker.session_name)
                if not mq_path.exists():
                    return
                tracker.reader = SharedStateReader(tracker.session_name)
                logger.info(f"TextReader 初始化成功: session={tracker.session_name}")
            except Exception as e:
                logger.warning(f"创建 TextReader 失败: {e}")
                return

        # 读取共享内存
        try:
            state = tracker.reader.read()
        except Exception as e:
            logger.error(f"读取共享内存失败: {e}")
            tracker.reader = None
            return

        blocks = state.get("blocks", [])
        total = len(blocks)

        # blocks 骤降检测（会话重启导致 blocks 从头累积）
        if total < tracker.next_idx:
            logger.warning(
                f"blocks 骤降: {tracker.next_idx} -> {total}, 重置索引"
            )
            tracker.next_idx = 0
            tracker.pending_idx = -1

        # 先检查之前 pending 的 streaming block 是否已完成
        if tracker.pending_idx >= 0:
            if tracker.pending_idx < total:
                pending_block = blocks[tracker.pending_idx]
                if pending_block.get("is_streaming", False):
                    return  # 仍在 streaming，等下一轮
                # streaming 完成，发送它
                await self._send_block(tracker, pending_block)
                tracker.next_idx = tracker.pending_idx + 1
            tracker.pending_idx = -1

        # 从 next_idx 开始处理新 blocks
        while tracker.next_idx < total:
            block = blocks[tracker.next_idx]

            if block.get("is_streaming", False):
                # 标记为 pending，等它完成
                tracker.pending_idx = tracker.next_idx
                break

            # 前瞻过滤：如果当前 block 后面紧跟工具调用 block，说明当前是思考过渡文本，跳过
            if self._is_intermediate_block(block, blocks, tracker.next_idx):
                tracker.next_idx += 1
                continue

            # 已完成的 block，直接发送
            await self._send_block(tracker, block)
            tracker.next_idx += 1

        # 检查 option_block（权限确认/选项交互）
        option_block = state.get("option_block")
        if option_block:
            ob_id = option_block.get("block_id", "")
            if ob_id and ob_id != tracker.last_option_block_id:
                await self._send_option_block(tracker, option_block)
                tracker.last_option_block_id = ob_id
        else:
            # option_block 消失（用户已选择或 CLI 状态变化），重置跟踪
            tracker.last_option_block_id = ""

    async def _send_block(self, tracker: TextTracker, block: dict) -> None:
        """发送单个 block 为交互卡片（Markdown 内容 + 底部快捷按钮）"""
        block_type = block.get("_type", "")
        if block_type not in ("OutputBlock", "SystemBlock"):
            return

        # 去重：检查 block_id 是否已发送过（防止 blocks 骤降重置索引后重复发送）
        block_id = block.get("block_id", "")
        if block_id and block_id in tracker.sent_block_ids:
            logger.debug(f"跳过已发送的 block: {block_id[:50]}")
            return

        text = self._format_block(block)
        if text:
            try:
                card = _build_reply_card(text, tracker.display_name)
                await self._card_service.send_interactive_card(tracker.chat_id, card)
                if block_id:
                    tracker.sent_block_ids.add(block_id)
                logger.info(f"发送回复卡片: {text[:50]}...")
            except Exception as e:
                logger.error(f"发送回复卡片失败: {e}")
                # 降级为纯文本
                try:
                    await self._card_service.send_text(tracker.chat_id, text)
                    if block_id:
                        tracker.sent_block_ids.add(block_id)
                except Exception:
                    pass

    async def _send_option_block(self, tracker: TextTracker, option_block: dict) -> None:
        """发送 option_block 为交互卡片（权限确认/选项选择）"""
        sub_type = option_block.get("sub_type", "option")
        options = option_block.get("options", [])
        if not options:
            return

        try:
            card = _build_option_card(option_block)
            await self._card_service.send_interactive_card(tracker.chat_id, card)
            logger.info(f"发送选项卡片: sub_type={sub_type}, options={len(options)}")
        except Exception as e:
            logger.error(f"发送选项卡片失败: {e}")

    def _format_block(self, block: dict) -> Optional[str]:
        """格式化单个 block 为文本消息，过滤工具调用等中间过程"""
        block_type = block.get("_type", "")

        if block_type == "OutputBlock":
            content = block.get("content", "")
            clean_content = self._strip_ansi(content)
            if not clean_content.strip():
                return None
            first_line = clean_content.strip().split('\n')[0].strip()
            # 过滤工具调用中间过程
            if self._is_tool_call(first_line):
                return None
            return clean_content.strip()

        elif block_type == "SystemBlock":
            # SystemBlock 是系统提示（如 thinking、context 加载），也过滤掉
            return None

        return None

    def _is_intermediate_block(self, block: dict, blocks: list, idx: int) -> bool:
        """判断当前 block 是否为中间过渡文本（后面紧跟工具调用 block）

        AI 思考过渡文本的特征：OutputBlock + 下一个 block 是工具调用。
        例如 "Let me find the file..." 后面跟着 "Searched for 2 patterns..."
        """
        if block.get("_type") != "OutputBlock":
            return False
        # 检查后续 block 是否为工具调用
        next_idx = idx + 1
        if next_idx >= len(blocks):
            return False  # 没有下一个 block，不确定，先不过滤
        next_block = blocks[next_idx]
        if next_block.get("_type") != "OutputBlock":
            return False
        next_content = self._strip_ansi(next_block.get("content", "")).strip()
        if not next_content:
            return False
        next_first_line = next_content.split('\n')[0].strip()
        return self._is_tool_call(next_first_line)

    def _is_tool_call(self, first_line: str) -> bool:
        """判断是否为工具调用 block（中间过程，不应发送给用户）

        工具调用 block 的首行通常为以下格式：
        - "toolname - function_name (MCP)(args...)"
        - "toolname (MCP)(args...)"
        - "Read(file_path)"
        - "Write(file_path)"
        - "Bash(command)"
        - "Glob(pattern)"
        - "Grep(pattern)"
        - "Task(description)"
        - "Searched for N patterns, read N file"
        - "... +N lines (ctrl+o to expand)"
        """
        # 匹配 "(MCP)" / "(Bash)" / "(Tool)" 等工具类型标记
        if re.search(r'\(MCP\)|\(Bash\)|\(Tool\)', first_line):
            return True
        # 匹配内置工具调用：Read(...) / Write(...) / Edit(...) 等
        if re.match(r'^(Read|Write|Edit|Bash|Glob|Grep|Task|WebFetch|WebSearch|TodoRead|TodoWrite)\s*\(', first_line):
            return True
        # 匹配搜索摘要行
        if re.match(r'^(Searched|Searching) for \d+ pattern', first_line):
            return True
        if re.match(r'^Read(ing)? \d+ file', first_line):
            return True
        # 匹配折叠行 / ctrl+o 提示
        if re.match(r'^… \+\d+ lines', first_line):
            return True
        if 'ctrl+o to expand' in first_line:
            return True
        # 匹配工具输出前缀 ⎿
        if first_line.startswith('⎿'):
            return True
        return False

    def _strip_ansi(self, text: str) -> str:
        """移除 ANSI 转义序列"""
        ansi_pattern = r'\x1b\[[0-9;]*[mGKH]'
        return re.sub(ansi_pattern, '', text)


def _build_reply_card(markdown: str, display_name: str = "Claude") -> dict:
    """构建 AI 回复卡片（JSON 2.0，column_set 按钮布局）"""
    title = display_name or "Claude"

    elements = [
        # 正文区：Markdown 渲染
        {"tag": "markdown", "content": markdown, "text_align": "left"},
        # 分割线
        {"tag": "hr"},
        # 快捷按钮行（column_set 布局，JSON 2.0 兼容）
        _build_button_row([
            ("📋 列表", {"action": "menu_list"}),
            ("🔌 断开", {"action": "menu_detach"}),
            ("❓ 帮助", {"action": "menu_help"}),
        ]),
    ]

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "purple",
            "icon": {"tag": "standard_icon", "token": "bot-spark_outlined", "color": "purple"},
        },
        "body": {"elements": elements},
    }


def _build_pending_card(user_text: str) -> dict:
    """构建「已发送，等待回复」卡片，附带中断按钮"""
    display_text = user_text if len(user_text) <= 30 else user_text[:30] + "..."
    elements = [
        {"tag": "markdown", "content": f"✅ **已发送：** {display_text}\n\n⏳ 等待 Claude 回复..."},
        # 中断按钮
        _build_button_row([
            ("⛔ 中断 AI", {"action": "send_key", "key": "esc"}),
        ], button_type="danger"),
    ]

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": "处理中..."},
            "template": "orange",
            "icon": {"tag": "standard_icon", "token": "bot-spark_outlined", "color": "orange"},
        },
        "body": {"elements": elements},
    }


def _build_button_row(buttons: list, button_type: str = "default") -> dict:
    """构建按钮行（column_set 布局，JSON 2.0 兼容）

    Args:
        buttons: [(label, callback_value), ...] 列表
        button_type: 按钮样式 "default" / "primary" / "danger"
    """
    columns = []
    for label, value in buttons:
        columns.append({
            "tag": "column",
            "width": "auto",
            "vertical_align": "center",
            "elements": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": label},
                "type": button_type,
                "width": "default",
                "behaviors": [{"type": "callback", "value": value}],
            }],
        })

    return {
        "tag": "column_set",
        "flex_mode": "flow",
        "background_style": "default",
        "horizontal_spacing": "8px",
        "columns": columns,
    }


def _build_option_card(option_block: dict) -> dict:
    """构建选项交互卡片（权限确认/选项选择）

    option_block 结构:
    {
        "_type": "OptionBlock",
        "sub_type": "permission" | "option",
        "title": "Bash command",
        "content": "rm -rf /tmp/test",
        "question": "Do you want to proceed?",
        "options": [{"label": "Yes", "value": "1"}, ...]
    }
    """
    sub_type = option_block.get("sub_type", "option")
    title = option_block.get("title", "")
    content = option_block.get("content", "")
    question = option_block.get("question", "")
    options = option_block.get("options", [])
    total = len(options)

    # 构建正文区 Markdown
    md_parts = []
    if sub_type == "permission":
        if title:
            md_parts.append(f"**{title}**")
        if content:
            md_parts.append(f"```\n{content}\n```")
        if question:
            md_parts.append(f"{question}")
    else:
        if question:
            md_parts.append(f"{question}")
        if content:
            md_parts.append(f"{content}")

    elements = []
    if md_parts:
        elements.append({"tag": "markdown", "content": "\n\n".join(md_parts), "text_align": "left"})

    elements.append({"tag": "hr"})

    # 每个选项一个按钮
    for i, opt in enumerate(options):
        label = opt.get("label", f"选项 {i+1}")
        value = opt.get("value", str(i + 1))
        needs_input = opt.get("needs_input", False)
        elements.append({
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [{
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"{i+1}. {label}"},
                    "type": "default",
                    "width": "fill",
                    "behaviors": [{
                        "type": "callback",
                        "value": {
                            "action": "select_option",
                            "value": value,
                            "needs_input": needs_input,
                            "total": str(total),
                        }
                    }],
                }],
            }],
        })

    # 卡片 header
    if sub_type == "permission":
        header_title = "🔐 等待权限确认"
        header_color = "red"
    else:
        header_title = "🤔 等待选择"
        header_color = "blue"

    return {
        "schema": "2.0",
        "header": {
            "title": {"tag": "plain_text", "content": header_title},
            "template": header_color,
            "icon": {"tag": "standard_icon", "token": "bot-spark_outlined", "color": header_color},
        },
        "body": {"elements": elements},
    }
