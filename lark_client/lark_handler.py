"""
飞书消息处理器 - 基于共享内存的推送架构

架构：
  Server → .mq 共享内存 → SharedMemoryPoller → 飞书卡片
  SessionBridge 只负责：连接管理 + 输入发送

群聊/私聊统一逻辑：以 chat_id 为 key 管理所有 bridge 和会话绑定。
"""

import asyncio
import difflib
import json
import logging
import os as _os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime as _datetime
from pathlib import Path
from typing import Optional, Dict, Any, List

logger = logging.getLogger('LarkHandler')

from .session_bridge import SessionBridge
from .card_service import card_service
from .card_builder import (
    build_stream_card,
    build_status_card,
    build_help_card,
    build_dir_card,
    build_menu_card,
    build_group_menu_card,
    build_group_recovery_card,
    build_monitor_list_card,
    build_summary_card,
    build_monitor_config_card,
    _build_header,
    _build_menu_button_only,
)
from .shared_memory_poller import SharedMemoryPoller, CardSlice
from .text_message_poller import TextMessagePoller, _build_pending_card
from .monitor_config import MonitorConfigService

# 消息模式: "card" (卡片模式) 或 "text" (文本模式)
# 可以通过环境变量 LARK_MESSAGE_MODE 设置
MESSAGE_MODE = _os.environ.get("LARK_MESSAGE_MODE", "text").lower()  # 默认使用文本模式

# 飞书机器人菜单中文名称 → 命令映射（菜单类型选"发送文字消息"，名称填左列）
# 底部菜单：会话列表 | 菜单 | ≡更多（创建群组/帮助/当前状态）
_MENU_ALIASES = {
    "会话列表": "/list",
    "菜单":    "/menu",
    "创建群组": "/new-group",
    "帮助":    "/help",
    "当前状态": "/status",
}

sys.path.insert(0, str(Path(__file__).parent.parent))
from utils.session import list_active_sessions, get_socket_path, get_chat_bindings_file, ensure_user_data_dir, USER_DATA_DIR


def _read_log_since(since: '_datetime', log_path: 'Path') -> str:
    """读取 startup.log 中 since 时间点之后的日志行"""
    if not log_path.exists():
        return ""
    lines = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            ts = _datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S.%f")
            if ts >= since:
                lines.append(line)
        except ValueError:
            if lines:
                lines.append(line)
    return "\n".join(lines)

try:
    from stats import track as _track_stats
except Exception:
    def _track_stats(*args, **kwargs): pass


