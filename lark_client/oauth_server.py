"""
飞书 OAuth 回调服务器

基于标准库 asyncio 实现的轻量级 HTTP 服务器，用于：
- 提供授权入口页面 (/oauth/authorize)
- 接收飞书 OAuth 回调 (/oauth/callback)
- 展示授权结果页面
"""

import asyncio
import logging
import sys
from http import HTTPStatus
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from .oauth_service import LarkOAuthService, OAuthError

logger = logging.getLogger(__name__)


# ---------- HTML 模板 ----------

_PAGE_AUTHORIZE = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>飞书用户授权</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #f5f6f7; }}
  .card {{ background: #fff; border-radius: 12px; padding: 40px; max-width: 420px;
           box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }}
  h1 {{ font-size: 22px; margin-bottom: 12px; }}
  p {{ color: #666; font-size: 14px; line-height: 1.6; }}
  a.btn {{ display: inline-block; margin-top: 20px; padding: 12px 32px;
           background: #3370ff; color: #fff; border-radius: 8px;
           text-decoration: none; font-size: 16px; }}
  a.btn:hover {{ background: #245bdb; }}
</style></head>
<body><div class="card">
  <h1>Remote Claude 用户授权</h1>
  <p>点击下方按钮授权后，Remote Claude 将能够以你的身份访问飞书 API。</p>
  <a class="btn" href="{auth_url}">前往飞书授权</a>
</div></body></html>"""

_PAGE_SUCCESS = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权成功</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #f5f6f7; }}
  .card {{ background: #fff; border-radius: 12px; padding: 40px; max-width: 420px;
           box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }}
  .icon {{ font-size: 48px; margin-bottom: 12px; }}
  h1 {{ font-size: 22px; color: #34c759; }}
  p {{ color: #666; font-size: 14px; }}
  .info {{ margin-top: 16px; padding: 12px; background: #f0f9eb; border-radius: 8px;
           font-size: 13px; color: #333; }}
</style></head>
<body><div class="card">
  <div class="icon">✅</div>
  <h1>授权成功</h1>
  <p>你已成功授权 Remote Claude 访问你的飞书账号。</p>
  <div class="info">用户：{user_name}<br>Open ID：{open_id}</div>
  <p style="margin-top:20px;color:#999;">可以关闭此页面了。</p>
</div></body></html>"""

_PAGE_ERROR = """<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>授权失败</title>
<style>
  body {{ font-family: -apple-system, sans-serif; display: flex; justify-content: center;
         align-items: center; min-height: 100vh; margin: 0; background: #f5f6f7; }}
  .card {{ background: #fff; border-radius: 12px; padding: 40px; max-width: 420px;
           box-shadow: 0 2px 12px rgba(0,0,0,.08); text-align: center; }}
  .icon {{ font-size: 48px; margin-bottom: 12px; }}
  h1 {{ font-size: 22px; color: #ff3b30; }}
  p {{ color: #666; font-size: 14px; }}
  .err {{ margin-top: 16px; padding: 12px; background: #fff0f0; border-radius: 8px;
          font-size: 13px; color: #c00; }}
</style></head>
<body><div class="card">
  <div class="icon">❌</div>
  <h1>授权失败</h1>
  <p>OAuth 授权过程中发生错误，请重试。</p>
  <div class="err">{error_message}</div>
</div></body></html>"""


# ---------- 服务器核心 ----------

class OAuthCallbackServer:
    """OAuth 回调 HTTP 服务器

    在本地启动一个轻量级 HTTP 服务器，处理 /oauth/authorize 和 /oauth/callback 路由。
    """

    def __init__(self, oauth_service: LarkOAuthService, host: str = "0.0.0.0", port: int = 8080) -> None:
        """初始化回调服务器。

        Args:
            oauth_service: OAuth 服务实例
            host: 监听地址
            port: 监听端口
        """
        self.oauth_service = oauth_service
        self.host = host
        self.port = port
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        """启动 HTTP 服务器。"""
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        logger.info("OAuth 回调服务器已启动: http://%s:%s", self.host, self.port)
        logger.info("授权页面: http://localhost:%s/oauth/authorize", self.port)

    async def stop(self) -> None:
        """停止 HTTP 服务器。"""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("OAuth 回调服务器已停止")

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_serving()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """处理 HTTP 连接。"""
        try:
            # 读取请求行
            request_line = await asyncio.wait_for(reader.readline(), timeout=10)
            if not request_line:
                writer.close()
                return

            request_str = request_line.decode("utf-8", errors="replace").strip()
            parts = request_str.split()
            if len(parts) < 2:
                await self._send_response(writer, 400, "Bad Request")
                return

            method, path = parts[0], parts[1]

            # 读取并丢弃剩余 headers
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break

            logger.debug("HTTP 请求: %s %s", method, path)

            # 路由分发
            parsed = urlparse(path)
            route = parsed.path

            if route == "/oauth/authorize":
                await self._handle_authorize(writer)
            elif route == "/oauth/callback":
                query_params = parse_qs(parsed.query)
                await self._handle_callback(writer, query_params)
            elif route == "/health":
                await self._send_response(writer, 200, "ok", content_type="text/plain")
            else:
                await self._send_response(writer, 404, "Not Found")

        except asyncio.TimeoutError:
            logger.debug("HTTP 连接超时")
        except Exception as e:
            logger.error("处理 HTTP 请求异常: %s", e)
            try:
                await self._send_response(writer, 500, "Internal Server Error")
            except Exception:
                pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_authorize(self, writer: asyncio.StreamWriter) -> None:
        """处理 /oauth/authorize：展示授权入口页面。"""
        auth_url = self.oauth_service.get_auth_url()
        html = _PAGE_AUTHORIZE.format(auth_url=auth_url)
        await self._send_response(writer, 200, html)

    async def _handle_callback(self, writer: asyncio.StreamWriter, query_params: dict) -> None:
        """处理 /oauth/callback：飞书授权重定向回调。"""
        code = (query_params.get("code") or [None])[0]
        state = (query_params.get("state") or [None])[0]

        # 参数校验
        if not code:
            logger.warning("OAuth 回调缺少 code 参数")
            html = _PAGE_ERROR.format(error_message="缺少授权码（code），请重新授权。")
            await self._send_response(writer, 400, html)
            return

        if not state:
            logger.warning("OAuth 回调缺少 state 参数")
            html = _PAGE_ERROR.format(error_message="缺少 state 参数，疑似 CSRF 攻击。")
            await self._send_response(writer, 400, html)
            return

        # CSRF 校验
        if not self.oauth_service.validate_state(state):
            logger.warning("OAuth state 校验失败: %s", state[:8])
            html = _PAGE_ERROR.format(error_message="state 参数无效或已过期，请重新授权。")
            await self._send_response(writer, 400, html)
            return

        # 用授权码换取 token
        try:
            token_data = await self.oauth_service.exchange_token(code)
        except OAuthError as e:
            logger.error("换取 token 失败: %s", e)
            html = _PAGE_ERROR.format(error_message=f"换取 token 失败：{e}")
            await self._send_response(writer, 500, html)
            return

        # 获取用户信息
        access_token = token_data.get("access_token", "")
        try:
            user_info = await self.oauth_service.get_user_info(access_token)
        except OAuthError as e:
            logger.error("获取用户信息失败: %s", e)
            html = _PAGE_ERROR.format(error_message=f"获取用户信息失败：{e}")
            await self._send_response(writer, 500, html)
            return

        # 保存 token
        open_id = user_info.get("open_id", "")
        user_name = user_info.get("name", "未知用户")
        if open_id:
            self.oauth_service.save_user_token(open_id, token_data)
            logger.info("用户 %s (%s) 授权成功", user_name, open_id[:8])
        else:
            logger.warning("用户信息中缺少 open_id")

        html = _PAGE_SUCCESS.format(user_name=user_name, open_id=open_id)
        await self._send_response(writer, 200, html)

    async def _send_response(
        self,
        writer: asyncio.StreamWriter,
        status_code: int,
        body: str,
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        """发送 HTTP 响应。"""
        status_phrase = HTTPStatus(status_code).phrase
        body_bytes = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status_code} {status_phrase}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body_bytes)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(header.encode("utf-8"))
        writer.write(body_bytes)
        await writer.drain()
