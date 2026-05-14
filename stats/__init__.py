"""
Remote Claude 使用统计模块

全局接口：
  track(category, event, **kwargs)   —— 记录事件
  close()                            —— 关闭前刷新
"""

import os

_ENABLED = True
_MIXPANEL_TOKEN = os.getenv('MIXPANEL_TOKEN', '')

from .collector import StatsCollector

_collector = StatsCollector(enabled=_ENABLED)
# 模块加载时自动初始化 Mixpanel（无 token 则跳过上报）
if _MIXPANEL_TOKEN:
    _collector.set_mixpanel_token(_MIXPANEL_TOKEN)
    _collector.report_install()


def track(category: str, event: str, **kwargs) -> None:
    """记录事件（非阻塞，线程安全，异常不传播）"""
    _collector.track(category, event, **kwargs)


def init_mixpanel(token: str) -> None:
    """配置 Mixpanel token（保留接口兼容性，通常无需手动调用）"""
    _collector.set_mixpanel_token(token)
    _collector.report_install()


def report_daily(date: str = None) -> None:
    """手动触发聚合上报（CLI --report 命令使用）"""
    _collector.report_daily(date)


def close() -> None:
    """关闭前刷新队列"""
    _collector.close()