class LarkHandler:
    """飞书消息处理器（群聊/私聊统一逻辑）"""

    _CHAT_BINDINGS_FILE = get_chat_bindings_file()
    _OLD_CHAT_BINDINGS_FILE = Path("/tmp/remote-claude/lark_chat_bindings.json")
    _LARK_GROUP_IDS_FILE = Path(get_chat_bindings_file()).parent / "lark_group_ids.json"
    _LARK_GROUP_META_FILE = Path(get_chat_bindings_file()).parent / "lark_group_meta.json"

    def __init__(self):
        # 兼容迁移：旧绑定文件存在而新路径不存在时，自动迁移
        if not self._CHAT_BINDINGS_FILE.exists() and self._OLD_CHAT_BINDINGS_FILE.exists():
            try:
                import shutil
                ensure_user_data_dir()
                shutil.move(str(self._OLD_CHAT_BINDINGS_FILE), str(self._CHAT_BINDINGS_FILE))
            except Exception as e:
                logger.warning(f"迁移旧绑定文件失败: {e}")
        # chat_id → SessionBridge（活跃连接）
        self._bridges: Dict[str, SessionBridge] = {}
        # chat_id → session_name（当前连接状态）
        self._chat_sessions: Dict[str, str] = {}
        # 消息轮询器（根据模式选择卡片或文本）
        if MESSAGE_MODE == "card":
            self._poller = SharedMemoryPoller(card_service)
            self._text_poller = None
            logger.info("使用卡片模式 (MESSAGE_MODE=card)")
        else:
            self._poller = None  # 卡片模式轮询器不启用
            self._text_poller = TextMessagePoller(card_service)
            logger.info("使用文本模式 (MESSAGE_MODE=text)")
        # chat_id → session_name 持久化绑定（重启后自动恢复）
        self._chat_bindings: Dict[str, str] = self._load_chat_bindings()
        # 专属群聊 chat_id 集合（仅包含通过 /new-group 创建的群）
        self._group_chat_ids: set = self._load_group_chat_ids()
        # 专属群聊元信息：chat_id -> {session_name, status, updated_at, retain}
        self._group_meta: Dict[str, Dict[str, Any]] = self._load_group_meta()
        self._sync_group_meta()
        self._cleanup_offline_groups()
        # chat_id → CardSlice（用户主动断开后保留，供重连时冻结旧卡片）- 仅卡片模式使用
        self._detached_slices: Dict[str, CardSlice] = {}
        # OAuth 服务实例（由 main.py 的 LarkBot._init_oauth 注入，未启用时为 None）
        self.oauth_service = None
        # 正在启动中的会话名集合（防止并发点击触发竞态）
        self._starting_sessions: set = set()
        # 监听配置服务
        self.monitor_config = MonitorConfigService()
        # User API 实例（由 main.py 注入，用于读取群聊信息）
        self.user_api = None
        # 群聊离线探测任务（chat_id -> task），用于 suspect_offline -> offline 防抖
        self._group_offline_probe_tasks: Dict[str, asyncio.Task] = {}
        # 群聊恢复锁（chat_id 级 single-flight）
        self._group_recovery_locks: Dict[str, asyncio.Lock] = {}
        # 群聊恢复进行态与最近结果（chat_id -> {request_id, result}）
        self._group_recovery_inflight: Dict[str, Dict[str, Any]] = {}
        self._group_recovery_last_result: Dict[str, Dict[str, Any]] = {}
        # 群聊总结锁（chat_id 级 single-flight）
        self._group_summary_locks: Dict[str, asyncio.Lock] = {}
        # 每个 chat 的“本轮对话变更”基线（进入会话时记录）
        # chat_id -> {session_name, repo_root, merge_base, baseline_head, baseline_status, baseline_diff}
        self._round_diff_baseline: Dict[str, Dict[str, Any]] = {}

    @property
    def _is_card_mode(self) -> bool:
        """是否为卡片模式"""
        return MESSAGE_MODE == "card"

    def _start_poller(self, chat_id: str, session_name: str, is_group: bool = False,
                      notify_user_id: Optional[str] = None) -> None:
        """启动轮询器（根据模式选择）"""
        if self._is_card_mode and self._poller:
            self._poller.start(chat_id, session_name, is_group=is_group,
                               notify_user_id=notify_user_id)
        elif self._text_poller:
            # 通过 pid 获取 cwd，取最后一级目录名作为卡片标题
            display_name = "Claude"
            sessions = list_active_sessions()
            session = next((s for s in sessions if s["name"] == session_name), None)
            if session:
                pid = session.get("pid")
                if pid:
                    cwd = self._get_pid_cwd(pid)
                    if cwd:
                        display_name = cwd.rstrip("/").rsplit("/", 1)[-1]
            self._text_poller.start(chat_id, session_name, is_group=is_group,
                                    display_name=display_name)

    def _stop_poller(self, chat_id: str) -> Optional[CardSlice]:
        """停止轮询器，返回 CardSlice（仅卡片模式有效）"""
        if self._is_card_mode and self._poller:
            return self._poller.stop_and_get_active_slice(chat_id)
        elif self._text_poller:
            self._text_poller.stop(chat_id)
        return None

    def _kick_poller(self, chat_id: str) -> None:
        """触发立即轮询"""
        if self._poller:
            self._poller.kick(chat_id)
        elif self._text_poller:
            self._text_poller.kick(chat_id)

    def _read_snapshot(self, chat_id: str) -> Optional[dict]:
        """读取共享内存快照（兼容两种模式）"""
        # 优先使用 poller（它维护了 reader 实例）
        if self._poller:
            return self._poller.read_snapshot(chat_id)
        # 文本模式：直接从共享内存读取
        session_name = self._chat_sessions.get(chat_id)
        if not session_name:
            return None
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
            from shared_state import SharedStateReader, get_mq_path
            mq_path = get_mq_path(session_name)
            if not mq_path.exists():
                return None
            reader = SharedStateReader(session_name)
            return reader.read()
        except Exception as e:
            logger.warning(f"读取共享内存失败: {e}")
            return None

    @staticmethod
    def _count_user_input_blocks(snapshot: Optional[dict]) -> int:
        if not isinstance(snapshot, dict):
            return -1
        blocks = snapshot.get('blocks') or []
        return sum(1 for b in blocks if isinstance(b, dict) and b.get('_type') == 'UserInput')

    async def _wait_user_input_reflected(self, chat_id: str, before_count: int,
                                         timeout_seconds: float = 2.0,
                                         interval_seconds: float = 0.2) -> bool:
        """等待本次输入在快照中出现（UserInput 数量增加）。"""
        if before_count < 0:
            return True
        deadline = time.time() + max(0.2, timeout_seconds)
        while time.time() < deadline:
            await asyncio.sleep(max(0.05, interval_seconds))
            snap = self._read_snapshot(chat_id)
            if self._count_user_input_blocks(snap) > before_count:
                return True
        return False

    async def _verify_group_recovery_ready(self, chat_id: str, session_name: str,
                                           *, timeout_seconds: float = 2.2,
                                           interval_seconds: float = 0.15) -> bool:
        """恢复后健康校验：bridge/session 映射稳定，且共享内存可读。"""
        deadline = time.time() + max(0.6, timeout_seconds)
        while time.time() < deadline:
            bridge = self._bridges.get(chat_id)
            if bridge and bridge.running and self._chat_sessions.get(chat_id) == session_name:
                snapshot = self._read_snapshot(chat_id)
                if isinstance(snapshot, dict):
                    return True
            await asyncio.sleep(max(0.05, interval_seconds))
        return False

    # ── 持久化绑定 ──────────────────────────────────────────────────────────

    def _load_chat_bindings(self) -> Dict[str, str]:
        try:
            if self._CHAT_BINDINGS_FILE.exists():
                return json.loads(self._CHAT_BINDINGS_FILE.read_text())
        except Exception:
            pass
        return {}

    def _save_chat_bindings(self):
        try:
            ensure_user_data_dir()
            self._CHAT_BINDINGS_FILE.write_text(
                json.dumps(self._chat_bindings, ensure_ascii=False)
            )
        except Exception as e:
            logger.warning(f"保存绑定失败: {e}")

    def _load_group_chat_ids(self) -> set:
        try:
            if self._LARK_GROUP_IDS_FILE.exists():
                return set(json.loads(self._LARK_GROUP_IDS_FILE.read_text()))
        except Exception:
            pass
        return set()

    def _save_group_chat_ids(self):
        try:
            ensure_user_data_dir()
            self._LARK_GROUP_IDS_FILE.write_text(
                json.dumps(list(self._group_chat_ids), ensure_ascii=False)
            )
        except Exception as e:
            logger.warning(f"保存群聊 ID 失败: {e}")

    def _load_group_meta(self) -> Dict[str, Dict[str, Any]]:
        try:
            if self._LARK_GROUP_META_FILE.exists():
                data = json.loads(self._LARK_GROUP_META_FILE.read_text())
                if isinstance(data, dict):
                    return data
        except Exception as e:
            logger.warning(f"读取群聊元信息失败: {e}")
        return {}

    def _save_group_meta(self):
        try:
            ensure_user_data_dir()
            self._LARK_GROUP_META_FILE.write_text(
                json.dumps(self._group_meta, ensure_ascii=False)
            )
        except Exception as e:
            logger.warning(f"保存群聊元信息失败: {e}")

    def _set_group_status(self, chat_id: str, session_name: str, status: str, *,
                          retain: Optional[bool] = None,
                          reason: Optional[str] = None):
        now = int(time.time())
        meta = self._group_meta.get(chat_id, {})
        meta['session_name'] = session_name
        meta['status'] = status
        meta['updated_at'] = now
        if retain is not None:
            meta['retain'] = bool(retain)
        elif 'retain' not in meta:
            meta['retain'] = False
        if reason is not None:
            meta['reason'] = reason
        elif status == 'active':
            meta.pop('reason', None)
        self._group_meta[chat_id] = meta
        self._save_group_meta()

    def _sync_group_meta(self):
        changed = False
        for cid in list(self._group_meta.keys()):
            if cid not in self._group_chat_ids:
                self._group_meta.pop(cid, None)
                changed = True

        for cid in self._group_chat_ids:
            sname = self._chat_bindings.get(cid, '')
            meta = self._group_meta.get(cid)
            if not meta:
                self._group_meta[cid] = {
                    'session_name': sname,
                    'status': 'active' if cid in self._chat_sessions else 'offline',
                    'updated_at': int(time.time()),
                    'retain': False,
                }
                changed = True
                continue
            if sname and meta.get('session_name') != sname:
                meta['session_name'] = sname
                changed = True
            if 'retain' not in meta:
                meta['retain'] = False
                changed = True
            if 'updated_at' not in meta:
                meta['updated_at'] = int(time.time())
                changed = True
            if 'status' not in meta:
                meta['status'] = 'active' if cid in self._chat_sessions else 'offline'
                changed = True
            if meta.get('status') == 'active' and 'reason' in meta:
                meta.pop('reason', None)
                changed = True

        if changed:
            self._save_group_meta()

    def _cleanup_offline_groups(self, retention_days: int = 7):
        """清理超期离线群元数据（仅清理本地元数据，不调用飞书解散）。"""
        cutoff = int(time.time()) - retention_days * 86400
        removed = 0

        for cid, meta in list(self._group_meta.items()):
            if meta.get('retain', False):
                continue
            if meta.get('status') != 'offline':
                continue
            updated_at = int(meta.get('updated_at', 0) or 0)
            if updated_at <= 0 or updated_at >= cutoff:
                continue

            self._group_meta.pop(cid, None)
            self._group_chat_ids.discard(cid)
            self._chat_bindings.pop(cid, None)
            self._bridges.pop(cid, None)
            self._chat_sessions.pop(cid, None)
            self._detached_slices.pop(cid, None)
            removed += 1

        if removed:
            logger.info(f"已清理超期离线群元数据: {removed} 个")
            self._save_group_meta()
            self._save_group_chat_ids()
            self._save_chat_bindings()

    def _remove_group_meta(self, chat_id: str):
        if chat_id in self._group_meta:
            self._group_meta.pop(chat_id, None)
            self._save_group_meta()

    def _get_group_meta(self, chat_id: str) -> Dict[str, Any]:
        return self._group_meta.get(chat_id, {})

    def _get_group_offline_reason(self, chat_id: str) -> str:
        meta = self._group_meta.get(chat_id, {})
        return str(meta.get('reason', '') or '').strip()

    def _set_group_last_summary(self, chat_id: str, summary_text: str, *,
                                seq: Optional[int] = None,
                                block_cursor: Optional[int] = None,
                                filtered_count: Optional[int] = None):
        meta = self._group_meta.get(chat_id, {})
        meta['last_summary'] = summary_text
        if seq is not None:
            meta['summary_seq'] = int(seq)
        if block_cursor is not None:
            meta['summary_block_cursor'] = int(block_cursor)
        if filtered_count is not None:
            meta['summary_filtered_count'] = int(filtered_count)
        meta['updated_at'] = int(time.time())
        self._group_meta[chat_id] = meta
        self._save_group_meta()

    def _get_group_last_summary(self, chat_id: str) -> str:
        meta = self._group_meta.get(chat_id, {})
        return str(meta.get('last_summary', '') or '').strip()

    def _get_group_summary_seq(self, chat_id: str) -> int:
        meta = self._group_meta.get(chat_id, {})
        return int(meta.get('summary_seq', 0) or 0)

    def _get_group_summary_filtered_count(self, chat_id: str) -> int:
        meta = self._group_meta.get(chat_id, {})
        return int(meta.get('summary_filtered_count', 0) or 0)

    def _ensure_group_recovery_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._group_recovery_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_recovery_locks[chat_id] = lock
        return lock

    def _ensure_group_summary_lock(self, chat_id: str) -> asyncio.Lock:
        lock = self._group_summary_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._group_summary_locks[chat_id] = lock
        return lock

    def _is_summary_block(self, content: str) -> bool:
        text = (content or "").strip()
        return text.startswith("[RC-SUMMARY")

    def _is_recovery_context_block(self, content: str) -> bool:
        text = (content or "").strip()
        return text.startswith("[RECOVERY_CONTEXT v1]")

    def _is_summary_prompt_block(self, content: str) -> bool:
        text = (content or "").strip()
        return text.startswith("[RC-SUMMARY-PROMPT v1]")

    def _get_block_text(self, block: Dict[str, Any]) -> str:
        t = block.get('_type', '')
        if t == 'UserInput':
            return str(block.get('text', '') or '')
        return str(block.get('content', '') or '')

    def _is_recovery_ack_block(self, content: str) -> bool:
        text = (content or '').strip()
        if not text:
            return False
        hints = [
            "恢复回放",
            "恢复上下文",
            "不是新的用户请求",
            "不是新指令",
            "不会重复执行历史步骤",
            "当前状态保持不变",
        ]
        hit = sum(1 for h in hints if h in text)
        return hit >= 2

    def _is_system_noise_block(self, block: Dict[str, Any]) -> bool:
        t = block.get('_type', '')
        if t == 'SystemBlock':
            return True
        if t == 'OutputBlock':
            txt = str(block.get('content', '') or '')
            if 'menu_open' in txt and 'action' in txt:
                return True
            if self._is_recovery_context_block(txt):
                return True
            if self._is_recovery_ack_block(txt):
                return True
        if t == 'UserInput':
            txt = str(block.get('text', '') or '')
            if self._is_summary_prompt_block(txt):
                return True
            if self._is_recovery_context_block(txt):
                return True
        return False

    def _normalize_recovery_text(self, text: str, *, max_len: int = 4000) -> str:
        s = (text or '').strip()
        if not s:
            return ''
        if len(s) <= max_len:
            return s
        head = s[:500]
        tail = s[-500:]
        return head + "\n[truncated]\n" + tail

    def _extract_user_speaker(self, text: str) -> str:
        # 兼容 parser 产出的前缀："[张三|ou_xxx] xxx"
        m = re.match(r'^\[(.+?)\]\s*(.*)$', (text or '').strip(), flags=re.S)
        if m:
            return m.group(1)
        return ''

    def _build_recovery_messages_from_blocks(self, blocks: List[Dict[str, Any]], *,
                                             max_messages: int = 120,
                                             total_budget: int = 16000) -> List[Dict[str, str]]:
        msgs: List[Dict[str, str]] = []
        budget = 0

        # 从后往前取，最后再 reverse，保证近期优先
        for b in reversed(blocks):
            t = b.get('_type', '')
            if self._is_system_noise_block(b):
                continue

            raw = ''
            role = ''
            if t == 'UserInput':
                role = 'user'
                raw = self._get_block_text(b)
                speaker = self._extract_user_speaker(raw)
                if speaker:
                    raw = f"[{speaker}] " + re.sub(r'^\[(.+?)\]\s*', '', raw)
            elif t in ('OutputBlock', 'PlanBlock'):
                role = 'assistant'
                raw = self._get_block_text(b)
                if self._is_summary_block(raw) or self._is_recovery_context_block(raw):
                    continue
            elif t == 'SystemBlock':
                role = 'system'
                raw = self._get_block_text(b)
            else:
                continue

            raw = self._normalize_recovery_text(raw)
            if not raw:
                continue

            add_cost = len(raw)
            if budget + add_cost > total_budget:
                continue

            msgs.append({'role': role, 'content': raw})
            budget += add_cost
            if len(msgs) >= max_messages:
                break

        msgs.reverse()
        return msgs

    def _build_recovery_context_package(self, chat_id: str, snapshot: Dict[str, Any]) -> str:
        blocks = list(snapshot.get('blocks', []) or [])

        # 定位最近 summary，优先取其后的增量
        last_summary_idx = -1
        last_summary_text = ''
        for i in range(len(blocks) - 1, -1, -1):
            b = blocks[i]
            if b.get('_type') == 'OutputBlock':
                content = str(b.get('content', '') or '')
                if self._is_summary_block(content):
                    last_summary_idx = i
                    last_summary_text = self._normalize_recovery_text(content, max_len=4000)
                    break

        if not last_summary_text:
            last_summary_text = self._get_group_last_summary(chat_id)

        if last_summary_idx >= 0:
            candidate_blocks = blocks[last_summary_idx + 1:]
        else:
            candidate_blocks = blocks[-80:]

        msgs = self._build_recovery_messages_from_blocks(candidate_blocks)

        lines = [
            "[RECOVERY_CONTEXT v1]",
            "这是历史回放，不是新的用户请求；请不要重复执行历史步骤。",
            "",
            "checkpoint:",
            last_summary_text or "<none>",
            "",
            "messages:",
        ]
        for m in msgs:
            lines.append(f"- role={m['role']}: {m['content']}")

        lines.extend([
            "",
            "请从“最后一条 user 意图”继续执行，并先用 3-5 条要点确认你的理解。",
        ])
        return "\n".join(lines)

    async def _inject_recovery_context(self, chat_id: str) -> bool:
        bridge = self._bridges.get(chat_id)
        if not bridge or not bridge.running:
            return False

        snapshot = self._read_snapshot(chat_id)
        if not snapshot:
            return False

        payload = self._build_recovery_context_package(chat_id, snapshot)
        if not payload.strip():
            return False

        ok = await bridge.send_input(payload)
        if ok:
            self._kick_poller(chat_id)
        return bool(ok)

    @staticmethod
    def _count_bullets(text: str) -> int:
        return sum(1 for ln in (text or "").splitlines() if ln.strip().startswith("- "))

    def _extract_summary_block_from_output(self, content: str, seq: int) -> str:
        marker = f"[RC-SUMMARY v1 #{seq}]"
        text = (content or "").strip()
        idx = text.find(marker)
        if idx < 0:
            return ""
        tail = text[idx:]
        return self._normalize_recovery_text(tail, max_len=5000)

    def _build_ai_summary_prompt(self, *, seq: int, trigger_text: str, delta: int,
                                 msgs: List[Dict[str, str]]) -> str:
        lines = [
            "[RC-SUMMARY-PROMPT v1]",
            "你是群聊会话恢复助手，请根据给定历史生成结构化滚动总结。",
            "仅输出最终总结块，不要输出解释、前言、代码块。",
            "",
            f"固定要求：首行必须是 [RC-SUMMARY v1 #{seq}]",
            f"固定要求：触发方式必须是 `{trigger_text}`",
            f"固定要求：触发依据必须包含“新增有效消息约 {delta} 条（阈值 80）”",
            "",
            "history:",
        ]
        for m in msgs[-30:]:
            role = m.get('role', 'user')
            content = self._truncate_text(str(m.get('content', '')).replace("\n", " "), 220)
            lines.append(f"- role={role}: {content}")

        lines.extend([
            "",
            "请严格输出以下 10 行结构，不要增删字段：",
            f"[RC-SUMMARY v1 #{seq}]",
            f"- 触发方式：{trigger_text}",
            "- 目标与范围：<一句话，15-30字>",
            "- 最近用户意图：<一句话，包含具体动作/需求>",
            "- 最近助手进展：<一句话，包含已完成动作>",
            "- 已完成：<一句话，用“约N条”表达>",
            "- 当前状态：<一句话>",
            "- 未决问题：<一句话，包含待处理项>",
            f"- 触发依据：自上次总结后新增有效消息约 {delta} 条（阈值 80）",
            "- 关键约束：群聊恢复为近似恢复，不保证原进程内存级一致",
            "- 下一步：从最后一条 user 意图继续执行",
        ])
        return "\n".join(lines)

    def _build_fallback_summary_text(self, *, seq: int, trigger_text: str, delta: int,
                                     done: int, todo: int, user_focus: str, assistant_focus: str) -> str:
        return "\n".join([
            f"[RC-SUMMARY v1 #{seq}]",
            f"- 触发方式：{trigger_text}",
            "- 目标与范围：基于最近会话记录自动提炼",
            f"- 最近用户意图：{user_focus}",
            f"- 最近助手进展：{assistant_focus}",
            f"- 已完成：近段 assistant 输出约 {done} 条",
            "- 当前状态：会话可继续推进",
            f"- 未决问题：待处理 user 输入约 {todo} 条",
            f"- 触发依据：自上次总结后新增有效消息约 {delta} 条（阈值 80）",
            "- 关键约束：群聊恢复为近似恢复，不保证原进程内存级一致",
            "- 下一步：从最后一条 user 意图继续执行",
        ])

    async def _wait_ai_group_summary(self, chat_id: str, *, seq: int,
                                     start_ts: float, timeout_seconds: float = 22.0) -> str:
        deadline = time.time() + timeout_seconds
        candidate = ""

        while time.time() < deadline:
            snapshot = self._read_snapshot(chat_id)
            blocks = list((snapshot or {}).get('blocks', []) or [])
            for b in reversed(blocks):
                if b.get('_type') != 'OutputBlock':
                    continue
                b_ts = float(b.get('timestamp', 0) or 0)
                if b_ts and b_ts < start_ts:
                    continue
                content = str(b.get('content', '') or '')
                summary = self._extract_summary_block_from_output(content, seq)
                if not summary:
                    continue
                if self._count_bullets(summary) >= 9:
                    return summary
                candidate = summary

            await asyncio.sleep(0.6)

        return candidate

    async def _emit_group_summary(self, chat_id: str, *, force: bool = False,
                                  trigger: str = "auto") -> bool:
        """按 80 条滚动总结（群内写回）。force=True 时忽略阈值。"""
        if chat_id not in self._group_chat_ids:
            return False
        if chat_id in self._group_recovery_inflight:
            # 恢复流程进行中，避免窗口竞争
            return False

        lock = self._ensure_group_summary_lock(chat_id)
        if lock.locked():
            return False

        async with lock:
            snapshot = self._read_snapshot(chat_id)
            if not snapshot:
                return False

            blocks = list(snapshot.get('blocks', []) or [])

            # 全量过滤一次，结合本地游标判断“自上次总结以来是否新增 >=80 条”
            filtered_all: List[Dict[str, Any]] = []
            scanned_seq = 1
            for b in blocks:
                if self._is_system_noise_block(b):
                    continue
                if b.get('_type') == 'OutputBlock':
                    content = str(b.get('content', '') or '')
                    if self._is_summary_block(content):
                        scanned_seq += 1
                        continue
                    if self._is_recovery_context_block(content):
                        continue
                filtered_all.append(b)

            current_filtered_total = len(filtered_all)
            prev_filtered_total = self._get_group_summary_filtered_count(chat_id)
            if current_filtered_total < prev_filtered_total:
                # 共享窗口重置/历史裁剪后，防止阈值基线失真
                prev_filtered_total = current_filtered_total
            if not force and current_filtered_total < prev_filtered_total + 80:
                return False

            meta_seq = self._get_group_summary_seq(chat_id)
            if meta_seq <= 0 and self._get_group_last_summary(chat_id):
                meta_seq = 1
            seq = max(scanned_seq, meta_seq + 1)

            recent = filtered_all[-80:] if current_filtered_total > 80 else filtered_all
            msgs = self._build_recovery_messages_from_blocks(recent, max_messages=30, total_budget=6000)
            done = sum(1 for m in msgs if m['role'] == 'assistant')
            todo = sum(1 for m in msgs if m['role'] == 'user')
            last_user = next((m['content'] for m in reversed(msgs) if m['role'] == 'user'), "")
            last_assistant = next((m['content'] for m in reversed(msgs) if m['role'] == 'assistant'), "")
            user_focus = self._truncate_text((last_user or "（暂无）").replace("\n", " "), 140)
            assistant_focus = self._truncate_text((last_assistant or "（暂无）").replace("\n", " "), 140)

            delta = max(0, current_filtered_total - prev_filtered_total)
            trigger_text = f"手动触发（/summarize-now）" if trigger == "manual" else "自动触发（新增消息达到阈值）"
            fallback_summary_text = self._build_fallback_summary_text(
                seq=seq,
                trigger_text=trigger_text,
                delta=delta,
                done=done,
                todo=todo,
                user_focus=user_focus,
                assistant_focus=assistant_focus,
            )

            bridge = self._bridges.get(chat_id)
            if not bridge or not bridge.running:
                return False

            summary_text = fallback_summary_text
            send_input = getattr(bridge, 'send_input', None)
            if callable(send_input):
                prompt = self._build_ai_summary_prompt(
                    seq=seq,
                    trigger_text=trigger_text,
                    delta=delta,
                    msgs=msgs,
                )
                ask_ts = time.time()
                ok = await send_input(prompt)
                if ok:
                    self._kick_poller(chat_id)
                    ai_summary = await self._wait_ai_group_summary(chat_id, seq=seq, start_ts=ask_ts)
                    if ai_summary:
                        required_trigger = f"- 触发方式：{trigger_text}"
                        required_basis = f"- 触发依据：自上次总结后新增有效消息约 {delta} 条（阈值 80）"
                        if required_trigger in ai_summary and required_basis in ai_summary:
                            summary_text = ai_summary
                        else:
                            logger.warning(f"AI 总结格式缺失关键行，回退模板: chat={chat_id[:8]}...")
                    else:
                        logger.warning(f"等待 AI 总结超时，回退模板: chat={chat_id[:8]}...")

            msg_id = await card_service.send_text(chat_id, summary_text)
            if msg_id:
                self._set_group_last_summary(
                    chat_id,
                    summary_text,
                    seq=seq,
                    block_cursor=len(blocks),
                    filtered_count=current_filtered_total,
                )
            return bool(msg_id)

    async def _maybe_emit_group_summary_after_delay(self, chat_id: str, delay_seconds: float = 1.2):
        """用户消息发出后延迟尝试自动总结（best effort）。"""
        try:
            await asyncio.sleep(max(0.5, delay_seconds))
            await self._emit_group_summary(chat_id, force=False, trigger="auto")
        except Exception as e:
            logger.debug(f"自动总结尝试失败: chat={chat_id[:8]}... err={e}")

    async def _probe_group_offline(self, chat_id: str, session_name: str, user_id: Optional[str],
                                   *, reason: str = "", debounce_seconds: float = 3.5):
        """离线防抖探测：suspect_offline -> offline。"""
        try:
            await asyncio.sleep(max(1.0, debounce_seconds))

            # 群已解绑或会话已切走，直接结束
            bound = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
            if not bound or bound != session_name:
                return

            # 如果已经连上则恢复 active
            bridge = self._bridges.get(chat_id)
            if bridge and bridge.running:
                self._set_group_status(chat_id, session_name, 'active')
                return

            # 尝试一次快速 attach，避免瞬时误判
            ok = await self._attach(chat_id, session_name, user_id=user_id)
            if ok:
                self._chat_bindings[chat_id] = session_name
                self._save_chat_bindings()
                self._set_group_status(chat_id, session_name, 'active')
                return

            # 连续失败 -> offline，并推恢复卡片
            offline_reason = reason or f"会话 {session_name} 当前不可连接"
            self._set_group_status(chat_id, session_name, 'offline', reason=offline_reason)
            card = build_group_recovery_card(session_name, reason=offline_reason)
            await card_service.create_and_send_card(chat_id, card)
        except Exception as e:
            logger.warning(f"群聊离线探测失败: chat={chat_id[:8]}... err={e}")
        finally:
            self._group_offline_probe_tasks.pop(chat_id, None)

    def _schedule_group_offline_probe(self, chat_id: str, session_name: str,
                                      user_id: Optional[str] = None, *, reason: str = ""):
        """为群聊启动离线防抖探测任务（同群仅保留最新一次）。"""
        old = self._group_offline_probe_tasks.pop(chat_id, None)
        if old and not old.done():
            old.cancel()

        self._set_group_status(chat_id, session_name, 'suspect_offline', reason=reason or "连接不稳定，正在探测")
        task = asyncio.create_task(
            self._probe_group_offline(chat_id, session_name, user_id, reason=reason)
        )
        self._group_offline_probe_tasks[chat_id] = task

    def _cancel_group_offline_probe(self, chat_id: str):
        task = self._group_offline_probe_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    def _remove_binding_by_chat(self, chat_id: str, force: bool = False):
        """移除 chat_id 的绑定。
        群聊绑定默认不移除（避免断开后无法解散群）；
        force=True 时强制移除（用于会话终止/解散群场景）。
        """
        if not force and chat_id in self._group_chat_ids:
            return
        self._chat_bindings.pop(chat_id, None)
        self._save_chat_bindings()

    # ── 统一 attach / detach / on_disconnect ────────────────────────────────

    async def _attach(self, chat_id: str, session_name: str,
                      user_id: Optional[str] = None) -> bool:
        """统一 attach 逻辑（私聊/群聊共用）"""
        # 已经连接到同一会话时直接复用，避免重复 attach 造成瞬断
        current_session = self._chat_sessions.get(chat_id)
        current_bridge = self._bridges.get(chat_id)
        if current_bridge and current_bridge.running and current_session == session_name:
            if chat_id in self._group_chat_ids:
                self._cancel_group_offline_probe(chat_id)
                self._set_group_status(chat_id, session_name, 'active')
            self._capture_round_diff_baseline_for_chat(chat_id, session_name)
            return True

        # 在断开旧连接之前，先更新旧流式卡片为已断开状态（仅卡片模式）
        old_session = current_session
        old_slice = self._stop_poller(chat_id)
        if old_slice and old_session and self._is_card_mode:
            await self._update_card_disconnected(chat_id, old_session, old_slice)

        # 断开旧 bridge
        old = self._bridges.pop(chat_id, None)
        if old:
            await old.disconnect()
        # _poller.stop 已通过 _stop_poller 完成
        self._chat_sessions.pop(chat_id, None)
        self._detached_slices.pop(chat_id, None)

        bridge = SessionBridge(session_name)

        def on_disconnect():
            asyncio.create_task(self._on_disconnect(
                chat_id,
                session_name,
                expected_client_id=bridge.client_id,
            ))

        bridge.on_disconnect = on_disconnect
        if await bridge.connect():
            self._bridges[chat_id] = bridge
            self._chat_sessions[chat_id] = session_name
            self._start_poller(chat_id, session_name, is_group=(chat_id in self._group_chat_ids),
                               notify_user_id=user_id)
            if chat_id in self._group_chat_ids:
                self._cancel_group_offline_probe(chat_id)
                self._set_group_status(chat_id, session_name, 'active')
            self._capture_round_diff_baseline_for_chat(chat_id, session_name)
            _track_stats('lark', 'attach', session_name=session_name,
                         chat_id=chat_id)
            return True
        return False

    async def _detach(self, chat_id: str):
        """统一 detach 逻辑（私聊/群聊共用）"""
        bridge = self._bridges.pop(chat_id, None)
        if bridge:
            await bridge.disconnect()
        self._chat_sessions.pop(chat_id, None)
        self._stop_poller(chat_id)

    async def _on_disconnect(self, chat_id: str, session_name: str,
                             expected_client_id: Optional[str] = None):
        """服务端关闭连接时的统一处理"""
        current = self._bridges.get(chat_id)
        # 忽略旧 bridge 的延迟断线回调，避免误清理新连接
        if expected_client_id and current and current.client_id != expected_client_id:
            logger.info(
                f"忽略过期断线回调: chat_id={chat_id[:8]}..., session={session_name}, "
                f"expected_client={expected_client_id[:8]}..., current_client={current.client_id[:8]}..."
            )
            return

        logger.info(f"会话 '{session_name}' 断线, chat_id={chat_id[:8]}...")
        _track_stats('lark', 'disconnect', session_name=session_name,
                     chat_id=chat_id)
        active_slice = self._stop_poller(chat_id)
        self._bridges.pop(chat_id, None)
        self._chat_sessions.pop(chat_id, None)
        self._detached_slices.pop(chat_id, None)
        self._remove_binding_by_chat(chat_id)

        if active_slice and self._is_card_mode:
            await self._update_card_disconnected(chat_id, session_name, active_slice)

        # 生命周期管理：会话断开后将该会话的专属群进入 suspect_offline，防抖后再离线
        for cid in list(self._group_chat_ids):
            if self._chat_bindings.get(cid) == session_name:
                self._schedule_group_offline_probe(
                    cid,
                    session_name,
                    reason=f"会话 {session_name} 连接断开",
                )

    # ── 消息入口 ────────────────────────────────────────────────────────────

    async def handle_message(self, user_id: str, chat_id: str, text: str,
                              chat_type: str = "p2p"):
        """处理用户消息（群聊/私聊统一路由）"""
        logger.info(f"收到消息: user={user_id[:8]}..., chat={chat_id[:8]}..., type={chat_type}, text={text[:50]}")
        text = text.strip()

        try:
            # 清理飞书文本里常见的不可见字符，避免 "/skills" 被误判为普通文本
            text = text.replace("\ufeff", "").replace("\u200b", "").strip()
            # 兼容全角斜杠命令（／skills）
            if text.startswith("／"):
                text = "/" + text[1:]
            # 兼容全角感叹号命令（！pwd），用于 Claude Bash mode
            if text.startswith("！"):
                text = "!" + text[1:]

            if not text:
                if chat_type != "p2p":
                    await self._show_group_entry_card(user_id, chat_id)
                return
            # 飞书菜单发送的中文名称 → 映射为命令
            menu_alias = _MENU_ALIASES.get(text)
            if menu_alias:
                text = menu_alias

            if text.startswith("/"):
                # /cl 前缀：去掉前缀，转发给 Claude
                if text == "/cl" or text.startswith("/cl "):
                    claude_text = text[3:].strip()
                    if claude_text:
                        await self._forward_to_claude(user_id, chat_id, claude_text)
                        _track_stats('lark', 'message',
                                     session_name=self._chat_sessions.get(chat_id, ''),
                                     chat_id=chat_id)
                else:
                    await self._handle_command(user_id, chat_id, text)
            else:
                # 普通聊天消息：直接转发给 Claude
                await self._forward_to_claude(user_id, chat_id, text)
                _track_stats('lark', 'message',
                             session_name=self._chat_sessions.get(chat_id, ''),
                             chat_id=chat_id)
        except Exception as e:
            logger.error(f"处理消息失败: {e}", exc_info=True)
            try:
                await card_service.send_text(
                    chat_id,
                    f"❌ 处理消息时发生错误\n\n"
                    f"错误信息: {str(e)}\n\n"
                    f"请稍后重试或联系管理员。"
                )
            except Exception as send_error:
                logger.error(f"发送错误消息失败: {send_error}")

    async def forward_to_claude(self, user_id: str, chat_id: str, text: str):
        """卡片输入框直通 Claude（跳过命令路由）"""
        await self._forward_to_claude(user_id, chat_id, text)
        _track_stats('lark', 'message',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id)

    async def _handle_command(self, user_id: str, chat_id: str, text: str):
        """处理命令（群聊/私聊共用同一逻辑）"""
        parts = text.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        _track_stats('lark', 'cmd',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=command)

        if command == "/attach":
            await self._cmd_attach(user_id, chat_id, args)
        elif command == "/detach":
            await self._cmd_detach(user_id, chat_id)
        elif command == "/list":
            await self._cmd_list(user_id, chat_id)
        elif command == "/status":
            await self._cmd_status(user_id, chat_id)
        elif command == "/start":
            await self._cmd_start(user_id, chat_id, args)
        elif command == "/kill":
            await self._cmd_kill(user_id, chat_id, args)
        elif command in ("/ls", "/tree"):
            await self._cmd_ls(user_id, chat_id, args, tree=(command == "/tree"))
        elif command == "/new-group":
            await self._cmd_new_group(user_id, chat_id, args)
        elif command == "/summarize-now":
            await self._cmd_summarize_now(user_id, chat_id)
        elif command == "/diff":
            await self._cmd_view_round_diff(user_id, chat_id)
        elif command == "/help":
            await self._cmd_help(user_id, chat_id)
        elif command == "/menu":
            await self._cmd_menu(user_id, chat_id)
        elif command == "/oauth":
            await self._cmd_oauth(user_id, chat_id)
        elif command == "/oauth-status":
            await self._cmd_oauth_status(user_id, chat_id)
        elif command == "/oauth-revoke":
            await self._cmd_oauth_revoke(user_id, chat_id)
        elif command in ("/check-messages", "/check-mentions"):  # 新命令 + 旧命令别名
            await self._cmd_check_mentions(user_id, chat_id, args)
        elif command in ("/messages-auto", "/mentions-auto"):  # 新命令 + 旧命令别名
            await self._cmd_mentions_auto(user_id, chat_id, args)
        elif command in ("/messages-config", "/mentions-config"):  # 新命令 + 旧命令别名
            await self._cmd_mentions_config(user_id, chat_id, args)
        elif command in ("/messages-status", "/mentions-status"):  # 新命令 + 旧命令别名
            await self._cmd_mentions_status(user_id, chat_id)
        elif command == "/commands":
            await self._cmd_commands(user_id, chat_id)
        elif command == "/config":
            await self._cmd_config(user_id, chat_id, args)
        elif command == "/monitor":
            await self._cmd_monitor(user_id, chat_id, args)
        else:
            # 非 Remote Claude 命令 → 转发给 Claude CLI（如 /clear、/compact 等）
            await self._forward_to_claude(user_id, chat_id, text)

    # ── 命令处理 ─────────────────────────────────────────────────────────────

    async def _cmd_attach(self, user_id: str, chat_id: str, args: str,
                          message_id: Optional[str] = None):
        """连接到会话"""
        session_name = args.strip()

        if chat_id in self._group_chat_ids:
            if not session_name:
                await self._cmd_group_choose_takeover(user_id, chat_id, message_id=message_id)
                return
            await card_service.send_text(
                chat_id,
                "⚠️ 群聊切换会话会影响当前 AI 工作上下文，请谨慎操作。\n"
                "建议优先尝试恢复原会话，仅在原会话无法恢复时再切换。"
            )

        if not session_name:
            await self._cmd_list(user_id, chat_id, message_id=message_id)
            return

        sessions = list_active_sessions()
        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(
                chat_id, f"会话 '{session_name}' 不存在，使用 /list 查看可用会话"
            )
            return

        ok = await self._attach(chat_id, session_name, user_id=user_id)
        if ok:
            self._chat_bindings[chat_id] = session_name
            self._save_chat_bindings()
            if message_id:
                await self._cmd_list(user_id, chat_id, message_id=message_id)
        else:
            await card_service.send_text(chat_id, f"❌ 无法连接到会话 '{session_name}'")

    async def _cmd_detach(self, user_id: str, chat_id: str,
                           message_id: Optional[str] = None):
        """断开会话"""
        if chat_id not in self._bridges and chat_id not in self._chat_sessions:
            await card_service.send_text(chat_id, "当前未连接到任何会话")
            return

        session_name = self._chat_sessions.get(chat_id) or self._chat_bindings.get(chat_id) or ""
        self._remove_binding_by_chat(chat_id)
        await self._detach(chat_id)

        if chat_id in self._group_chat_ids and session_name:
            self._cancel_group_offline_probe(chat_id)
            self._set_group_status(chat_id, session_name, 'offline', reason='手动断开连接，请恢复或接管会话')

        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_list(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None):
        """列出会话（等价于菜单）"""
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_status(self, user_id: str, chat_id: str, message_id: Optional[str] = None):
        """显示状态"""
        session_name = self._chat_sessions.get(chat_id)
        bridge = self._bridges.get(chat_id)
        if bridge and bridge.running and session_name:
            card = build_status_card(True, session_name)
            await self._send_or_update_card(chat_id, card, message_id)
            return

        if chat_id in self._group_chat_ids:
            await self._cmd_group_show_recovery(
                user_id,
                chat_id,
                message_id=message_id,
                reason_text="当前群未连接会话，请先恢复或接管会话。",
            )
            return

        card = build_status_card(False)
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_start(self, user_id: str, chat_id: str, args: str):
        """启动新会话"""
        parts = args.strip().split(maxsplit=1)
        if not parts:
            await card_service.send_text(
                chat_id,
                "用法: /start <会话名> [工作路径]\n\n"
                "示例:\n"
                "  /start mywork ~/dev/myproject\n"
                "  /start test ~/dev/myproject"
            )
            return

        session_name = parts[0]
        work_dir = parts[1] if len(parts) > 1 else None

        if work_dir:
            work_path = Path(work_dir).expanduser()
            if not work_path.exists():
                await card_service.send_text(chat_id, f"错误: 路径不存在: {work_dir}")
                return
            if not work_path.is_dir():
                await card_service.send_text(chat_id, f"错误: 不是目录: {work_dir}")
                return
            work_dir = str(work_path.absolute())

        sessions = list_active_sessions()
        if any(s["name"] == session_name for s in sessions):
            await card_service.send_text(
                chat_id,
                f"错误: 会话 '{session_name}' 已存在\n使用 /attach {session_name} 连接"
            )
            return

        if session_name in self._starting_sessions:
            await card_service.send_text(chat_id, f"会话 '{session_name}' 正在启动中，请稍候")
            return
        self._starting_sessions.add(session_name)

        script_dir = Path(__file__).parent.parent.absolute()
        server_script = script_dir / "server" / "server.py"
        cmd = ["uv", "run", "--project", str(script_dir), "python3", str(server_script), session_name]
        if self._is_card_mode and self._poller and self._poller.get_bypass_enabled():
            cmd += ["--", "--dangerously-skip-permissions", "--permission-mode=dontAsk"]

        logger.info(f"启动会话: {session_name}, 工作目录: {work_dir}, 命令: {' '.join(cmd)}")
        _track_stats('lark', 'cmd_start', session_name=session_name, chat_id=chat_id)

        try:
            env = _os.environ.copy()
            env.pop("CLAUDECODE", None)

            log_path = USER_DATA_DIR / "startup.log"
            start_time = _datetime.now()

            with open(log_path, 'a') as stderr_fd:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr_fd,
                    start_new_session=True,
                    cwd=work_dir,
                    env=env,
                )

            socket_path = get_socket_path(session_name)
            for i in range(120):
                await asyncio.sleep(0.1)
                if socket_path.exists():
                    break
                if (i + 1) % 10 == 0:
                    elapsed = (i + 1) // 10
                    rc = proc.poll()
                    if rc is not None:
                        log_content = _read_log_since(start_time, log_path)
                        logger.warning(f"会话启动失败: server 进程已退出 (exitcode={rc}, elapsed={elapsed}s)\n{log_content}")
                        await card_service.send_text(chat_id, f"错误: Server 进程意外退出 (code={rc})\n\n{log_content}")
                        return
                    logger.info(f"等待 server socket... ({elapsed}s)")
            else:
                log_content = _read_log_since(start_time, log_path)
                logger.error(f"会话启动超时 (12s), session={session_name}\n{log_content}")
                await card_service.send_text(chat_id, f"错误: 会话启动超时 (12s)\n\n{log_content}")
                return

            ok = await self._attach(chat_id, session_name, user_id=user_id)
            if ok:
                self._chat_bindings[chat_id] = session_name
                self._save_chat_bindings()
            else:
                await card_service.send_text(
                    chat_id,
                    f"会话已启动但连接失败\n使用 /attach {session_name} 重试"
                )

        except Exception as e:
            logger.error(f"启动会话失败: {e}")
            await card_service.send_text(chat_id, f"错误: 启动失败 - {e}")
        finally:
            self._starting_sessions.discard(session_name)

    async def _cmd_start_and_new_group(self, user_id: str, chat_id: str,
                                       session_name: str, path: str):
        """在指定目录启动会话并创建专属群聊"""
        work_path = Path(path).expanduser()
        if not work_path.is_dir():
            await card_service.send_text(chat_id, f"错误: 路径无效: {path}")
            return

        sessions = list_active_sessions()
        active_names = {s["name"] for s in sessions}
        if session_name in active_names or session_name in self._starting_sessions:
            session_name = f"{session_name}_{_datetime.now().strftime('%m%d_%H%M%S')}"

        self._starting_sessions.add(session_name)

        work_dir = str(work_path.absolute())
        script_dir = Path(__file__).parent.parent.absolute()
        server_script = script_dir / "server" / "server.py"
        cmd = ["uv", "run", "--project", str(script_dir), "python3", str(server_script), session_name]
        if self._is_card_mode and self._poller and self._poller.get_bypass_enabled():
            cmd += ["--", "--dangerously-skip-permissions", "--permission-mode=dontAsk"]

        try:
            env = _os.environ.copy()
            env.pop("CLAUDECODE", None)

            log_path = USER_DATA_DIR / "startup.log"
            start_time = _datetime.now()

            with open(log_path, 'a') as stderr_fd:
                proc = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=stderr_fd,
                    start_new_session=True, cwd=work_dir, env=env,
                )

            socket_path = get_socket_path(session_name)
            for i in range(120):
                await asyncio.sleep(0.1)
                if socket_path.exists():
                    break
                if (i + 1) % 10 == 0:
                    elapsed = (i + 1) // 10
                    rc = proc.poll()
                    if rc is not None:
                        log_content = _read_log_since(start_time, log_path)
                        logger.warning(f"启动并创建群聊失败: server 进程已退出 (exitcode={rc}, elapsed={elapsed}s)\n{log_content}")
                        await card_service.send_text(chat_id, f"错误: Server 进程意外退出 (code={rc})\n\n{log_content}")
                        return
            else:
                log_content = _read_log_since(start_time, log_path)
                logger.error(f"启动并创建群聊超时 (12s), session={session_name}\n{log_content}")
                await card_service.send_text(chat_id, f"错误: 会话启动超时 (12s)\n\n{log_content}")
                return

            await self._cmd_new_group(user_id, chat_id, session_name)

        except Exception as e:
            logger.error(f"启动并创建群聊失败: {e}")
            await card_service.send_text(chat_id, f"操作失败：{e}")
        finally:
            self._starting_sessions.discard(session_name)

    async def _cmd_kill(self, user_id: str, chat_id: str, args: str,
                        message_id: Optional[str] = None):
        """终止会话"""
        from utils.session import cleanup_session, tmux_session_exists, tmux_kill_session

        session_name = args.strip()
        if not session_name:
            await card_service.send_text(chat_id, "用法: /kill <会话名>")
            return

        sessions = list_active_sessions()
        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(chat_id, f"错误: 会话 '{session_name}' 不存在")
            return

        # 断开所有连接到此会话的 chat
        for cid, sname in list(self._chat_sessions.items()):
            if sname == session_name:
                active_slice = self._stop_poller(cid)
                if active_slice and self._is_card_mode:
                    await self._update_card_disconnected(cid, sname, active_slice)
                await self._detach(cid)
                if cid in self._group_chat_ids:
                    # 群聊生命周期：会话关闭后保留群，标记为离线
                    self._set_group_status(cid, session_name, 'offline', reason=f"会话 {session_name} 已终止")
                else:
                    self._remove_binding_by_chat(cid, force=True)

        # 清理残留绑定：群聊保留并标记离线，非群聊移除绑定
        changed = False
        for cid in [c for c, s in list(self._chat_bindings.items()) if s == session_name]:
            if cid in self._group_chat_ids:
                self._set_group_status(cid, session_name, 'offline', reason=f"会话 {session_name} 已终止")
            else:
                self._chat_bindings.pop(cid, None)
                changed = True

        if changed:
            self._save_chat_bindings()

        if tmux_session_exists(session_name):
            tmux_kill_session(session_name)
        cleanup_session(session_name)

        await card_service.send_text(chat_id, f"✅ 会话 '{session_name}' 已终止")
        await self._cmd_list(user_id, chat_id, message_id=message_id)

    async def _handle_list_detach(self, user_id: str, chat_id: str,
                                   message_id: Optional[str] = None):
        """会话列表卡片中断开连接，就地刷新列表"""
        session_name = self._chat_sessions.get(chat_id, "")
        # 更新流式卡片为已断开状态（仅卡片模式）
        active_slice = self._stop_poller(chat_id)
        if active_slice and session_name and self._is_card_mode:
            await self._update_card_disconnected(chat_id, session_name, active_slice)

        self._remove_binding_by_chat(chat_id)
        await self._detach(chat_id)   # bridge.disconnect + _stop_poller（幂等）
        await self._cmd_list(user_id, chat_id, message_id=message_id)

    async def _update_card_disconnected(self, chat_id: str, session_name: str,
                                        active_slice: 'CardSlice') -> bool:
        """读取最新 blocks 并就地更新卡片为断开状态（disconnected=True）。Best-effort，不降级发新卡。"""
        blocks = []
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
            from shared_state import SharedStateReader, get_mq_path
            mq_path = get_mq_path(session_name)
            if mq_path.exists():
                reader = SharedStateReader(session_name)
                state = reader.read()
                reader.close()
                blocks = state.get("blocks", [])
        except Exception:
            pass
        blocks_slice = blocks[active_slice.start_idx:]
        card = build_stream_card(
            blocks_slice,
            disconnected=True,
            session_name=session_name,
            is_group=(chat_id in self._group_chat_ids),
        )
        try:
            return await card_service.update_card(
                card_id=active_slice.card_id,
                sequence=active_slice.sequence + 1,
                card_content=card,
            )
        except Exception as e:
            logger.warning(f"_update_card_disconnected 失败 ({chat_id[:8]}...): {e}")
            return False

    async def _handle_stream_detach(self, user_id: str, chat_id: str,
                                     session_name: str, message_id: Optional[str] = None):
        """流式卡片中断开连接，就地更新卡片为已断开状态（仅卡片模式）"""
        # 停止轮询并获取活跃 CardSlice（原子操作）
        active_slice = self._stop_poller(chat_id)

        # 读取最后快照的 blocks
        blocks = []
        try:
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
            from shared_state import SharedStateReader, get_mq_path
            mq_path = get_mq_path(session_name)
            if mq_path.exists():
                reader = SharedStateReader(session_name)
                state = reader.read()
                reader.close()
                blocks = state.get("blocks", [])
        except Exception:
            pass

        self._remove_binding_by_chat(chat_id)
        # _detach 中 _poller.stop() 幂等（已调用 stop_and_get_active_slice）
        await self._detach(chat_id)

        blocks_slice = blocks[active_slice.start_idx:] if active_slice else blocks
        card = build_stream_card(
            blocks_slice,
            disconnected=True,
            session_name=session_name,
            is_group=(chat_id in self._group_chat_ids),
        )

        if chat_id in self._group_chat_ids:
            self._cancel_group_offline_probe(chat_id)
            self._set_group_status(chat_id, session_name, 'offline', reason='手动断开连接，请恢复或接管会话')

        updated = False
        if active_slice:
            try:
                success = await card_service.update_card(
                    card_id=active_slice.card_id,
                    sequence=active_slice.sequence + 1,
                    card_content=card,
                )
                if success:
                    active_slice.sequence += 1
                    self._detached_slices[chat_id] = active_slice
                    updated = True
            except Exception as e:
                logger.warning(f"_handle_stream_detach 就地更新失败: {e}")

        if not updated:
            await self._send_or_update_card(chat_id, card, message_id)

    async def _handle_stream_reconnect(self, user_id: str, chat_id: str,
                                       session_name: str, message_id: Optional[str] = None):
        """流式卡片中重新连接：冻结旧断开卡片 → 重新 attach"""
        # 冻结旧断开卡片
        old_slice = self._detached_slices.pop(chat_id, None)
        if old_slice:
            try:
                frozen_card = build_stream_card([], is_frozen=True, session_name=session_name)
                await card_service.update_card(
                    card_id=old_slice.card_id,
                    sequence=old_slice.sequence + 1,
                    card_content=frozen_card,
                )
            except Exception as e:
                logger.warning(f"_handle_stream_reconnect 冻结旧卡片失败: {e}")
        elif message_id:
            try:
                frozen_card = build_stream_card([], is_frozen=True, session_name=session_name)
                await card_service.update_card_by_message_id(message_id, frozen_card)
            except Exception as e:
                logger.warning(f"_handle_stream_reconnect 按 message_id 冻结失败: {e}")

        await self._cmd_attach(user_id, chat_id, session_name)

    async def _show_unconnected_menu_entry(self, user_id: str, chat_id: str,
                                           reason_text: Optional[str] = None):
        """未连接会话时，主动给出菜单入口"""
        await card_service.send_text(
            chat_id,
            reason_text or "未连接到任何会话，已为你打开能力菜单。"
        )
        await self._cmd_menu(user_id, chat_id)

    async def _show_group_entry_card(self, user_id: str, chat_id: str):
        """群聊首次 @ 但未带文本时，主动展示入口卡，降低使用门槛。"""
        if chat_id not in self._group_chat_ids:
            await self._cmd_menu(user_id, chat_id)
            return

        session_name = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
        bridge = self._bridges.get(chat_id)
        connected = bool(bridge and bridge.running)

        if connected:
            await self._cmd_menu(user_id, chat_id)
            return

        if session_name:
            reason = f"当前会话 {session_name} 离线，请先恢复会话。"
        else:
            reason = "当前群未绑定可用会话，请先选择接管会话。"
        await self._cmd_group_show_recovery(user_id, chat_id, reason_text=reason)

    async def _cmd_help(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None):
        """显示帮助"""
        card = build_help_card()
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_menu(self, user_id: str, chat_id: str,
                         message_id: Optional[str] = None, page: int = 0):
        """显示会话列表菜单"""
        # 专属群聊：展示精简菜单，避免误操作切换会话
        if chat_id in self._group_chat_ids:
            session_name = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
            bridge = self._bridges.get(chat_id)
            connected = bool(bridge and bridge.running)
            meta = self._get_group_meta(chat_id)
            card = build_group_menu_card(
                session_name,
                connected=connected,
                status=str(meta.get('status', 'active' if connected else 'offline')),
                reason=str(meta.get('reason', '') or ''),
            )
            await self._send_or_update_card(chat_id, card, message_id)
            return

        sessions = list_active_sessions()
        current = self._chat_sessions.get(chat_id)

        # 会话 -> 专属群 chat_id 映射（用于会话列表展示“进入群聊/创建群聊”）
        session_groups: Dict[str, str] = {}
        for cid in sorted(self._group_chat_ids):
            session_name = self._chat_bindings.get(cid)
            if session_name and session_name not in session_groups:
                session_groups[session_name] = cid

        notify_enabled = self._poller.get_notify_enabled() if self._is_card_mode and self._poller else False
        urgent_enabled = self._poller.get_urgent_enabled() if self._is_card_mode and self._poller else False
        bypass_enabled = self._poller.get_bypass_enabled() if self._is_card_mode and self._poller else False

        card = build_menu_card(
            sessions,
            current_session=current,
            session_groups=session_groups,
            page=page,
            notify_enabled=notify_enabled,
            urgent_enabled=urgent_enabled,
            bypass_enabled=bypass_enabled,
            show_preferences=bool(self._is_card_mode and self._poller),
        )
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_group_show_recovery(self, user_id: str, chat_id: str,
                                       message_id: Optional[str] = None,
                                       reason_text: str = ""):
        """群聊离线恢复入口卡片"""
        if chat_id not in self._group_chat_ids:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return

        bridge = self._bridges.get(chat_id)
        session_name = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
        connected = bool(bridge and bridge.running and self._chat_sessions.get(chat_id))
        if connected:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return

        reason = reason_text or self._get_group_offline_reason(chat_id)
        card = build_group_recovery_card(session_name, reason=reason)
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_group_reconnect_original(self, user_id: str, chat_id: str,
                                            message_id: Optional[str] = None):
        """群聊：重连当前绑定会话（single-flight + request_id 幂等）"""
        if chat_id not in self._group_chat_ids:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return

        session_name = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
        if not session_name:
            await self._cmd_group_show_recovery(
                user_id,
                chat_id,
                message_id=message_id,
                reason_text="当前群未绑定会话，请选择现有会话接管。",
            )
            return

        bridge = self._bridges.get(chat_id)
        if bridge and bridge.running and self._chat_sessions.get(chat_id) == session_name:
            if message_id:
                await self._cmd_menu(user_id, chat_id, message_id=message_id)
            else:
                await card_service.send_text(chat_id, f"ℹ️ 当前会话 {session_name} 已在线，无需恢复。")
            return

        last = self._group_recovery_last_result.get(chat_id)
        now_ts = int(time.time())
        if last and last.get('session') == session_name and now_ts - int(last.get('ts', 0)) <= 30:
            ok = bool(last.get('ok'))
            ok_text = "成功" if ok else "失败"
            if message_id:
                if ok:
                    await self._cmd_menu(user_id, chat_id, message_id=message_id)
                else:
                    await self._cmd_group_show_recovery(
                        user_id,
                        chat_id,
                        message_id=message_id,
                        reason_text=f"最近恢复结果（request_id={last.get('request_id', '-')}, 失败），请重新选择恢复方式。",
                    )
                return
            await card_service.send_text(chat_id, f"ℹ️ 已返回最近恢复结果（request_id={last.get('request_id', '-')}, {ok_text}）。")
            return

        if chat_id in self._group_recovery_inflight:
            rid = self._group_recovery_inflight[chat_id].get('request_id', '-')
            if message_id:
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"恢复进行中（request_id={rid}），请稍候。",
                )
                return
            await card_service.send_text(chat_id, f"⏳ 恢复进行中（request_id={rid}），请稍候。")
            return

        request_id = str(uuid.uuid4())
        self._group_recovery_inflight[chat_id] = {"request_id": request_id, "kind": "reconnect", "session": session_name}

        try:
            if message_id:
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"恢复进行中（request_id={request_id}），正在重连原会话。",
                )

            lock = self._ensure_group_recovery_lock(chat_id)
            async with lock:
                ok = await self._attach(chat_id, session_name, user_id=user_id)
                if ok:
                    ready = await self._verify_group_recovery_ready(chat_id, session_name)
                    if not ready:
                        self._set_group_status(chat_id, session_name, 'offline', reason=f"恢复校验失败：会话 {session_name} 尚未就绪")
                        self._group_recovery_last_result[chat_id] = {
                            "request_id": request_id,
                            "ok": False,
                            "session": session_name,
                            "reason": "not_ready",
                            "ts": int(time.time()),
                        }
                        await self._cmd_group_show_recovery(
                            user_id,
                            chat_id,
                            message_id=message_id,
                            reason_text=f"恢复校验失败（request_id={request_id}）：会话 {session_name} 尚未就绪，请重新恢复或接管。",
                        )
                        return

                    self._chat_bindings[chat_id] = session_name
                    self._save_chat_bindings()
                    self._set_group_status(chat_id, session_name, 'active')
                    injected = await self._inject_recovery_context(chat_id)
                    msg = f"✅ 已恢复会话：{session_name}（request_id={request_id}）"
                    if not injected:
                        msg += "\n⚠️ 上下文恢复未完成，请手动补充背景后继续。"
                        if message_id:
                            self._set_group_status(chat_id, session_name, 'active', reason="上下文恢复未完成，请手动补充背景后继续。")
                    if not message_id:
                        await card_service.send_text(chat_id, msg)
                    self._group_recovery_last_result[chat_id] = {
                        "request_id": request_id,
                        "ok": True,
                        "session": session_name,
                        "ts": int(time.time()),
                    }
                    await self._cmd_menu(user_id, chat_id, message_id=message_id)
                    return

                self._set_group_status(chat_id, session_name, 'offline', reason=f"重连失败：原会话 {session_name} 当前不可连接")
                self._group_recovery_last_result[chat_id] = {
                    "request_id": request_id,
                    "ok": False,
                    "session": session_name,
                    "reason": "unreachable",
                    "ts": int(time.time()),
                }
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"重连失败：原会话 {session_name} 当前不可连接，请选择现有会话接管。",
                )
        finally:
            self._group_recovery_inflight.pop(chat_id, None)

    async def _cmd_group_choose_takeover(self, user_id: str, chat_id: str,
                                         message_id: Optional[str] = None):
        """群聊：展示可接管会话列表"""
        if chat_id not in self._group_chat_ids:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return

        session_name = self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id)
        sessions = list_active_sessions()
        card = build_group_recovery_card(
            session_name,
            reason="请选择一个现有会话接管到本群。",
            sessions=sessions,
        )
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_group_takeover_session(self, user_id: str, chat_id: str,
                                          session_name: str,
                                          message_id: Optional[str] = None):
        """群聊：接管到指定现有会话（single-flight + request_id 幂等）"""
        if chat_id not in self._group_chat_ids:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return

        session_name = (session_name or "").strip()
        if not session_name:
            await self._cmd_group_choose_takeover(user_id, chat_id, message_id=message_id)
            return

        last = self._group_recovery_last_result.get(chat_id)
        now_ts = int(time.time())
        if last and last.get('session') == session_name and now_ts - int(last.get('ts', 0)) <= 30:
            ok = bool(last.get('ok'))
            ok_text = "成功" if ok else "失败"
            if message_id:
                if ok:
                    await self._cmd_menu(user_id, chat_id, message_id=message_id)
                else:
                    await self._cmd_group_show_recovery(
                        user_id,
                        chat_id,
                        message_id=message_id,
                        reason_text=f"最近恢复结果（request_id={last.get('request_id', '-')}, 失败），请重新选择恢复方式。",
                    )
                return
            await card_service.send_text(chat_id, f"ℹ️ 已返回最近恢复结果（request_id={last.get('request_id', '-')}, {ok_text}）。")
            return

        if chat_id in self._group_recovery_inflight:
            rid = self._group_recovery_inflight[chat_id].get('request_id', '-')
            if message_id:
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"恢复进行中（request_id={rid}），请稍候。",
                )
                return
            await card_service.send_text(chat_id, f"⏳ 恢复进行中（request_id={rid}），请稍候。")
            return

        sessions = list_active_sessions()
        if not any(s.get("name") == session_name for s in sessions):
            card = build_group_recovery_card(
                self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id),
                reason=f"会话 {session_name} 不存在或已退出，请重新选择。",
                sessions=sessions,
            )
            await self._send_or_update_card(chat_id, card, message_id)
            return

        request_id = str(uuid.uuid4())
        self._group_recovery_inflight[chat_id] = {"request_id": request_id, "kind": "takeover", "session": session_name}

        try:
            if message_id:
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"恢复进行中（request_id={request_id}），正在接管会话 {session_name}。",
                )

            lock = self._ensure_group_recovery_lock(chat_id)
            async with lock:
                ok = await self._attach(chat_id, session_name, user_id=user_id)
                if not ok:
                    self._set_group_status(chat_id, session_name, 'offline', reason=f"接管失败：会话 {session_name} 当前不可连接")
                    self._group_recovery_last_result[chat_id] = {
                        "request_id": request_id,
                        "ok": False,
                        "session": session_name,
                        "reason": "unreachable",
                        "ts": int(time.time()),
                    }
                    card = build_group_recovery_card(
                        self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id),
                        reason=f"接管失败：会话 {session_name} 当前不可连接，请重新选择或稍后重试。",
                        sessions=sessions,
                    )
                    await self._send_or_update_card(chat_id, card, message_id)
                    return

                ready = await self._verify_group_recovery_ready(chat_id, session_name)
                if not ready:
                    self._set_group_status(chat_id, session_name, 'offline', reason=f"接管校验失败：会话 {session_name} 尚未就绪")
                    self._group_recovery_last_result[chat_id] = {
                        "request_id": request_id,
                        "ok": False,
                        "session": session_name,
                        "reason": "not_ready",
                        "ts": int(time.time()),
                    }
                    card = build_group_recovery_card(
                        self._chat_bindings.get(chat_id) or self._chat_sessions.get(chat_id),
                        reason=f"接管校验失败（request_id={request_id}）：会话 {session_name} 尚未就绪，请重新选择或稍后重试。",
                        sessions=sessions,
                    )
                    await self._send_or_update_card(chat_id, card, message_id)
                    return

                self._chat_bindings[chat_id] = session_name
                self._save_chat_bindings()
                self._set_group_status(chat_id, session_name, 'active')
                injected = await self._inject_recovery_context(chat_id)
                msg = f"✅ 接管成功，当前会话：{session_name}（request_id={request_id}）"
                if not injected:
                    msg += "\n⚠️ 上下文恢复未完成，请手动补充背景后继续。"
                    if message_id:
                        self._set_group_status(chat_id, session_name, 'active', reason="上下文恢复未完成，请手动补充背景后继续。")
                if not message_id:
                    await card_service.send_text(chat_id, msg)
                self._group_recovery_last_result[chat_id] = {
                    "request_id": request_id,
                    "ok": True,
                    "session": session_name,
                    "ts": int(time.time()),
                }
                await self._cmd_menu(user_id, chat_id, message_id=message_id)
        finally:
            self._group_recovery_inflight.pop(chat_id, None)

    async def _cmd_summarize_now(self, user_id: str, chat_id: str,
                                 message_id: Optional[str] = None):
        """群聊手动触发总结"""
        if chat_id not in self._group_chat_ids:
            await card_service.send_text(chat_id, "该命令仅支持在专属群聊中使用。")
            return

        if chat_id in self._group_recovery_inflight:
            rid = self._group_recovery_inflight[chat_id].get('request_id', '-')
            if message_id:
                await self._cmd_group_show_recovery(
                    user_id,
                    chat_id,
                    message_id=message_id,
                    reason_text=f"恢复进行中（request_id={rid}），请稍后再触发总结。",
                )
                return
            await card_service.send_text(chat_id, f"⏳ 当前正在恢复会话（request_id={rid}），稍后再触发总结。")
            return

        bridge = self._bridges.get(chat_id)
        if not bridge or not bridge.running:
            await self._cmd_group_show_recovery(
                user_id,
                chat_id,
                message_id=message_id,
                reason_text="当前会话离线，请先恢复会话后再总结。",
            )
            return

        lock = self._group_summary_locks.get(chat_id)
        if lock and lock.locked():
            if message_id:
                await self._cmd_menu(user_id, chat_id, message_id=message_id)
                return
            await card_service.send_text(chat_id, "⏳ 总结任务进行中，请稍后再试。")
            return

        ok = await self._emit_group_summary(chat_id, force=True, trigger="manual")
        if message_id:
            await self._cmd_menu(user_id, chat_id, message_id=message_id)
            return
        if ok:
            await card_service.send_text(chat_id, "✅ 已触发群聊总结。")
        else:
            await card_service.send_text(chat_id, "⚠️ 总结触发失败（上下文不足或暂不可用）。")

    @staticmethod
    def _truncate_text(text: str, limit: int) -> str:
        content = (text or "").strip()
        if len(content) <= limit:
            return content
        return content[:limit].rstrip() + "\n...（已截断）"

    @staticmethod
    def _format_code_block(text: str, *, lang: str = "text", limit: int = 2200) -> str:
        content = (text or "").replace("```", "'''")
        content = LarkHandler._truncate_text(content, limit)
        return f"```{lang}\n{content or '(empty)'}\n```"

    def _git_run(self, repo_root: Path, args: List[str], *, timeout: int = 8) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def _ensure_round_diff_baseline(self, chat_id: str, session_name: str, repo_root: Path) -> Optional[Dict[str, Any]]:
        """确保 chat 级“本轮对话变更”基线已建立。"""
        baseline = self._round_diff_baseline.get(chat_id)
        if baseline and baseline.get("session_name") == session_name and baseline.get("repo_root") == str(repo_root):
            return baseline

        inside = self._git_run(repo_root, ["rev-parse", "--is-inside-work-tree"], timeout=5)
        if inside.returncode != 0:
            return None

        head_proc = self._git_run(repo_root, ["rev-parse", "HEAD"], timeout=5)
        head = (head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""

        merge_base = ""
        mb_proc = self._git_run(repo_root, ["merge-base", "HEAD", "main"], timeout=5)
        if mb_proc.returncode == 0:
            merge_base = (mb_proc.stdout or "").strip()

        status_proc = self._git_run(repo_root, ["status", "--short"], timeout=8)
        staged_proc = self._git_run(repo_root, ["diff", "--no-color", "--staged", "--unified=1"], timeout=8)
        unstaged_proc = self._git_run(repo_root, ["diff", "--no-color", "--unified=1"], timeout=8)

        baseline_status_lines = [ln.rstrip() for ln in (status_proc.stdout or "").splitlines() if ln.strip()]
        baseline_diff = "\n".join([
            (staged_proc.stdout or "").strip(),
            (unstaged_proc.stdout or "").strip(),
        ]).strip()

        baseline = {
            "session_name": session_name,
            "repo_root": str(repo_root),
            "merge_base": merge_base,
            "baseline_head": head,
            "baseline_status_lines": baseline_status_lines,
            "baseline_diff": baseline_diff,
            "captured_at": int(time.time()),
        }
        self._round_diff_baseline[chat_id] = baseline
        return baseline

    def _capture_round_diff_baseline_for_chat(self, chat_id: str, session_name: str):
        """在 attach 成功后捕获本 chat 的对话基线。"""
        try:
            sessions = list_active_sessions()
            current = next((s for s in sessions if s.get("name") == session_name), None)
            cwd = str((current or {}).get("cwd", "") or "").strip()
            if not cwd:
                return
            # 每次显式 attach/reconnect 都重置“本轮”范围
            self._round_diff_baseline.pop(chat_id, None)
            self._ensure_round_diff_baseline(chat_id, session_name, Path(cwd))
        except Exception as e:
            logger.debug(f"捕获 round diff 基线失败: {e}")

    async def _cmd_view_round_diff(self, user_id: str, chat_id: str,
                                   message_id: Optional[str] = None):
        """查看当前 chat 绑定会话的工作区变更（文件清单 + diff 摘要）。"""
        session_name = self._chat_sessions.get(chat_id) or self._chat_bindings.get(chat_id)
        if not session_name:
            await card_service.send_text(chat_id, "当前 chat 未绑定会话，无法定位本轮变更。请先 /attach 或在群里恢复会话。")
            return

        sessions = list_active_sessions()
        current = next((s for s in sessions if s.get("name") == session_name), None)
        cwd = str((current or {}).get("cwd", "") or "").strip()
        if not cwd:
            await card_service.send_text(chat_id, f"未找到会话 `{session_name}` 的工作目录，无法查看本轮变更。")
            return

        repo_root = Path(cwd)

        try:
            inside = self._git_run(repo_root, ["rev-parse", "--is-inside-work-tree"], timeout=5)
            if inside.returncode != 0:
                await card_service.send_text(chat_id, f"会话 `{session_name}` 当前目录不是 Git 仓库，无法查看变更 diff。")
                return

            baseline = self._ensure_round_diff_baseline(chat_id, session_name, repo_root)

            head_proc = self._git_run(repo_root, ["rev-parse", "HEAD"], timeout=5)
            current_head = (head_proc.stdout or "").strip() if head_proc.returncode == 0 else ""

            status_proc = self._git_run(repo_root, ["status", "--short"], timeout=8)
            staged_proc = self._git_run(repo_root, ["diff", "--no-color", "--staged", "--unified=1"], timeout=8)
            unstaged_proc = self._git_run(repo_root, ["diff", "--no-color", "--unified=1"], timeout=8)

            status_lines = [ln.rstrip() for ln in (status_proc.stdout or "").splitlines() if ln.strip()]
            files_text = "\n".join(status_lines[:25]) if status_lines else "工作区无未提交变更。"
            if len(status_lines) > 25:
                files_text += f"\n...（其余 {len(status_lines) - 25} 项已省略）"

            staged_diff = (staged_proc.stdout or "").strip()
            unstaged_diff = (unstaged_proc.stdout or "").strip()
            current_diff = "\n".join([staged_diff, unstaged_diff]).strip()

            round_status_text = ""
            round_diff_text = ""
            has_round_delta = False

            if baseline:
                baseline_status_lines = [str(x) for x in (baseline.get("baseline_status_lines") or [])]
                baseline_diff = str(baseline.get("baseline_diff", "") or "")

                status_delta = list(difflib.unified_diff(
                    baseline_status_lines,
                    status_lines,
                    fromfile="baseline-status",
                    tofile="current-status",
                    lineterm="",
                ))
                diff_delta = list(difflib.unified_diff(
                    baseline_diff.splitlines(),
                    current_diff.splitlines(),
                    fromfile="baseline-diff",
                    tofile="current-diff",
                    lineterm="",
                ))
                round_status_text = "\n".join(status_delta).strip()
                round_diff_text = "\n".join(diff_delta).strip()
                has_round_delta = bool(round_status_text or round_diff_text)

            captured_at = int((baseline or {}).get("captured_at", 0) or 0)
            captured_at_str = _datetime.fromtimestamp(captured_at).strftime("%Y-%m-%d %H:%M:%S") if captured_at else "未知"

            elements: List[Dict[str, Any]] = [
                {
                    "tag": "markdown",
                    "content": (
                        "**本轮变更范围：仅当前 chat 对话期间新增的变更（相对会话接入基线）**\n"
                        f"会话：`{session_name}`\n"
                        f"仓库路径：`{repo_root}`\n"
                        f"基线时间：`{captured_at_str}`\n"
                        f"基线 HEAD：`{(baseline or {}).get('baseline_head', '') or '(unknown)'}`\n"
                        f"当前 HEAD：`{current_head or '(unknown)'}`"
                    ),
                },
                {"tag": "hr"},
                {"tag": "markdown", "content": "**本轮新增/变化文件（相对基线）**"},
            ]

            if baseline and has_round_delta:
                elements.append({"tag": "markdown", "content": self._format_code_block(round_status_text or "(有变更，但状态列表为空)", limit=1800)})
            elif baseline:
                elements.append({"tag": "markdown", "content": self._format_code_block("本轮尚无新增变更（相对基线）。", limit=600)})
            else:
                elements.append({"tag": "markdown", "content": self._format_code_block("当前无法建立本轮基线，回退为工作区视角。", limit=600)})

            if baseline and round_diff_text:
                elements.extend([
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**本轮关键 Diff（相对基线）**"},
                    {"tag": "markdown", "content": self._format_code_block(round_diff_text, lang="diff", limit=2600)},
                ])

            elements.extend([
                {"tag": "hr"},
                {"tag": "markdown", "content": "**当前工作区快照（用于对照）**"},
                {"tag": "markdown", "content": self._format_code_block(files_text, limit=1400)},
            ])

            if staged_diff:
                elements.extend([
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**工作区 Diff（已暂存）**"},
                    {"tag": "markdown", "content": self._format_code_block(staged_diff, lang="diff", limit=2200)},
                ])

            if unstaged_diff:
                elements.extend([
                    {"tag": "hr"},
                    {"tag": "markdown", "content": "**工作区 Diff（未暂存）**"},
                    {"tag": "markdown", "content": self._format_code_block(unstaged_diff, lang="diff", limit=2200)},
                ])

            elements.extend([
                {"tag": "hr"},
                {
                    "tag": "markdown",
                    "content": (
                        "**说明**\n"
                        "- 本轮视角：以当前 chat 连接该会话时的基线为起点。\n"
                        "- 若你希望重置“本轮”范围，重新 /attach（或恢复接管）该会话即可。"
                    ),
                },
                {"tag": "hr"},
                _build_menu_button_only(),
            ])

            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": _build_header("🔍 本轮变更", "blue"),
                "body": {"elements": elements},
            }
            await self._send_or_update_card(chat_id, card, message_id)
        except Exception as e:
            logger.error(f"查看本轮变更失败: {e}", exc_info=True)
            await card_service.send_text(chat_id, f"查看本轮变更失败：{e}")

    async def _cmd_toggle_notify(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换就绪通知开关并刷新菜单卡片"""
        if not self._is_card_mode or not self._poller:
            await card_service.send_text(chat_id, "此功能仅在卡片模式下可用")
            return
        new_value = not self._poller.get_notify_enabled()
        self._poller.set_notify_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_toggle_urgent(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换加急通知开关并刷新菜单卡片"""
        if not self._is_card_mode or not self._poller:
            await card_service.send_text(chat_id, "此功能仅在卡片模式下可用")
            return
        new_value = not self._poller.get_urgent_enabled()
        self._poller.set_urgent_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    async def _cmd_toggle_bypass(self, user_id: str, chat_id: str,
                                  message_id: Optional[str] = None):
        """切换新会话 bypass 开关并刷新菜单卡片"""
        if not self._is_card_mode or not self._poller:
            await card_service.send_text(chat_id, "此功能仅在卡片模式下可用")
            return
        new_value = not self._poller.get_bypass_enabled()
        self._poller.set_bypass_enabled(new_value)
        await self._cmd_menu(user_id, chat_id, message_id=message_id)

    # ── OAuth 授权命令 ──────────────────────────────────────────────────────

    async def _cmd_oauth(self, user_id: str, chat_id: str):
        """显示 OAuth 授权链接和当前状态"""
        if not self.oauth_service:
            await card_service.send_text(chat_id, "用户授权功能未启用。\n请在 .env 中设置 ENABLE_USER_AUTH=true 后重启飞书客户端。")
            return

        from . import config
        auth_url = f"http://localhost:{config.OAUTH_SERVER_PORT}/oauth/authorize"
        token_data = self.oauth_service.get_user_token(user_id)

        if token_data and not self.oauth_service.is_token_expired(token_data):
            status_text = "✅ 你已授权，token 有效。\n如需重新授权，请点击下方链接。"
        elif token_data:
            status_text = "⚠️ 你的授权已过期，请重新授权。"
        else:
            status_text = "你尚未授权，点击下方链接完成授权。"

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": _build_header("🔐 用户授权", "blue"),
            "body": {"elements": [
                {"tag": "markdown", "content": status_text},
                {"tag": "markdown", "content": f"**授权页面**: [点击前往授权]({auth_url})"},
                {"tag": "hr"},
                {"tag": "markdown", "content": "授权后可使用以用户身份发送消息等高级功能。\n• `/oauth-status` 查看授权状态\n• `/oauth-revoke` 撤销授权"},
            ]}
        }
        await card_service.create_and_send_card(chat_id, card)

    async def _cmd_oauth_status(self, user_id: str, chat_id: str):
        """查看当前用户的 OAuth 授权状态"""
        if not self.oauth_service:
            await card_service.send_text(chat_id, "用户授权功能未启用。")
            return

        token_data = self.oauth_service.get_user_token(user_id)
        if not token_data:
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": _build_header("🔐 授权状态", "grey"),
                "body": {"elements": [
                    {"tag": "markdown", "content": "**状态**: 未授权\n\n发送 `/oauth` 获取授权链接。"},
                ]}
            }
            await card_service.create_and_send_card(chat_id, card)
            return

        expired = self.oauth_service.is_token_expired(token_data)
        saved_at = token_data.get("saved_at", 0)
        expires_in = token_data.get("expires_in", 0)

        from datetime import datetime
        auth_time = datetime.fromtimestamp(saved_at).strftime("%Y-%m-%d %H:%M:%S") if saved_at else "未知"
        expire_time = datetime.fromtimestamp(saved_at + expires_in).strftime("%Y-%m-%d %H:%M:%S") if saved_at and expires_in else "未知"

        if expired:
            status_icon = "⚠️"
            status_text = "已过期"
            color = "orange"
        else:
            status_icon = "✅"
            status_text = "有效"
            color = "green"

        info_lines = [
            f"**状态**: {status_icon} {status_text}",
            f"**授权时间**: {auth_time}",
            f"**过期时间**: {expire_time}",
            f"**Token 预览**: {token_data.get('access_token', '')[:8]}...",
        ]
        has_refresh = bool(token_data.get("refresh_token"))
        info_lines.append(f"**Refresh Token**: {'有' if has_refresh else '无'}")

        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": _build_header("🔐 授权状态", color),
            "body": {"elements": [
                {"tag": "markdown", "content": "\n".join(info_lines)},
            ]}
        }
        await card_service.create_and_send_card(chat_id, card)

    async def _cmd_oauth_revoke(self, user_id: str, chat_id: str):
        """撤销当前用户的 OAuth 授权"""
        if not self.oauth_service:
            await card_service.send_text(chat_id, "用户授权功能未启用。")
            return

        removed = self.oauth_service.remove_user_token(user_id)
        if removed:
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": _build_header("🔐 授权已撤销", "red"),
                "body": {"elements": [
                    {"tag": "markdown", "content": "你的授权已被撤销，token 已删除。\n\n如需重新授权，发送 `/oauth`。"},
                ]}
            }
        else:
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": _build_header("🔐 撤销授权", "grey"),
                "body": {"elements": [
                    {"tag": "markdown", "content": "你当前没有授权记录。"},
                ]}
            }
        await card_service.create_and_send_card(chat_id, card)

    async def _cmd_check_mentions(self, user_id: str, chat_id: str, args: str):
        """立即检查未回复消息（@消息和私聊）"""
        if not self.oauth_service:
            await card_service.send_text(chat_id, "用户授权功能未启用，无法使用消息检测功能。")
            return

        # 检查是否授权
        from .user_api import LarkUserApi
        user_api = LarkUserApi(self.oauth_service)
        token_status = await user_api.check_token_validity(user_id)

        if not token_status.get("authorized") or not token_status.get("has_valid_token"):
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": _build_header("🔐 需要授权", "orange"),
                "body": {"elements": [
                    {"tag": "markdown", "content": "使用消息检测功能需要先进行 OAuth 授权。\n\n发送 `/oauth` 开始授权流程。"},
                ]}
            }
            await card_service.create_and_send_card(chat_id, card)
            return

        # 检查是否是 --all 参数
        check_all = "--all" in args.lower()

        # 解析时间范围参数（支持自然语言）
        hours_limit = None
        args_lower = args.lower()
        import re

        # 匹配 "最近X小时" 或 "X小时" 或 "X hours"
        time_patterns = [
            r'最近\s*(\d+)\s*小时',
            r'(\d+)\s*小时',
            r'(\d+)\s*hours?',
            r'past\s+(\d+)\s+hours?',
        ]

        for pattern in time_patterns:
            match = re.search(pattern, args_lower)
            if match:
                hours_limit = int(match.group(1))
                break

        # 发送检查中提示
        time_hint = f"（最近{hours_limit}小时内）" if hours_limit else ""
        await card_service.send_text(
            chat_id,
            f"🔍 正在检查{'所有' if check_all else ''}未回复消息{time_hint}...\n\n"
            f"📊 检查 30 个群 + 实时私聊消息\n"
            f"⚡ 预计需要 30-60 秒\n\n"
            f"💡 私聊消息通过实时推送检测，无需轮询"
        )

        try:
            # 执行检查（不设置超时，确保能拿到完整数据）
            if not hasattr(self, 'mention_poller'):
                await card_service.send_text(chat_id, "❌ 消息轮询器未初始化")
                return

            # 直接调用，不设超时
            mentions = await self.mention_poller.check_now(
                user_id,
                notify_chat_id=chat_id,
                check_all=check_all,
                hours_limit=hours_limit
            )

            if not mentions:
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": _build_header("✅ 没有未回复消息", "green"),
                    "body": {"elements": [
                        {"tag": "markdown", "content": "太棒了！所有消息都已回复。"},
                    ]}
                }
            else:
                # 构建消息列表卡片
                from .card_builder import build_mentions_card
                card = build_mentions_card(mentions)

            await card_service.create_and_send_card(chat_id, card)

        except Exception as e:
            logger.error(f"检查消息失败: {e}", exc_info=True)
            await card_service.send_text(chat_id, f"❌ 检查失败：{str(e)}")

    async def _cmd_mentions_auto(self, user_id: str, chat_id: str, args: str):
        """开启/关闭自动检查"""
        if not hasattr(self, 'config_service') or not hasattr(self, 'mention_poller'):
            await card_service.send_text(chat_id, "❌ 消息检测功能未初始化")
            return

        parts = args.strip().split()
        if not parts or parts[0] not in ("on", "off"):
            await card_service.send_text(
                chat_id,
                "用法: /messages-auto on|off [interval]\n\n"
                "示例:\n"
                "  /messages-auto on         # 开启自动检查（默认10分钟）\n"
                "  /messages-auto on 15      # 开启自动检查，间隔15分钟\n"
                "  /messages-auto off        # 关闭自动检查"
            )
            return

        action = parts[0]
        interval = int(parts[1]) if len(parts) > 1 else 10

        # 验证间隔
        if not (5 <= interval <= 60):
            await card_service.send_text(chat_id, "❌ 检查间隔必须在 5-60 分钟范围内")
            return

        try:
            if action == "on":
                self.config_service.set("mention.auto_check_enabled", True)
                self.config_service.set("mention.check_interval_minutes", interval)
                self.config_service.save()
                self.mention_poller.restart_with_new_interval(interval)

                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": _build_header("✅ 自动检查已开启", "green"),
                    "body": {"elements": [
                        {"tag": "markdown", "content": f"消息自动检查已开启（@消息和私聊）\n\n检查间隔: **{interval} 分钟**"},
                    ]}
                }
            else:
                self.config_service.set("mention.auto_check_enabled", False)
                self.config_service.save()
                self.mention_poller.stop()

                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": _build_header("⏸️ 自动检查已关闭", "grey"),
                    "body": {"elements": [
                        {"tag": "markdown", "content": "消息自动检查已关闭"},
                    ]}
                }

            await card_service.create_and_send_card(chat_id, card)

        except Exception as e:
            logger.error(f"设置自动检查失败: {e}", exc_info=True)
            await card_service.send_text(chat_id, f"❌ 设置失败：{str(e)}")

    async def _cmd_mentions_config(self, user_id: str, chat_id: str, args: str):
        """配置黑名单/重点群"""
        if not hasattr(self, 'config_service'):
            await card_service.send_text(chat_id, "❌ 配置服务未初始化")
            return

        parts = args.strip().split()
        if len(parts) < 2:
            await card_service.send_text(
                chat_id,
                "用法: /messages-config blacklist|priority add|remove|list [chat_id]\n\n"
                "示例:\n"
                "  /messages-config blacklist list           # 查看黑名单\n"
                "  /messages-config blacklist add oc_xxx     # 添加到黑名单\n"
                "  /messages-config blacklist remove oc_xxx  # 从黑名单移除\n"
                "  /messages-config priority add oc_xxx      # 添加到重点群\n"
                "  /messages-config priority list            # 查看重点群"
            )
            return

        list_type = parts[0]  # blacklist 或 priority
        action = parts[1]     # add, remove, list

        if list_type not in ("blacklist", "priority"):
            await card_service.send_text(chat_id, "❌ 列表类型必须是 blacklist 或 priority")
            return

        list_key = f"mention.{list_type}_chats"

        try:
            if action == "list":
                # 列出当前配置
                items = self.config_service.get_list(list_key)
                list_name = "黑名单" if list_type == "blacklist" else "重点群"

                if not items:
                    content = f"{list_name}为空"
                else:
                    content = f"{list_name}（共 {len(items)} 个）:\n\n" + "\n".join(f"• `{item}`" for item in items)

                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": _build_header(f"📋 {list_name}", "blue"),
                    "body": {"elements": [
                        {"tag": "markdown", "content": content},
                    ]}
                }
                await card_service.create_and_send_card(chat_id, card)

            elif action == "add":
                if len(parts) < 3:
                    await card_service.send_text(chat_id, "❌ 请提供 chat_id")
                    return

                chat_id_to_add = parts[2]
                self.config_service.add_to_list(list_key, chat_id_to_add)
                self.config_service.save()

                list_name = "黑名单" if list_type == "blacklist" else "重点群"
                await card_service.send_text(chat_id, f"✅ 已添加到{list_name}: {chat_id_to_add}")

            elif action == "remove":
                if len(parts) < 3:
                    await card_service.send_text(chat_id, "❌ 请提供 chat_id")
                    return

                chat_id_to_remove = parts[2]
                self.config_service.remove_from_list(list_key, chat_id_to_remove)
                self.config_service.save()

                list_name = "黑名单" if list_type == "blacklist" else "重点群"
                await card_service.send_text(chat_id, f"✅ 已从{list_name}移除: {chat_id_to_remove}")

            else:
                await card_service.send_text(chat_id, "❌ 操作必须是 add, remove 或 list")

        except ValueError as e:
            await card_service.send_text(chat_id, f"❌ {str(e)}")
        except Exception as e:
            logger.error(f"配置失败: {e}", exc_info=True)
            await card_service.send_text(chat_id, f"❌ 配置失败：{str(e)}")

    async def _cmd_mentions_status(self, user_id: str, chat_id: str):
        """查看消息检查状态"""
        if not hasattr(self, 'config_service') or not hasattr(self, 'mention_poller'):
            await card_service.send_text(chat_id, "❌ 消息检测功能未初始化")
            return

        try:
            auto_enabled = self.config_service.get("mention.auto_check_enabled", False)
            interval = self.config_service.get("mention.check_interval_minutes", 10)
            last_check = self.mention_poller.state.last_check_time
            unreplied_count = len(self.mention_poller.state.known_unreplied)

            # 格式化最后检查时间
            if last_check > 0:
                from datetime import datetime
                last_check_dt = datetime.fromtimestamp(last_check / 1000)
                last_check_str = last_check_dt.strftime("%Y-%m-%d %H:%M:%S")
            else:
                last_check_str = "从未检查"

            # 构建状态卡片
            from .card_builder import build_mention_status_card
            status_data = {
                "auto_enabled": auto_enabled,
                "interval": interval,
                "last_check": last_check_str,
                "unreplied_count": unreplied_count
            }
            card = build_mention_status_card(status_data)
            await card_service.create_and_send_card(chat_id, card)

        except Exception as e:
            logger.error(f"获取状态失败: {e}", exc_info=True)
            await card_service.send_text(chat_id, f"❌ 获取状态失败：{str(e)}")

    async def _cmd_commands(self, user_id: str, chat_id: str):
        """显示所有可用命令"""
        from .card_builder import build_commands_card
        card = build_commands_card()
        await card_service.create_and_send_card(chat_id, card)

    async def _cmd_config(self, user_id: str, chat_id: str, args: str):
        """统一配置管理"""
        if not hasattr(self, 'config_service'):
            await card_service.send_text(chat_id, "❌ 配置服务未初始化")
            return

        parts = args.strip().split(maxsplit=1)

        if not parts:
            # 显示所有配置
            from .card_builder import build_config_card
            card = build_config_card(self.config_service.config)
            await card_service.create_and_send_card(chat_id, card)
        else:
            # 修改配置
            key = parts[0]
            value_str = parts[1] if len(parts) > 1 else None

            if not value_str:
                # 只查询单个配置项
                value = self.config_service.get(key)
                await card_service.send_text(chat_id, f"配置项 `{key}`: `{value}`")
            else:
                # 设置配置项（自动类型推断）
                try:
                    # 尝试解析为 JSON
                    import json
                    value = json.loads(value_str)
                except json.JSONDecodeError:
                    # 解析失败，作为字符串
                    value = value_str

                self.config_service.set(key, value)
                self.config_service.save()
                await card_service.send_text(chat_id, f"✅ 配置已更新: `{key}` = `{value}`")

    async def _cmd_ls(self, user_id: str, chat_id: str, args: str,
                       tree: bool = False, message_id: Optional[str] = None, page: int = 0):
        """查看目录文件结构"""
        all_sessions = list_active_sessions()
        sessions_info = []
        for s in all_sessions:
            pid = s.get("pid")
            cwd = self._get_pid_cwd(pid) if pid else None
            sessions_info.append({"name": s["name"], "cwd": cwd or ""})

        bound_session = self._chat_sessions.get(chat_id)
        if bound_session:
            pid = next((s.get("pid") for s in all_sessions if s["name"] == bound_session), None)
            session_cwd = self._get_pid_cwd(pid) if pid else None
            root = Path(session_cwd) if session_cwd else Path.home()
        else:
            root = Path.home()

        target_arg = args.strip()
        if target_arg:
            target = Path(target_arg).expanduser()
            if not target.is_absolute():
                target = root / target
        else:
            target = root

        target = target.resolve()
        if not target.exists():
            await card_service.send_text(chat_id, f"路径不存在：{target}")
            return

        try:
            if tree:
                entries = self._collect_tree_entries(target)
            else:
                entries = self._collect_ls_entries(target)
        except Exception as e:
            await card_service.send_text(chat_id, f"读取目录失败：{e}")
            return

        session_groups = {
            self._chat_bindings[cid]: cid
            for cid in self._group_chat_ids
            if cid in self._chat_bindings
        }
        card = build_dir_card(target, entries, sessions_info, tree=tree, session_groups=session_groups, page=page)
        await self._send_or_update_card(chat_id, card, message_id)

    async def _cmd_new_group(self, user_id: str, chat_id: str, args: str,
                              message_id: Optional[str] = None):
        """创建专属群聊并绑定 Claude 会话"""
        sessions = list_active_sessions()
        if not sessions:
            await card_service.send_text(chat_id, "当前没有可用会话，请先 /start 启动")
            return

        session_arg = args.strip()

        def _render_session_choices(prefix: str = "") -> str:
            home = str(Path.home())
            lines = []
            if prefix:
                lines.append(prefix)
                lines.append("")
            lines.append("请选择要创建群聊的会话（ID=会话名）：")
            for i, s in enumerate(sessions, 1):
                name = s.get("name", "")
                start_time = s.get("start_time", "")
                cwd = (s.get("cwd", "") or "").replace(home, "~")
                lines.append(f"{i}. `{name}`")
                meta = []
                if start_time:
                    meta.append(f"启动：{start_time}")
                if cwd:
                    meta.append(cwd)
                if meta:
                    lines.append("   " + " ｜ ".join(meta))
            lines.append("")
            lines.append("用法：`/new-group <会话ID>` 或 `/new-group <序号>`")
            return "\n".join(lines)

        if not session_arg:
            await card_service.send_text(chat_id, _render_session_choices())
            return

        # 支持序号选择，降低手输会话名出错概率
        if session_arg.isdigit():
            idx = int(session_arg)
            if idx < 1 or idx > len(sessions):
                await card_service.send_text(
                    chat_id,
                    _render_session_choices(f"序号 {idx} 超出范围"),
                )
                return
            session_name = sessions[idx - 1].get("name", "")
        else:
            session_name = session_arg

        if not any(s["name"] == session_name for s in sessions):
            await card_service.send_text(
                chat_id,
                _render_session_choices(f"会话 '{session_name}' 不存在"),
            )
            return

        # A 方案：已存在群聊时不重复创建，直接进入已有群聊
        existing_group_chat_id = next(
            (
                cid for cid in sorted(self._group_chat_ids)
                if self._chat_bindings.get(cid) == session_name
            ),
            None,
        )
        if existing_group_chat_id:
            # 已有群聊：刷新为活跃状态并返回直达链接
            self._set_group_status(existing_group_chat_id, session_name, 'active')
            await card_service.send_text(
                chat_id,
                "该会话已有专属群聊，点击进入：\n"
                f"https://applink.feishu.cn/client/chat/open?openChatId={existing_group_chat_id}"
            )
            if message_id:
                await self._cmd_list(user_id, chat_id, message_id=message_id)
            return

        session = next((s for s in sessions if s["name"] == session_name), None)
        pid = session.get("pid") if session else None
        cwd = self._get_pid_cwd(pid) if pid else None
        dir_label = cwd.rstrip("/").rsplit("/", 1)[-1] if cwd else session_name

        from . import config
        try:
            import json as _json
            import urllib.request
            import datetime
            _time_str = datetime.datetime.now().strftime("%H-%M")
            group_name = f"{config.GROUP_NAME_PREFIX}{dir_label}-{_time_str}"
            req_body = {
                "name": group_name,
                "description": f"Remote Claude 专属群 - 会话 {session_name}",
                "user_id_list": [user_id],
            }
            token_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.larkoffice.com/open-apis/auth/v3/tenant_access_token/internal",
                    data=_json.dumps({"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                ), timeout=10
            )
            token_data = _json.loads(token_resp.read())
            token = token_data["tenant_access_token"]

            create_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.larkoffice.com/open-apis/im/v1/chats?user_id_type=open_id",
                    data=_json.dumps(req_body).encode(),
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
                    method="POST"
                ), timeout=10
            )
            create_data = _json.loads(create_resp.read())

            if create_data.get("code") != 0:
                await card_service.send_text(chat_id, f"创建群失败：{create_data.get('msg')}")
                return

            group_chat_id = create_data["data"]["chat_id"]
            self._chat_bindings[group_chat_id] = session_name
            self._save_chat_bindings()
            self._group_chat_ids.add(group_chat_id)
            self._save_group_chat_ids()
            self._set_group_status(group_chat_id, session_name, 'offline', reason="群聊新建完成，等待会话接管")
            # 立即 attach，让新群即刻开始接收 Claude 输出
            await self._attach(group_chat_id, session_name, user_id=user_id)
            # 新建群后立即推送入口卡，避免用户进群后无反馈
            await self._show_group_entry_card(user_id, group_chat_id)

            # 刷新会话列表卡片，使"创建群聊"按钮变为"进入群聊"
            await self._cmd_list(user_id, chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"创建群失败: {e}")
            await card_service.send_text(chat_id, f"创建群失败：{e}")

    async def _disband_group_via_api(self, group_chat_id: str) -> tuple:
        """调用飞书 API 解散群聊，返回 (ok: bool, err_msg: str)"""
        import json as _json
        import urllib.request
        import urllib.error
        from . import config
        try:
            token_resp = urllib.request.urlopen(
                urllib.request.Request(
                    "https://open.larkoffice.com/open-apis/auth/v3/tenant_access_token/internal",
                    data=_json.dumps({"app_id": config.FEISHU_APP_ID, "app_secret": config.FEISHU_APP_SECRET}).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST"
                ), timeout=10
            )
            token = _json.loads(token_resp.read())["tenant_access_token"]
            try:
                disband_resp = urllib.request.urlopen(
                    urllib.request.Request(
                        f"https://open.larkoffice.com/open-apis/im/v1/chats/{group_chat_id}",
                        headers={"Authorization": f"Bearer {token}"},
                        method="DELETE"
                    ), timeout=10
                )
                disband_data = _json.loads(disband_resp.read())
                if disband_data.get("code") == 0:
                    return True, ""
                return False, disband_data.get("msg", "")
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="replace")
                try:
                    err_data = _json.loads(err_body)
                    return False, f"code={err_data.get('code')} {err_data.get('msg', '')}"
                except Exception:
                    return False, f"HTTP {e.code}"
        except Exception as e:
            return False, str(e)

    async def _disband_groups_for_session(self, session_name: str, source: str = ""):
        """解散绑定到指定会话的所有专属群聊"""
        disbanded = []
        for cid in list(self._group_chat_ids):
            if self._chat_bindings.get(cid) == session_name:
                log_prefix = f"[{source}] " if source else ""
                logger.info(f"{log_prefix}自动解散群聊: chat_id={cid[:8]}..., session={session_name}")
                # 先清理本地状态（防止并发协程重入时重复处理）
                self._group_chat_ids.discard(cid)
                self._chat_bindings.pop(cid, None)
                disbanded.append(cid)
                # 停止轮询 + 断开 bridge
                self._stop_poller(cid)
                bridge = self._bridges.pop(cid, None)
                if bridge:
                    await bridge.disconnect()
                self._chat_sessions.pop(cid, None)
                self._detached_slices.pop(cid, None)
                # 调用飞书 API 解散
                ok, err = await self._disband_group_via_api(cid)
                if not ok:
                    logger.warning(f"{log_prefix}解散群 {cid[:8]}... API 失败: {err}")
        if disbanded:
            for cid in disbanded:
                self._group_meta.pop(cid, None)
            self._save_group_meta()
            self._save_chat_bindings()
            self._save_group_chat_ids()

    async def _cmd_disband_group(self, user_id: str, chat_id: str, session_name: str,
                                  message_id: Optional[str] = None):
        """解散与指定会话绑定的专属群聊"""
        group_chat_id = next(
            (cid for cid, sname in self._chat_bindings.items() if sname == session_name and cid.startswith("oc_")),
            None
        )
        if not group_chat_id:
            await card_service.send_text(chat_id, f"会话 '{session_name}' 没有绑定群聊")
            return

        try:
            feishu_ok, feishu_msg = await self._disband_group_via_api(group_chat_id)
            if not feishu_ok:
                logger.error(f"解散群 API 失败: {feishu_msg}")

            # 无论 Feishu delete 是否成功，都清理本地绑定
            self._group_chat_ids.discard(group_chat_id)
            self._save_group_chat_ids()
            self._remove_binding_by_chat(group_chat_id, force=True)
            self._remove_group_meta(group_chat_id)
            await self._detach(group_chat_id)

            if not feishu_ok:
                await card_service.send_text(
                    chat_id,
                    f"⚠️ Feishu 群解散失败（{feishu_msg}），已解除本地绑定。如需彻底解散请在飞书群内手动操作"
                )
            await self._cmd_list(user_id, chat_id, message_id=message_id)
        except Exception as e:
            logger.error(f"解散群失败: {e}")
            await card_service.send_text(chat_id, f"解散群失败：{e}")

    # ── 消息转发 ─────────────────────────────────────────────────────────────

    async def _forward_to_claude(self, user_id: str, chat_id: str, text: str):
        """转发消息给 Claude

        卡片模式：冻结当前卡片，回复出现在新卡片
        文本模式：直接发送输入，由 TextMessagePoller 发送回复
        """
        bridge = self._bridges.get(chat_id)

        if not bridge or not bridge.running:
            # 尝试从持久化绑定自动恢复
            saved_session = self._chat_bindings.get(chat_id)
            if saved_session:
                logger.info(f"自动恢复绑定: chat_id={chat_id[:8]}..., session={saved_session}")
                ok = await self._attach(chat_id, saved_session, user_id=user_id)
                if not ok:
                    if chat_id in self._group_chat_ids:
                        # 生命周期管理：保留群聊，标记为离线，避免自动解散
                        self._set_group_status(chat_id, saved_session, 'offline', reason=f"会话 {saved_session} 当前离线")
                        await self._cmd_group_show_recovery(
                            user_id,
                            chat_id,
                            reason_text=f"会话 {saved_session} 当前离线，请先重连或接管。",
                        )
                    else:
                        self._remove_binding_by_chat(chat_id, force=True)
                        await card_service.send_text(
                            chat_id, f"会话 '{saved_session}' 当前离线，请先 /start 后再重试"
                        )
                    return
                bridge = self._bridges.get(chat_id)
            else:
                if chat_id in self._group_chat_ids:
                    await self._cmd_group_show_recovery(
                        user_id,
                        chat_id,
                        reason_text="当前群未绑定可用会话，请选择现有会话接管。",
                    )
                else:
                    await self._show_unconnected_menu_entry(
                        user_id,
                        chat_id,
                        reason_text="未连接到任何会话，已为你打开能力菜单。"
                    )
                return

        if not bridge:
            return

        # 卡片模式：冻结当前卡片，让回复出现在新卡片中
        if self._is_card_mode and self._poller:
            freeze_result = await self._poller.freeze_current_card(chat_id)
            logger.info(f"[_forward_to_claude] freeze_current_card 返回: {freeze_result}")

        # 发送前记录快照中的 UserInput 计数，用于投递确认
        before_snapshot = self._read_snapshot(chat_id)
        before_user_inputs = self._count_user_input_blocks(before_snapshot)

        # 发送用户输入（加超时，避免“已断开但无反馈”）
        try:
            success = await asyncio.wait_for(bridge.send_input(text), timeout=4.0)
        except asyncio.TimeoutError:
            logger.warning(f"发送输入超时: chat_id={chat_id[:8]}..., session={self._chat_sessions.get(chat_id, '-')}")
            await card_service.send_text(
                chat_id,
                "⚠️ 消息投递超时：连接可能已断开但未及时上报。\n"
                "建议点击“📋 菜单”后执行“🔗 恢复会话”或重新 /attach 再重试。"
            )
            return
        except Exception as e:
            logger.error(f"发送输入异常: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                "⚠️ 消息投递失败：发送过程中发生异常。\n"
                "建议点击“📋 菜单”后执行“🔗 恢复会话”或重新 /attach 再重试。"
            )
            return

        if success:
            self._kick_poller(chat_id)
            reflected = await self._wait_user_input_reflected(chat_id, before_user_inputs)

            if reflected:
                # 文本模式：给用户即时反馈，确认消息已送达
                if not self._is_card_mode:
                    await card_service.send_interactive_card(chat_id, _build_pending_card(text))
            else:
                await card_service.send_text(
                    chat_id,
                    "⚠️ 当前会话状态异常：显示已连接，但本次输入未在终端侧确认落盘。\n"
                    "建议点击“📋 菜单”后执行“🔗 恢复会话”或重新 /attach 再重试。"
                )

            if chat_id in self._group_chat_ids:
                asyncio.create_task(self._maybe_emit_group_summary_after_delay(chat_id))
        else:
            await card_service.send_text(
                chat_id,
                "⚠️ 消息投递失败：连接可能已断开。\n"
                "建议点击“📋 菜单”后执行“🔗 恢复会话”或重新 /attach 再重试。"
            )

    # ── 选项处理 ─────────────────────────────────────────────────────────────

    async def handle_option_select(self, user_id: str, chat_id: str, option_value: str, option_total: int = 0, *, needs_input: bool = False):
        """闭环选项选择：箭头键导航 + 共享内存验证

        发箭头键导航到目标选项，每步从共享内存读取 selected_value 确认是否到位，
        到位后发 Enter 确认。避免数字键在溢出选项上无效的问题。
        """
        logger.info(f"处理选项选择: user={user_id[:8]}..., option={option_value}, total={option_total}")
        _track_stats('lark', 'option_select',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=option_value)

        bridge = self._bridges.get(chat_id)
        if not bridge or not bridge.running:
            await card_service.send_text(chat_id, "未连接到任何会话，请先使用 /attach <会话名> 连接")
            return

        target = option_value  # 目标选项 value（如 "2"）
        max_steps = max(option_total, 10) if option_total > 0 else 10

        # 记录初始 option_block 的 block_id，防止跨选项交互误操作
        initial_snapshot = self._read_snapshot(chat_id)
        if not initial_snapshot:
            return
        initial_ob = initial_snapshot.get('option_block')
        if not initial_ob:
            return
        initial_block_id = initial_ob.get('block_id', '')

        for step in range(max_steps):
            # 1. 读取当前选中项
            snapshot = self._read_snapshot(chat_id)
            if not snapshot:
                break
            ob = snapshot.get('option_block')
            if not ob:
                break  # option_block 已消失（CLI 已进入下一状态）

            # 检查 block_id 一致性
            if initial_block_id and ob.get('block_id', '') != initial_block_id:
                logger.warning(f"option_block 已切换，中止选项选择")
                break

            current = ob.get('selected_value', '')

            # 闪烁帧重试：❯ 光标字符会时隐时现，selected_value 为空时短暂重试
            if not current:
                for _retry in range(5):  # 最多重试 5 次，共 500ms
                    await asyncio.sleep(0.1)
                    snap = self._read_snapshot(chat_id)
                    if not snap:
                        break
                    retry_ob = snap.get('option_block')
                    if not retry_ob:
                        break
                    current = retry_ob.get('selected_value', '')
                    if current:
                        break

            # 2. 已到位 → 发 Enter（自由输入选项只导航不发 Enter）
            if current == target:
                if needs_input:
                    logger.info(f"自由输入选项已到位: target={target}，不发送 Enter")
                    self._kick_poller(chat_id)
                    return
                logger.info(f"选项已到位: current={current} == target={target}，发送 Enter")
                success = await bridge.send_raw(b"\r")
                if success:
                    self._kick_poller(chat_id)
                else:
                    await card_service.send_text(chat_id, "发送选择失败")
                return

            # 3. 未到位 → 发箭头键
            if current and target:
                try:
                    if int(current) < int(target):
                        logger.info(f"步骤{step}: current={current} < target={target}，发送 ↓")
                        await bridge.send_raw(b"\x1b[B")  # ↓
                    else:
                        logger.info(f"步骤{step}: current={current} > target={target}，发送 ↑")
                        await bridge.send_raw(b"\x1b[A")  # ↑
                except ValueError:
                    logger.warning(f"步骤{step}: 无法比较 current={current!r} 和 target={target!r}，发送 ↓")
                    await bridge.send_raw(b"\x1b[B")
            else:
                # selected_value 重试后仍为空（真正的初始状态），默认向下
                logger.info(f"步骤{step}: selected_value 重试后仍为空，发送 ↓")
                await bridge.send_raw(b"\x1b[B")

            # 4. 等待共享内存更新（轮询直到 selected_value 变为另一个非空值或超时）
            old_selected = current
            deadline = time.time() + 2.0  # 单步超时 2s
            while time.time() < deadline:
                await asyncio.sleep(0.1)  # 100ms 轮询
                snap = self._read_snapshot(chat_id)
                if not snap:
                    break
                new_ob = snap.get('option_block')
                if not new_ob:
                    break  # option_block 消失，退出
                if initial_block_id and new_ob.get('block_id', '') != initial_block_id:
                    break  # block_id 变了，外层会处理
                new_selected = new_ob.get('selected_value', '')
                # 忽略闪烁帧：只有变为另一个非空值才视为真正变化
                if new_selected and new_selected != old_selected:
                    break

        # 超过 max_steps 仍未到位，记录警告
        logger.warning(f"选项选择超步数: target={target}, steps={max_steps}")

    def has_active_option(self, chat_id: str) -> bool:
        """检查当前是否有活跃的 option_block"""
        snapshot = self._read_snapshot(chat_id)
        if not snapshot:
            return False
        return bool(snapshot.get('option_block'))

    async def handle_option_input(self, user_id: str, chat_id: str, text: str):
        """处理用户在输入框输入的选项文本

        当有活跃 option_block 时：
        - 纯数字（如 "1" 或 "2"）→ 导航到该选项并 Enter 确认
        - 其他文本 → 导航到 "Other" 选项，输入文本后 Enter
        """
        logger.info(f"处理选项输入: user={user_id[:8]}..., text={text!r}")

        snapshot = self._read_snapshot(chat_id)
        if not snapshot:
            return
        ob = snapshot.get('option_block')
        if not ob:
            return

        options = ob.get('options', [])
        if not options:
            return

        text = text.strip()

        # 纯数字 → 直接选择对应选项
        if text.isdigit():
            idx = int(text)
            if 1 <= idx <= len(options):
                opt = options[idx - 1]
                value = opt.get('value', str(idx))
                needs_input = opt.get('needs_input', False)
                await self.handle_option_select(
                    user_id, chat_id, value, len(options),
                    needs_input=needs_input
                )
                return

        # 非数字文本 → 找 "Other" 选项（needs_input=True），导航 + 输入文本
        other_opt = None
        for opt in options:
            if opt.get('needs_input', False):
                other_opt = opt
                break

        if other_opt:
            value = other_opt.get('value', '')
            # 先导航到 Other 选项
            await self.handle_option_select(
                user_id, chat_id, value, len(options), needs_input=True
            )
            # 等待导航完成
            await asyncio.sleep(0.3)
            # 输入文本
            bridge = self._bridges.get(chat_id)
            if bridge and bridge.running:
                await bridge.send_input(text)
                await asyncio.sleep(0.1)
                # 发 Enter 提交
                await bridge.send_raw(b"\r")
                self._kick_poller(chat_id)
        else:
            # 没有 Other 选项，直接把文本当作输入发给 Claude
            await self._forward_to_claude(user_id, chat_id, text)

    # ── 快捷键发送 ─────────────────────────────────────────────────────────────

    async def send_raw_key(self, user_id: str, chat_id: str, key_name: str):
        """发送原始控制键到 Claude CLI"""
        _track_stats('lark', 'raw_key',
                     session_name=self._chat_sessions.get(chat_id, ''),
                     chat_id=chat_id, detail=key_name)
        KEY_MAP = {
            "up": b"\x1b[A",         # ↑ 上箭头
            "down": b"\x1b[B",       # ↓ 下箭头
            "enter": b"\r",          # Enter
            "ctrl_o": b"\x0f",       # Ctrl+O
            "shift_tab": b"\x1b[Z",  # Shift+Tab
            "esc": b"\x1b",          # ESC
        }
        raw = KEY_MAP.get(key_name)
        if not raw:
            logger.warning(f"未知快捷键: {key_name}")
            return

        bridge = self._bridges.get(chat_id)
        if not bridge or not bridge.running:
            logger.warning(f"send_raw_key: chat_id={chat_id[:8]}... 未连接会话")
            return

        success = await bridge.send_raw(raw)
        if success:
            logger.info(f"已发送快捷键 {key_name} 到 Claude")
            self._kick_poller(chat_id)
        else:
            logger.warning(f"发送快捷键 {key_name} 失败")

    # ── 辅助方法 ─────────────────────────────────────────────────────────────

    async def _send_or_update_card(
        self, chat_id: str, card: dict, message_id: Optional[str] = None
    ):
        """有 message_id 时就地更新原卡片，否则发新消息；更新失败时降级为发新卡片"""
        if message_id:
            success = await card_service.update_card_by_message_id(message_id, card)
            if success:
                return
        await card_service.create_and_send_card(chat_id, card)

    @staticmethod
    def _collect_ls_entries(target) -> list:
        """获取一级目录内容（隐藏文件除外，目录优先）"""
        entries = []
        try:
            items = sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            for item in items:
                if item.name.startswith('.'):
                    continue
                entries.append({
                    "name": item.name,
                    "full_path": str(item),
                    "is_dir": item.is_dir(),
                    "depth": 0,
                })
        except PermissionError:
            pass
        return entries

    @staticmethod
    def _collect_tree_entries(target, max_depth: int = 2, max_items: int = 60) -> list:
        """获取树状目录内容"""
        entries = []

        def _walk(path, depth: int):
            if depth > max_depth or len(entries) >= max_items:
                return
            try:
                for item in sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
                    if len(entries) >= max_items:
                        break
                    if item.name.startswith('.'):
                        continue
                    entries.append({
                        "name": item.name,
                        "full_path": str(item),
                        "is_dir": item.is_dir(),
                        "depth": depth,
                    })
                    if item.is_dir() and depth < max_depth:
                        _walk(item, depth + 1)
            except PermissionError:
                pass

        _walk(target, 0)
        return entries

    async def disconnect_all_for_shutdown(self) -> None:
        """lark stop 时清理所有活跃流式卡片（更新为已断开状态，仅卡片模式）"""
        chat_ids = list(self._bridges.keys())
        for chat_id in chat_ids:
            session_name = self._chat_sessions.get(chat_id, "")
            active_slice = self._stop_poller(chat_id)
            if active_slice and session_name and self._is_card_mode:
                await self._update_card_disconnected(chat_id, session_name, active_slice)

    @staticmethod
    def _get_pid_cwd(pid: int) -> Optional[str]:
        """获取进程的工作目录（macOS/Linux）"""
        try:
            result = subprocess.run(
                ["lsof", "-p", str(pid), "-a", "-d", "cwd", "-F", "n"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                if line.startswith("n"):
                    return line[1:].strip()
        except Exception:
            pass
        return None

    # ── 监听管理命令 ───────────────────────────────────────────────────────────

    async def _cmd_monitor(self, user_id: str, chat_id: str, args: str):
        """处理 /monitor 命令路由"""
        parts = args.split(maxsplit=2) if args else []
        subcommand = parts[0].lower() if parts else ""

        if subcommand == "add":
            await self._cmd_monitor_add(user_id, chat_id)
        elif subcommand == "list":
            await self._cmd_monitor_list(user_id, chat_id)
        elif subcommand == "remove":
            index_str = parts[1] if len(parts) > 1 else ""
            await self._cmd_monitor_remove(user_id, chat_id, index_str)
        elif subcommand == "config":
            # 如果有额外参数，处理配置更新
            if len(parts) > 1:
                config_args = " ".join(parts[1:])
                await self._cmd_monitor_config_update(user_id, chat_id, config_args)
            else:
                # 显示配置卡片
                await self._cmd_monitor_config(user_id, chat_id)
        else:
            # 显示帮助信息
            await card_service.send_text(
                chat_id,
                "📋 监听管理命令\n\n"
                "• `/monitor add` - 添加当前群聊到监听列表\n"
                "• `/monitor list` - 查看监听列表\n"
                "• `/monitor remove <序号>` - 删除指定群聊\n"
                "• `/monitor config` - 查看配置\n"
                "• `/monitor config interval <分钟>` - 设置检查间隔\n"
                "• `/monitor config quiet <开始> <结束>` - 设置静默时段"
            )

    async def _cmd_monitor_add(self, user_id: str, chat_id: str):
        """添加当前群聊到监听列表"""
        try:
            # 1. 获取群聊信息
            if not self.user_api or not self.oauth_service:
                await card_service.send_text(
                    chat_id,
                    "❌ 监听功能需要用户授权，请先执行 `/oauth` 完成授权。"
                )
                return

            # 获取用户 token
            token_data = self.oauth_service.get_user_token(user_id)
            if not token_data:
                await card_service.send_text(
                    chat_id,
                    "❌ 未找到授权信息，请先执行 `/oauth` 完成授权。"
                )
                return

            # 获取群聊信息
            try:
                from .user_api import TokenExpiredError
                chat_info = await self.user_api.get_chat_info(
                    user_id,
                    chat_id
                )
                chat_name = chat_info.get('name', '未知群聊')
            except TokenExpiredError as e:
                logger.warning(f"用户 token 未授权或已过期: {e}")
                await card_service.send_text(
                    chat_id,
                    "🔐 **需要授权才能使用监听功能**\n\n"
                    "监听功能需要以您的身份读取群聊消息，请先完成授权：\n\n"
                    "**1️⃣ 获取授权链接**\n"
                    "在本对话中输入：`/oauth`\n\n"
                    "**2️⃣ 完成授权**\n"
                    "点击返回的链接，同意授权后复制授权码\n\n"
                    "**3️⃣ 提交授权码**\n"
                    "输入：`/oauth <授权码>`\n\n"
                    "授权完成后，再次执行 `/monitor add` 即可添加监听。"
                )
                return
            except Exception as e:
                logger.error(f"获取群聊信息失败: {e}")
                await card_service.send_text(
                    chat_id,
                    f"❌ 获取群聊信息失败: {str(e)}\n\n"
                    f"请确认机器人在该群聊中有权读取信息。\n"
                    f"如果授权已过期，请执行 `/oauth` 重新授权。"
                )
                return

            # 2. 添加到配置
            chat_data = {
                'chat_id': chat_id,
                'chat_name': chat_name,
                'chat_type': 'group',
                'added_at': int(time.time()),
                'last_check_time': 0
            }

            success = self.monitor_config.add_chat(user_id, chat_data)

            # 3. 发送确认消息
            if success:
                await card_service.send_text(
                    chat_id,
                    f"✅ 已添加群聊「{chat_name}」到监听列表\n\n"
                    f"系统将定时检查该群的新消息并推送摘要给您。\n"
                    f"使用 `/monitor list` 查看监听列表。"
                )
                logger.info(f"[监听] 用户 {user_id[:8]}... 添加群聊: {chat_name} ({chat_id[:8]}...)")
            else:
                await card_service.send_text(
                    chat_id,
                    f"ℹ️ 群聊「{chat_name}」已在监听列表中"
                )

        except Exception as e:
            logger.error(f"添加监听群聊失败: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                f"❌ 添加监听失败: {str(e)}"
            )

    async def _cmd_monitor_list(self, user_id: str, chat_id: str):
        """查看监听列表"""
        try:
            config = self.monitor_config.get_user_config(user_id)
            monitored_chats = config.get('monitored_chats', [])

            # 构建卡片
            card = build_monitor_list_card(monitored_chats)
            await card_service.create_and_send_card(chat_id, card)

        except Exception as e:
            logger.error(f"查看监听列表失败: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                f"❌ 查看监听列表失败: {str(e)}"
            )

    async def _cmd_monitor_remove(self, user_id: str, chat_id: str, index_str: str):
        """删除监听的群聊"""
        try:
            # 验证序号
            if not index_str or not index_str.isdigit():
                await card_service.send_text(
                    chat_id,
                    "❌ 请指定要删除的群聊序号\n\n"
                    "用法: `/monitor remove <序号>`\n"
                    "例如: `/monitor remove 1`"
                )
                return

            index = int(index_str)

            # 获取当前配置
            config = self.monitor_config.get_user_config(user_id)
            monitored_chats = config.get('monitored_chats', [])

            # 验证序号范围
            if index < 1 or index > len(monitored_chats):
                await card_service.send_text(
                    chat_id,
                    f"❌ 序号无效: {index}\n\n"
                    f"请输入 1-{len(monitored_chats)} 之间的序号"
                )
                return

            # 获取要删除的群聊信息
            chat_to_remove = monitored_chats[index - 1]
            chat_name = chat_to_remove.get('chat_name', '未知群聊')

            # 删除
            success = self.monitor_config.remove_chat(user_id, index)

            if success:
                await card_service.send_text(
                    chat_id,
                    f"✅ 已从监听列表中移除「{chat_name}」\n\n"
                    f"使用 `/monitor list` 查看当前列表。"
                )
                logger.info(f"[监听] 用户 {user_id[:8]}... 移除群聊: {chat_name}")
            else:
                await card_service.send_text(
                    chat_id,
                    f"❌ 删除失败: 群聊序号 {index} 不存在"
                )

        except Exception as e:
            logger.error(f"删除监听群聊失败: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                f"❌ 删除监听失败: {str(e)}"
            )

    async def _cmd_monitor_config(self, user_id: str, chat_id: str):
        """显示监听配置"""
        try:
            config = self.monitor_config.get_user_config(user_id)

            # 导入 build_monitor_config_card
            from .card_builder import build_monitor_config_card
            card = build_monitor_config_card(config)
            await card_service.create_and_send_card(chat_id, card)

        except Exception as e:
            logger.error(f"查看监听配置失败: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                f"❌ 查看配置失败: {str(e)}"
            )

    async def _cmd_monitor_config_update(self, user_id: str, chat_id: str, args: str):
        """更新监听配置"""
        try:
            parts = args.split()
            if not parts:
                await card_service.send_text(
                    chat_id,
                    "❌ 请指定配置项\n\n"
                    "用法:\n"
                    "• `/monitor config interval <分钟>` - 设置检查间隔（5/10/15/30）\n"
                    "• `/monitor config quiet <开始> <结束>` - 设置静默时段\n"
                    "• `/monitor config quiet off` - 关闭静默时段"
                )
                return

            config_type = parts[0].lower()

            if config_type == "interval":
                # 设置检查间隔
                if len(parts) < 2:
                    await card_service.send_text(
                        chat_id,
                        "❌ 请指定检查间隔（分钟）\n\n"
                        "用法: `/monitor config interval <分钟>`\n"
                        "支持: 5, 10, 15, 30"
                    )
                    return

                try:
                    interval = int(parts[1])
                    success = self.monitor_config.update_check_interval(user_id, interval)

                    if success:
                        await card_service.send_text(
                            chat_id,
                            f"✅ 检查间隔已更新为 {interval} 分钟"
                        )
                        logger.info(f"[监听配置] 用户 {user_id[:8]}... 更新间隔: {interval}分钟")
                    else:
                        await card_service.send_text(
                            chat_id,
                            f"❌ 无效的间隔值: {interval}\n\n"
                            f"支持的值: 5, 10, 15, 30"
                        )
                except ValueError:
                    await card_service.send_text(
                        chat_id,
                        "❌ 间隔值必须为数字"
                    )

            elif config_type == "quiet":
                # 设置静默时段
                if len(parts) < 2:
                    await card_service.send_text(
                        chat_id,
                        "❌ 请指定静默时段\n\n"
                        "用法:\n"
                        "• `/monitor config quiet <开始> <结束>` - 设置时段（如 22:00 08:00）\n"
                        "• `/monitor config quiet off` - 关闭静默时段"
                    )
                    return

                if parts[1].lower() == "off":
                    # 关闭静默时段
                    settings = {"enabled": False}
                    success = self.monitor_config.update_quiet_hours(user_id, settings)

                    if success:
                        await card_service.send_text(
                            chat_id,
                            "✅ 静默时段已关闭"
                        )
                        logger.info(f"[监听配置] 用户 {user_id[:8]}... 关闭静默时段")
                    else:
                        await card_service.send_text(
                            chat_id,
                            "❌ 更新配置失败"
                        )
                else:
                    # 设置静默时段
                    if len(parts) < 3:
                        await card_service.send_text(
                            chat_id,
                            "❌ 请同时指定开始和结束时间\n\n"
                            "用法: `/monitor config quiet <开始> <结束>`\n"
                            "示例: `/monitor config quiet 22:00 08:00`"
                        )
                        return

                    start_time = parts[1]
                    end_time = parts[2]

                    settings = {
                        "enabled": True,
                        "start": start_time,
                        "end": end_time
                    }

                    success = self.monitor_config.update_quiet_hours(user_id, settings)

                    if success:
                        await card_service.send_text(
                            chat_id,
                            f"✅ 静默时段已更新\n\n"
                            f"• 开始: {start_time}\n"
                            f"• 结束: {end_time}\n\n"
                            f"在此时段内将不会推送消息摘要"
                        )
                        logger.info(
                            f"[监听配置] 用户 {user_id[:8]}... "
                            f"更新静默时段: {start_time} - {end_time}"
                        )
                    else:
                        await card_service.send_text(
                            chat_id,
                            "❌ 更新配置失败\n\n"
                            "请检查时间格式（应为 HH:MM，如 22:00）"
                        )
            else:
                await card_service.send_text(
                    chat_id,
                    f"❌ 未知的配置项: {config_type}\n\n"
                    f"支持的配置项: interval, quiet"
                )

        except Exception as e:
            logger.error(f"更新监听配置失败: {e}", exc_info=True)
            await card_service.send_text(
                chat_id,
                f"❌ 更新配置失败: {str(e)}"
            )


# 全局处理器实例
handler = LarkHandler()
