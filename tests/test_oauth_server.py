"""
OAuth 回调服务器单元测试
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lark_client.oauth_service import LarkOAuthService, OAuthError
from lark_client.oauth_server import OAuthCallbackServer


async def _read_full_response(reader: asyncio.StreamReader) -> str:
    """读取完整 HTTP 响应（等待服务端关闭连接）。"""
    chunks = []
    while True:
        chunk = await asyncio.wait_for(reader.read(8192), timeout=5)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8")


class TestOAuthCallbackServer(unittest.TestCase):
    """OAuthCallbackServer 单元测试"""

    def setUp(self):
        self.oauth_service = LarkOAuthService(
            app_id="cli_test_app_id",
            app_secret="test_app_secret",
            redirect_uri="http://localhost:9999/oauth/callback",
        )
        self.server = OAuthCallbackServer(self.oauth_service, port=0)

    def _run(self, coro):
        """辅助方法：运行异步代码"""
        return asyncio.run(coro)

    def test_start_and_stop(self):
        """服务器能正常启动和停止"""
        async def run():
            await self.server.start()
            self.assertTrue(self.server.is_running)
            await self.server.stop()
            self.assertFalse(self.server.is_running)

        self._run(run())

    def test_health_endpoint(self):
        """/health 端点返回 200"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response_str = await _read_full_response(reader)
                self.assertIn("200 OK", response_str)
                self.assertIn("ok", response_str)
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())

    def test_authorize_page(self):
        """/oauth/authorize 返回包含授权链接的 HTML"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /oauth/authorize HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response_str = await _read_full_response(reader)
                self.assertIn("200 OK", response_str)
                self.assertIn("open.feishu.cn", response_str)
                self.assertIn("cli_test_app_id", response_str)
                self.assertIn("用户授权", response_str)
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())

    def test_callback_missing_code(self):
        """/oauth/callback 缺少 code 参数返回 400"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /oauth/callback?state=abc HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response_str = await _read_full_response(reader)
                self.assertIn("400", response_str)
                self.assertIn("授权码", response_str)
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())

    def test_callback_invalid_state(self):
        """/oauth/callback state 校验失败返回 400"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /oauth/callback?code=test_code&state=invalid HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response_str = await _read_full_response(reader)
                self.assertIn("400", response_str)
                self.assertIn("state", response_str)
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())

    def test_callback_success(self):
        """完整的授权回调成功流程"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            state = self.oauth_service.generate_state()

            mock_token = {
                "code": 0,
                "data": {
                    "access_token": "u-test",
                    "refresh_token": "ur-test",
                    "expires_in": 7200,
                },
            }
            mock_user = {
                "code": 0,
                "data": {
                    "open_id": "ou_test_user",
                    "name": "测试用户",
                },
            }

            try:
                with patch("lark_client.oauth_service._http_post_json", return_value=mock_token), \
                     patch("lark_client.oauth_service._http_get_json", return_value=mock_user), \
                     patch.object(self.oauth_service, "save_user_token") as mock_save:

                    reader, writer = await asyncio.open_connection("127.0.0.1", port)
                    req = f"GET /oauth/callback?code=auth_code_123&state={state} HTTP/1.1\r\nHost: localhost\r\n\r\n"
                    writer.write(req.encode())
                    await writer.drain()
                    response_str = await _read_full_response(reader)

                    self.assertIn("200 OK", response_str)
                    self.assertIn("授权成功", response_str)
                    self.assertIn("测试用户", response_str)
                    self.assertIn("ou_test_user", response_str)

                    mock_save.assert_called_once()
                    call_args = mock_save.call_args
                    self.assertEqual(call_args[0][0], "ou_test_user")

                    writer.close()
                    await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())

    def test_404_for_unknown_route(self):
        """未知路由返回 404"""
        async def run():
            server = OAuthCallbackServer(self.oauth_service, port=0)
            await server.start()
            port = server._server.sockets[0].getsockname()[1]

            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.write(b"GET /unknown HTTP/1.1\r\nHost: localhost\r\n\r\n")
                await writer.drain()
                response_str = await _read_full_response(reader)
                self.assertIn("404", response_str)
                writer.close()
                await writer.wait_closed()
            finally:
                await server.stop()

        self._run(run())


if __name__ == "__main__":
    unittest.main()
