"""
记忆状态监控发布（Memory monitor）
Author: ZerolanLiveRobot

与 framework/brain/monitor.py 同款的进程内单例：bot 把当前的记忆快照（会话摘要、长期目标、
待办、工作窗口里的最近对话、上一次长期检索结果）publish 到这里，Brain WebUI 的 /brain/memory
接口只管 get，二者解耦、互不持有引用。
"""

import threading
from typing import Any, Dict, Optional

_lock = threading.Lock()
_latest: Optional[Dict[str, Any]] = None


def publish(snapshot: Dict[str, Any]) -> None:
    global _latest
    with _lock:
        _latest = snapshot


def get_latest() -> Optional[Dict[str, Any]]:
    with _lock:
        return _latest
