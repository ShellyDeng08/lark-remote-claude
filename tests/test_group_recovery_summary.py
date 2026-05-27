#!/usr/bin/env python3
"""
群聊恢复与滚动总结相关单元测试

覆盖：
1. 恢复上下文包 checkpoint 回退（无 summary 时回退本地 last_summary）
2. 恢复消息过滤（summary/recovery/system 噪声不进入 messages）
3. 自动总结阈值（按 filtered_count 增量 80 条触发）
4. /summarize-now 在恢复进行中的拦截提示
5. 恢复幂等（30s 内同会话重复点击返回最近结果）
6. 查看本轮变更（view_round_diff）卡片构建与非 Git 兜底
7. 自动总结优先走 AI 生成，失败回退模板
"""

import asyncio
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

from lark_client.lark_handler import LarkHandler


class _DummyBridge:
    def __init__(self, running=True, client_id='c_dummy'):
        self.running = running
        self.client_id = client_id


class _DummyBridgeWithInput(_DummyBridge):
    def __init__(self, running=True):
        super().__init__(running=running)
        self.sent_inputs = []

    async def send_input(self, text: str):
        self.sent_inputs.append(text)
        return True


class TestGroupRecoverySummary(unittest.IsolatedAsyncioTestCase):

    def _make_handler(self):
        h = LarkHandler.__new__(LarkHandler)
        h._group_chat_ids = set()
        h._group_recovery_inflight = {}
        h._group_recovery_last_result = {}
        h._group_summary_locks = {}
        h._group_recovery_locks = {}
        h._group_meta = {}
        h._group_offline_probe_tasks = {}
        h._round_diff_baseline = {}

        h._chat_bindings = {}
        h._chat_sessions = {}
        h._bridges = {}
        h._detached_slices = {}

        h._poller = MagicMock()

        # 持久化与外部副作用全部 mock 掉
        h._save_group_meta = MagicMock()
        h._save_chat_bindings = MagicMock()
        h._save_group_chat_ids = MagicMock()

        # 在部分测试里会覆盖
        h._read_snapshot = MagicMock(return_value=None)
        h._cmd_menu = AsyncMock()
        h._attach = AsyncMock(return_value=True)
        h._verify_group_recovery_ready = AsyncMock(return_value=True)
        return h

    def test_build_recovery_context_fallback_to_last_summary(self):
        h = self._make_handler()
        h._group_meta = {
            'chat1': {
                'last_summary': '[RC-SUMMARY v1 #9]\n- 当前状态：等待继续',
            }
        }
        snapshot = {
            'blocks': [
                {'_type': 'UserInput', 'text': '[张三|ou_xxx] 请继续'},
                {'_type': 'OutputBlock', 'content': '好的，开始处理'},
            ]
        }

        payload = h._build_recovery_context_package('chat1', snapshot)
        self.assertIn('checkpoint:\n[RC-SUMMARY v1 #9]', payload)
        self.assertIn('role=user: [张三|ou_xxx] 请继续', payload)
        self.assertIn('role=assistant: 好的，开始处理', payload)

    def test_recovery_messages_filter_summary_and_recovery_context(self):
        h = self._make_handler()
        blocks = [
            {'_type': 'OutputBlock', 'content': '[RC-SUMMARY v1 #1]\n- ...'},
            {'_type': 'OutputBlock', 'content': '[RECOVERY_CONTEXT v1]\n...'},
            {'_type': 'UserInput', 'text': '[RECOVERY_CONTEXT v1]\n这是历史回放，不是新的用户请求；请不要重复执行历史步骤。'},
            {'_type': 'SystemBlock', 'content': '系统播报'},
            {'_type': 'UserInput', 'text': '[Alice|ou_1] 请修这个 bug'},
            {'_type': 'OutputBlock', 'content': '已修复，见最新提交'},
        ]

        msgs = h._build_recovery_messages_from_blocks(blocks)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['role'], 'user')
        self.assertIn('[Alice|ou_1]', msgs[0]['content'])
        self.assertEqual(msgs[1]['role'], 'assistant')
        self.assertIn('已修复', msgs[1]['content'])

    def test_recovery_messages_filter_recovery_ack_output(self):
        h = self._make_handler()
        blocks = [
            {'_type': 'OutputBlock', 'content': '收到，这也是恢复回放，不是新指令。\n我不会重复执行历史步骤，当前状态保持不变，随时可以继续。'},
            {'_type': 'UserInput', 'text': '[Alice|ou_1] 这部分我觉得还要再完善一下'},
            {'_type': 'OutputBlock', 'content': '好的，我按你说的补齐和前面三个效率的对齐关系。'},
        ]

        msgs = h._build_recovery_messages_from_blocks(blocks)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]['role'], 'user')
        self.assertIn('还要再完善', msgs[0]['content'])
        self.assertEqual(msgs[1]['role'], 'assistant')
        self.assertIn('补齐和前面三个效率', msgs[1]['content'])

    async def test_emit_group_summary_threshold_by_filtered_count(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        h._group_meta = {
            'chat1': {
                'summary_filtered_count': 60,  # 上次计数
                'summary_seq': 1,
            }
        }

        # 当前只有 100 条有效（不足 60+80=140），不应触发
        h._read_snapshot = MagicMock(return_value={
            'blocks': [{'_type': 'UserInput', 'text': f'u{i}'} for i in range(100)]
        })

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock(return_value='m1')
            ok = await h._emit_group_summary('chat1', force=False)
            self.assertFalse(ok)
            mock_cs.send_text.assert_not_called()

            # force=True 应触发发送
            ok2 = await h._emit_group_summary('chat1', force=True, trigger='manual')
            self.assertTrue(ok2)
            mock_cs.send_text.assert_called_once()
            summary = mock_cs.send_text.call_args.args[1]
            self.assertIn('触发方式：手动触发', summary)
            self.assertIn('触发依据：自上次总结后新增有效消息约', summary)
            self.assertIn('阈值 80', summary)
            # 发送成功后应更新本地 summary 元信息
            self.assertGreaterEqual(h._group_meta['chat1'].get('summary_seq', 0), 2)
            self.assertGreaterEqual(h._group_meta['chat1'].get('summary_filtered_count', 0), 100)

    async def test_emit_group_summary_auto_trigger_marked_as_auto(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridgeWithInput(running=True)}
        h._group_meta = {
            'chat1': {
                'summary_filtered_count': 0,
                'summary_seq': 0,
            }
        }

        blocks = [
            {'_type': 'UserInput', 'text': '[Alice|ou_1] 请继续推进'},
            {'_type': 'OutputBlock', 'content': '好的，我开始处理。'},
        ] + [{'_type': 'UserInput', 'text': f'u{i}'} for i in range(90)]
        h._read_snapshot = MagicMock(return_value={'blocks': blocks})
        h._wait_ai_group_summary = AsyncMock(return_value='\n'.join([
            '[RC-SUMMARY v1 #1]',
            '- 触发方式：自动触发（新增消息达到阈值）',
            '- 目标与范围：围绕当前会话目标继续推进',
            '- 最近用户意图：请继续推进当前任务',
            '- 最近助手进展：已开始处理并给出下一步',
            '- 已完成：近段 assistant 输出约 1 条',
            '- 当前状态：会话可继续推进',
            '- 未决问题：仍有用户输入待处理',
            '- 触发依据：自上次总结后新增有效消息约 92 条（阈值 80）',
            '- 关键约束：群聊恢复为近似恢复，不保证原进程内存级一致',
            '- 下一步：从最后一条 user 意图继续执行',
        ]))

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock(return_value='m2')
            ok = await h._emit_group_summary('chat1', force=False, trigger='auto')
            self.assertTrue(ok)
            mock_cs.send_text.assert_called_once()
            summary = mock_cs.send_text.call_args.args[1]
            self.assertIn('触发方式：自动触发', summary)
            self.assertIn('最近用户意图：', summary)
            self.assertIn('最近助手进展：', summary)

    async def test_maybe_emit_group_summary_after_delay_uses_auto_trigger(self):
        h = self._make_handler()
        h._emit_group_summary = AsyncMock(return_value=True)
        with patch('lark_client.lark_handler.asyncio.sleep', new=AsyncMock()):
            await h._maybe_emit_group_summary_after_delay('chat1', delay_seconds=0.6)
            h._emit_group_summary.assert_awaited_once_with('chat1', force=False, trigger='auto')

    async def test_cmd_summarize_now_blocked_when_recovering(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._group_recovery_inflight = {'chat1': {'request_id': 'rid-1'}}

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_summarize_now('u1', 'chat1')
            mock_cs.send_text.assert_called_once()
            self.assertIn('正在恢复会话', mock_cs.send_text.call_args.args[1])
            self.assertIn('request_id=rid-1', mock_cs.send_text.call_args.args[1])

    async def test_cmd_summarize_now_blocked_when_recovering_update_card_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._group_recovery_inflight = {'chat1': {'request_id': 'rid-1'}}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_summarize_now('u1', 'chat1', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('恢复进行中', args.kwargs.get('reason_text', ''))
            mock_cs.send_text.assert_not_called()

    async def test_cmd_summarize_now_blocked_when_offline(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.card_service'):
            h._emit_group_summary = AsyncMock(return_value=True)
            await h._cmd_summarize_now('u1', 'chat1')
            h._cmd_group_show_recovery.assert_awaited_once_with(
                'u1',
                'chat1',
                message_id=None,
                reason_text='当前会话离线，请先恢复会话后再总结。',
            )
            h._emit_group_summary.assert_not_awaited()

    async def test_cmd_summarize_now_blocked_when_summary_running(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        lock = asyncio.Lock()
        await lock.acquire()
        h._group_summary_locks = {'chat1': lock}

        try:
            with patch('lark_client.lark_handler.card_service') as mock_cs:
                mock_cs.send_text = AsyncMock()
                h._emit_group_summary = AsyncMock(return_value=True)
                await h._cmd_summarize_now('u1', 'chat1')
                mock_cs.send_text.assert_called_once()
                self.assertIn('总结任务进行中', mock_cs.send_text.call_args.args[1])
                h._emit_group_summary.assert_not_awaited()
        finally:
            lock.release()

    async def test_cmd_status_group_offline_show_recovery(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.build_status_card') as mock_status:
            await h._cmd_status('u1', 'chat1')
            h._cmd_group_show_recovery.assert_awaited_once_with(
                'u1',
                'chat1',
                message_id=None,
                reason_text='当前群未连接会话，请先恢复或接管会话。',
            )
            mock_status.assert_not_called()

    async def test_cmd_attach_group_without_session_open_takeover(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._cmd_group_choose_takeover = AsyncMock()

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_attach('u1', 'chat1', '')
            h._cmd_group_choose_takeover.assert_awaited_once_with('u1', 'chat1', message_id=None)
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_cmd_attach_group_with_session_warn_and_switch(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}

        with patch('lark_client.lark_handler.card_service') as mock_cs, \
             patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 's2'}]):
            mock_cs.send_text = AsyncMock()
            await h._cmd_attach('u1', 'chat1', 's2')
            mock_cs.send_text.assert_called_once()
            tip = mock_cs.send_text.call_args.args[1]
            self.assertIn('群聊切换会话会影响当前 AI 工作上下文', tip)
            h._attach.assert_awaited_once_with('chat1', 's2', user_id='u1')
            self.assertEqual(h._chat_bindings.get('chat1'), 's2')

    async def test_cmd_detach_group_marks_offline(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_sessions = {'chat1': 's1'}
        h._detach = AsyncMock()

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_detach('u1', 'chat1')
            h._detach.assert_awaited_once_with('chat1')
            self.assertEqual(h._group_meta['chat1'].get('status'), 'offline')
            self.assertIn('手动断开连接', h._group_meta['chat1'].get('reason', ''))
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id=None)

    async def test_group_show_recovery_when_connected_redirect_menu(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._chat_sessions = {'chat1': 's1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}

        with patch('lark_client.lark_handler.build_group_recovery_card') as mock_build:
            await h._cmd_group_show_recovery('u1', 'chat1', message_id='m1')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_build.assert_not_called()

    async def test_group_reconnect_original_when_connected_noop(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._chat_sessions = {'chat1': 's1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1')
            mock_cs.send_text.assert_called_once()
            self.assertIn('已在线，无需恢复', mock_cs.send_text.call_args.args[1])
            h._attach.assert_not_called()

    async def test_attach_same_session_reuse_connection(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_sessions = {'chat1': 's1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        h._stop_poller = MagicMock(side_effect=AssertionError('should not stop poller'))
        h._cancel_group_offline_probe = MagicMock()
        h._set_group_status = MagicMock()
        h._capture_round_diff_baseline_for_chat = MagicMock()

        ok = await LarkHandler._attach(h, 'chat1', 's1', user_id='u1')
        self.assertTrue(ok)
        h._cancel_group_offline_probe.assert_called_once_with('chat1')
        h._set_group_status.assert_called_once_with('chat1', 's1', 'active')
        h._capture_round_diff_baseline_for_chat.assert_called_once_with('chat1', 's1')

    async def test_verify_group_recovery_ready_success(self):
        h = self._make_handler()
        h._verify_group_recovery_ready = LarkHandler._verify_group_recovery_ready.__get__(h, LarkHandler)
        h._bridges = {'chat1': _DummyBridge(running=True)}
        h._chat_sessions = {'chat1': 's1'}
        h._read_snapshot = MagicMock(side_effect=[None, {'blocks': []}])

        with patch('lark_client.lark_handler.asyncio.sleep', new=AsyncMock()):
            ok = await h._verify_group_recovery_ready('chat1', 's1', timeout_seconds=0.3, interval_seconds=0.01)

        self.assertTrue(ok)

    async def test_verify_group_recovery_ready_timeout(self):
        h = self._make_handler()
        h._verify_group_recovery_ready = LarkHandler._verify_group_recovery_ready.__get__(h, LarkHandler)
        h._bridges = {'chat1': _DummyBridge(running=False)}
        h._chat_sessions = {'chat1': 's1'}
        h._read_snapshot = MagicMock(return_value=None)

        with patch('lark_client.lark_handler.asyncio.sleep', new=AsyncMock()):
            ok = await h._verify_group_recovery_ready('chat1', 's1', timeout_seconds=0.05, interval_seconds=0.01)

        self.assertFalse(ok)

    async def test_cmd_summarize_now_blocked_when_summary_running_update_menu_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        lock = asyncio.Lock()
        await lock.acquire()
        h._group_summary_locks = {'chat1': lock}

        try:
            with patch('lark_client.lark_handler.card_service') as mock_cs:
                mock_cs.send_text = AsyncMock()
                h._emit_group_summary = AsyncMock(return_value=True)
                await h._cmd_summarize_now('u1', 'chat1', message_id='m1')
                h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
                mock_cs.send_text.assert_not_called()
                h._emit_group_summary.assert_not_awaited()
        finally:
            lock.release()

    async def test_cmd_summarize_now_update_menu_on_message_id_when_ok(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        h._emit_group_summary = AsyncMock(return_value=True)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_summarize_now('u1', 'chat1', message_id='m1')
            h._emit_group_summary.assert_awaited_once_with('chat1', force=True, trigger='manual')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()

    async def test_cmd_summarize_now_update_menu_on_message_id_when_failed(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}
        h._emit_group_summary = AsyncMock(return_value=False)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_summarize_now('u1', 'chat1', message_id='m1')
            h._emit_group_summary.assert_awaited_once_with('chat1', force=True, trigger='manual')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()

    async def test_reconnect_idempotent_recent_result(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._group_recovery_last_result = {
            'chat1': {
                'request_id': 'rid-last',
                'ok': True,
                'session': 's1',
                'ts': int(time.time()),
            }
        }

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1')
            mock_cs.send_text.assert_called_once()
            self.assertIn('最近恢复结果', mock_cs.send_text.call_args.args[1])
            h._attach.assert_not_called()

    async def test_reconnect_idempotent_recent_result_update_card_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._group_recovery_last_result = {
            'chat1': {
                'request_id': 'rid-last',
                'ok': True,
                'session': 's1',
                'ts': int(time.time()),
            }
        }

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1', message_id='m1')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_reconnect_idempotent_recent_failed_result_update_recovery_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._group_recovery_last_result = {
            'chat1': {
                'request_id': 'rid-last',
                'ok': False,
                'session': 's1',
                'ts': int(time.time()),
            }
        }

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('最近恢复结果', args.kwargs.get('reason_text', ''))
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_reconnect_inflight_update_recovery_card_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._group_recovery_inflight = {'chat1': {'request_id': 'rid-1'}}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('恢复进行中', args.kwargs.get('reason_text', ''))
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_reconnect_fresh_request_update_recovery_then_menu_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._inject_recovery_context = AsyncMock(return_value=True)
        h._attach = AsyncMock(return_value=True)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('恢复进行中', args.kwargs.get('reason_text', ''))
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()

    async def test_reconnect_fresh_request_when_not_ready_show_recovery_card(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._inject_recovery_context = AsyncMock(return_value=True)
        h._attach = AsyncMock(return_value=True)
        h._verify_group_recovery_ready = AsyncMock(return_value=False)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_reconnect_original('u1', 'chat1', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited()
            last_args = h._cmd_group_show_recovery.await_args
            self.assertIn('恢复校验失败', last_args.kwargs.get('reason_text', ''))
            self.assertEqual(last_args.kwargs.get('message_id'), 'm1')
            h._cmd_menu.assert_not_awaited()
            self.assertEqual(h._group_meta['chat1'].get('status'), 'offline')
            self.assertEqual(h._group_recovery_last_result['chat1'].get('reason'), 'not_ready')
            mock_cs.send_text.assert_not_called()

    async def test_takeover_idempotent_recent_result_update_card_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._group_recovery_last_result = {
            'chat1': {
                'request_id': 'rid-last',
                'ok': True,
                'session': 's2',
                'ts': int(time.time()),
            }
        }

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_takeover_session('u1', 'chat1', 's2', message_id='m1')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_takeover_idempotent_recent_failed_result_update_recovery_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._group_recovery_last_result = {
            'chat1': {
                'request_id': 'rid-last',
                'ok': False,
                'session': 's2',
                'ts': int(time.time()),
            }
        }

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_takeover_session('u1', 'chat1', 's2', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('最近恢复结果', args.kwargs.get('reason_text', ''))
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_takeover_inflight_update_recovery_card_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._group_recovery_inflight = {'chat1': {'request_id': 'rid-2'}}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_takeover_session('u1', 'chat1', 's2', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('恢复进行中', args.kwargs.get('reason_text', ''))
            mock_cs.send_text.assert_not_called()
            h._attach.assert_not_called()

    async def test_takeover_fresh_request_update_recovery_then_menu_on_message_id(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._inject_recovery_context = AsyncMock(return_value=True)
        h._attach = AsyncMock(return_value=True)

        with patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 's2'}]), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_takeover_session('u1', 'chat1', 's2', message_id='m1')
            h._cmd_group_show_recovery.assert_awaited_once()
            args = h._cmd_group_show_recovery.await_args
            self.assertEqual(args.kwargs.get('message_id'), 'm1')
            self.assertIn('恢复进行中', args.kwargs.get('reason_text', ''))
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1', message_id='m1')
            mock_cs.send_text.assert_not_called()

    async def test_takeover_fresh_request_when_not_ready_show_recovery_card(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._cmd_group_show_recovery = AsyncMock()
        h._inject_recovery_context = AsyncMock(return_value=True)
        h._attach = AsyncMock(return_value=True)
        h._verify_group_recovery_ready = AsyncMock(return_value=False)
        h._send_or_update_card = AsyncMock()

        with patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 's2'}]), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_group_takeover_session('u1', 'chat1', 's2', message_id='m1')
            h._cmd_menu.assert_not_awaited()
            h._send_or_update_card.assert_awaited_once()
            self.assertEqual(h._group_meta['chat1'].get('status'), 'offline')
            self.assertEqual(h._group_recovery_last_result['chat1'].get('reason'), 'not_ready')
            mock_cs.send_text.assert_not_called()

    async def test_handle_message_empty_text_show_group_entry_recovery(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._bridges = {}
        h._cmd_group_show_recovery = AsyncMock()

        with patch('lark_client.lark_handler.card_service'):
            await h.handle_message('u1', 'chat1', '   ', chat_type='group')
            h._cmd_group_show_recovery.assert_awaited_once()

    async def test_handle_message_empty_text_show_group_menu_when_connected(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._bridges = {'chat1': _DummyBridge(running=True)}

        with patch('lark_client.lark_handler.card_service'):
            await h.handle_message('u1', 'chat1', '   ', chat_type='group')
            h._cmd_menu.assert_awaited_once_with('u1', 'chat1')

    async def test_probe_group_offline_attach_success_back_to_active(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._attach = AsyncMock(return_value=True)

        with patch('lark_client.lark_handler.asyncio.sleep', new=AsyncMock()), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.create_and_send_card = AsyncMock()
            await h._probe_group_offline('chat1', 's1', user_id='u1', reason='连接断开')

            self.assertEqual(h._group_meta['chat1'].get('status'), 'active')
            mock_cs.create_and_send_card.assert_not_called()

    async def test_on_disconnect_ignores_stale_bridge_callback(self):
        h = self._make_handler()
        h._bridges = {'chat1': _DummyBridge(running=True, client_id='new_client')}
        h._stop_poller = MagicMock(return_value=None)
        h._remove_binding_by_chat = MagicMock()

        await h._on_disconnect('chat1', 's1', expected_client_id='old_client')

        h._stop_poller.assert_not_called()
        h._remove_binding_by_chat.assert_not_called()
        self.assertIn('chat1', h._bridges)

    async def test_unknown_slash_command_forwarded_to_claude(self):
        h = self._make_handler()
        h._forward_to_claude = AsyncMock()

        await LarkHandler._handle_command(h, 'u1', 'chat1', '/skills')
        h._forward_to_claude.assert_awaited_once_with('u1', 'chat1', '/skills')

    async def test_forward_to_claude_warn_when_not_reflected(self):
        h = self._make_handler()
        h._bridges = {'chat1': _DummyBridgeWithInput(running=True)}
        h._chat_sessions = {'chat1': 's1'}
        h._group_chat_ids = set()
        h._poller = MagicMock()
        h._poller.freeze_current_card = AsyncMock(return_value='f1')
        h._kick_poller = MagicMock()
        h._read_snapshot = MagicMock(return_value={'blocks': []})
        h._count_user_input_blocks = MagicMock(return_value=0)
        h._wait_user_input_reflected = AsyncMock(return_value=False)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            mock_cs.send_interactive_card = AsyncMock()
            await h._forward_to_claude('u1', 'chat1', 'hello world')

            self.assertEqual(h._bridges['chat1'].sent_inputs, ['hello world'])
            h._kick_poller.assert_called_once_with('chat1')
            mock_cs.send_text.assert_called_once()
            self.assertIn('输入未在终端侧确认落盘', mock_cs.send_text.call_args.args[1])

    async def test_forward_to_claude_no_warn_when_reflected(self):
        h = self._make_handler()
        h._bridges = {'chat1': _DummyBridgeWithInput(running=True)}
        h._chat_sessions = {'chat1': 's1'}
        h._group_chat_ids = set()
        h._poller = MagicMock()
        h._poller.freeze_current_card = AsyncMock(return_value='f1')
        h._kick_poller = MagicMock()
        h._read_snapshot = MagicMock(return_value={'blocks': []})
        h._count_user_input_blocks = MagicMock(return_value=0)
        h._wait_user_input_reflected = AsyncMock(return_value=True)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            mock_cs.send_interactive_card = AsyncMock()
            await h._forward_to_claude('u1', 'chat1', 'hello world')

            self.assertEqual(h._bridges['chat1'].sent_inputs, ['hello world'])
            h._kick_poller.assert_called_once_with('chat1')
            mock_cs.send_text.assert_not_called()

    async def test_forward_to_claude_warn_when_send_timeout(self):
        h = self._make_handler()
        b = _DummyBridgeWithInput(running=True)
        b.send_input = AsyncMock(side_effect=asyncio.TimeoutError())
        h._bridges = {'chat1': b}
        h._chat_sessions = {'chat1': 's1'}
        h._group_chat_ids = set()
        h._poller = MagicMock()
        h._poller.freeze_current_card = AsyncMock(return_value='f1')
        h._kick_poller = MagicMock()
        h._read_snapshot = MagicMock(return_value={'blocks': []})
        h._count_user_input_blocks = MagicMock(return_value=0)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            mock_cs.send_interactive_card = AsyncMock()
            await h._forward_to_claude('u1', 'chat1', 'hello world')

            h._kick_poller.assert_not_called()
            mock_cs.send_text.assert_called_once()
            self.assertIn('消息投递超时', mock_cs.send_text.call_args.args[1])

    async def test_forward_to_claude_warn_when_send_exception(self):
        h = self._make_handler()
        b = _DummyBridgeWithInput(running=True)
        b.send_input = AsyncMock(side_effect=RuntimeError('boom'))
        h._bridges = {'chat1': b}
        h._chat_sessions = {'chat1': 's1'}
        h._group_chat_ids = set()
        h._poller = MagicMock()
        h._poller.freeze_current_card = AsyncMock(return_value='f1')
        h._kick_poller = MagicMock()
        h._read_snapshot = MagicMock(return_value={'blocks': []})
        h._count_user_input_blocks = MagicMock(return_value=0)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            mock_cs.send_interactive_card = AsyncMock()
            await h._forward_to_claude('u1', 'chat1', 'hello world')

            h._kick_poller.assert_not_called()
            mock_cs.send_text.assert_called_once()
            self.assertIn('消息投递失败：发送过程中发生异常', mock_cs.send_text.call_args.args[1])

    async def test_forward_to_claude_warn_when_send_returns_false(self):
        h = self._make_handler()
        b = _DummyBridgeWithInput(running=True)
        b.send_input = AsyncMock(return_value=False)
        h._bridges = {'chat1': b}
        h._chat_sessions = {'chat1': 's1'}
        h._group_chat_ids = set()
        h._poller = MagicMock()
        h._poller.freeze_current_card = AsyncMock(return_value='f1')
        h._kick_poller = MagicMock()
        h._read_snapshot = MagicMock(return_value={'blocks': []})
        h._count_user_input_blocks = MagicMock(return_value=0)

        with patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            mock_cs.send_interactive_card = AsyncMock()
            await h._forward_to_claude('u1', 'chat1', 'hello world')

            h._kick_poller.assert_not_called()
            mock_cs.send_text.assert_called_once()
            self.assertIn('连接可能已断开', mock_cs.send_text.call_args.args[1])

    async def test_handle_message_normalizes_invisible_chars_before_slash_command(self):
        h = self._make_handler()
        h._handle_command = AsyncMock()

        await h.handle_message('u1', 'chat1', '\ufeff\u200b/skills', chat_type='group')
        h._handle_command.assert_awaited_once_with('u1', 'chat1', '/skills')

    async def test_handle_message_normalizes_fullwidth_slash_command(self):
        h = self._make_handler()
        h._handle_command = AsyncMock()

        await h.handle_message('u1', 'chat1', '／skills', chat_type='group')
        h._handle_command.assert_awaited_once_with('u1', 'chat1', '/skills')

    async def test_handle_message_normalizes_fullwidth_exclamation_for_bash_mode(self):
        h = self._make_handler()
        h._forward_to_claude = AsyncMock()

        await h.handle_message('u1', 'chat1', '！pwd', chat_type='group')
        h._forward_to_claude.assert_awaited_once_with('u1', 'chat1', '!pwd')

    async def test_probe_group_offline_attach_fail_mark_offline_and_send_card(self):
        h = self._make_handler()
        h._group_chat_ids = {'chat1'}
        h._chat_bindings = {'chat1': 's1'}
        h._attach = AsyncMock(return_value=False)

        with patch('lark_client.lark_handler.asyncio.sleep', new=AsyncMock()), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.create_and_send_card = AsyncMock()
            await h._probe_group_offline('chat1', 's1', user_id='u1', reason='连接断开')

            self.assertEqual(h._group_meta['chat1'].get('status'), 'offline')
            self.assertIn('连接断开', h._group_meta['chat1'].get('reason', ''))
            mock_cs.create_and_send_card.assert_called_once()

    async def test_cmd_new_group_pushes_entry_card_after_created(self):
        h = self._make_handler()
        h._cmd_list = AsyncMock()
        h._show_group_entry_card = AsyncMock()

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                import json as _json
                return _json.dumps(self._payload).encode('utf-8')

        token_resp = _Resp({'tenant_access_token': 't'})
        create_resp = _Resp({'code': 0, 'data': {'chat_id': 'chat_new'}})

        with patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 's1', 'pid': None, 'cwd': '/tmp', 'start_time': '10:00'}]), \
             patch('urllib.request.urlopen', side_effect=[token_resp, create_resp]), \
             patch('lark_client.lark_handler.card_service'):
            await h._cmd_new_group('u1', 'chat_dm', 's1')
            h._show_group_entry_card.assert_awaited_once_with('u1', 'chat_new')

    async def test_view_round_diff_non_git_fallback_text(self):
        h = self._make_handler()
        h._chat_bindings = {'chat1': 's1'}

        non_git = MagicMock(returncode=1, stdout='')
        with patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 's1', 'cwd': '/tmp/non-git'}]), \
             patch('lark_client.lark_handler.subprocess.run', return_value=non_git), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_view_round_diff('u1', 'chat1')
            mock_cs.send_text.assert_called_once()
            self.assertIn('不是 Git 仓库', mock_cs.send_text.call_args.args[1])

    async def test_view_round_diff_build_card_and_update_by_message_id(self):
        h = self._make_handler()
        h._chat_bindings = {'chat1': 'sA'}
        h._send_or_update_card = AsyncMock()
        h._round_diff_baseline = {
            'chat1': {
                'session_name': 'sA',
                'repo_root': '/repo/A',
                'baseline_head': 'abc123',
                'baseline_status_lines': [],
                'baseline_diff': '',
                'captured_at': 1710000000,
            }
        }

        run_results = [
            MagicMock(returncode=0, stdout='true\n'),
            MagicMock(returncode=0, stdout='def456\n'),
            MagicMock(returncode=0, stdout=' M a.py\n?? tests/test_main_card_actions.py\n'),
            MagicMock(returncode=0, stdout='diff --git a/a.py b/a.py\n+added staged\n'),
            MagicMock(returncode=0, stdout='diff --git a/b.py b/b.py\n+added unstaged\n'),
        ]
        h._git_run = MagicMock(side_effect=run_results)

        with patch('lark_client.lark_handler.list_active_sessions', return_value=[{'name': 'sA', 'cwd': '/repo/A'}]), \
             patch('lark_client.lark_handler.card_service') as mock_cs:
            mock_cs.send_text = AsyncMock()
            await h._cmd_view_round_diff('u1', 'chat1', message_id='m1')

            h._send_or_update_card.assert_awaited_once()
            args = h._send_or_update_card.await_args.args
            self.assertEqual(args[0], 'chat1')
            card = args[1]
            self.assertEqual(args[2], 'm1')

            body = card.get('body', {})
            markdown = '\n'.join(
                str(e.get('content', ''))
                for e in (body.get('elements') or [])
                if isinstance(e, dict) and e.get('tag') == 'markdown'
            )
            self.assertIn('会话：`sA`', markdown)
            self.assertIn('仓库路径：`/repo/A`', markdown)
            self.assertIn('本轮新增/变化文件（相对基线）', markdown)
            self.assertIn('本轮关键 Diff（相对基线）', markdown)
            self.assertIn('当前工作区快照（用于对照）', markdown)
            self.assertIn('工作区 Diff（已暂存）', markdown)
            self.assertIn('工作区 Diff（未暂存）', markdown)
            self.assertIn('a.py', markdown)
            mock_cs.send_text.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
