"""
监听配置管理服务

管理用户的群聊监听列表、检查频率、静默时段等配置。
配置文件存储在 ~/.remote-claude/monitor_config.json
"""

import json
import time
import fcntl
from pathlib import Path
from typing import Dict, List, Optional

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.session import USER_DATA_DIR


class MonitorConfigService:
    """监听配置管理服务"""

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置服务

        Args:
            config_path: 配置文件路径，默认为 ~/.remote-claude/monitor_config.json
        """
        self.config_path = config_path or (USER_DATA_DIR / "monitor_config.json")
        self._ensure_config_file()

    def _ensure_config_file(self):
        """确保配置文件存在"""
        if not self.config_path.exists():
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            self._save_all({})

    def _load_all(self) -> Dict:
        """
        加载所有用户配置（带文件锁）

        Returns:
            Dict: 用户配置字典，key 为 open_id
        """
        with open(self.config_path, "r", encoding="utf-8") as f:
            # 获取共享锁（读锁）
            fcntl.flock(f.fileno(), fcntl.LOCK_SH)
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def _save_all(self, all_configs: Dict) -> bool:
        """
        保存所有用户配置（带文件锁）

        Args:
            all_configs: 所有用户配置

        Returns:
            bool: 是否保存成功
        """
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                # 获取排他锁（写锁）
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(all_configs, f, ensure_ascii=False, indent=2)
                    return True
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        except Exception as e:
            print(f"[监听配置] 保存配置失败: {e}")
            return False

    def _get_default_config(self) -> Dict:
        """
        获取默认配置

        Returns:
            Dict: 默认配置结构
        """
        return {
            "monitored_chats": [],
            "check_interval_minutes": 10,
            "quiet_hours": {
                "enabled": True,
                "start": "22:00",
                "end": "08:00"
            }
        }

    def get_user_config(self, open_id: str) -> Dict:
        """
        获取单个用户的配置

        Args:
            open_id: 用户 open_id

        Returns:
            Dict: 用户配置，如不存在则返回默认配置
        """
        all_configs = self._load_all()
        return all_configs.get(open_id, self._get_default_config())

    def save_user_config(self, open_id: str, config: Dict) -> bool:
        """
        保存单个用户的配置

        Args:
            open_id: 用户 open_id
            config: 用户配置

        Returns:
            bool: 是否保存成功
        """
        all_configs = self._load_all()
        all_configs[open_id] = config
        return self._save_all(all_configs)

    def add_chat(self, open_id: str, chat: Dict) -> bool:
        """
        添加群聊到监听列表

        Args:
            open_id: 用户 open_id
            chat: 群聊信息，包含 chat_id, chat_name, chat_type, added_at, last_check_time

        Returns:
            bool: 是否添加成功（如已存在则返回 False）
        """
        config = self.get_user_config(open_id)

        # 去重检查
        chat_ids = [c['chat_id'] for c in config['monitored_chats']]
        if chat['chat_id'] in chat_ids:
            return False

        # 确保必要字段存在
        chat.setdefault('added_at', int(time.time()))
        chat.setdefault('last_check_time', 0)

        config['monitored_chats'].append(chat)
        return self.save_user_config(open_id, config)

    def remove_chat(self, open_id: str, index: int) -> bool:
        """
        根据序号删除监听的群聊（序号从 1 开始）

        Args:
            open_id: 用户 open_id
            index: 群聊序号（从 1 开始）

        Returns:
            bool: 是否删除成功（序号无效则返回 False）
        """
        config = self.get_user_config(open_id)
        monitored_chats = config['monitored_chats']

        # 验证序号有效性
        if index < 1 or index > len(monitored_chats):
            return False

        # 删除指定序号的群聊（序号从 1 开始，列表索引从 0 开始）
        monitored_chats.pop(index - 1)
        return self.save_user_config(open_id, config)

    def remove_chat_by_id(self, open_id: str, chat_id: str) -> bool:
        """
        根据 chat_id 删除监听的群聊

        Args:
            open_id: 用户 open_id
            chat_id: 群聊 ID

        Returns:
            bool: 是否删除成功（chat_id 不存在则返回 False）
        """
        config = self.get_user_config(open_id)
        monitored_chats = config['monitored_chats']

        # 查找并删除
        original_length = len(monitored_chats)
        config['monitored_chats'] = [
            c for c in monitored_chats if c['chat_id'] != chat_id
        ]

        # 如果长度未变化，说明未找到
        if len(config['monitored_chats']) == original_length:
            return False

        return self.save_user_config(open_id, config)

    def update_check_interval(self, open_id: str, minutes: int) -> bool:
        """
        更新检查间隔

        Args:
            open_id: 用户 open_id
            minutes: 检查间隔（分钟），支持 5/10/15/30

        Returns:
            bool: 是否更新成功
        """
        # 验证参数
        if minutes not in [5, 10, 15, 30]:
            return False

        config = self.get_user_config(open_id)
        config['check_interval_minutes'] = minutes
        return self.save_user_config(open_id, config)

    def update_quiet_hours(self, open_id: str, settings: Dict) -> bool:
        """
        更新静默时段配置

        Args:
            open_id: 用户 open_id
            settings: 静默时段配置，包含 enabled, start, end

        Returns:
            bool: 是否更新成功
        """
        # 验证参数
        if 'enabled' not in settings:
            return False

        if settings['enabled']:
            # 验证时间格式（HH:MM）
            import re
            time_pattern = r'^([01]\d|2[0-3]):([0-5]\d)$'
            if not re.match(time_pattern, settings.get('start', '')):
                return False
            if not re.match(time_pattern, settings.get('end', '')):
                return False

        config = self.get_user_config(open_id)
        config['quiet_hours'] = settings
        return self.save_user_config(open_id, config)

    def get_all_users(self) -> List[str]:
        """
        获取所有配置了监听的用户列表

        Returns:
            List[str]: 用户 open_id 列表
        """
        all_configs = self._load_all()
        return list(all_configs.keys())

    def update_last_check_time(self, open_id: str, chat_id: str, timestamp: int) -> bool:
        """
        更新指定群聊的最后检查时间

        Args:
            open_id: 用户 open_id
            chat_id: 群聊 ID
            timestamp: 时间戳

        Returns:
            bool: 是否更新成功
        """
        config = self.get_user_config(open_id)

        # 查找并更新
        for chat in config['monitored_chats']:
            if chat['chat_id'] == chat_id:
                chat['last_check_time'] = timestamp
                return self.save_user_config(open_id, config)

        return False
