"""
OAuth 服务核心模块单元测试
"""

import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lark_client.oauth_service import LarkOAuthService, OAuthError, TOKEN_STORAGE_PATH


class TestLarkOAuthService(unittest.TestCase):
    """LarkOAuthService 单元测试"""

    def setUp(self):
        self.service = LarkOAuthService(
            app_id="cli_test_app_id",
            app_secret="test_app_secret",
            redirect_uri="http://localhost:8080/oauth/callback",
        )
        # 使用临时文件替代真实 token 存储
        self._tmp_dir = tempfile.mkdtemp()
        import lark_client.oauth_service as mod
        self._mod = mod
        self._orig_path = mod.TOKEN_STORAGE_PATH
        mod.TOKEN_STORAGE_PATH = Path(self._tmp_dir) / "user_tokens.json"

    def tearDown(self):
        self._mod.TOKEN_STORAGE_PATH = self._orig_path
        tmp_path = Path(self._tmp_dir)
        for f in tmp_path.iterdir():
            f.unlink()
        tmp_path.rmdir()

    # ---------- state 管理 ----------

    def test_generate_state(self):
        """生成的 state 应该是非空字符串"""
        state = self.service.generate_state()
        self.assertIsInstance(state, str)
        self.assertTrue(len(state) > 10)

    def test_validate_state_success(self):
        """已注册的 state 应该校验通过（一次性消费）"""
        state = self.service.generate_state()
        self.assertTrue(self.service.validate_state(state))
        # 二次使用应失败
        self.assertFalse(self.service.validate_state(state))

    def test_validate_state_unknown(self):
        """未注册的 state 应该校验失败"""
        self.assertFalse(self.service.validate_state("unknown_state"))

    def test_validate_state_expired(self):
        """过期的 state 应该校验失败"""
        state = self.service.generate_state()
        self.service._pending_states[state] = time.time() - 700
        self.assertFalse(self.service.validate_state(state))

    # ---------- 授权 URL ----------

    def test_get_auth_url_contains_required_params(self):
        """授权 URL 应包含必要参数"""
        url = self.service.get_auth_url(state="test_state_123")
        self.assertIn("app_id=cli_test_app_id", url)
        self.assertIn("redirect_uri=", url)
        self.assertIn("state=test_state_123", url)
        self.assertIn("response_type=code", url)
        self.assertIn("open.feishu.cn", url)

    def test_get_auth_url_auto_generates_state(self):
        """不传 state 时应自动生成"""
        url = self.service.get_auth_url()
        self.assertIn("state=", url)
        self.assertEqual(len(self.service._pending_states), 1)

    # ---------- Token 换取 (mock) ----------

    def test_exchange_token_success(self):
        """成功换取 token"""
        mock_response = {
            "code": 0,
            "data": {
                "access_token": "u-test_access_token",
                "refresh_token": "ur-test_refresh_token",
                "expires_in": 7200,
                "token_type": "Bearer",
            },
        }

        async def run():
            with patch("lark_client.oauth_service._http_post_json", return_value=mock_response):
                result = await self.service.exchange_token("test_code")
                self.assertEqual(result["access_token"], "u-test_access_token")
                self.assertEqual(result["refresh_token"], "ur-test_refresh_token")
                self.assertEqual(result["expires_in"], 7200)

        asyncio.run(run())

    def test_exchange_token_failure(self):
        """换取 token 失败应抛出 OAuthError"""
        mock_response = {"code": 10003, "msg": "invalid code"}

        async def run():
            with patch("lark_client.oauth_service._http_post_json", return_value=mock_response):
                with self.assertRaises(OAuthError) as ctx:
                    await self.service.exchange_token("bad_code")
                self.assertIn("invalid code", str(ctx.exception))

        asyncio.run(run())

    # ---------- Token 刷新 (mock) ----------

    def test_refresh_token_success(self):
        """成功刷新 token"""
        mock_response = {
            "code": 0,
            "data": {
                "access_token": "u-new_token",
                "refresh_token": "ur-new_refresh",
                "expires_in": 7200,
            },
        }

        async def run():
            with patch("lark_client.oauth_service._http_post_json", return_value=mock_response):
                result = await self.service.refresh_token("ur-old_refresh")
                self.assertEqual(result["access_token"], "u-new_token")

        asyncio.run(run())

    # ---------- 用户信息 (mock) ----------

    def test_get_user_info_success(self):
        """成功获取用户信息"""
        mock_response = {
            "code": 0,
            "data": {
                "open_id": "ou_test123",
                "name": "测试用户",
                "avatar_url": "https://example.com/avatar.png",
            },
        }

        async def run():
            with patch("lark_client.oauth_service._http_get_json", return_value=mock_response):
                result = await self.service.get_user_info("u-test_access_token")
                self.assertEqual(result["open_id"], "ou_test123")
                self.assertEqual(result["name"], "测试用户")

        asyncio.run(run())

    def test_get_user_info_failure(self):
        """获取用户信息失败"""
        mock_response = {"code": 99991663, "msg": "token invalid"}

        async def run():
            with patch("lark_client.oauth_service._http_get_json", return_value=mock_response):
                with self.assertRaises(OAuthError):
                    await self.service.get_user_info("u-invalid")

        asyncio.run(run())

    # ---------- Token 存储 ----------

    def test_save_and_get_user_token(self):
        """保存并读取用户 token"""
        token_data = {
            "access_token": "u-abc",
            "refresh_token": "ur-def",
            "expires_in": 7200,
        }
        self.service.save_user_token("ou_user1", token_data)

        loaded = self.service.get_user_token("ou_user1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["access_token"], "u-abc")
        self.assertIn("saved_at", loaded)

    def test_get_user_token_not_found(self):
        """不存在的用户应返回 None"""
        result = self.service.get_user_token("ou_nonexistent")
        self.assertIsNone(result)

    def test_remove_user_token(self):
        """删除用户 token"""
        self.service.save_user_token("ou_user1", {"access_token": "x"})
        self.assertTrue(self.service.remove_user_token("ou_user1"))
        self.assertIsNone(self.service.get_user_token("ou_user1"))

    def test_remove_user_token_not_found(self):
        """删除不存在的用户应返回 False"""
        self.assertFalse(self.service.remove_user_token("ou_nonexistent"))

    def test_multiple_users(self):
        """多用户 token 互不干扰"""
        self.service.save_user_token("ou_a", {"access_token": "token_a"})
        self.service.save_user_token("ou_b", {"access_token": "token_b"})

        self.assertEqual(self.service.get_user_token("ou_a")["access_token"], "token_a")
        self.assertEqual(self.service.get_user_token("ou_b")["access_token"], "token_b")

    # ---------- Token 过期判断 ----------

    def test_is_token_expired_not_expired(self):
        """未过期的 token"""
        token_data = {"saved_at": time.time(), "expires_in": 7200}
        self.assertFalse(self.service.is_token_expired(token_data))

    def test_is_token_expired_expired(self):
        """已过期的 token"""
        token_data = {"saved_at": time.time() - 8000, "expires_in": 7200}
        self.assertTrue(self.service.is_token_expired(token_data))

    def test_is_token_expired_within_buffer(self):
        """在 5 分钟缓冲期内视为过期"""
        token_data = {"saved_at": time.time() - 7000, "expires_in": 7200}
        self.assertTrue(self.service.is_token_expired(token_data))

    # ---------- 自动刷新 ----------

    def test_get_valid_access_token_not_expired(self):
        """未过期时直接返回"""
        self.service.save_user_token("ou_user1", {
            "access_token": "u-valid",
            "refresh_token": "ur-xxx",
            "expires_in": 7200,
        })

        async def run():
            token = await self.service.get_valid_access_token("ou_user1")
            self.assertEqual(token, "u-valid")

        asyncio.run(run())

    def test_get_valid_access_token_no_auth(self):
        """未授权用户返回 None"""
        async def run():
            token = await self.service.get_valid_access_token("ou_unknown")
            self.assertIsNone(token)

        asyncio.run(run())

    def test_get_valid_access_token_auto_refresh(self):
        """过期 token 自动刷新"""
        # 保存一个已过期的 token
        self.service.save_user_token("ou_user1", {
            "access_token": "u-old",
            "refresh_token": "ur-refresh",
            "expires_in": 7200,
        })
        # 手动修改 saved_at 使其过期
        import lark_client.oauth_service as mod
        all_tokens = json.loads(mod.TOKEN_STORAGE_PATH.read_text(encoding="utf-8"))
        all_tokens["ou_user1"]["saved_at"] = time.time() - 8000
        mod.TOKEN_STORAGE_PATH.write_text(json.dumps(all_tokens), encoding="utf-8")

        mock_response = {
            "code": 0,
            "data": {
                "access_token": "u-refreshed",
                "refresh_token": "ur-new",
                "expires_in": 7200,
            },
        }

        async def run():
            with patch("lark_client.oauth_service._http_post_json", return_value=mock_response):
                token = await self.service.get_valid_access_token("ou_user1")
                self.assertEqual(token, "u-refreshed")

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
