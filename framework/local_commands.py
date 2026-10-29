"""
本地命令解析（Local commands）
Author: ZerolanLiveRobot

把"不需要大模型就能确定执行"的语音短句解析成动作，例如：

    "安静"              -> mode.set / focus
    "恢复"              -> mode.resume
    "添加待办：买牛奶"   -> todo.add / 买牛奶
    "把写周报加入待办"   -> todo.add / 写周报
    "完成待办 买牛奶"    -> todo.done / 买牛奶
    "删除待办 买牛奶"    -> todo.remove / 买牛奶

设计原则：
    - 纯函数，不 import 项目其它模块，便于单元测试；
    - 只认**整句精确匹配**（或很严格的句式），宁可漏识别交给 LLM，也不要把正常聊天
      误判成命令；
    - 返回 None 表示"不是本地命令"，调用方继续走正常对话流程。
"""

import re
from typing import Optional, Tuple

# (动作, 参数)；参数为空串表示不需要。
LocalCommand = Tuple[str, str]

_MODE_PHRASES = {
    # 专注 / 安静
    "专注模式": "focus",
    "进入专注模式": "focus",
    "安静": "focus",
    "安静模式": "focus",
    "安静一点": "focus",
    "安静一下": "focus",
    "安静一会儿": "focus",
    "别说话了": "focus",
    # 普通
    "普通模式": "normal",
    "正常模式": "normal",
    # 陪聊
    "陪我聊天": "companion",
    "陪聊模式": "companion",
    "陪伴模式": "companion",
    "活跃一点": "companion",
    # 自动
    "自动模式": "auto",
    "你自己看着办": "auto",
}

# "恢复"类：如果当前在安静模式就恢复原模式，否则回到自动。
_RESUME_PHRASES = {"恢复", "恢复正常", "可以说话了", "继续说话", "说话吧"}

# 待办 / 目标：正则在"去掉空格后的整句"上匹配，避免 ASR 插入空格导致漏识别。
_TODO_ADD_PATTERNS = [
    r"^(?:帮我)?(?:添加|新增|加个|加一个)待办[:：,，]?(?P<x>.+)$",
    r"^待办(?:加|添加)[:：,，]?(?P<x>.+)$",
    r"^待办[:：,，](?P<x>.+)$",
    r"^把(?P<x>.+?)(?:加入|加到|放进|放到|记到)待办$",
]
_TODO_DONE_PATTERNS = [
    r"^完成待办[:：,，]?(?P<x>.+)$",
    r"^(?:我)?(?:做完|完成)了待办[:：,，]?(?P<x>.+)$",
    r"^把待办(?P<x>.+?)(?:标记为)?完成$",
    r"^待办(?P<x>.+?)完成了?$",
]
_TODO_REMOVE_PATTERNS = [
    r"^(?:删除|删掉|移除)待办[:：,，]?(?P<x>.+)$",
    r"^把(?P<x>.+?)从待办(?:里|中)?(?:删除|删掉|移除)$",
]
_GOAL_ADD_PATTERNS = [
    r"^(?:添加|新增|加个|加一个)目标[:：,，]?(?P<x>.+)$",
    r"^把(?P<x>.+?)(?:加入|加到|设为)目标$",
]
_GOAL_REMOVE_PATTERNS = [
    r"^(?:删除|删掉|移除)目标[:：,，]?(?P<x>.+)$",
    r"^把(?P<x>.+?)从目标(?:里|中)?(?:删除|删掉|移除)$",
]

_TRAILING_PUNC = "。！？!?，,、；; \t\r\n"


def _clean(text: str) -> str:
    return (text or "").strip().strip(_TRAILING_PUNC)


def _match_any(patterns, text: str) -> Optional[str]:
    for pat in patterns:
        m = re.match(pat, text)
        if m:
            item = _clean(m.group("x"))
            if item:
                return item
    return None


def parse_local_command(text: str) -> Optional[LocalCommand]:
    """把一句话解析成本地命令；不是本地命令时返回 None。"""
    t = _clean(text)
    if not t:
        return None
    # 中文里 ASR 偶尔会插空格，匹配前统一去掉；命令本身都不含空格。
    compact = re.sub(r"\s+", "", t)

    if compact in _RESUME_PHRASES:
        return "mode.resume", ""
    if compact in _MODE_PHRASES:
        return "mode.set", _MODE_PHRASES[compact]

    # 顺序：更具体的"完成/删除"先于"添加"，避免"删除待办X"被当成添加。
    for action, patterns in (
        ("todo.done", _TODO_DONE_PATTERNS),
        ("todo.remove", _TODO_REMOVE_PATTERNS),
        ("goal.remove", _GOAL_REMOVE_PATTERNS),
        ("todo.add", _TODO_ADD_PATTERNS),
        ("goal.add", _GOAL_ADD_PATTERNS),
    ):
        item = _match_any(patterns, compact)
        if item:
            return action, item
    return None
