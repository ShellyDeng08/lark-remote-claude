#!/usr/bin/env python3
"""
main.handle_card_action 关键 action 路由单测

覆盖：
1. group_show_recovery
2. group_reconnect_original
3. group_choose_takeover
4. group_takeover_session
5. group_summarize_now
6. view_round_diff
7. 卡片动作可视化提示
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Dict, Any
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lark_client import main as lark_main


class TestMainCardActionRouting(unittest.IsolatedAsyncioTestCase):

    @staticmethod
    def _make_event(action: str, *, extra: Optional[Dict[str, Any]] = None):
        value = {"action": action}
        if extra:
            value.update(extra)

        action_obj = SimpleNamespace(value=value, form_value=None)
        operator = SimpleNamespace(open_id="u_test")
        context = SimpleNamespace(open_chat_id="chat_test", open_message_id="msg_test")
        event = SimpleNamespace(event=SimpleNamespace(action=action_obj, operator=operator, context=context))
        return event

    async def test_group_show_recovery_route(self):
        event = self._make_event("group_show_recovery")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_group_show_recovery", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", message_id="msg_test")
            mock_tip.assert_awaited_once()

    async def test_group_reconnect_original_route(self):
        event = self._make_event("group_reconnect_original")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_group_reconnect_original", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", message_id="msg_test")
            mock_tip.assert_awaited_once()

    async def test_group_choose_takeover_route(self):
        event = self._make_event("group_choose_takeover")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_group_choose_takeover", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", message_id="msg_test")
            mock_tip.assert_awaited_once()

    async def test_group_takeover_session_route(self):
        event = self._make_event("group_takeover_session", extra={"session": "s_test"})

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_group_takeover_session", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", "s_test", message_id="msg_test")
            mock_tip.assert_awaited_once()

    async def test_group_summarize_now_route(self):
        event = self._make_event("group_summarize_now")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_summarize_now", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", message_id="msg_test")
            mock_tip.assert_awaited_once()

    async def test_view_round_diff_route(self):
        event = self._make_event("view_round_diff")

        with patch("lark_client.main.check_user_allowed", return_value=True), \
             patch("lark_client.main.card_service.send_text", new=AsyncMock()) as mock_tip, \
             patch.object(lark_main.handler, "_cmd_view_round_diff", new=AsyncMock()) as mock_cmd:
            ret = lark_main.handle_card_action(event)
            self.assertIsNone(ret)
            await asyncio.sleep(0)
            mock_cmd.assert_awaited_once_with("u_test", "chat_test", message_id="msg_test")
            mock_tip.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
