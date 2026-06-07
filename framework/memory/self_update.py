"""
自我编辑工具协议（Prompt-JSON）
Author: ZerolanLiveRobot

LLM 在回复尾部用 `<self_update>{...}</self_update>` 标记发起一次自我编辑（改长期目标/todolist）。
本模块负责：
    1. MARKER：标记常量，供流式播放时识别并把标记内容从 TTS 中剥离（不读出来）。
    2. extract_and_strip(text)：从完整回复里抽出所有 <self_update> 块并解析为 dict 列表，
       同时返回剥离了这些块的"可朗读文本"。
    3. ToolRegistry：可扩展的工具分发雏形（按工具名路由）。目前只注册 update_self 一个
       fire-and-forget 工具；以后接 MCP/本地 skill/二趟回填循环时在此扩展，无需改主流程。

注意：本协议刻意只用一个固定标记，JSON 体即 update_self 的参数。未来通用工具可改用
`<tool_call>{"name":..., "arguments":...}</tool_call>` 形式，在 ToolRegistry 上扩展。
"""

import re
from typing import Callable, List, Tuple

from loguru import logger

from common.utils.json_util import smart_load_json_like

MARKER_OPEN = "<self_update>"
MARKER_CLOSE = "</self_update>"

_BLOCK_RE = re.compile(re.escape(MARKER_OPEN) + r"(.*?)" + re.escape(MARKER_CLOSE), re.DOTALL)


def extract_and_strip(text: str) -> Tuple[str, List[dict]]:
    """从回复里抽出所有 <self_update> 块。

    :return: (剥离标记后的可朗读文本, 解析出的 update dict 列表)
    """
    if not text or MARKER_OPEN not in text:
        return text, []

    updates: List[dict] = []
    for m in _BLOCK_RE.finditer(text):
        payload = (m.group(1) or "").strip()
        if not payload:
            continue
        try:
            data = smart_load_json_like(payload)
        except Exception as e:
            logger.warning(f"Failed to parse <self_update> payload: {e}; raw={payload!r}")
            continue
        if isinstance(data, dict):
            updates.append(data)
        elif isinstance(data, list):
            updates.extend(d for d in data if isinstance(d, dict))

    # 把闭合的块整体删除；若出现未闭合的开标记（流式截断/异常），从开标记处截掉，避免泄漏到 TTS。
    cleaned = _BLOCK_RE.sub("", text)
    open_idx = cleaned.find(MARKER_OPEN)
    if open_idx != -1:
        cleaned = cleaned[:open_idx]
    return cleaned, updates


class ToolRegistry:
    """按工具名路由的极简工具分发器，便于以后接入 MCP / 本地 skill。"""

    def __init__(self):
        self._handlers: dict[str, Callable[[dict], bool]] = {}

    def register(self, name: str, handler: Callable[[dict], bool]) -> None:
        self._handlers[name] = handler

    def dispatch(self, name: str, args: dict) -> bool:
        handler = self._handlers.get(name)
        if handler is None:
            logger.warning(f"No tool handler registered for '{name}'.")
            return False
        try:
            return bool(handler(args))
        except Exception as e:
            logger.exception(e)
            return False
