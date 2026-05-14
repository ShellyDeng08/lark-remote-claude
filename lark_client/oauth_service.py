"""
飞书 OAuth 2.0 服务核心模块

实现 OAuth 授权码模式的核心逻辑，包括：
- 生成授权 URL
- 用授权码换取 user_access_token
- 获取用户信息
- Token 持久化存储与读取
"""

import asyncio
import json
import logging
import secrets
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from utils.session import USER_DATA_DIR

logger = logging.getLogger(__name__)

# 飞书 OAuth 端点
# 注意：授权页用 accounts.feishu.cn，API 接口用 open.larkoffice.com（与应用注册平台一致）
FEISHU_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/index"
# v2 接口：标准 OAuth 2.0，支持 refresh_token
FEISHU_TOKEN_URL = "https://open.larkoffice.com/open-apis/authen/v2/oauth/token"
FEISHU_REFRESH_TOKEN_URL = "https://open.larkoffice.com/open-apis/authen/v2/oauth/token"
FEISHU_USER_INFO_URL = "https://open.larkoffice.com/open-apis/authen/v1/user_info"

# Token 存储路径
TOKEN_STORAGE_PATH = USER_DATA_DIR / "user_tokens.json"


def _http_post_json(url: str, payload: dict, headers: Optional[dict] = None) -> dict:
    """同步发送 JSON POST 请求并返回响应。

    Args:
        url: 请求地址
        payload: JSON 请求体
        headers: 额外的 HTTP 头

    Returns:
        响应 JSON 解析后的字典

    Raises:
        OAuthError: 请求失败
    """
    all_headers = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=all_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise OAuthError(f"HTTP 请求失败: {e}")


