"""
进程内日志环形缓冲（Log monitor）
Author: ZerolanLiveRobot

给 loguru 挂一个轻量 sink，把最近若干条日志收进一个线程安全的环形缓冲，供 Brain WebUI
的 /brain/logs 接口读取，让日志能直接显示在面板里（无需另开终端翻 stderr）。

除了原始字段（时间/级别/来源/正文），每条记录还会带一个 **分类**（VAD / LLM / TTS /
MEMORY / CONTROL / CAP / …），面板据此做"按分类 + 按最低级别 + 关键字"过滤。

只采集"本进程"（主程序 robot）的日志。core / GPT-SoVITS 跑在各自的控制台窗口里，自带日志。
"""

import threading
from collections import deque
from typing import Any, Deque, Dict, List, Optional

_lock = threading.Lock()
_buffer: Deque[Dict[str, Any]] = deque(maxlen=1000)
_installed = False

# 按 logger 名（模块路径）前缀归类。顺序有意义：更具体的前缀放前面。
_CATEGORY_RULES = (
    ("devices.microphone", "VAD"),
    ("devices.aec", "VAD"),
    ("devices.keyboard", "INPUT"),
    ("devices.speaker", "TTS"),
    ("framework.brain", "BRAIN"),
    ("framework.memory", "MEMORY"),
    ("framework.control", "CONTROL"),
    ("framework.capabilities", "CAP"),
    ("framework.journal", "JOURNAL"),
    ("framework.telemetry", "TELEMETRY"),
    ("framework.backchannel", "TTS"),
    ("framework.local_commands", "CONTROL"),
    ("pipeline.llm", "LLM"),
    ("pipeline.asr", "ASR"),
    ("pipeline.tts", "TTS"),
    ("services.playground.res", "UI"),
    ("services.live_stream", "LIVE"),
    ("manager.", "CONFIG"),
    ("bot", "BOT"),
    ("__main__", "BOT"),
)

_LEVEL_ORDER = {
    "TRACE": 5, "DEBUG": 10, "INFO": 20, "SUCCESS": 25,
    "WARNING": 30, "ERROR": 40, "CRITICAL": 50,
}


def _category(name: str) -> str:
    name = name or ""
    for prefix, cat in _CATEGORY_RULES:
        if name == prefix or name.startswith(prefix + "."):
            return cat
    return "OTHER"


def _sink(message) -> None:
    try:
        rec = message.record
        name = rec["name"]
        _buffer.append({
            "time": rec["time"].strftime("%H:%M:%S"),
            "level": rec["level"].name,
            "category": _category(name),
            "src": f'{name}:{rec["function"]}:{rec["line"]}',
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


def categories() -> List[str]:
    return sorted({c for _, c in _CATEGORY_RULES} | {"OTHER"})


def get_logs(category: Optional[str] = None, level: Optional[str] = None,
             q: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """按分类 / 最低级别 / 关键字过滤，返回时间正序的日志（最后 limit 条）。"""
    min_level = _LEVEL_ORDER.get((level or "").upper(), 0)
    needle = (q or "").strip().lower()
    with _lock:
        rows = list(_buffer)
    out = []
    for r in rows:
        if category and category.upper() not in ("ALL", "*") and r["category"] != category.upper():
            continue
        if min_level and _LEVEL_ORDER.get(r["level"], 0) < min_level:
            continue
        if needle and needle not in r["message"].lower() and needle not in r["src"].lower():
            continue
        out.append(r)
    if limit and limit > 0:
        out = out[-int(limit):]
    return out
