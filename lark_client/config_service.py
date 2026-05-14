"""
配置服务 - 统一管理 Remote Claude 飞书客户端的所有配置
"""
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ConfigService:
    """统一配置管理服务"""

    # 默认配置
    DEFAULT_CONFIG = {
        "version": "1.0",
        "mention": {
            "auto_check_enabled": True,
            "check_interval_minutes": 10,
            "only_recent_active": True,  # 只检查最近1天活跃的群（性能优化）
            "blacklist_chats": [],
            "priority_chats": [],
            "notify_priority_only": False
        },
        "notification": {
            "on_complete": True,
            "on_error": True,
            "urgent_at_mention": True
        },
        "ui": {
            "message_mode": "text",
            "bypass_permission": False
        }
    }

    def __init__(self, config_path: Optional[Path] = None):
        """
        初始化配置服务

        Args:
            config_path: 配置文件路径，默认为 ~/.remote-claude/config.json
        """
        if config_path is None:
            config_path = Path.home() / ".remote-claude" / "config.json"
        self.config_path = config_path
        self.config: Dict[str, Any] = {}
        self.load()

    def load(self) -> Dict[str, Any]:
        """
        加载配置文件，如果文件不存在或格式错误，使用默认配置

        Returns:
            配置字典
        """
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    loaded_config = json.load(f)
                # 深度合并：用加载的配置覆盖默认配置
                self.config = self._deep_merge(self.DEFAULT_CONFIG.copy(), loaded_config)
                logger.info(f"配置已从 {self.config_path} 加载")
            else:
                # 文件不存在，使用默认配置
                self.config = self.DEFAULT_CONFIG.copy()
                logger.info("配置文件不存在，使用默认配置")
                # 自动保存默认配置
                self.save()
        except json.JSONDecodeError as e:
            logger.error(f"配置文件格式错误: {e}，使用默认配置")
            self.config = self.DEFAULT_CONFIG.copy()
        except Exception as e:
            logger.error(f"加载配置失败: {e}，使用默认配置")
            self.config = self.DEFAULT_CONFIG.copy()

        return self.config

    def save(self) -> None:
        """保存配置到文件"""
        try:
            # 确保目录存在
            self.config_path.parent.mkdir(parents=True, exist_ok=True)
            # 保存配置
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            logger.info(f"配置已保存到 {self.config_path}")
        except Exception as e:
            logger.error(f"保存配置失败: {e}")
            raise

    def get(self, key: str, default: Any = None) -> Any:
        """
        获取配置项，支持点号分隔的嵌套键

        Args:
            key: 配置键，如 "mention.auto_check_enabled"
            default: 默认值

        Returns:
            配置值
        """
        keys = key.split('.')
        value = self.config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    def set(self, key: str, value: Any) -> None:
        """
        设置配置项，支持点号分隔的嵌套键

        Args:
            key: 配置键，如 "mention.auto_check_enabled"
            value: 配置值

        Raises:
            ValueError: 配置验证失败
            TypeError: 配置类型错误
        """
        keys = key.split('.')
        target = self.config

        # 导航到倒数第二层
        for k in keys[:-1]:
            if k not in target:
                target[k] = {}
            target = target[k]

        # 设置最后一层的值
        target[keys[-1]] = value

        # 验证配置
        self.validate()

    def validate(self) -> None:
        """
        验证配置合法性

        Raises:
            ValueError: 配置值不合法
            TypeError: 配置类型错误
        """
        # 验证 check_interval_minutes
        interval = self.get("mention.check_interval_minutes")
        if interval is not None:
            if not isinstance(interval, int):
                raise TypeError(f"mention.check_interval_minutes 必须是整数类型，当前: {type(interval).__name__}")
            if not (5 <= interval <= 60):
                raise ValueError(f"mention.check_interval_minutes 必须在 5-60 范围内，当前: {interval}")

        # 验证 auto_check_enabled
        auto_check = self.get("mention.auto_check_enabled")
        if auto_check is not None and not isinstance(auto_check, bool):
            raise TypeError(f"mention.auto_check_enabled 必须是 bool 类型，当前: {type(auto_check).__name__}")

        # 验证 blacklist_chats
        blacklist = self.get("mention.blacklist_chats", [])
        if not isinstance(blacklist, list):
            raise TypeError(f"mention.blacklist_chats 必须是列表类型，当前: {type(blacklist).__name__}")
        for chat_id in blacklist:
            self._validate_chat_id(chat_id, "blacklist")

        # 验证 priority_chats
        priority = self.get("mention.priority_chats", [])
        if not isinstance(priority, list):
            raise TypeError(f"mention.priority_chats 必须是列表类型，当前: {type(priority).__name__}")
        for chat_id in priority:
            self._validate_chat_id(chat_id, "priority")

    def _validate_chat_id(self, chat_id: str, list_type: str) -> None:
        """
        验证 chat_id 格式

        Args:
            chat_id: 群聊 ID
            list_type: 列表类型（用于错误消息）

        Raises:
            ValueError: chat_id 格式不合法
        """
        if not isinstance(chat_id, str):
            raise ValueError(f"{list_type} 中的 chat_id 必须是字符串，当前: {type(chat_id).__name__}")
        if not (chat_id.startswith("oc_") or chat_id.startswith("ou_")):
            raise ValueError(f"{list_type} 中的 chat_id 格式不正确，必须以 oc_ 或 ou_ 开头: {chat_id}")

    def _deep_merge(self, base: Dict, overlay: Dict) -> Dict:
        """
        深度合并两个字典

        Args:
            base: 基础字典
            overlay: 覆盖字典

        Returns:
            合并后的字典
        """
        result = base.copy()
        for key, value in overlay.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def add_to_list(self, list_key: str, item: str) -> None:
        """
        添加项到列表配置

        Args:
            list_key: 列表配置键，如 "mention.blacklist_chats"
            item: 要添加的项

        Raises:
            ValueError: 配置验证失败或项已存在
        """
        current_list = self.get(list_key, [])
        if not isinstance(current_list, list):
            raise TypeError(f"{list_key} 不是列表类型")
        if item in current_list:
            raise ValueError(f"项 {item} 已存在于 {list_key}")
        current_list.append(item)
        self.set(list_key, current_list)

    def remove_from_list(self, list_key: str, item: str) -> None:
        """
        从列表配置中移除项

        Args:
            list_key: 列表配置键，如 "mention.blacklist_chats"
            item: 要移除的项

        Raises:
            ValueError: 项不存在
        """
        current_list = self.get(list_key, [])
        if not isinstance(current_list, list):
            raise TypeError(f"{list_key} 不是列表类型")
        if item not in current_list:
            raise ValueError(f"项 {item} 不存在于 {list_key}")
        current_list.remove(item)
        self.set(list_key, current_list)

    def get_list(self, list_key: str) -> List[str]:
        """
        获取列表配置

        Args:
            list_key: 列表配置键，如 "mention.blacklist_chats"

        Returns:
            列表配置
        """
        current_list = self.get(list_key, [])
        if not isinstance(current_list, list):
            return []
        return current_list
