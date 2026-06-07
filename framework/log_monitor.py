"""
进程内日志环形缓冲（Log monitor）
Author: ZerolanLiveRobot

给 loguru 挂一个轻量 sink，把最近若干条日志收进一个线程安全的环形缓冲，供 Brain WebUI
的 /brain/logs 接口读取，让日志能直接显示在面板里（无需另开终端翻 stderr）。

只采集“本进程”（主程序 robot）的日志。core / GPT-SoVITS 跑在各自的控制台窗口里，自带日志。
"""

import threading
from collections import deque
from typing import Any, Deque, Dict, List

_lock = threading.Lock()
_buffer: Deque[Dict[str, Any]] = deque(maxlen=500)
_installed = False


def _sink(message) -> None:
    try:
        rec = message.record
        _buffer.append({
            "time": rec["time"].strftime("%H:%M:%S"),
            "level": rec["level"].name,
            "src": f'{rec["name"]}:{rec["function"]}:{rec["line"]}',
            "message": rec["message"],
        })
    except Exception:
        # sink 绝不能抛异常，否则会污染整个日志链路。
        pass


def install_sink(level: str = "INFO") -> None:
    """把环形缓冲 sink 挂到 loguru。重复调用只生效一次。"""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    from loguru import logger
    logger.add(_sink, level=level, enqueue=False, backtrace=False, diagnose=False)


def get_logs() -> List[Dict[str, Any]]:
    with _lock:
        return list(_buffer)
