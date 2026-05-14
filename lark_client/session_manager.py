"""
Session 管理器

管理分析专用 Claude 会话的生命周期，包括启动、健康检查、空闲清理。
每个用户有独立的分析会话：lark-analyzer-{user_id[:8]}
"""

import asyncio
import time
import logging
from typing import Dict, Optional
from pathlib import Path

logger = logging.getLogger("lark_client.session_manager")


class SessionManager:
    """Session 管理器 - 管理分析专用会话"""

    def __init__(self, remote_claude_path: Optional[str] = None):
        """
        初始化 SessionManager

        Args:
            remote_claude_path: remote_claude.py 的路径，默认为项目根目录
        """
        self.remote_claude_path = remote_claude_path or str(
            Path(__file__).resolve().parent.parent / "remote_claude.py"
        )
        self._active_sessions: Dict[str, float] = {}  # session_name -> last_active_time

    async def get_or_create_session(self, user_id: str) -> str:
        """
        获取或创建用户的分析会话

        Args:
            user_id: 用户 ID（open_id）

        Returns:
            str: 会话名称

        Raises:
            Exception: 会话启动失败
        """
        session_name = f"lark-analyzer-{user_id[:8]}"

        # 1. 检查是否已存在且健康
        if session_name in self._active_sessions:
            if await self._is_session_healthy(session_name):
                self._active_sessions[session_name] = time.time()
                logger.debug(f"[SessionManager] 复用会话: {session_name}")
                return session_name
            else:
                # 会话不健康，尝试清理
                logger.warning(f"[SessionManager] 会话不健康，尝试清理: {session_name}")
                await self._cleanup_session(session_name)

        # 2. 启动新会话
        await self._start_session(session_name)
        self._active_sessions[session_name] = time.time()
        return session_name

    async def _start_session(self, session_name: str):
        """
        启动分析会话（后台运行）

        Args:
            session_name: 会话名称

        Raises:
            Exception: 启动失败
        """
        logger.info(f"[SessionManager] 启动分析会话: {session_name}")

        try:
            # 启动 Claude 会话（后台模式）
            process = await asyncio.create_subprocess_exec(
                "uv", "run", "python3", self.remote_claude_path,
                "start", session_name, "--background",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            # 等待启动完成
            await asyncio.sleep(3)

            # 验证会话已启动
            if not await self._is_session_healthy(session_name):
                stderr_output = ""
                if process.stderr:
                    stderr_data = await process.stderr.read()
                    stderr_output = stderr_data.decode('utf-8', errors='ignore')

                raise Exception(
                    f"会话启动失败: {session_name}\n"
                    f"stderr: {stderr_output}"
                )

            logger.info(f"[SessionManager] 会话启动成功: {session_name}")

        except Exception as e:
            logger.error(f"[SessionManager] 启动会话失败: {session_name}, 错误: {e}")
            raise

    async def _is_session_healthy(self, session_name: str) -> bool:
        """
        检查会话是否健康

        Args:
            session_name: 会话名称

        Returns:
            bool: 是否健康
        """
        try:
            # 使用 remote_claude.py status 命令检查会话状态
            process = await asyncio.create_subprocess_exec(
                "uv", "run", "python3", self.remote_claude_path,
                "status", session_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            stdout, stderr = await process.communicate()

            # 如果命令返回 0，说明会话存在且健康
            if process.returncode == 0:
                return True

            # 如果返回非 0，检查输出
            output = stdout.decode('utf-8', errors='ignore')
            if "not found" in output.lower() or "不存在" in output:
                return False

            return False

        except Exception as e:
            logger.debug(f"[SessionManager] 健康检查失败: {session_name}, 错误: {e}")
            return False

    async def _cleanup_session(self, session_name: str):
        """
        清理会话

        Args:
            session_name: 会话名称
        """
        try:
            logger.info(f"[SessionManager] 清理会话: {session_name}")

            # 使用 remote_claude.py kill 命令终止会话
            process = await asyncio.create_subprocess_exec(
                "uv", "run", "python3", self.remote_claude_path,
                "kill", session_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            await process.communicate()

            # 从活跃列表移除
            if session_name in self._active_sessions:
                del self._active_sessions[session_name]

            logger.info(f"[SessionManager] 会话已清理: {session_name}")

        except Exception as e:
            logger.error(f"[SessionManager] 清理会话失败: {session_name}, 错误: {e}")

    async def cleanup_idle_sessions(self, idle_timeout: int = 3600):
        """
        清理空闲会话（默认 1 小时无活动）

        Args:
            idle_timeout: 空闲超时时间（秒）
        """
        now = time.time()
        to_cleanup = [
            name for name, last_active in self._active_sessions.items()
            if now - last_active > idle_timeout
        ]

        if to_cleanup:
            logger.info(
                f"[SessionManager] 清理 {len(to_cleanup)} 个空闲会话 "
                f"(超过 {idle_timeout}s 无活动)"
            )

        for session_name in to_cleanup:
            await self._cleanup_session(session_name)

    def mark_session_active(self, session_name: str):
        """
        标记会话为活跃状态

        Args:
            session_name: 会话名称
        """
        self._active_sessions[session_name] = time.time()

    def get_active_sessions(self) -> Dict[str, float]:
        """
        获取所有活跃会话

        Returns:
            Dict[str, float]: 会话名称 -> 最后活跃时间
        """
        return self._active_sessions.copy()
