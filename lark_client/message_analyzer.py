"""
AI 消息分析器

使用 Claude 会话（零成本）或 Anthropic API 分析消息并生成摘要。
实现三级智能降级：Session → Start Session → API
"""

import asyncio
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional
from pathlib import Path

logger = logging.getLogger("lark_client.message_analyzer")

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lark_client.session_bridge import SessionBridge
from server.shared_state import SharedStateReader, get_mq_path
from lark_client.session_manager import SessionManager


class MessageAnalyzer:
    """智能消息分析器（支持 Session-based + API 降级）"""

    def __init__(
        self,
        session_manager: Optional[SessionManager] = None,
        api_key: Optional[str] = None
    ):
        """
        初始化分析器

        Args:
            session_manager: 会话管理器
            api_key: Anthropic API Key (可选，仅作降级备用)
        """
        self.session_manager = session_manager
        self.api_key = api_key
        self.client = None

        if api_key:
            try:
                import anthropic
                self.client = anthropic.AsyncAnthropic(api_key=api_key)
                logger.info("[MessageAnalyzer] Anthropic API 客户端已初始化")
            except ImportError:
                logger.warning("[MessageAnalyzer] anthropic 包未安装，API 降级不可用")
        else:
            logger.info("[MessageAnalyzer] 未配置 API Key，将仅使用 Session-based 分析")

    async def analyze_messages(
        self,
        messages: List[dict],
        user_id: str,
        user_name: str
    ) -> dict:
        """
        分析消息（智能降级）

        Args:
            messages: 消息列表
            user_id: 用户 ID
            user_name: 用户名称

        Returns:
            dict: 分析结果 {"summary": str, "action_items": List}

        Raises:
            Exception: 所有分析方式都失败
        """
        start_time = time.time()

        # 1️⃣ 优先：使用分析专用会话（零成本）
        if self.session_manager:
            try:
                session_name = await self.session_manager.get_or_create_session(user_id)
                logger.info(f"[MessageAnalyzer] 使用 Claude 会话分析（零成本）: {session_name}")

                result = await self._analyze_via_session(
                    session_name, messages, user_name
                )

                elapsed = time.time() - start_time
                logger.info(
                    f"✅ 会话分析成功: session={session_name}, "
                    f"响应时间={elapsed:.1f}s, 成本=$0"
                )
                return result

            except Exception as e:
                logger.warning(f"[MessageAnalyzer] 会话分析失败，尝试降级: {e}")

        # 2️⃣ 降级：使用 Anthropic API（有成本）
        if self.client:
            try:
                logger.info("[MessageAnalyzer] 使用 Anthropic API 分析（有成本）")
                result = await self._analyze_via_api(messages, user_name)

                elapsed = time.time() - start_time
                logger.info(f"⚠️ API 降级成功: 响应时间={elapsed:.1f}s")
                return result

            except Exception as e:
                logger.error(f"[MessageAnalyzer] API 分析失败: {e}")
                raise

        # 3️⃣ 无可用方式
        raise Exception("无可用的分析方式（Session 失败且未配置 API Key）")

    async def _analyze_via_session(
        self,
        session_name: str,
        messages: List[dict],
        user_name: str
    ) -> dict:
        """
        通过 Claude 会话分析（零成本）

        Args:
            session_name: 会话名称
            messages: 消息列表
            user_name: 用户名称

        Returns:
            dict: 分析结果

        Raises:
            Exception: 分析失败
        """
        # 1. 构建分析 prompt
        prompt = self._build_prompt(messages, user_name)

        # 2. 发送到会话（通过 Unix Socket）
        bridge = SessionBridge(session_name)
        connected = await bridge.connect()
        if not connected:
            raise Exception(f"无法连接到会话: {session_name}")

        try:
            await bridge.send_input(prompt)

            # 3. 监听共享内存，等待响应
            response = await self._wait_for_session_response(
                session_name, start_time=time.time(), timeout=30
            )

            # 4. 解析 JSON 响应
            return self._parse_response(response)

        finally:
            await bridge.disconnect()

    async def _wait_for_session_response(
        self,
        session_name: str,
        start_time: float,
        timeout: int = 30
    ) -> str:
        """
        等待会话响应（读取共享内存）

        Args:
            session_name: 会话名称
            start_time: 开始时间戳
            timeout: 超时时间（秒）

        Returns:
            str: 响应的 JSON 字符串

        Raises:
            TimeoutError: 超时
        """
        reader = SharedStateReader(session_name)

        while time.time() - start_time < timeout:
            # 读取共享内存
            snapshot = reader.read()
            blocks = snapshot.get('blocks', [])

            # 检查最新的输出块（从后往前遍历）
            for block in reversed(blocks):
                if block.get('_type') != 'OutputBlock':
                    continue

                # 检查时间戳是否在请求之后
                block_ts = block.get('timestamp', 0)
                if block_ts <= start_time:
                    continue

                # 尝试提取 JSON
                content = block.get('content', '')
                json_str = self._extract_json(content)
                if json_str:
                    logger.debug(f"[MessageAnalyzer] 从会话获取响应: {len(json_str)} 字节")
                    return json_str

            await asyncio.sleep(0.5)

        raise TimeoutError(f"等待 Claude 响应超时（{timeout}秒）")

    def _extract_json(self, text: str) -> Optional[str]:
        """
        从文本中提取 JSON 对象

        Args:
            text: 文本内容

        Returns:
            Optional[str]: 提取的 JSON 字符串，如果未找到则返回 None
        """
        # 寻找 JSON 代码块
        json_block_pattern = r'```json\s*\n(.*?)\n```'
        match = re.search(json_block_pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 寻找裸 JSON 对象
        json_pattern = r'\{[^\{]*"summary"[^\}]*\}'
        match = re.search(json_pattern, text, re.DOTALL)
        if match:
            return match.group(0).strip()

        return None

    async def _analyze_via_api(
        self,
        messages: List[dict],
        user_name: str
    ) -> dict:
        """
        通过 Anthropic API 分析（有成本）

        Args:
            messages: 消息列表
            user_name: 用户名称

        Returns:
            dict: 分析结果

        Raises:
            Exception: API 调用失败
        """
        prompt = self._build_prompt(messages, user_name)

        response = await self.client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1024,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}]
        )

        # 记录 token 使用
        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        cost = self._calculate_cost(input_tokens, output_tokens)

        logger.info(
            f"[MessageAnalyzer] API 调用完成: "
            f"输入={input_tokens}, 输出={output_tokens}, 成本=${cost:.4f}"
        )

        return self._parse_response(response.content[0].text)

    def _build_prompt(self, messages: List[dict], user_name: str) -> str:
        """
        构建分析 prompt

        Args:
            messages: 消息列表
            user_name: 用户名称

        Returns:
            str: Prompt 文本
        """
        # 格式化消息列表
        msg_lines = []
        for m in messages[:50]:  # 限制最多 50 条消息
            chat_name = m.get('chat_name', '未知群聊')
            sender_name = m.get('sender_name', '未知用户')
            content = m.get('content', '')
            # 截断过长内容
            if len(content) > 200:
                content = content[:200] + "..."
            msg_lines.append(f"[{chat_name}] {sender_name}: {content}")

        msg_text = "\n".join(msg_lines)

        return f"""你是一个飞书消息助手。请分析以下群聊消息，帮用户 {user_name} 总结待处理事项。

消息列表:
{msg_text}

请以 JSON 格式输出:
```json
{{
  "summary": "总体情况描述",
  "action_items": [
    {{
      "chat_name": "群聊名称",
      "description": "事项描述",
      "priority": "high/medium/low",
      "suggestion": "处理建议"
    }}
  ]
}}
```

要求:
1. 识别与 {user_name} 直接相关的待处理事项(@消息、问题、请求等)
2. 判断优先级:high(需立即处理)、medium(今天内处理)、low(可稍后查看)
3. 给出具体处理建议(是否需要回复、回复什么、是否需要行动等)
"""

    def _parse_response(self, response_text: str) -> dict:
        """
        解析 AI 响应

        Args:
            response_text: 响应文本

        Returns:
            dict: 解析后的结果

        Raises:
            ValueError: 解析失败
        """
        # 提取 JSON
        json_str = self._extract_json(response_text)
        if not json_str:
            raise ValueError(f"无法从响应中提取 JSON: {response_text[:200]}")

        # 解析 JSON
        try:
            result = json.loads(json_str)
            # 验证必需字段
            if 'summary' not in result or 'action_items' not in result:
                raise ValueError("响应缺少必需字段: summary 或 action_items")
            return result
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON 解析失败: {e}\n内容: {json_str[:200]}")

    def _calculate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """
        计算 API 调用成本

        Claude 3.5 Sonnet 定价:
        - Input: $3 / 1M tokens
        - Output: $15 / 1M tokens

        Args:
            input_tokens: 输入 token 数
            output_tokens: 输出 token 数

        Returns:
            float: 成本（美元）
        """
        input_cost = input_tokens * 3 / 1_000_000
        output_cost = output_tokens * 15 / 1_000_000
        return input_cost + output_cost