def _http_get_json(url: str, headers: Optional[dict] = None) -> dict:
    """同步发送 GET 请求并返回响应。

    Args:
        url: 请求地址
        headers: HTTP 头

    Returns:
        响应 JSON 解析后的字典

    Raises:
        OAuthError: 请求失败
    """
    req = urllib.request.Request(url, headers=headers or {}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise OAuthError(f"HTTP 请求失败: {e}")


class LarkOAuthService:
    """飞书 OAuth 2.0 服务

    管理 OAuth 授权流程和 Token 生命周期。
    """

    def __init__(self, app_id: str, app_secret: str, redirect_uri: str) -> None:
        """初始化 OAuth 服务。

        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
            redirect_uri: OAuth 回调地址，需与飞书开放平台配置一致
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.redirect_uri = redirect_uri
        # state -> 创建时间，用于 CSRF 校验
        self._pending_states: dict[str, float] = {}
        # state 有效期（秒）
        self._state_ttl = 600

    def generate_state(self) -> str:
        """生成并记录一个随机 state 值，用于 CSRF 防护。

        Returns:
            随机生成的 state 字符串
        """
        state = secrets.token_urlsafe(32)
        self._pending_states[state] = time.time()
        self._cleanup_expired_states()
        return state

    def validate_state(self, state: str) -> bool:
        """校验并消费 state 参数（一次性使用）。

        Args:
            state: 待校验的 state 值

        Returns:
            校验是否通过
        """
        created_at = self._pending_states.pop(state, None)
        if created_at is None:
            logger.warning("OAuth state 校验失败：未知的 state")
            return False
        if time.time() - created_at > self._state_ttl:
            logger.warning("OAuth state 已过期")
            return False
        return True

    def get_auth_url(self, state: Optional[str] = None) -> str:
        """生成飞书 OAuth 授权页面 URL。

        Args:
            state: CSRF 防护参数。若为 None 则自动生成。

        Returns:
            完整的授权 URL
        """
        if state is None:
            state = self.generate_state()

        # 飞书 OAuth：必须在 scope 中包含 offline_access 才能获取 refresh_token
        # 参考：https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/authen-v1/authen/refresh_access_token
        params = {
            "app_id": self.app_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "state": state,
            "scope": "offline_access",  # 必须包含此 scope 才会返回 refresh_token
        }
        url = f"{FEISHU_AUTHORIZE_URL}?{urlencode(params)}"
        logger.info("生成授权 URL: state=%s..., scope=offline_access", state[:8])
        return url

    async def _get_app_access_token(self) -> str:
        """获取应用级 access_token。

        Returns:
            app_access_token

        Raises:
            OAuthError: 获取失败
        """
        payload = {
            "app_id": self.app_id,
            "app_secret": self.app_secret,
        }

        logger.info("正在获取 app_access_token...")
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None,
            _http_post_json,
            "https://open.larkoffice.com/open-apis/auth/v3/app_access_token/internal",
            payload,
        )

        if data.get("code") != 0:
            error_msg = data.get("msg", "未知错误")
            logger.error("获取 app_access_token 失败: code=%s, msg=%s", data.get("code"), error_msg)
            raise OAuthError(f"获取 app_access_token 失败: {error_msg}")

        app_token = data.get("app_access_token")
        logger.info("成功获取 app_access_token")
        return app_token

    async def exchange_token(self, code: str) -> dict:
        """用授权码换取 user_access_token。

        Args:
            code: 飞书回调返回的授权码

        Returns:
            包含 access_token、refresh_token、expires_in 等字段的字典

        Raises:
            OAuthError: 换取 token 失败
        """
        # v2 接口：直接使用 client_id + client_secret，不需要 app_access_token
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "code": code,
            "redirect_uri": self.redirect_uri,  # 必须与授权链接一致
        }

        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }

        logger.info("正在用授权码换取 token (v2 接口)...")
        logger.info("请求 payload: grant_type=authorization_code, code=%s...", code[:10])
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, _http_post_json, FEISHU_TOKEN_URL, payload, headers
        )

        logger.info("🔍 飞书 API 完整响应: code=%s", data.get("code"))
        logger.info("完整响应内容: %s", json.dumps(data, ensure_ascii=False))
        if data.get("code") == 0:
            token_data = data.get("data", {})
            logger.info("data 字段内容: %s", json.dumps(token_data, ensure_ascii=False)[:500])
            logger.info("响应字段: %s", list(token_data.keys()))

        if data.get("code") != 0:
            error_msg = data.get("msg", "未知错误")
            logger.error("换取 token 失败: code=%s, msg=%s", data.get("code"), error_msg)
            raise OAuthError(f"换取 token 失败: {error_msg}")

        token_data = data.get("data", {})
        logger.info(
            "成功获取 token: expires_in=%s, has_refresh_token=%s",
            token_data.get("expires_in"),
            "refresh_token" in token_data
        )
        logger.info("Token data keys: %s", list(token_data.keys()))
        if "refresh_token" not in token_data:
            logger.warning("⚠️ 飞书未返回 refresh_token！")
            logger.warning("完整响应字段: %s", list(token_data.keys()))
            logger.warning("可能原因: 1) 应用后台未开通 offline_access 2) scope 参数未生效 3) 应用类型不支持")
        return token_data

    async def refresh_token(self, refresh_token_value: str) -> dict:
        """使用 refresh_token 刷新 access_token。

        Args:
            refresh_token_value: 有效的 refresh_token

        Returns:
            刷新后的 token 数据字典

        Raises:
            OAuthError: 刷新 token 失败
        """
        # v2 接口：直接使用 client_id + client_secret
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.app_id,
            "client_secret": self.app_secret,
            "refresh_token": refresh_token_value,
        }

        headers = {
            "Content-Type": "application/json; charset=utf-8",
        }

        logger.info("正在刷新 token (v2 接口)...")
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, _http_post_json, FEISHU_REFRESH_TOKEN_URL, payload, headers
        )

        if data.get("code") != 0:
            error_msg = data.get("msg", "未知错误")
            logger.error("刷新 token 失败: code=%s, msg=%s", data.get("code"), error_msg)
            raise OAuthError(f"刷新 token 失败: {error_msg}")

        token_data = data.get("data", {})
        logger.info("token 刷新成功: expires_in=%s", token_data.get("expires_in"))
        return token_data

    async def get_user_info(self, access_token: str) -> dict:
        """使用 user_access_token 获取用户信息。

        Args:
            access_token: 有效的 user_access_token

        Returns:
            包含 open_id、name、avatar_url 等字段的用户信息字典

        Raises:
            OAuthError: 获取用户信息失败
        """
        headers = {
            "Authorization": f"Bearer {access_token}",
        }

        logger.info("正在获取用户信息...")
        loop = asyncio.get_event_loop()
        data = await loop.run_in_executor(
            None, _http_get_json, FEISHU_USER_INFO_URL, headers
        )

        if data.get("code") != 0:
            error_msg = data.get("msg", "未知错误")
            logger.error("获取用户信息失败: code=%s, msg=%s", data.get("code"), error_msg)
            raise OAuthError(f"获取用户信息失败: {error_msg}")

        user_info = data.get("data", {})
        logger.info("获取用户信息成功: name=%s", user_info.get("name"))
        return user_info

    def save_user_token(self, open_id: str, token_data: dict) -> None:
        """保存用户的 token 到本地 JSON 文件。

        Token 数据会附加 saved_at 时间戳，方便后续判断是否过期。

        Args:
            open_id: 用户的 open_id
            token_data: 包含 access_token、refresh_token、expires_in 的字典
        """
        all_tokens = self._load_all_tokens()

        # 附加存储时间戳，用于计算 token 是否过期
        token_data["saved_at"] = time.time()
        all_tokens[open_id] = token_data

        TOKEN_STORAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_STORAGE_PATH.write_text(
            json.dumps(all_tokens, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("已保存用户 token: open_id=%s...", open_id[:8])

    def get_user_token(self, open_id: str) -> Optional[dict]:
        """读取指定用户的 token 数据。

        Args:
            open_id: 用户的 open_id

        Returns:
            token 数据字典，不存在则返回 None
        """
        all_tokens = self._load_all_tokens()
        token_data = all_tokens.get(open_id)
        if token_data:
            logger.debug("读取用户 token: open_id=%s...", open_id[:8])
        return token_data

    def remove_user_token(self, open_id: str) -> bool:
        """删除指定用户的 token（撤销授权）。

        Args:
            open_id: 用户的 open_id

        Returns:
            是否成功删除（用户之前有授权记录则为 True）
        """
        all_tokens = self._load_all_tokens()
        if open_id not in all_tokens:
            return False

        del all_tokens[open_id]
        TOKEN_STORAGE_PATH.write_text(
            json.dumps(all_tokens, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("已删除用户 token: open_id=%s...", open_id[:8])
        return True

    def is_token_expired(self, token_data: dict) -> bool:
        """判断 token 是否已过期。

        Args:
            token_data: 包含 saved_at 和 expires_in 的 token 数据

        Returns:
            True 表示已过期或即将过期（提前 5 分钟）
        """
        saved_at = token_data.get("saved_at", 0)
        expires_in = token_data.get("expires_in", 0)
        # 提前 5 分钟视为过期，留出刷新缓冲
        return time.time() > saved_at + expires_in - 300

    async def get_valid_access_token(self, open_id: str) -> Optional[str]:
        """获取用户的有效 access_token，过期时自动刷新。

        Args:
            open_id: 用户的 open_id

        Returns:
            有效的 access_token，未授权或刷新失败返回 None
        """
        token_data = self.get_user_token(open_id)
        if not token_data:
            return None

        if not self.is_token_expired(token_data):
            return token_data.get("access_token")

        # Token 已过期，尝试刷新
        rt = token_data.get("refresh_token")
        if not rt:
            logger.warning("用户 %s... token 已过期且无 refresh_token", open_id[:8])
            return None

        try:
            new_token_data = await self.refresh_token(rt)
            self.save_user_token(open_id, new_token_data)
            return new_token_data.get("access_token")
        except OAuthError as e:
            logger.error("自动刷新 token 失败: %s", e)
            return None

    def _load_all_tokens(self) -> dict:
        """从 JSON 文件加载所有用户的 token 数据。"""
        if not TOKEN_STORAGE_PATH.exists():
            return {}
        try:
            return json.loads(TOKEN_STORAGE_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("读取 token 文件失败: %s", e)
            return {}

    def _cleanup_expired_states(self) -> None:
        """清理过期的 state 记录。"""
        now = time.time()
        expired = [s for s, t in self._pending_states.items() if now - t > self._state_ttl]
        for s in expired:
            del self._pending_states[s]


class OAuthError(Exception):
    """OAuth 流程异常"""
    pass
