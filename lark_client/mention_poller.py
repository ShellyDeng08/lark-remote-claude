"""
@消息轮询器 - 自动检测和通知未回复的@消息
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Set
from collections import deque

from .config_service import ConfigService
from .user_api import LarkUserApi, TokenExpiredError, UserApiError

logger = logging.getLogger(__name__)


@dataclass
class MentionInfo:
    """@消息/私聊消息信息"""
    message_id: str
    chat_id: str
    chat_name: str
    time: float  # 时间戳（毫秒）
    sender_id: str
    sender_name: str
    text: str
    location: str  # "主消息" 或 "话题回复（根消息: XX-XX XX:XX）" 或 "私聊"
    chat_link: str  # 飞书群聊链接
    message_type: str = "mention"  # "mention" 或 "dm" (direct message)


class MentionState:
    """@消息状态跟踪"""

    def __init__(self, state_path: Optional[Path] = None):
        """
        初始化状态跟踪

        Args:
            state_path: 状态文件路径，默认为 ~/.remote-claude/mention_state.json
        """
        if state_path is None:
            state_path = Path.home() / ".remote-claude" / "mention_state.json"
        self.state_path = state_path
        self.last_check_time: float = 0
        self.known_unreplied: Dict[str, MentionInfo] = {}  # message_id -> MentionInfo
        self.last_checked_index: int = 0  # 普通群轮换索引
        self.priority_last_check: float = 0  # 重点群上次检查时间
        self.rotation_last_check: float = 0  # 普通群轮换上次检查时间
        self.total_chats_count: int = 0  # 总群数
        self.activity_cache_time: float = 0  # 活跃度缓存时间
        self.sorted_chat_ids: List[str] = []  # 按活跃度排序的 chat_id 列表

    def load(self) -> None:
        """从文件加载状态"""
        try:
            if self.state_path.exists():
                with open(self.state_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)

                self.last_check_time = data.get("last_check_time", 0)
                self.last_checked_index = data.get("last_checked_index", 0)
                self.priority_last_check = data.get("priority_last_check", 0)
                self.rotation_last_check = data.get("rotation_last_check", 0)
                self.total_chats_count = data.get("total_chats_count", 0)
                self.activity_cache_time = data.get("activity_cache_time", 0)
                self.sorted_chat_ids = data.get("sorted_chat_ids", [])

                # 反序列化 known_unreplied
                unreplied_data = data.get("known_unreplied", {})
                self.known_unreplied = {
                    msg_id: MentionInfo(**info)
                    for msg_id, info in unreplied_data.items()
                }
                logger.info(f"状态已从 {self.state_path} 加载，已知未回复: {len(self.known_unreplied)} 条")
        except Exception as e:
            logger.error(f"加载状态失败: {e}，使用空状态")
            self.known_unreplied = {}

    def save(self) -> None:
        """保存状态到文件"""
        try:
            # 确保目录存在
            self.state_path.parent.mkdir(parents=True, exist_ok=True)

            # 序列化 known_unreplied
            unreplied_data = {
                msg_id: asdict(info)
                for msg_id, info in self.known_unreplied.items()
            }

            data = {
                "last_check_time": self.last_check_time,
                "last_checked_index": self.last_checked_index,
                "priority_last_check": self.priority_last_check,
                "rotation_last_check": self.rotation_last_check,
                "total_chats_count": self.total_chats_count,
                "activity_cache_time": self.activity_cache_time,
                "sorted_chat_ids": self.sorted_chat_ids,
                "known_unreplied": unreplied_data
            }

            with open(self.state_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"状态已保存到 {self.state_path}，已知未回复: {len(self.known_unreplied)} 条")
        except Exception as e:
            logger.error(f"保存状态失败: {e}")

    def get_new_mentions(self, current_unreplied: Dict[str, MentionInfo]) -> List[MentionInfo]:
        """
        对比当前和已知，找出新增的未回复@消息

        Args:
            current_unreplied: 当前检测到的未回复消息

        Returns:
            新增的未回复消息列表
        """
        new_mentions = []
        for msg_id, info in current_unreplied.items():
            if msg_id not in self.known_unreplied:
                new_mentions.append(info)

        # 更新已知未回复列表
        self.known_unreplied = current_unreplied

        return new_mentions


class MentionPoller:
    """@消息自动检查轮询器"""

    def __init__(
        self,
        config_service: ConfigService,
        user_api: LarkUserApi,
        card_sender=None,  # 卡片发送器（可选，用于发送通知）
        oauth_service=None  # OAuth 服务（可选，用于自动检查所有用户）
    ):
        """
        初始化轮询器

        Args:
            config_service: 配置服务
            user_api: 用户 API
            card_sender: 卡片发送器（用于发送通知）
            oauth_service: OAuth 服务（用于自动检查所有用户）
        """
        self.config = config_service
        self.user_api = user_api
        self.oauth_service = oauth_service
        self.card_sender = card_sender
        self.state = MentionState()
        self.state.load()

        self._running = False
        self._task: Optional[asyncio.Task] = None

        # 私聊消息队列（从事件订阅接收）
        self._dm_queue: deque = deque(maxlen=1000)  # 最多保留1000条私聊消息

    def start(self) -> None:
        """启动轮询（如果 auto_check_enabled）"""
        if not self.config.get("mention.auto_check_enabled", True):
            logger.info("自动检查未启用，跳过启动轮询")
            return

        if self._running:
            logger.warning("轮询器已在运行")
            return

        self._running = True
        try:
            # 尝试获取当前事件循环
            loop = asyncio.get_running_loop()
            self._task = loop.create_task(self._run_check_loop())
            logger.info("@消息轮询器已启动")
        except RuntimeError:
            # 如果没有运行的事件循环，标记为未启动
            logger.warning("@消息轮询器: 没有运行的事件循环，将在后续启动")
            self._running = False

    def stop(self) -> None:
        """停止轮询"""
        if not self._running:
            return

        self._running = False
        if self._task:
            self._task.cancel()
        logger.info("@消息轮询器已停止")

    def add_dm_message(self, message_data: dict):
        """
        添加从事件订阅接收到的私聊消息，并立即推送通知

        Args:
            message_data: 私聊消息数据，包含：
                - message_id: 消息 ID
                - chat_id: 会话 ID
                - sender_id: 发送者 ID
                - content: 消息内容
                - create_time: 创建时间（毫秒）
        """
        try:
            self._dm_queue.append(message_data)
            logger.info(f"收到私聊消息并加入队列: message_id={message_data.get('message_id', '')[:10]}...")

            # 立即触发推送通知
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._handle_realtime_dm(message_data))
                else:
                    logger.warning("事件循环未运行，无法触发实时推送")
            except RuntimeError:
                logger.warning("无法获取事件循环，无法触发实时推送")
        except Exception as e:
            logger.error(f"添加私聊消息到队列失败: {e}")

    def restart_with_new_interval(self, interval_minutes: int) -> None:
        """
        使用新的间隔重启轮询

        Args:
            interval_minutes: 新的检查间隔（分钟）
        """
        was_running = self._running
        if was_running:
            self.stop()

        self.config.set("mention.check_interval_minutes", interval_minutes)
        self.config.save()

        if was_running:
            self.start()
        logger.info(f"轮询器已重启，新间隔: {interval_minutes} 分钟")

    async def _run_check_loop(self) -> None:
        """异步轮询循环"""
        logger.info("自动检查循环已启动")

        while self._running:
            try:
                # 执行检查：遍历所有已授权用户
                logger.info("执行自动@消息检查")
                try:
                    # 获取所有已授权用户的 token 文件
                    if hasattr(self, 'oauth_service') and self.oauth_service:
                        all_tokens = self.oauth_service._load_all_tokens()
                        logger.info(f"发现 {len(all_tokens)} 个已授权用户，开始自动检查")

                        # 加载用户私聊 chat_id 映射
                        from pathlib import Path
                        dm_chats_file = Path.home() / ".remote-claude" / "dm_chats.json"
                        user_chat_map = {}
                        try:
                            if dm_chats_file.exists():
                                import json
                                with open(dm_chats_file, 'r', encoding='utf-8') as f:
                                    data = json.load(f)
                                    for item in data.get("dm_chats", []):
                                        user_chat_map[item["user_id"]] = item["chat_id"]
                                logger.info(f"加载了 {len(user_chat_map)} 个用户的私聊 chat_id 映射")
                        except Exception as e:
                            logger.error(f"加载私聊映射失败: {e}")

                        for open_id, token_data in all_tokens.items():
                            try:
                                # 获取用户的私聊 chat_id
                                notify_chat_id = user_chat_map.get(open_id)
                                if notify_chat_id:
                                    logger.info(f"用户 {open_id[:8]}... 自动检查并推送通知")
                                    # 使用 check_all=True 以检查所有群（而不是轮换30个）
                                    mentions = await self.check_now(open_id, notify_chat_id=notify_chat_id, check_all=True)
                                else:
                                    logger.warning(f"用户 {open_id[:8]}... 没有私聊记录，跳过通知")
                                    mentions = await self.check_now(open_id, check_all=True)

                                # 如果检测到@消息，发送推送通知
                                if mentions:
                                    logger.info(f"用户 {open_id[:8]}... 有 {len(mentions)} 条未回复@消息")
                                    if notify_chat_id and self.card_service:
                                        try:
                                            # 发送通知卡片
                                            card_json = self.card_builder.build_mentions_card(mentions)
                                            await self.card_service.send_card(
                                                notify_chat_id,
                                                card_json,
                                                user_access_token=token_data.get("access_token")
                                            )
                                            logger.info(f"✅ 已向用户 {open_id[:8]}... 发送自动检查通知（{len(mentions)} 条@消息）")
                                        except Exception as send_err:
                                            logger.error(f"发送自动检查通知失败: {send_err}")
                            except Exception as user_err:
                                logger.error(f"检查用户 {open_id[:8]}... 失败: {user_err}")
                    else:
                        logger.warning("OAuth 服务未初始化，无法执行自动检查")
                except Exception as check_err:
                    logger.error(f"自动检查失败: {check_err}")

                # 等待下次检查
                interval_minutes = self.config.get("mention.check_interval_minutes", 10)
                interval_seconds = interval_minutes * 60

                if not self._running:
                    break

                logger.info(f"下次检查将在 {interval_minutes} 分钟后执行")
                await asyncio.sleep(interval_seconds)

            except asyncio.CancelledError:
                logger.info("轮询循环已取消")
                break
            except Exception as e:
                logger.error(f"轮询循环异常: {e}", exc_info=True)
                # 发生异常后等待一段时间再继续
                await asyncio.sleep(60)

    async def check_now(
        self,
        user_id: str,
        notify_chat_id: Optional[str] = None,
        check_all: bool = False,
        hours_limit: Optional[int] = None
    ) -> List[MentionInfo]:
        """
        手动触发检查

        Args:
            user_id: 用户 open_id
            notify_chat_id: 通知卡片发送到的 chat_id（可选）
            check_all: 是否全量检查（忽略50个群的限制，并检查最近24小时而非增量）
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            所有未回复的@消息列表
        """
        time_hint = f", hours_limit={hours_limit}" if hours_limit else ""
        logger.info(f"手动检查@消息: user_id={user_id[:8]}..., check_all={check_all}{time_hint}")

        # 第一步：立即检查并返回私聊消息（1-2秒内完成）
        dm_messages = await self._check_dm_queue(user_id, hours_limit=hours_limit)

        # 第二步：检查群聊@消息（30-60秒）
        # 如果是全量检查，临时保存并重置 last_check_time
        original_last_check_time = None
        if check_all:
            original_last_check_time = self.state.last_check_time
            self.state.last_check_time = 0  # 重置为0，强制检查最近24小时
            logger.info("全量检查: 将检查最近24小时的消息")

        try:
            group_mentions = await self._check_group_mentions(user_id, check_all=check_all, hours_limit=hours_limit)
        finally:
            # 恢复原来的 last_check_time（如果是全量检查）
            if check_all and original_last_check_time is not None:
                self.state.last_check_time = original_last_check_time

        # 合并所有结果
        all_mentions = dm_messages + group_mentions
        return all_mentions

    async def _check_dm_queue(self, user_id: str, hours_limit: Optional[int] = None) -> List[MentionInfo]:
        """
        检查私聊消息（队列 + 最近私聊会话）

        Args:
            user_id: 用户 open_id
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            未回复的私聊消息列表
        """
        dm_mentions_dict = {}

        # 计算时间截止点
        cutoff_time = None
        if hours_limit:
            cutoff_time = time.time() - (hours_limit * 3600)
            logger.info(f"私聊消息时间过滤: 只检查最近 {hours_limit} 小时内的消息")

        # 第1部分：检查事件队列中的实时消息
        dm_messages_from_queue = list(self._dm_queue)
        logger.info(f"检查私聊消息: 从事件队列获取到 {len(dm_messages_from_queue)} 条")

        for msg_data in dm_messages_from_queue:
            try:
                # 如果有时间限制，检查消息时间
                if cutoff_time and msg_data.get("create_time"):
                    msg_time = int(msg_data.get("create_time")) / 1000  # 毫秒转秒
                    if msg_time < cutoff_time:
                        continue

                dm_info = await self._process_dm_event(user_id, msg_data)
                if dm_info:
                    dm_mentions_dict[dm_info.message_id] = dm_info
            except Exception as e:
                logger.error(f"处理队列私聊消息失败: {e}")
                continue

        # 第2部分：主动检查最近的私聊会话（覆盖系统启动前的未读消息）
        # 注意：由于飞书API不支持区分私聊和群聊，这里只能依赖事件队列
        try:
            dm_chats = await self._get_dm_chats(user_id)
            if dm_chats:
                logger.info(f"获取到 {len(dm_chats)} 个私聊会话，开始检查未读消息")
                # 检查私聊会话
                for chat in dm_chats[:10]:  # 只检查前10个
                    chat_id = chat.get("chat_id")
                    chat_name = chat.get("name", "未知联系人")
                    try:
                        # 使用已有的_check_dm_messages方法检查单个私聊
                        chat_mentions = await self._check_dm_messages(user_id, chat_id, chat_name, hours_limit=hours_limit)
                        dm_mentions_dict.update(chat_mentions)
                    except Exception as e:
                        logger.error(f"检查私聊 {chat_name} 失败: {e}")
                        continue
            else:
                logger.info("没有找到私聊会话（飞书API限制）")
        except Exception as e:
            logger.error(f"获取私聊列表失败: {e}")

        dm_mentions = list(dm_mentions_dict.values())
        logger.info(f"私聊检查完成: 发现 {len(dm_mentions)} 条未回复消息")
        return dm_mentions

    async def _check_group_mentions(self, user_id: str, check_all: bool = False, hours_limit: Optional[int] = None) -> List[MentionInfo]:
        """
        检查群聊@消息（需要API调用，较慢）

        Args:
            user_id: 用户 open_id
            check_all: 是否全量检查
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            未回复的@消息列表
        """
        result = await self._check_mentions(user_id, check_all=check_all, hours_limit=hours_limit)
        return list(result.values())

    async def _check_mentions(
        self,
        user_id: str,
        check_all: bool = False,
        hours_limit: Optional[int] = None
    ) -> Dict[str, MentionInfo]:
        """
        执行检查逻辑

        Args:
            user_id: 用户 open_id
            check_all: 是否全量检查
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            未回复的@消息字典（message_id -> MentionInfo）
        """
        current_time = datetime.now().timestamp() * 1000
        unreplied_mentions: Dict[str, MentionInfo] = {}

        try:
            # 获取黑名单和重点群配置
            blacklist = set(self.config.get("mention.blacklist_chats", []))
            priority_chats = self.config.get("mention.priority_chats", [])
            # 注意：取消 only_recent_active 过滤，因为 API 不返回 last_message
            # 改用增量检查（每个群只查询上次检查后的新消息）来提升性能

            # 获取所有群
            all_chats = await self._get_all_chats(user_id, only_recent_active=False)
            logger.info(f"获取到 {len(all_chats)} 个群")

            # 过滤黑名单
            all_chats = [chat for chat in all_chats if chat["chat_id"] not in blacklist]
            logger.info(f"过滤黑名单后剩余 {len(all_chats)} 个聊天")

            # 按活跃度排序（使用缓存策略，避免超时）
            current_time_sec = datetime.now().timestamp()
            cache_age_sec = (current_time_sec - self.state.activity_cache_time / 1000) if self.state.activity_cache_time > 0 else float('inf')

            # 如果有缓存，使用缓存
            if self.state.sorted_chat_ids and len(self.state.sorted_chat_ids) > 0:
                logger.info(f"使用活跃度缓存（{cache_age_sec/60:.1f}分钟前更新）")
                # 按缓存的顺序重新排列
                chat_dict = {chat["chat_id"]: chat for chat in all_chats}
                sorted_chats = [chat_dict[cid] for cid in self.state.sorted_chat_ids if cid in chat_dict]
                # 添加新增的群（在缓存后新加入的）
                cached_ids = set(self.state.sorted_chat_ids)
                new_chats = [chat for chat in all_chats if chat["chat_id"] not in cached_ids]
                if new_chats:
                    logger.info(f"发现 {len(new_chats)} 个新群，添加到队列末尾")
                    sorted_chats.extend(new_chats)
                all_chats = sorted_chats

                # 如果缓存过期超过1小时，启动后台任务更新缓存
                if cache_age_sec > 3600:
                    logger.info("活跃度缓存已过期，将在本次检查后更新")
                    asyncio.create_task(self._update_activity_cache_async(user_id, all_chats))
            else:
                # 首次检查，没有缓存：不排序，直接使用原顺序，避免超时
                logger.info("首次检查，跳过活跃度排序（避免超时），将在后台异步排序")
                # 启动后台任务进行排序
                asyncio.create_task(self._update_activity_cache_async(user_id, all_chats))

            # 分离重点群和普通群
            group_chats = all_chats  # all_chats 全是群聊

            priority_set = set(priority_chats)
            priority_chats_list = [chat for chat in group_chats if chat["chat_id"] in priority_set]
            normal_chats_list = [chat for chat in group_chats if chat["chat_id"] not in priority_set]

            logger.info(f"重点群: {len(priority_chats_list)} 个，普通群: {len(normal_chats_list)} 个")

            # 确定要检查的群
            chats_to_check = []

            # 重点群总是检查
            chats_to_check.extend(priority_chats_list)

            # 普通群根据策略检查
            if check_all:
                # 全量检查：检查所有群（按活跃度排序）
                selected_normal = normal_chats_list  # 检查所有普通群
                chats_to_check.extend(selected_normal)
                logger.info(f"全量检查: 检查所有 {len(selected_normal)} 个普通群（按活跃度排序）")
            else:
                # 轮换检查（最多30个普通群，提升速度）
                max_normal = 30 if len(priority_chats_list) <= 30 else max(0, 30 - len(priority_chats_list))

                if normal_chats_list:
                    # 从上次停止的位置继续
                    start_idx = self.state.last_checked_index
                    end_idx = min(start_idx + max_normal, len(normal_chats_list))

                    selected_normal = normal_chats_list[start_idx:end_idx]
                    chats_to_check.extend(selected_normal)

                    # 更新索引
                    if end_idx >= len(normal_chats_list):
                        # 已检查到末尾，重置索引
                        self.state.last_checked_index = 0
                        logger.info("普通群轮换已完成一轮，重置索引")
                    else:
                        self.state.last_checked_index = end_idx

                    logger.info(f"本次检查普通群: {start_idx} - {end_idx-1}，下次从 {self.state.last_checked_index} 开始")

            logger.info(f"本次检查 {len(chats_to_check)} 个群聊")

            # 并发检查每个群的@消息（显著提升速度）
            total_mentions_found = 0

            # 创建并发任务
            async def check_single_chat(i: int, chat: dict):
                chat_id = chat["chat_id"]
                chat_name = chat.get("name", "未知群")
                try:
                    mentions = await self._check_chat_mentions(user_id, chat_id, chat_name, hours_limit=hours_limit)
                    if mentions:
                        logger.info(f"  [{i+1}/{len(chats_to_check)}] {chat_name}: 发现 {len(mentions)} 条@消息")
                    return mentions
                except Exception as e:
                    logger.error(f"检查群 {chat_name} 失败: {e}")
                    return {}

            # 并发执行所有检查（每5个群一批，避免过载）
            batch_size = 5
            for batch_start in range(0, len(chats_to_check), batch_size):
                batch_end = min(batch_start + batch_size, len(chats_to_check))
                batch_chats = chats_to_check[batch_start:batch_end]

                # 并发执行这一批
                tasks = [check_single_chat(batch_start + i, chat) for i, chat in enumerate(batch_chats)]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                # 合并结果
                for result in results:
                    if isinstance(result, dict):
                        unreplied_mentions.update(result)
                        total_mentions_found += len(result)

            logger.info(f"群聊检查完成: 检查了 {len(chats_to_check)} 个群，发现 {total_mentions_found} 条@消息")

            # 更新状态
            self.state.last_check_time = current_time
            self.state.total_chats_count = len(all_chats)
            self.state.save()

            logger.info(f"群聊@消息检查完成，发现 {len(unreplied_mentions)} 条@消息")

        except TokenExpiredError as e:
            logger.error(f"Token 已失效: {e}")
            raise
        except Exception as e:
            logger.error(f"检查@消息失败: {e}", exc_info=True)

        return unreplied_mentions

    async def _get_dm_chats(self, user_id: str) -> List[dict]:
        """
        获取用户的所有私聊

        通过获取用户的所有会话，然后筛选出私聊类型

        Args:
            user_id: 用户 open_id

        Returns:
            私聊列表
        """
        try:
            # 方法1: 尝试通过 chats API (可能包含 chat_mode 字段)
            all_chats = []
            page_token = None

            while True:
                result = await self.user_api.get_user_chats(user_id, page_size=50, page_token=page_token)
                items = result.get("items", [])
                all_chats.extend(items)

                if not result.get("has_more", False):
                    break
                page_token = result.get("page_token")

            # 尝试通过 chat_mode 字段筛选私聊
            # chat_mode 可能的值: "p2p" (私聊), "group" (群聊)
            # 由于飞书API不返回此字段，这里返回空列表
            # 私聊消息只能通过事件订阅队列获取
            dm_chats = []

            logger.info(f"从 {len(all_chats)} 个会话中找到 {len(dm_chats)} 个私聊（飞书API不支持区分，仅依赖事件队列）")

            # DEBUG: 保存前20个会话的完整数据到文件
            if all_chats:
                import json
                from pathlib import Path

                debug_file = Path.home() / ".remote-claude" / "chats_debug.json"
                debug_data = {
                    "total_count": len(all_chats),
                    "chats": all_chats[:20]  # 前20个会话
                }

                with open(debug_file, 'w', encoding='utf-8') as f:
                    json.dump(debug_data, f, ensure_ascii=False, indent=2)

                logger.info(f"已保存前20个会话数据到 {debug_file}")

                # 同时打印前5个到日志
                for i, chat in enumerate(all_chats[:5]):
                    logger.info(f"会话 {i+1} 数据: {json.dumps(chat, ensure_ascii=False)}")

            # 如果没找到私聊，打印第一个会话的字段用于调试
            if all_chats and not dm_chats:
                sample = all_chats[0]
                logger.info(f"示例会话字段: {list(sample.keys())}, chat_mode={sample.get('chat_mode', 'N/A')}, chat_type={sample.get('chat_type', 'N/A')}")

            return dm_chats

        except Exception as e:
            logger.error(f"获取私聊列表失败: {e}", exc_info=True)
            return []

    async def _sort_chats_by_activity(
        self,
        user_id: str,
        chats: List[dict],
        batch_size: int = 50
    ) -> List[dict]:
        """
        按活跃度（最后一条消息时间）对群聊进行排序

        Args:
            user_id: 用户 open_id
            chats: 群聊列表
            batch_size: 每批检查的数量（避免一次性请求太多）

        Returns:
            按活跃度降序排序的群聊列表
        """
        import asyncio

        chat_activity = []  # [(chat, last_message_time), ...]

        # 分批处理，避免太多并发请求
        for i in range(0, len(chats), batch_size):
            batch = chats[i:i+batch_size]
            logger.info(f"检查群活跃度: {i+1}-{min(i+batch_size, len(chats))}/{len(chats)}")

            # 并发获取每个群的最后一条消息
            tasks = []
            for chat in batch:
                tasks.append(self._get_last_message_time(user_id, chat["chat_id"]))

            # 等待批次完成
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 记录结果
            for chat, result in zip(batch, results):
                if isinstance(result, Exception):
                    # 出错的群放到最后（时间设为0）
                    chat_activity.append((chat, 0))
                else:
                    chat_activity.append((chat, result))

        # 按时间降序排序（最新的在前面）
        chat_activity.sort(key=lambda x: x[1], reverse=True)

        # 返回排序后的群列表
        sorted_chats = [chat for chat, _ in chat_activity]

        # 打印前10个最活跃的群
        logger.info("最活跃的10个群:")
        for i, (chat, last_time) in enumerate(chat_activity[:10]):
            if last_time > 0:
                time_str = datetime.fromtimestamp(last_time / 1000).strftime("%Y-%m-%d %H:%M")
                logger.info(f"  {i+1}. {chat['name'][:30]} - 最后消息: {time_str}")
            else:
                logger.info(f"  {i+1}. {chat['name'][:30]} - 无法获取")

        return sorted_chats

    async def _get_last_message_time(self, user_id: str, chat_id: str) -> float:
        """
        获取指定群的最后一条消息时间

        Args:
            user_id: 用户 open_id
            chat_id: 群 ID

        Returns:
            最后一条消息的时间戳（毫秒），如果获取失败返回 0
        """
        try:
            # 只获取最后1条消息
            result = await self.user_api.get_chat_messages(
                user_id,
                chat_id,
                page_size=1,
                sort_type="ByCreateTimeDesc"  # 降序，最新的在前面
            )

            messages = result.get("items", [])
            if messages:
                # 返回最后一条消息的创建时间
                return float(messages[0].get("create_time", "0"))
            else:
                return 0

        except Exception as e:
            logger.debug(f"获取群 {chat_id[:8]}... 最后消息时间失败: {e}")
            return 0

    async def _update_activity_cache_async(self, user_id: str, chats: List[dict]):
        """
        在后台异步更新活跃度缓存

        Args:
            user_id: 用户 open_id
            chats: 群聊列表
        """
        try:
            logger.info("后台任务：开始更新活跃度缓存...")
            sorted_chats = await self._sort_chats_by_activity(user_id, chats)

            # 更新缓存
            self.state.sorted_chat_ids = [chat["chat_id"] for chat in sorted_chats]
            self.state.activity_cache_time = datetime.now().timestamp() * 1000
            self.state.total_chats_count = len(sorted_chats)
            self.state.save()

            logger.info("后台任务：活跃度缓存更新完成")
        except Exception as e:
            logger.error(f"后台任务：更新活跃度缓存失败: {e}", exc_info=True)

    def _load_dm_chats(self) -> List[dict]:
        """
        从记录文件加载私聊列表

        Returns:
            私聊列表，格式: [{"user_id": "ou_xxx", "chat_id": "oc_xxx"}, ...]
        """
        try:
            from pathlib import Path
            import json
            import os

            # 获取用户数据目录
            home = Path.home()
            dm_file = home / ".remote-claude" / "dm_chats.json"

            if not dm_file.exists():
                return []

            with open(dm_file, 'r', encoding='utf-8') as f:
                data = json.load(f)

            dm_chats = data.get("dm_chats", [])
            logger.info(f"从 {dm_file} 加载 {len(dm_chats)} 个私聊")
            return dm_chats
        except Exception as e:
            logger.error(f"加载私聊列表失败: {e}")
            return []

    async def _get_all_chats(self, user_id: str, only_recent_active: bool = False) -> List[dict]:
        """
        获取所有群聊

        Args:
            user_id: 用户 open_id
            only_recent_active: 已废弃（API不返回last_message字段，无法过滤）

        Returns:
            群聊列表
        """
        all_chats = []
        page_token = None

        while True:
            result = await self.user_api.get_user_chats(user_id, page_size=50, page_token=page_token)
            all_chats.extend(result["items"])

            if not result.get("has_more", False):
                break
            page_token = result.get("page_token")

        return all_chats

    async def _check_chat_mentions(
        self,
        user_id: str,
        chat_id: str,
        chat_name: str,
        hours_limit: Optional[int] = None
    ) -> Dict[str, MentionInfo]:
        """
        检查单个群的@消息（增量检查）

        Args:
            user_id: 用户 open_id
            chat_id: 群聊 ID
            chat_name: 群聊名称
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            该群未回复的@消息字典
        """
        mentions: Dict[str, MentionInfo] = {}

        try:
            # 计算查询时间范围（上次检查时间 or 默认24小时前）
            from datetime import datetime, timedelta

            if hours_limit:
                # 如果指定了小时限制，使用该限制
                start_time = (datetime.now() - timedelta(hours=hours_limit)).timestamp() * 1000
                logger.debug(f"  群 {chat_name}: 使用时间限制 {hours_limit} 小时")
            elif self.state.last_check_time > 0:
                # 使用上次检查时间，往前推5分钟避免边界情况
                start_time = self.state.last_check_time - (5 * 60 * 1000)
            else:
                # 首次检查，查询最近24小时（避免遗漏较早的消息）
                start_time = (datetime.now() - timedelta(hours=24)).timestamp() * 1000

            # 获取消息（按时间倒序，只获取start_time之后的）
            result = await self.user_api.get_chat_messages(
                user_id, chat_id, page_size=50, sort_type="ByCreateTimeDesc"
            )
            messages = result["items"]

            # 过滤出时间范围内的消息
            original_count = len(messages)
            messages = [msg for msg in messages if float(msg.get("create_time", 0)) >= start_time]

            logger.debug(f"  群 {chat_name}: 获取 {original_count} 条消息，时间过滤后剩余 {len(messages)} 条")

            if not messages:
                return mentions

            # 反转消息列表（变成时间升序）
            messages.reverse()

            # 检查主消息流中的@
            main_mentions = self._find_mentions_in_messages(messages, user_id)

            # 判断主消息流中的@是否已回复
            for mention in main_mentions:
                if not self._is_replied_in_main(messages, mention, user_id):
                    # 未回复 - 尝试从多个位置获取发送者名称
                    sender_name = "未知用户"
                    sender_obj = mention.get("sender", {})

                    # 尝试多种方式获取用户名
                    if isinstance(sender_obj, dict):
                        # 方式1: sender.sender_id.name
                        sender_id_obj = sender_obj.get("sender_id", {})
                        if isinstance(sender_id_obj, dict):
                            sender_name = sender_id_obj.get("name") or sender_id_obj.get("user_name") or sender_name
                        # 方式2: sender.name
                        sender_name = sender_obj.get("name") or sender_obj.get("user_name") or sender_name

                    info = MentionInfo(
                        message_id=mention["message_id"],
                        chat_id=chat_id,
                        chat_name=chat_name,
                        time=float(mention["create_time"]),
                        sender_id=mention["sender"]["id"],
                        sender_name=sender_name,
                        text=self._extract_text(mention),
                        location="主消息",
                        chat_link=self._build_chat_link(chat_id)
                    )
                    mentions[info.message_id] = info

            # 检查话题回复中的@
            thread_roots = [msg for msg in messages if msg.get("thread_id") and not msg.get("parent_id")]
            for root_msg in thread_roots:
                thread_id = root_msg.get("thread_id")
                if not thread_id:
                    continue

                try:
                    thread_result = await self.user_api.get_thread_messages(
                        user_id, thread_id, page_size=50
                    )
                    thread_messages = thread_result["items"]
                    thread_messages.reverse()  # 时间升序

                    # 找出话题中的@
                    thread_mentions = self._find_mentions_in_messages(thread_messages, user_id)

                    # 判断话题中的@是否已回复
                    for mention in thread_mentions:
                        if not self._is_replied_in_thread(thread_messages, mention, user_id):
                            # 未回复 - 尝试从多个位置获取发送者名称
                            root_time = datetime.fromtimestamp(float(root_msg["create_time"]) / 1000)
                            root_time_str = root_time.strftime("%m-%d %H:%M")

                            sender_name = "未知用户"
                            sender_obj = mention.get("sender", {})
                            if isinstance(sender_obj, dict):
                                sender_id_obj = sender_obj.get("sender_id", {})
                                if isinstance(sender_id_obj, dict):
                                    sender_name = sender_id_obj.get("name") or sender_id_obj.get("user_name") or sender_name
                                sender_name = sender_obj.get("name") or sender_obj.get("user_name") or sender_name

                            info = MentionInfo(
                                message_id=mention["message_id"],
                                chat_id=chat_id,
                                chat_name=chat_name,
                                time=float(mention["create_time"]),
                                sender_id=mention["sender"]["id"],
                                sender_name=sender_name,
                                text=self._extract_text(mention),
                                location=f"话题回复（根消息: {root_time_str}）",
                                chat_link=self._build_chat_link(chat_id, thread_id)
                            )
                            mentions[info.message_id] = info

                except UserApiError as e:
                    # 忽略 "invalid container_id" 错误（不是所有消息都有话题）
                    if "invalid" not in str(e).lower():
                        logger.error(f"获取话题回复失败: {e}")

        except Exception as e:
            logger.error(f"检查群 {chat_name} 的消息失败: {e}")

        return mentions

    async def _check_dm_messages(
        self,
        user_id: str,
        chat_id: str,
        chat_name: str,
        hours_limit: Optional[int] = None
    ) -> Dict[str, MentionInfo]:
        """
        检查单个私聊的未读消息（增量检查）

        Args:
            user_id: 用户 open_id
            chat_id: 私聊 chat_id
            chat_name: 联系人名称
            hours_limit: 时间限制（小时），如果指定则只返回最近N小时内的消息

        Returns:
            该私聊未回复的消息字典
        """
        unreplied: Dict[str, MentionInfo] = {}

        try:
            # 计算查询时间范围（上次检查时间 or 默认24小时前）
            from datetime import datetime, timedelta

            if hours_limit:
                # 如果指定了小时限制，使用该限制
                start_time = (datetime.now() - timedelta(hours=hours_limit)).timestamp() * 1000
                logger.debug(f"  私聊 {chat_name}: 使用时间限制 {hours_limit} 小时")
            elif self.state.last_check_time > 0:
                # 使用上次检查时间，往前推5分钟避免边界情况
                start_time = self.state.last_check_time - (5 * 60 * 1000)
            else:
                # 首次检查，查询最近24小时（避免遗漏较早的消息）
                start_time = (datetime.now() - timedelta(hours=24)).timestamp() * 1000

            # 获取消息（按时间倒序）
            result = await self.user_api.get_chat_messages(
                user_id, chat_id, page_size=20, sort_type="ByCreateTimeDesc"
            )
            messages = result["items"]

            # 过滤出时间范围内的消息
            messages = [msg for msg in messages if float(msg.get("create_time", 0)) >= start_time]

            if not messages:
                return unreplied

            # 反转消息列表（变成时间升序）
            messages.reverse()

            # 找到最后一条不是自己发送的消息
            last_other_message = None
            for msg in reversed(messages):  # 从后往前找
                sender_id = msg.get("sender", {}).get("id", "")
                if sender_id != user_id:
                    last_other_message = msg
                    break

            if not last_other_message:
                # 最后一条消息是自己发的，没有未读
                return unreplied

            # 检查在这条消息之后，是否有自己的回复
            last_other_time = float(last_other_message["create_time"])
            has_reply = False

            for msg in messages:
                msg_time = float(msg["create_time"])
                sender_id = msg.get("sender", {}).get("id", "")

                if msg_time > last_other_time and sender_id == user_id:
                    has_reply = True
                    break

            if not has_reply:
                # 没有回复，标记为未读 - 尝试从多个位置获取发送者名称
                sender_name = "未知用户"
                sender_obj = last_other_message.get("sender", {})
                if isinstance(sender_obj, dict):
                    sender_id_obj = sender_obj.get("sender_id", {})
                    if isinstance(sender_id_obj, dict):
                        sender_name = sender_id_obj.get("name") or sender_id_obj.get("user_name") or sender_name
                    sender_name = sender_obj.get("name") or sender_obj.get("user_name") or sender_name

                info = MentionInfo(
                    message_id=last_other_message["message_id"],
                    chat_id=chat_id,
                    chat_name=chat_name,
                    time=last_other_time,
                    sender_id=last_other_message["sender"]["id"],
                    sender_name=sender_name,
                    text=self._extract_text(last_other_message),
                    location="私聊",
                    chat_link=self._build_chat_link(chat_id),
                    message_type="dm"
                )
                unreplied[info.message_id] = info

        except Exception as e:
            logger.error(f"检查私聊 {chat_name} 失败: {e}")

        return unreplied

    def _find_mentions_in_messages(self, messages: List[dict], user_id: str) -> List[dict]:
        """
        在消息列表中找出@当前用户的消息

        Args:
            messages: 消息列表
            user_id: 用户 open_id

        Returns:
            包含@的消息列表
        """
        mentions = []
        for msg in messages:
            # 检查 mentions 字段
            msg_mentions = msg.get("mentions", [])
            if any(m.get("id") == user_id for m in msg_mentions):
                mentions.append(msg)
        return mentions

    def _is_replied_in_main(self, messages: List[dict], mention_msg: dict, user_id: str) -> bool:
        """
        判断主消息流中的@是否已回复

        Args:
            messages: 消息列表（时间升序）
            mention_msg: @消息
            user_id: 用户 open_id

        Returns:
            是否已回复
        """
        mention_time = float(mention_msg["create_time"])

        # 检查@消息之后是否有用户发送的消息
        for msg in messages:
            msg_time = float(msg["create_time"])
            sender_id = msg.get("sender", {}).get("id", "")

            if msg_time > mention_time and sender_id == user_id:
                return True

        return False

    def _is_replied_in_thread(self, thread_messages: List[dict], mention_msg: dict, user_id: str) -> bool:
        """
        判断话题中的@是否已回复

        Args:
            thread_messages: 话题消息列表（时间升序）
            mention_msg: @消息
            user_id: 用户 open_id

        Returns:
            是否已回复
        """
        mention_time = float(mention_msg["create_time"])

        # 检查@消息之后是否有用户在该话题中发送的消息
        for msg in thread_messages:
            msg_time = float(msg["create_time"])
            sender_id = msg.get("sender", {}).get("id", "")

            if msg_time > mention_time and sender_id == user_id:
                return True

        return False

    def _extract_text(self, message: dict) -> str:
        """
        提取消息文本内容

        Args:
            message: 消息对象

        Returns:
            消息文本（最多100字）
        """
        msg_type = message.get("msg_type", "")
        body = message.get("body", {})
        content = body.get("content", "")

        try:
            if msg_type == "text":
                data = json.loads(content) if isinstance(content, str) else content
                text = data.get("text", "")
            elif msg_type == "post":
                # 富文本消息
                text = "[富文本消息]"
            else:
                text = f"[{msg_type}消息]"

            # 截取前100字
            if len(text) > 100:
                text = text[:100] + "..."

            return text
        except Exception as e:
            logger.error(f"提取消息文本失败: {e}")
            return "[无法解析的消息]"

    def _build_chat_link(self, chat_id: str, thread_id: Optional[str] = None) -> str:
        """
        构建飞书群聊深链接

        Args:
            chat_id: 群聊 ID
            thread_id: 话题 ID（可选，暂不支持直接跳转到话题）

        Returns:
            飞书群聊深链接（使用 applink 格式，可在网页和客户端打开）
        """
        # 使用飞书的 applink 格式，支持在网页和客户端打开
        # 注意：目前飞书深链接不支持直接跳转到具体话题，只能跳转到群聊
        return f"https://applink.feishu.cn/client/chat/open?openChatId={chat_id}"

    async def _handle_realtime_dm(self, message_data: dict):
        """
        处理实时私聊消息并立即推送通知

        Args:
            message_data: 私聊消息数据，包含：
                - message_id: 消息 ID
                - chat_id: 会话 ID（私聊的 chat_id）
                - sender_id: 发送者 ID
                - recipient_id: 接收者 ID（消息接收人）
                - content: 消息内容
                - create_time: 创建时间（毫秒）
        """
        try:
            chat_id = message_data.get("chat_id")
            sender_id = message_data.get("sender_id")
            recipient_user_id = message_data.get("recipient_id")

            if not chat_id or not sender_id or not recipient_user_id:
                logger.warning(f"私聊消息缺少必要字段: chat_id={chat_id}, sender_id={sender_id}, recipient={recipient_user_id}")
                return

            # 过滤掉用户自己发送的消息
            if sender_id == recipient_user_id:
                logger.debug(f"跳过用户 {recipient_user_id[:8]}... 自己发送的消息")
                return

            # 调用 _process_dm_event 创建 MentionInfo
            mention_info = await self._process_dm_event(recipient_user_id, message_data)
            if not mention_info:
                logger.debug("消息不需要处理")
                return

            # 立即发送通知（通知发送到接收者的 chat_id）
            logger.info(f"实时推送私聊消息通知: {sender_id[:8]}... -> {recipient_user_id[:8]}...")
            await self._send_notification(chat_id, [mention_info], message_type="私聊")

        except Exception as e:
            logger.error(f"处理实时私聊消息失败: {e}", exc_info=True)

    async def _process_dm_event(self, user_id: str, msg_data: dict) -> Optional[MentionInfo]:
        """
        处理从事件订阅接收到的私聊消息

        Args:
            user_id: 当前用户的 open_id
            msg_data: 私聊消息数据，包含：
                - message_id: 消息 ID
                - chat_id: 会话 ID
                - sender_id: 发送者 ID
                - content: 消息内容
                - create_time: 创建时间（毫秒字符串）

        Returns:
            MentionInfo 或 None（如果消息不需要处理）
        """
        try:
            message_id = msg_data.get("message_id")
            chat_id = msg_data.get("chat_id")
            sender_id = msg_data.get("sender_id")
            content = msg_data.get("content", "")
            create_time = float(msg_data.get("create_time", "0"))

            # 跳过自己发送的消息
            if sender_id == user_id:
                return None

            # 获取发送者信息
            try:
                sender_info = await self.user_api.get_user_info(user_id, sender_id)
                sender_name = sender_info.get("name", sender_id[:12])
            except:
                sender_name = sender_id[:12]

            # 创建 MentionInfo
            mention_info = MentionInfo(
                message_id=message_id,
                chat_id=chat_id,
                chat_name=f"私聊 - {sender_name}",
                time=create_time,
                sender_id=sender_id,
                sender_name=sender_name,
                text=content,
                location="私聊",
                chat_link=self._build_chat_link(chat_id),
                message_type="dm"
            )

            logger.info(f"处理私聊消息: {sender_name} - {content[:50]}...")
            return mention_info

        except Exception as e:
            logger.error(f"处理私聊事件失败: {e}", exc_info=True)
            return None

    async def _send_notification(self, chat_id: str, mentions: List[MentionInfo], message_type: str = "全部") -> None:
        """
        发送通知卡片

        Args:
            chat_id: 接收通知的 chat_id
            mentions: @消息列表
            message_type: 消息类型（"私聊"、"群聊"、"全部"），用于卡片标题
        """
        if not self.card_sender:
            logger.warning("未配置 card_sender，跳过发送通知")
            return

        # 使用lark_handler的card发送机制
        from .card_builder import build_mentions_card

        # 构建卡片
        card = build_mentions_card(mentions)

        # 修改标题以区分消息类型
        if message_type == "私聊":
            card["header"]["title"]["content"] = "💬 私聊未读消息"
        elif message_type == "群聊":
            card["header"]["title"]["content"] = "📢 群聊@消息"

        # 发送卡片
        try:
            from .card_service import card_service
            await card_service.create_and_send_card(chat_id, card)
            logger.info(f"已发送{message_type}通知到 {chat_id}，包含 {len(mentions)} 条消息")
        except Exception as e:
            logger.error(f"发送卡片失败: {e}", exc_info=True)
