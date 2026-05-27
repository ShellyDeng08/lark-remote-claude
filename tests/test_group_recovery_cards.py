#!/usr/bin/env python3
"""
群聊恢复卡片构建单元测试

覆盖：
1. 群菜单卡片在线/离线状态文案与恢复按钮动作
2. 恢复卡片按钮动作与禁用状态
3. 可接管会话列表动作映射（group_takeover_session）
4. 查看本轮变更按钮注入（view_round_diff）
"""

import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).parent.parent))

from lark_client.card_builder import build_group_menu_card, build_group_recovery_card, build_status_card


def _collect_buttons(node: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(node, dict):
        if node.get("tag") == "button":
            out.append(node)
        for v in node.values():
            out.extend(_collect_buttons(v))
    elif isinstance(node, list):
        for item in node:
            out.extend(_collect_buttons(item))
    return out


def _button_action(btn: Dict[str, Any]) -> str:
    behaviors = btn.get("behaviors") or []
    if not behaviors:
        return ""
    value = behaviors[0].get("value") or {}
    return str(value.get("action", ""))


def _button_text(btn: Dict[str, Any]) -> str:
    text = btn.get("text") or {}
    return str(text.get("content", ""))


class TestGroupRecoveryCards(unittest.TestCase):

    def test_group_menu_offline_recovery_button(self):
        card = build_group_menu_card(
            "s1",
            connected=False,
            status="offline",
            reason="会话不可连接",
        )

        body = card.get("body", {})
        markdown = "\n".join(
            str(e.get("content", ""))
            for e in (body.get("elements") or [])
            if isinstance(e, dict) and e.get("tag") == "markdown"
        )
        self.assertIn("状态：离线", markdown)
        self.assertIn("原因：会话不可连接", markdown)

        buttons = _collect_buttons(card)
        by_action = {_button_action(b): b for b in buttons}

        self.assertIn("group_show_recovery", by_action)
        self.assertEqual(_button_text(by_action["group_show_recovery"]), "🔗 恢复会话")
        self.assertIn("group_summarize_now", by_action)
        self.assertTrue(bool(by_action["group_summarize_now"].get("disabled", False)))

    def test_group_menu_connected_refresh_button(self):
        card = build_group_menu_card("s1", connected=True, status="active")
        buttons = _collect_buttons(card)

        refresh = [b for b in buttons if _button_action(b) == "menu_open" and _button_text(b) == "⚡ 刷新菜单"]
        self.assertEqual(len(refresh), 1)

        summarize = [b for b in buttons if _button_action(b) == "group_summarize_now"]
        self.assertEqual(len(summarize), 1)
        self.assertFalse(bool(summarize[0].get("disabled", False)))

        view_diff = [b for b in buttons if _button_action(b) == "view_round_diff"]
        self.assertGreaterEqual(len(view_diff), 1)

    def test_group_menu_suspect_offline_show_checking_button(self):
        card = build_group_menu_card("s1", connected=False, status="suspect_offline", reason="连接不稳定")
        buttons = _collect_buttons(card)

        checking = [b for b in buttons if _button_text(b) == "⏳ 检测中"]
        self.assertEqual(len(checking), 1)
        self.assertEqual(_button_action(checking[0]), "menu_open")

    def test_group_menu_terminated_prefers_takeover(self):
        card = build_group_menu_card("s1", connected=False, status="offline", reason="会话 s1 已终止")
        buttons = _collect_buttons(card)
        takeover = [b for b in buttons if _button_text(b) == "📋 接管会话"]
        self.assertEqual(len(takeover), 1)
        self.assertEqual(_button_action(takeover[0]), "group_choose_takeover")

    def test_group_recovery_card_disable_reconnect_when_unbound(self):
        card = build_group_recovery_card(None, reason="未绑定会话")
        buttons = _collect_buttons(card)

        reconnect = [b for b in buttons if _button_action(b) == "group_reconnect_original"]
        self.assertEqual(len(reconnect), 1)
        self.assertTrue(bool(reconnect[0].get("disabled", False)))

    def test_group_recovery_card_terminated_prefers_takeover(self):
        card = build_group_recovery_card("s1", reason="会话 s1 已终止")
        buttons = _collect_buttons(card)

        reconnect = [b for b in buttons if _button_action(b) == "group_reconnect_original"]
        self.assertEqual(len(reconnect), 0)

        takeover = [b for b in buttons if _button_text(b) == "📋 接管其他会话"]
        self.assertEqual(len(takeover), 1)
        self.assertEqual(_button_action(takeover[0]), "group_choose_takeover")

    def test_group_recovery_card_sessions_takeover_actions(self):
        sessions = [
            {"name": "s1", "cwd": "/tmp/p1", "start_time": "10:00"},
            {"name": "s2", "cwd": "/tmp/p2", "start_time": "10:01"},
        ]
        card = build_group_recovery_card("origin", reason="离线", sessions=sessions)
        buttons = _collect_buttons(card)

        takeover_buttons = [b for b in buttons if _button_action(b) == "group_takeover_session"]
        self.assertEqual(len(takeover_buttons), 2)

        sessions_in_btn = {
            (b.get("behaviors") or [{}])[0].get("value", {}).get("session")
            for b in takeover_buttons
        }
        self.assertEqual(sessions_in_btn, {"s1", "s2"})

    def test_status_card_offline_hide_diff_show_help(self):
        card = build_status_card(False)
        buttons = _collect_buttons(card)
        actions = {_button_action(b) for b in buttons}
        self.assertIn("menu_open", actions)
        self.assertIn("menu_help", actions)
        self.assertNotIn("view_round_diff", actions)


if __name__ == "__main__":
    unittest.main(verbosity=2)
