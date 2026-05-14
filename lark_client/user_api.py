"""
飞书用户 API 调用模块

使用 User Access Token 调用飞书 API，实现以用户身份：
- 发送消息
- 获取群列表
- 获取消息历史
"""

import asyncio
import json
import logging
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from .oauth_service import LarkOAuthService, OAuthError

logger = logging.getLogger(__name__)

# 飞书 API 基地址（与应用注册平台一致）
FEISHU_API_BASE = "https://open.larkoffice.com/open-apis"


class UserApiError(Exception):
    """用户 API 调用异常"""
    pass


class TokenExpiredError(UserApiError):
    """Token 已过期且无法刷新"""
    pass


class LarkUserApi:
    """飞书用户 API 封装

    使用 User Access Token 以用户身份调用飞书 API。
    """

    def __init__(self, oauth_service: LarkOAuthService) -> None:
        """初始化用户 API。

        Args:
            oauth_service: OAuth 服务实例，用于获取和刷新 token
        """
        self.oauth_service = oauth_service

    async def _get_token_or_raise(self, open_id: str) -> str:
        """获取有效的 access_token，失败则抛异常。

        Args:
            open_id: 用户的 open_id

        Returns:
            有效的 access_token

        Raises:
            TokenExpiredError: 用户未授权或 token 已失效
        """
        token = await self.oauth_service.get_valid_access_token(open_id)
        if not token:
            raise TokenExpiredError(
                f"用户 {open_id[:8]}... 未授权或 token 已失效，请重新授权。"
            )
        return token

    async def _request(
        self,
        method: str,
        path: str,
        token: str,
        body: Optional[dict] = None,
        params: Optional[dict] = None,
    ) -> dict:
        """发送带 User Access Token 的 API 请求。

        Args:
            method: HTTP 方法（GET/POST/PUT/DELETE）
            path: API 路径（如 /im/v1/messages）
            token: user_access_token
            body: POST/PUT 请求体
            params: URL 查询参数

        Returns:
            API 响应 JSON

        Raises:
            UserApiError: 请求失败
        """
        url = f"{FEISHU_API_BASE}{path}"
        if params:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode(params)}"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        data = json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        loop = asyncio.get_event_loop()

        def _do_request():
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                resp_body = e.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(resp_body)
                except json.JSONDecodeError:
                    raise UserApiError(f"HTTP {e.code}: {resp_body[:200]}")
            except urllib.error.URLError as e:
                raise UserApiError(f"请求失败: {e}")

        result = await loop.run_in_executor(None, _do_request)

        # 检查飞书 API 业务错误码
        code = result.get("code", -1)
        if code != 0:
            msg = result.get("msg", "未知错误")
            # token 失效的常见错误码
            if code in (99991663, 99991668, 99991664):
                raise TokenExpiredError(f"Token 已失效 (code={code}): {msg}")
            logger.error("API 调用失败: %s %s -> code=%s, msg=%s", method, path, code, msg)
            raise UserApiError(f"API 错误 (code={code}): {msg}")

        return result

    async def send_message_as_user(
        self,
        open_id: str,
        chat_id: str,
        content: str,
        msg_type: str = "text",
    ) -> Optional[str]:
        """以用户身份发送消息。

        Args:
            open_id: 发送者的 open_id
            chat_id: 目标聊天 ID
            content: 消息内容（text 类型时为纯文本）
            msg_type: 消息类型，默认 text

        Returns:
            发送成功返回 message_id，失败返回 None
        """
        token = await self._get_token_or_raise(open_id)

        if msg_type == "text":
            body_content = json.dumps({"text": content})
        else:
            body_content = content

        body = {
            "receive_id": chat_id,
            "msg_type": msg_type,
            "content": body_content,
        }

        try:
            result = await self._request(
                "POST",
                "/im/v1/messages",
                token,
                body=body,
                params={"receive_id_type": "chat_id"},
            )
            message_id = result.get("data", {}).get("message_id")
            logger.info(
                "以用户身份发送消息成功: open_id=%s..., chat_id=%s..., message_id=%s",
                open_id[:8], chat_id[:8], message_id,
            )
            return message_id
        except TokenExpiredError:
            raise
        except UserApiError as e:
            logger.error("以用户身份发送消息失败: %s", e)
            return None

    async def get_user_chats(
        self,
        open_id: str,
        page_size: int = 50,
        page_token: Optional[str] = None,
    ) -> dict:
        """获取用户可见的聊天列表。

        Args:
            open_id: 用户的 open_id
            page_size: 每页数量，最大 50
            page_token: 分页标记

        Returns:
            包含 items（聊天列表）和 page_token（下一页标记）的字典

        Raises:
            TokenExpiredError: token 已失效
            UserApiError: API 调用失败
        """
        token = await self._get_token_or_raise(open_id)

        params = {"page_size": str(page_size)}
        if page_token:
            params["page_token"] = page_token

        result = await self._request("GET", "/im/v1/chats", token, params=params)
        data = result.get("data", {})

        items = data.get("items", [])
        logger.info("获取用户群列表: open_id=%s..., 数量=%d", open_id[:8], len(items))

        return {
            "items": items,
            "page_token": data.get("page_token", ""),
            "has_more": data.get("has_more", False),
        }

    async def get_chat_messages(
        self,
        open_id: str,
        chat_id: str,
        page_size: int = 20,
        page_token: Optional[str] = None,
        sort_type: str = "ByCreateTimeDesc",
    ) -> dict:
        """获取指定群的消息历史。

        Args:
            open_id: 用户的 open_id
            chat_id: 聊天 ID
            page_size: 每页数量
            page_token: 分页标记
            sort_type: 排序方式，ByCreateTimeAsc（升序）或 ByCreateTimeDesc（降序，默认）

        Returns:
            包含 items（消息列表）和 page_token 的字典

        Raises:
            TokenExpiredError: token 已失效
            UserApiError: API 调用失败
        """
        token = await self._get_token_or_raise(open_id)

        params = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "page_size": str(page_size),
            "sort_type": sort_type,
        }
        if page_token:
            params["page_token"] = page_token

        result = await self._request("GET", "/im/v1/messages", token, params=params)
        data = result.get("data", {})

        items = data.get("items", [])
        logger.info(
            "获取消息历史: open_id=%s..., chat_id=%s..., 数量=%d",
            open_id[:8], chat_id[:8], len(items),
        )

        return {
            "items": items,
            "page_token": data.get("page_token", ""),
            "has_more": data.get("has_more", False),
        }

    async def get_thread_messages(
        self,
        open_id: str,
        message_id: str,
        page_size: int = 20,
        page_token: Optional[str] = None,
    ) -> dict:
        """获取指定消息的话题回复列表。

        Args:
            open_id: 用户的 open_id
            message_id: 根消息的 message_id
            page_size: 每页数量
            page_token: 分页标记

        Returns:
            包含 items（消息列表）和 page_token 的字典

        Raises:
            TokenExpiredError: token 已失效
            UserApiError: API 调用失败
        """
        token = await self._get_token_or_raise(open_id)

        params = {
            "container_id_type": "thread",
            "container_id": message_id,  # 话题的根消息ID
            "page_size": str(page_size),
            "sort_type": "ByCreateTimeDesc",
        }
        if page_token:
            params["page_token"] = page_token

        result = await self._request("GET", "/im/v1/messages", token, params=params)
        data = result.get("data", {})

        items = data.get("items", [])
        logger.info(
            "获取话题回复: open_id=%s..., message_id=%s..., 数量=%d",
            open_id[:8], message_id[:8], len(items),
        )

        return {
            "items": items,
            "page_token": data.get("page_token", ""),
            "has_more": data.get("has_more", False),
        }

    async def get_chat_info(self, open_id: str, chat_id: str) -> dict:
        """获取群聊信息。

        Args:
            open_id: 用户的 open_id
            chat_id: 聊天 ID

        Returns:
            群聊信息字典，包含 name、description、owner_id 等

        Raises:
            TokenExpiredError: token 已失效
            UserApiError: API 调用失败
        """
        token = await self._get_token_or_raise(open_id)

        result = await self._request("GET", f"/im/v1/chats/{chat_id}", token)
        data = result.get("data", {})

        logger.info(
            "获取群聊信息: open_id=%s..., chat_id=%s..., name=%s",
            open_id[:8], chat_id[:8], data.get("name", "未知"),
        )

        return data

    async def check_token_validity(self, open_id: str) -> dict:
        """检查用户 token 的有效性。

        Args:
            open_id: 用户的 open_id

        Returns:
            包含 authorized (bool)、expired (bool)、user_name (str) 等状态信息
        """
        token_data = self.oauth_service.get_user_token(open_id)
        if not token_data:
            return {"authorized": False, "expired": False, "user_name": ""}

        expired = self.oauth_service.is_token_expired(token_data)

        # 尝试获取有效 token 来确认真实状态
        valid_token = await self.oauth_service.get_valid_access_token(open_id)

        return {
            "authorized": True,
            "expired": valid_token is None and expired,
            "has_valid_token": valid_token is not None,
            "access_token_preview": (token_data.get("access_token", "")[:8] + "...") if token_data.get("access_token") else "",
        }
