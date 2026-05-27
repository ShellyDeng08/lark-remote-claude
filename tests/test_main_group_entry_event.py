#!/usr/bin/env python3
"""
main.handle_chat_member_bot_added 事件路由单测

覆盖：
1. 机器人被拉入群后，主动触发群入口卡
2. 缺少 operator_id 时，降级为 system 占位
3. 群聊 @ 机器人但无文本时，仍触发入口卡路由
"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lark_client import main as lark_main


class TestMainGroupEntryEvent(unittest.IsolatedAsyncioTestCase):

    @staticmethod
    def _make_receive_event(*, text: str, chat_type: str = "group"):
        message = SimpleNamespace(
            chat_id="chat_test",
            message_type="text",
            chat_type=chat_type,
            content=f'{{"text":"{text}"}}',
            mentions=[SimpleNamespace(key="bot_key")],
            message_id="msg_test",
        )
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="u_test"))
        event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        return event

    @staticmethod
    def _make_receive_event_post(*, text: str, chat_type: str = "group"):
        post_content = {
            "zh_cn": {
                "title": "",
                "content": [[{"tag": "text", "text": text}]],
            }
        }
        message = SimpleNamespace(
            chat_id="chat_test",
            message_type="post",
            chat_type=chat_type,
            content=json.dumps(post_content, ensure_ascii=False),
            mentions=[],
            message_id="msg_test",
        )
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="u_test"))
        event = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))
        return event

    async def test_bot_added_triggers_group_entry_card(self):
        data = SimpleNamespace(
            event=SimpleNamespace(
                chat_id="chat_test",
                operator_id=SimpleNamespace(open_id="u_test"),
            )
        )

        with patch.object(lark_main.handler, "_show_group_entry_card", new=AsyncMock()) as mock_show:
            ret = lark_main.handle_chat_member_bot_added(data)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_show.assert_awaited_once_with("u_test", "chat_test")

    async def test_bot_added_without_operator_uses_system(self):
        data = SimpleNamespace(
            event=SimpleNamespace(
                chat_id="chat_test",
                operator_id=None,
            )
        )

        with patch.object(lark_main.handler, "_show_group_entry_card", new=AsyncMock()) as mock_show:
            lark_main.handle_chat_member_bot_added(data)
            await asyncio.sleep(0)
            mock_show.assert_awaited_once_with("system", "chat_test")

    async def test_group_mention_without_text_still_routes_to_handler(self):
        data = self._make_receive_event(text="@_bot_key")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch.object(lark_main.handler, "handle_message", new=AsyncMock()) as mock_handle:
            lark_main.handle_message_receive(data)
            await asyncio.sleep(0)
            mock_handle.assert_awaited_once_with("u_test", "chat_test", "", chat_type="group")

    async def test_group_post_message_routes_to_handler(self):
        data = self._make_receive_event_post(text="这是一条 post 消息")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch.object(lark_main.handler, "handle_message", new=AsyncMock()) as mock_handle:
            lark_main.handle_message_receive(data)
            await asyncio.sleep(0)
            mock_handle.assert_awaited_once_with("u_test", "chat_test", "这是一条 post 消息", chat_type="group")

    async def test_group_non_mention_empty_text_sends_warning(self):
        message = SimpleNamespace(
            chat_id="chat_test",
            message_type="post",
            chat_type="group",
            content=json.dumps({"zh_cn": {"title": "", "content": [[{"tag": "img", "image_key": "k1"}]]}}, ensure_ascii=False),
            mentions=[],
            message_id="msg_test",
        )
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="u_test"))
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch.object(lark_main.handler, "handle_message", new=AsyncMock()) as mock_handle, \
             patch.object(lark_main.card_service, "send_text", new=AsyncMock()) as mock_send_text:
            lark_main.handle_message_receive(data)
            await asyncio.sleep(0)
            mock_handle.assert_not_awaited()
            mock_send_text.assert_awaited_once()
            self.assertIn("未识别到可解析文本", mock_send_text.await_args.args[1])

    async def test_group_post_unknown_tag_with_text_fallback_routes(self):
        message = SimpleNamespace(
            chat_id="chat_test",
            message_type="post",
            chat_type="group",
            content=json.dumps({"zh_cn": {"title": "", "content": [[{"tag": "code_block", "text": "print('hi')"}]]}}, ensure_ascii=False),
            mentions=[],
            message_id="msg_test",
        )
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="u_test"))
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch.object(lark_main.handler, "handle_message", new=AsyncMock()) as mock_handle:
            lark_main.handle_message_receive(data)
            await asyncio.sleep(0)
            mock_handle.assert_awaited_once_with("u_test", "chat_test", "print('hi')", chat_type="group")

    async def test_group_post_with_mentions_routes_message(self):
        message = SimpleNamespace(
            chat_id="chat_test",
            message_type="post",
            chat_type="group",
            content=json.dumps({"zh_cn": {"title": "", "content": [[{"tag": "at", "name": "机器人"}]]}}, ensure_ascii=False),
            mentions=[SimpleNamespace(key="bot_key")],
            message_id="msg_test",
        )
        sender = SimpleNamespace(sender_id=SimpleNamespace(open_id="u_test"))
        data = SimpleNamespace(event=SimpleNamespace(message=message, sender=sender))

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch.object(lark_main.handler, "handle_message", new=AsyncMock()) as mock_handle, \
             patch.object(lark_main.card_service, "send_text", new=AsyncMock()) as mock_send_text:
            lark_main.handle_message_receive(data)
            await asyncio.sleep(0)
            mock_handle.assert_awaited_once_with("u_test", "chat_test", "@机器人", chat_type="group")
            mock_send_text.assert_not_awaited()


if __name__ == "__main__":
    unittest.main(verbosity=2)
