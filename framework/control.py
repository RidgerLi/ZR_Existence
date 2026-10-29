"""
本地控制面（Control plane）
Author: ZerolanLiveRobot

目的
----
让"外部"（Brain 面板的前端、局域网里的脚本、手机快捷方式等）能够调用机器人的能力，
又不必 `import bot`（那会把整个运行时、音频设备、模型管线全拖进来，也容易循环依赖）。

做法就是一张极简的动作注册表：

    # 机器人启动时注册
    control.register("mode.set", self._ctl_set_mode)

    # HTTP 层调用
    control.call("mode.set", {"mode": "focus"})
    -> {"ok": True, "result": {"mode": "focus"}}

设计约定
--------
- 每个动作都是 `fn(payload: dict) -> 任意 JSON 可序列化对象`，抛异常即视为失败；
- `call()` 永远返回 `{"ok": bool, ...}`，不会把异常抛给 HTTP 层；
- 注册表本身不加业务逻辑，保持可测试、可扩展。
"""

import threading
from typing import Any, Callable, Dict

from loguru import logger

Handler = Callable[[Dict[str, Any]], Any]

_handlers: Dict[str, Handler] = {}
_lock = threading.Lock()


def register(name: str, handler: Handler) -> None:
    """注册（或覆盖）一个动作。重复注册用于热重载场景，直接覆盖。"""
    if not name:
        raise ValueError("control action name must not be empty")
    with _lock:
        _handlers[name] = handler
    logger.debug(f"Control action registered: {name}")


def unregister(name: str) -> None:
    with _lock:
        _handlers.pop(name, None)


def available() -> list[str]:
    """已注册的动作名（供 /control/state 或调试用）。"""
    with _lock:
        return sorted(_handlers)


def has(name: str) -> bool:
    with _lock:
        return name in _handlers


def call(name: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """调用一个动作，永远返回可 JSON 序列化的 dict。"""
    with _lock:
        handler = _handlers.get(name)
    if handler is None:
        return {"ok": False, "error": f"unknown action: {name}"}
    try:
        result = handler(payload or {})
        return {"ok": True, "result": result}
    except Exception as e:
        logger.exception(f"Control action failed: {name}")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
