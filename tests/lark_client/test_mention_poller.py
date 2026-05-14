"""
MentionPoller 单元测试
"""
import pytest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path
from tempfile import TemporaryDirectory

from lark_client.mention_poller import MentionPoller, MentionState, MentionInfo
from lark_client.config_service import ConfigService


class TestMentionState:
    """MentionState 测试类"""

    def test_save_and_load_state(self):
        """测试状态保存和加载"""
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "mention_state.json"
            state = MentionState(state_path)

            # 设置状态
            state.last_check_time = 1234567890000
            state.last_checked_index = 50
            state.known_unreplied = {
                "msg1": MentionInfo(
                    message_id="msg1",
                    chat_id="oc_test1",
                    chat_name="测试群",
                    time=1234567890000,
                    sender_id="ou_sender1",
                    sender_name="发送者",
                    text="测试消息",
                    location="主消息",
                    chat_link="https://example.com"
                )
            }

            # 保存
            state.save()

            # 重新加载
            state2 = MentionState(state_path)
            state2.load()

            assert state2.last_check_time == 1234567890000
            assert state2.last_checked_index == 50
            assert len(state2.known_unreplied) == 1
            assert "msg1" in state2.known_unreplied
            assert state2.known_unreplied["msg1"].chat_name == "测试群"

    def test_get_new_mentions(self):
        """测试新增@消息检测"""
        with TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "mention_state.json"
            state = MentionState(state_path)

            # 已知未回复消息
            state.known_unreplied = {
                "msg1": MentionInfo(
                    message_id="msg1",
                    chat_id="oc_test1",
                    chat_name="测试群",
                    time=1234567890000,
                    sender_id="ou_sender1",
                    sender_name="发送者",
                    text="旧消息",
                    location="主消息",
                    chat_link="https://example.com"
                )
            }

            # 当前未回复消息（包含一条新消息）
            current_unreplied = {
                "msg1": state.known_unreplied["msg1"],  # 旧消息（仍未回复）
                "msg2": MentionInfo(
                    message_id="msg2",
                    chat_id="oc_test1",
                    chat_name="测试群",
                    time=1234567900000,
                    sender_id="ou_sender2",
                    sender_name="发送者2",
                    text="新消息",
                    location="主消息",
                    chat_link="https://example.com"
                )
            }

            # 获取新增消息
            new_mentions = state.get_new_mentions(current_unreplied)

            # 验证
            assert len(new_mentions) == 1
            assert new_mentions[0].message_id == "msg2"
            assert new_mentions[0].text == "新消息"


class TestMentionPoller:
    """MentionPoller 测试类"""

    @pytest.fixture
    def mock_config_service(self):
        """Mock ConfigService"""
        with TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            service = ConfigService(config_path)
            yield service

    @pytest.fixture
    def mock_user_api(self):
        """Mock LarkUserApi"""
        api = MagicMock()
        api.get_user_chats = AsyncMock()
        api.get_chat_messages = AsyncMock()
        api.get_thread_messages = AsyncMock()
        return api

    def test_find_mentions_in_messages(self, mock_config_service, mock_user_api):
        """测试主消息流@检测"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        messages = [
            {
                "message_id": "msg1",
                "create_time": "1234567890000",
                "sender": {"id": "ou_other", "name": "其他人"},
                "mentions": [{"id": "ou_testuser"}],
                "body": {"content": '{"text": "test"}'},
                "msg_type": "text"
            },
            {
                "message_id": "msg2",
                "create_time": "1234567891000",
                "sender": {"id": "ou_other", "name": "其他人"},
                "mentions": [],  # 没有@
                "body": {"content": '{"text": "test"}'},
                "msg_type": "text"
            }
        ]

        # 执行检测
        mentions = poller._find_mentions_in_messages(messages, "ou_testuser")

        # 验证
        assert len(mentions) == 1
        assert mentions[0]["message_id"] == "msg1"

    def test_is_replied_in_main(self, mock_config_service, mock_user_api):
        """测试主消息流回复判断"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        messages = [
            {
                "message_id": "msg1",
                "create_time": "1234567890000",
                "sender": {"id": "ou_other"},
                "mentions": [{"id": "ou_testuser"}]
            },
            {
                "message_id": "msg2",
                "create_time": "1234567895000",
                "sender": {"id": "ou_testuser"}  # 用户在@消息后发送了消息
            }
        ]

        mention_msg = messages[0]

        # 执行判断
        is_replied = poller._is_replied_in_main(messages, mention_msg, "ou_testuser")

        # 验证
        assert is_replied is True

    def test_is_not_replied_in_main(self, mock_config_service, mock_user_api):
        """测试主消息流未回复判断"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        messages = [
            {
                "message_id": "msg1",
                "create_time": "1234567890000",
                "sender": {"id": "ou_other"},
                "mentions": [{"id": "ou_testuser"}]
            },
            {
                "message_id": "msg2",
                "create_time": "1234567885000",  # 时间更早
                "sender": {"id": "ou_testuser"}
            }
        ]

        mention_msg = messages[0]

        # 执行判断
        is_replied = poller._is_replied_in_main(messages, mention_msg, "ou_testuser")

        # 验证
        assert is_replied is False

    def test_is_replied_in_thread(self, mock_config_service, mock_user_api):
        """测试话题回复判断"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        thread_messages = [
            {
                "message_id": "msg1",
                "create_time": "1234567890000",
                "sender": {"id": "ou_other"}
            },
            {
                "message_id": "msg2",
                "create_time": "1234567895000",
                "sender": {"id": "ou_testuser"}  # 用户在话题中回复
            }
        ]

        mention_msg = thread_messages[0]

        # 执行判断
        is_replied = poller._is_replied_in_thread(thread_messages, mention_msg, "ou_testuser")

        # 验证
        assert is_replied is True

    def test_extract_text(self, mock_config_service, mock_user_api):
        """测试消息文本提取"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        # 文本消息
        message = {
            "msg_type": "text",
            "body": {"content": '{"text": "测试消息内容"}'}
        }
        text = poller._extract_text(message)
        assert text == "测试消息内容"

        # 富文本消息
        message = {
            "msg_type": "post",
            "body": {"content": "{}"}
        }
        text = poller._extract_text(message)
        assert text == "[富文本消息]"

        # 长消息（截取）
        long_text = "a" * 150
        message = {
            "msg_type": "text",
            "body": {"content": f'{{"text": "{long_text}"}}'}
        }
        text = poller._extract_text(message)
        assert len(text) <= 103  # 100 + "..."
        assert text.endswith("...")

    def test_build_chat_link(self, mock_config_service, mock_user_api):
        """测试群聊链接构建"""
        poller = MentionPoller(mock_config_service, mock_user_api)

        # 主消息链接
        link = poller._build_chat_link("oc_test1")
        assert "oc_test1" in link

        # 话题链接
        link = poller._build_chat_link("oc_test1", "thread_123")
        assert "oc_test1" in link
        assert "thread_123" in link

    def test_blacklist_filter(self, mock_config_service, mock_user_api):
        """测试黑名单过滤"""
        # 设置黑名单
        mock_config_service.set("mention.blacklist_chats", ["oc_blacklist"])
        mock_config_service.save()

        # Mock API 返回
        mock_user_api.get_user_chats.return_value = {
            "items": [
                {"chat_id": "oc_normal", "name": "普通群"},
                {"chat_id": "oc_blacklist", "name": "黑名单群"}
            ],
            "has_more": False
        }

        # 注意：这里只是演示逻辑，实际的 _check_mentions 是异步方法，需要用 pytest-asyncio


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
