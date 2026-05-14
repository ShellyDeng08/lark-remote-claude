"""
飞书事件订阅模块

使用长连接（WebSocket）接收私聊消息和其他事件
"""
import asyncio
import logging
import threading
from typing import Optional, Callable
from datetime import datetime
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

logger = logging.getLogger(__name__)


class FeishuEventSubscriber:
    """飞书事件订阅器（长连接模式）"""

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        on_private_message: Optional[Callable] = None
    ):
        """
        初始化事件订阅器

        Args:
            app_id: 飞书应用 ID
            app_secret: 飞书应用密钥
            on_private_message: 接收到私聊消息时的回调函数
        """
        self.app_id = app_id
        self.app_secret = app_secret
        self.on_private_message = on_private_message
        self.client: Optional[lark.ws.Client] = None
        self.thread: Optional[threading.Thread] = None
        self.running = False

    def start(self):
        """启动事件订阅（在后台线程运行）"""
        if self.running:
            logger.warning("事件订阅已在运行")
            return

        logger.info("正在启动飞书事件订阅...")

        # 创建事件处理器
        event_handler = lark.EventDispatcherHandler.builder("", "") \
            .register_p2_im_message_receive_v1(self._handle_message_receive) \
            .build()

        # 初始化长连接客户端
        self.client = lark.ws.Client(
            self.app_id,
            self.app_secret,
            event_handler=event_handler
        )

        # 在后台线程启动客户端
        self.running = True
        self.thread = threading.Thread(target=self._run_client, daemon=True)
        self.thread.start()

        logger.info("飞书事件订阅已启动")

    def _run_client(self):
        """在后台线程运行客户端"""
        try:
            # 在新线程中，没有事件循环，所以 client.start() 可以创建自己的事件循环
            logger.info("事件订阅客户端线程启动")
            self.client.start()
        except Exception as e:
            logger.error(f"事件订阅客户端运行失败: {e}", exc_info=True)
            self.running = False
        finally:
            logger.info("事件订阅客户端线程结束")

    def stop(self):
        """停止事件订阅"""
        if not self.running:
            return

        logger.info("正在停止飞书事件订阅...")
        self.running = False

        # 停止客户端
        if self.client:
            try:
                # lark-oapi 的 ws.Client 没有提供 stop 方法
                # 只能通过标记 running=False 来让应用层知道不再处理
                pass
            except Exception as e:
                logger.error(f"停止事件订阅失败: {e}")

        logger.info("飞书事件订阅已停止")

    def _handle_message_receive(self, data: P2ImMessageReceiveV1):
        """
        处理接收到的消息事件

        Args:
            data: 消息接收事件数据
        """
        try:
            # 提取事件信息
            event = data.event
            message = event.message
            sender = event.sender

            # 基本信息
            message_id = message.message_id
            chat_id = message.chat_id
            chat_type = message.chat_type
            message_type = message.message_type
            content = message.content
            create_time = message.create_time
            sender_id = sender.sender_id.open_id if sender.sender_id else None

            logger.info(
                f"收到消息事件: message_id={message_id[:10]}..., "
                f"chat_type={chat_type}, sender={sender_id[:8] if sender_id else 'N/A'}..."
            )

            # 只处理私聊消息 (p2p)
            if chat_type != "p2p":
                logger.debug(f"跳过非私聊消息: chat_type={chat_type}")
                return

            # 解析消息内容
            import json
            try:
                content_obj = json.loads(content)
                text = content_obj.get("text", "")
            except:
                text = content

            # 构造消息数据
            message_data = {
                "message_id": message_id,
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": message_type,
                "content": text,
                "create_time": create_time,
                "sender_id": sender_id,
            }

            logger.info(f"接收到私聊消息: {text[:50]}...")

            # 调用回调函数
            if self.on_private_message:
                try:
                    # 如果回调是异步函数，需要在事件循环中执行
                    if asyncio.iscoroutinefunction(self.on_private_message):
                        # 创建新的事件循环或使用现有的
                        try:
                            loop = asyncio.get_event_loop()
                        except RuntimeError:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                        loop.create_task(self.on_private_message(message_data))
                    else:
                        self.on_private_message(message_data)
                except Exception as e:
                    logger.error(f"调用私聊消息回调失败: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"处理消息事件失败: {e}", exc_info=True)
