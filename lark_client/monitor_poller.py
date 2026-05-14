"""
监听轮询器 - 定时检查监听群聊的新消息

定时检查用户配置的监听群聊，筛选相关消息，调用 AI 分析并推送摘要。
"""

import asyncio
import logging
import time
from datetime import datetime, time as dt_time
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("lark_client.monitor_poller")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class MonitorPoller:
    """监听轮询器 - 定时检查监听群聊的新消息"""

    def __init__(
        self,
        monitor_config,
        user_api,
        oauth_service,
        card_service,
        message_analyzer=None
    ):
        """
        初始化轮询器

        Args:
            monitor_config: MonitorConfigService 实例
            user_api: LarkUserApi 实例
            oauth_service: OAuthService 实例
            card_service: CardService 实例
            message_analyzer: MessageAnalyzer 实例（可选）
        """
        self.monitor_config = monitor_config
        self.user_api = user_api
        self.oauth_service = oauth_service
        self.card_service = card_service
        self.message_analyzer = message_analyzer

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # 用户私聊 chat_id 缓存（open_id → chat_id）
        self._user_chat_map: Dict[str, str] = {}

    def start(self):
        """启动定时检查"""
        try:
            loop = asyncio.get_running_loop()
            self._running = True
            self._task = loop.create_task(self._run_check_loop())
            logger.info("[MonitorPoller] 定时监听已启动")
        except RuntimeError:
            logger.error("[MonitorPoller] 无法启动: 事件循环未运行")

    def stop(self):
        """停止定时检查"""
        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("[MonitorPoller] 定时监听已停止")

    async def _run_check_loop(self):
        """定时检查主循环"""
        while self._running:
            try:
                # 等待下一次检查
                await asyncio.sleep(60)  # 每分钟检查一次，但实际检查由用户配置控制

                # 获取所有配置了监听的用户
                all_users = self.monitor_config.get_all_users()
                if not all_users:
                    continue

                for open_id in all_users:
                    try:
                        config = self.monitor_config.get_user_config(open_id)

                        # 检查是否到达检查时间
                        check_interval = config.get('check_interval_minutes', 10)
                        now = int(time.time())

                        # 检查静默时段
                        if self._is_quiet_hours(config):
                            logger.debug(f"[MonitorPoller] 用户 {open_id[:8]}... 在静默时段，跳过检查")
                            continue

                        # 检查所有监听的群聊
                        await self._check_user_chats(open_id, config)

                    except Exception as e:
                        logger.error(f"[MonitorPoller] 检查用户 {open_id[:8]}... 失败: {e}", exc_info=True)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[MonitorPoller] 主循环异常: {e}", exc_info=True)
                await asyncio.sleep(60)

    async def _check_user_chats(self, open_id: str, config: Dict):
        """
        检查用户的所有监听群聊

        Args:
            open_id: 用户 open_id
            config: 用户配置
        """
        monitored_chats = config.get('monitored_chats', [])
        if not monitored_chats:
            return

        # 检查间隔
        check_interval = config.get('check_interval_minutes', 10)
        now = int(time.time())

        # 获取用户 token
        token_data = self.oauth_service.get_user_token(open_id)
        if not token_data:
            logger.debug(f"[MonitorPoller] 用户 {open_id[:8]}... 未授权，跳过")
            return

        access_token = token_data['access_token']

        # 遍历所有监听的群聊
        all_relevant_messages = []

        for chat in monitored_chats:
            chat_id = chat['chat_id']
            chat_name = chat.get('chat_name', '未知群聊')
            last_check_time = chat.get('last_check_time', 0)

            # 检查间隔是否到达
            if now - last_check_time < check_interval * 60:
                continue

            try:
                # 读取新消息
                result = await self.user_api.get_chat_messages(
                    open_id,
                    chat_id,
                    page_size=50,
                    sort_type="ByCreateTimeAsc"  # 按时间升序，获取最新消息
                )
                messages = result.get('items', [])

                # 筛选相关消息
                relevant = self._filter_relevant_messages(messages, open_id)

                if relevant:
                    # 添加群聊名称
                    for msg in relevant:
                        msg['chat_name'] = chat_name

                    all_relevant_messages.extend(relevant)
                    logger.info(
                        f"[MonitorPoller] 用户 {open_id[:8]}... "
                        f"群聊 {chat_name} 有 {len(relevant)} 条相关消息"
                    )

                # 更新检查时间
                chat['last_check_time'] = now

            except Exception as e:
                logger.error(
                    f"[MonitorPoller] 检查群聊 {chat_name} ({chat_id[:8]}...) 失败: {e}"
                )

        # 保存配置（更新 last_check_time）
        self.monitor_config.save_user_config(open_id, config)

        # 如果有相关消息，调用 AI 分析并推送
        if all_relevant_messages and self.message_analyzer:
            await self._analyze_and_push(open_id, all_relevant_messages)

    def _filter_relevant_messages(
        self,
        messages: List[dict],
        user_id: str
    ) -> List[dict]:
        """
        筛选与用户相关的消息

        Args:
            messages: 消息列表
            user_id: 用户 ID

        Returns:
            List[dict]: 相关消息列表
        """
        relevant = []

        for msg in messages:
            # 1. @消息: mentions 包含 user_id
            mentions = msg.get('mentions', [])
            if any(m.get('id') == user_id for m in mentions):
                relevant.append(msg)
                continue

            # 2. 回复消息: parent_id 存在
            if msg.get('parent_id'):
                relevant.append(msg)
                continue

            # 3. 话题消息: thread_id 或 root_id 存在
            if msg.get('thread_id') or msg.get('root_id'):
                relevant.append(msg)
                continue

        return relevant

    def _is_quiet_hours(self, config: Dict) -> bool:
        """
        检查当前时间是否在静默时段内

        Args:
            config: 用户配置

        Returns:
            bool: 是否在静默时段
        """
        quiet_hours = config.get('quiet_hours', {})
        if not quiet_hours.get('enabled', False):
            return False

        try:
            now = datetime.now().time()
            start_str = quiet_hours.get('start', '22:00')
            end_str = quiet_hours.get('end', '08:00')

            start = datetime.strptime(start_str, '%H:%M').time()
            end = datetime.strptime(end_str, '%H:%M').time()

            # 处理跨天场景（如 22:00 - 08:00）
            if start < end:
                # 正常时段（如 08:00 - 22:00）
                return start <= now <= end
            else:
                # 跨天时段（如 22:00 - 08:00）
                return now >= start or now <= end

        except Exception as e:
            logger.error(f"[MonitorPoller] 静默时段判断失败: {e}")
            return False

    async def _analyze_and_push(self, open_id: str, messages: List[dict]):
        """
        分析消息并推送摘要

        Args:
            open_id: 用户 ID
            messages: 消息列表
        """
        try:
            # 获取用户名称（从第一条消息的发送者推断，这里简化处理）
            user_name = "用户"

            # 调用 AI 分析
            summary = await self.message_analyzer.analyze_messages(
                messages, open_id, user_name
            )

            # 获取用户私聊 chat_id
            user_chat_id = await self._get_user_chat_id(open_id)
            if not user_chat_id:
                logger.warning(f"[MonitorPoller] 无法获取用户 {open_id[:8]}... 的私聊 chat_id")
                return

            # 构建摘要卡片
            from lark_client.card_builder import build_summary_card
            from datetime import datetime
            time_range = datetime.now().strftime('%H:%M')
            card = build_summary_card(summary, time_range)

            # 发送卡片
            await self.card_service.send_card(user_chat_id, card)

            logger.info(
                f"[MonitorPoller] ✅ 已推送摘要给用户 {open_id[:8]}..., "
                f"消息数: {len(messages)}, 待处理项: {len(summary.get('action_items', []))}"
            )

        except Exception as e:
            logger.error(f"[MonitorPoller] 分析并推送失败: {e}", exc_info=True)

    async def _get_user_chat_id(self, open_id: str) -> Optional[str]:
        """
        获取用户的私聊 chat_id

        Args:
            open_id: 用户 open_id

        Returns:
            Optional[str]: 私聊 chat_id，如果不存在则返回 None
        """
        # 从缓存读取
        if open_id in self._user_chat_map:
            return self._user_chat_map[open_id]

        # 飞书支持直接使用 open_id 作为 p2p 会话的 chat_id
        # 此时不需要额外的 API 调用
        self._user_chat_map[open_id] = open_id
        logger.debug(f"[MonitorPoller] 使用 open_id 作为用户 {open_id[:8]}... 的私聊 chat_id")
        return open_id
