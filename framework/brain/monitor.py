"""
大脑状态监控发布（Brain monitor）
Author: ZerolanLiveRobot

把 Brain 每个 tick 的快照发布到一个进程内的线程安全槽里，供可视化面板（运行在 Flask
线程里的 /brain/state 接口）读取。用模块级单例解耦：Brain 只管 publish，Web 只管 get，
彼此不持有对方引用，也不关心构造顺序。

同时维护一小段历史环形缓冲（默认最近 ~300 个 tick），让面板能画出趋势曲线而不需要前端
自己长时间累积。
"""

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

_lock = threading.Lock()
_latest: Optional[Dict[str, Any]] = None
_history: Deque[Dict[str, Any]] = deque(maxlen=300)


def publish(snapshot: Dict[str, Any]) -> None:
    global _latest
    with _lock:
        _latest = snapshot
        # 仅把画曲线需要的数值字段塞进历史，避免历史体积膨胀。
        drives = snapshot.get("drives", {})
        signals = snapshot.get("signals", {})
        _history.append({
            "t": snapshot.get("t", 0.0),
            "arousal": drives.get("arousal", 0.0),
            "social_need": drives.get("social_need", 0.0),
            "expression_urge": drives.get("expression_urge", 0.0),
            "mic_energy": signals.get("mic_energy", 0.0),
            "keyboard": signals.get("keyboard", 0.0),
            "user_speaking": signals.get("user_speaking", 0.0),
            "threshold": snapshot.get("fire_threshold", 1.0),
        })


def get_latest() -> Optional[Dict[str, Any]]:
    with _lock:
        return _latest


def get_history() -> List[Dict[str, Any]]:
    with _lock:
        return list(_history)
