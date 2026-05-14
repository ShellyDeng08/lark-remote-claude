"""
用户 API 调用模块单元测试
"""

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lark_client.oauth_service import LarkOAuthService
from lark_client.user_api import LarkUserApi, UserApiError, TokenExpiredError


class TestLarkUserApi(unittest.TestCase):
    """LarkUserApi 单元测试"""

    def setUp(self):
        self.oauth_service = LarkOAuthService(
            app_id="cli_test_app_id",
            app_secret="test_app_secret",
            redirect_uri="http://localhost:8080/oauth/callback",
        )
        self.api = LarkUserApi(self.oauth_service)

        # 使用临时文件替代真实 token 存储
        self._tmp_dir = tempfile.mkdtemp()
        import lark_client.oauth_service as mod
        self._mod = mod
        self._orig_path = mod.TOKEN_STORAGE_PATH
        mod.TOKEN_STORAGE_PATH = Path(self._tmp_dir) / "user_tokens.json"

        # 保存一个有效 token
        self.oauth_service.save_user_token("ou_user1", {
            "access_token": "u-valid-token",
            "refresh_token": "ur-refresh",
            "expires_in": 7200,
        })

    def tearDown(self):
        self._mod.TOKEN_STORAGE_PATH = self._orig_path
        tmp_path = Path(self._tmp_dir)
        for f in tmp_path.iterdir():
            f.unlink()
        tmp_path.rmdir()

    def _run(self, coro):
        return asyncio.run(coro)

    # ---------- Token 获取 ----------

    def test_get_token_or_raise_success(self):
        """有效用户能获取 token"""
        async def run():
            token = await self.api._get_token_or_raise("ou_user1")
            self.assertEqual(token, "u-valid-token")

        self._run(run())

    def test_get_token_or_raise_no_auth(self):
        """未授权用户抛 TokenExpiredError"""
        async def run():
            with self.assertRaises(TokenExpiredError):
                await self.api._get_token_or_raise("ou_unknown")

        self._run(run())

    # ---------- 发送消息 ----------

    def test_send_message_as_user_success(self):
        """成功以用户身份发送消息"""
        mock_response = {
            "code": 0,
            "data": {"message_id": "om_test_msg_123"},
        }

        async def run():
            with patch.object(self.api, "_request", return_value=mock_response) as mock_req:
                msg_id = await self.api.send_message_as_user("ou_user1", "oc_chat1", "hello")
                self.assertEqual(msg_id, "om_test_msg_123")
                # 验证请求参数
                mock_req.assert_called_once()
                call_args = mock_req.call_args
                self.assertEqual(call_args[0][0], "POST")
                self.assertIn("/im/v1/messages", call_args[0][1])

        self._run(run())

    def test_send_message_as_user_token_expired(self):
        """token 过期抛 TokenExpiredError"""
        async def run():
            with patch.object(self.api, "_request", side_effect=TokenExpiredError("token expired")):
                with self.assertRaises(TokenExpiredError):
                    await self.api.send_message_as_user("ou_user1", "oc_chat1", "hello")

        self._run(run())

    def test_send_message_as_user_api_error(self):
        """API 错误返回 None"""
        async def run():
            with patch.object(self.api, "_request", side_effect=UserApiError("权限不足")):
                result = await self.api.send_message_as_user("ou_user1", "oc_chat1", "hello")
                self.assertIsNone(result)

        self._run(run())

    def test_send_message_no_auth(self):
        """未授权用户发消息抛 TokenExpiredError"""
        async def run():
            with self.assertRaises(TokenExpiredError):
                await self.api.send_message_as_user("ou_unknown", "oc_chat1", "hello")

        self._run(run())

    # ---------- 获取群列表 ----------

    def test_get_user_chats_success(self):
        """成功获取用户群列表"""
        mock_response = {
            "code": 0,
            "data": {
                "items": [
                    {"chat_id": "oc_chat1", "name": "测试群1"},
                    {"chat_id": "oc_chat2", "name": "测试群2"},
                ],
                "page_token": "next_page",
                "has_more": True,
            },
        }

        async def run():
            with patch.object(self.api, "_request", return_value=mock_response):
                result = await self.api.get_user_chats("ou_user1")
                self.assertEqual(len(result["items"]), 2)
                self.assertEqual(result["items"][0]["name"], "测试群1")
                self.assertTrue(result["has_more"])
                self.assertEqual(result["page_token"], "next_page")

        self._run(run())

    def test_get_user_chats_with_pagination(self):
        """分页参数传递正确"""
        mock_response = {"code": 0, "data": {"items": [], "has_more": False}}

        async def run():
            with patch.object(self.api, "_request", return_value=mock_response) as mock_req:
                await self.api.get_user_chats("ou_user1", page_size=10, page_token="abc")
                call_args = mock_req.call_args
                params = call_args[1].get("params") or call_args[0][4] if len(call_args[0]) > 4 else call_args[1].get("params")
                self.assertEqual(params["page_size"], "10")
                self.assertEqual(params["page_token"], "abc")

        self._run(run())

    # ---------- 获取消息历史 ----------

    def test_get_chat_messages_success(self):
        """成功获取消息历史"""
        mock_response = {
            "code": 0,
            "data": {
                "items": [
                    {"message_id": "om_msg1", "msg_type": "text"},
                    {"message_id": "om_msg2", "msg_type": "text"},
                ],
                "has_more": False,
            },
        }

        async def run():
            with patch.object(self.api, "_request", return_value=mock_response):
                result = await self.api.get_chat_messages("ou_user1", "oc_chat1")
                self.assertEqual(len(result["items"]), 2)
                self.assertFalse(result["has_more"])

        self._run(run())

    # ---------- Token 有效性检查 ----------

    def test_check_token_validity_authorized(self):
        """已授权用户的 token 状态"""
        async def run():
            result = await self.api.check_token_validity("ou_user1")
            self.assertTrue(result["authorized"])
            self.assertTrue(result["has_valid_token"])

        self._run(run())

    def test_check_token_validity_not_authorized(self):
        """未授权用户"""
        async def run():
            result = await self.api.check_token_validity("ou_unknown")
            self.assertFalse(result["authorized"])

        self._run(run())

    # ---------- _request 方法 ----------

    def test_request_token_expired_error_code(self):
        """API 返回 token 失效错误码时抛 TokenExpiredError"""
        mock_response = {"code": 99991663, "msg": "token invalid"}

        async def run():
            with patch("lark_client.user_api.urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(mock_response).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                with self.assertRaises(TokenExpiredError):
                    await self.api._request("GET", "/test", "u-token")

        self._run(run())

    def test_request_api_error(self):
        """API 返回业务错误时抛 UserApiError"""
        mock_response = {"code": 230001, "msg": "no permission"}

        async def run():
            with patch("lark_client.user_api.urllib.request.urlopen") as mock_urlopen:
                mock_resp = MagicMock()
                mock_resp.read.return_value = json.dumps(mock_response).encode()
                mock_resp.__enter__ = MagicMock(return_value=mock_resp)
                mock_resp.__exit__ = MagicMock(return_value=False)
                mock_urlopen.return_value = mock_resp

                with self.assertRaises(UserApiError) as ctx:
                    await self.api._request("GET", "/test", "u-token")
                self.assertIn("no permission", str(ctx.exception))

        self._run(run())


if __name__ == "__main__":
    unittest.main()
