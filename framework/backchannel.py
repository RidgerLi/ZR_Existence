"""
垫话脚本（Backchannel script）
Author: ZerolanLiveRobot

"垫话"是用户刚说完、AI 还没开始回复时先播的那一小句（如"嗯？"）。它只负责填住
等待空档，不进入对话历史。这里把"说什么"从代码里抽出来，放进一个可以随手改的 JSON：

    resources/backchannel.json
    ┌──────────────────────────────────────────────┐
    │ {
    │   "avoid_repeat_window": 3,
    │   "phrases": [
    │     {"text": "嗯？",   "weight": 3, "tags": ["question"]},
    │     {"text": "怎么啦", "weight": 2, "tags": ["question"]},
    │     {"text": "嗯…",    "weight": 2, "tags": ["statement"]},
    │     {"text": "在呢",   "weight": 1, "tags": ["any"]}
    │   ]
    │ }
    └──────────────────────────────────────────────┘

字段说明：
    phrases[*].text    要念的文本（建议 1~4 个字；太长会拖慢真正回复的开播时间）
    phrases[*].weight  相对权重，默认 1
    phrases[*].tags    适用场景：question（用户像在提问）/ statement（陈述）/ any（通用）
                       —— 这就是"不那么呆"的关键：问句回"嗯？/怎么啦"，陈述回"嗯嗯/在呢"。
    avoid_repeat_window  最近 N 次用过的句子不重复（默认 3）

文件缺失时会自动生成一份默认脚本；改完保存即可，下次触发自动生效（按文件修改时间热加载），
新增的句子会在后台用当前音色合成，合成好之后才会被使用。
"""

import json
import os
import re
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

from loguru import logger

TAG_QUESTION = "question"
TAG_STATEMENT = "statement"
TAG_ANY = "any"
_VALID_TAGS = {TAG_QUESTION, TAG_STATEMENT, TAG_ANY}

# 默认脚本：短、口语、按问句/陈述分开，避免永远只有一句"嗯？"
DEFAULT_PHRASES = [
    {"text": "嗯？", "weight": 3, "tags": [TAG_QUESTION]},
    {"text": "怎么啦", "weight": 2, "tags": [TAG_QUESTION]},
    {"text": "嗯…", "weight": 2, "tags": [TAG_STATEMENT]},
    {"text": "嗯嗯", "weight": 2, "tags": [TAG_STATEMENT]},
    {"text": "在呢", "weight": 1, "tags": [TAG_ANY]},
    {"text": "你说", "weight": 1, "tags": [TAG_STATEMENT]},
]
DEFAULT_AVOID_REPEAT = 3

_QUESTION_RE = re.compile(r"[?？]|吗|什么|怎么|为什么|哪儿|哪里|哪个|谁|几点|多少|是不是|能不能|可不可以|要不要|好不好")


@dataclass(frozen=True)
class Phrase:
    text: str
    weight: float = 1.0
    tags: tuple = (TAG_ANY,)

    def matches(self, tag: str) -> bool:
        return TAG_ANY in self.tags or tag in self.tags


@dataclass
class Script:
    phrases: List[Phrase] = field(default_factory=list)
    avoid_repeat_window: int = DEFAULT_AVOID_REPEAT
    source: str = "default"


def looks_like_question(text: str) -> bool:
    """用很轻的启发式判断"用户是不是在问问题"，只用于选垫话的语气，不参与业务逻辑。"""
    return bool(_QUESTION_RE.search(text or ""))


def _parse_entry(entry) -> Optional[Phrase]:
    if isinstance(entry, str):
        text = entry.strip()
        return Phrase(text=text, weight=1.0, tags=(TAG_ANY,)) if text else None
    if not isinstance(entry, dict):
        return None
    text = str(entry.get("text", "")).strip()
    if not text:
        return None
    try:
        weight = float(entry.get("weight", 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    if weight <= 0:
        weight = 1.0
    raw_tags = entry.get("tags")
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = tuple(t for t in (raw_tags or [TAG_ANY]) if t in _VALID_TAGS) or (TAG_ANY,)
    return Phrase(text=text, weight=weight, tags=tags)


def parse_script(data: dict, source: str = "inline") -> Script:
    """把已解析的 JSON 字典变成 Script；格式不对时退回默认脚本。"""
    if not isinstance(data, dict):
        return default_script(source=source)
    raw = data.get("phrases") or []
    phrases: List[Phrase] = []
    seen = set()
    for entry in raw:
        p = _parse_entry(entry)
        if p and p.text not in seen:
            phrases.append(p)
            seen.add(p.text)
    if not phrases:
        logger.warning(f"Backchannel script at {source} has no usable phrase; using defaults.")
        return default_script(source=source)
    try:
        window = int(data.get("avoid_repeat_window", DEFAULT_AVOID_REPEAT))
    except (TypeError, ValueError):
        window = DEFAULT_AVOID_REPEAT
    return Script(phrases=phrases, avoid_repeat_window=max(0, window), source=source)


def default_script(source: str = "default") -> Script:
    return parse_script({"phrases": DEFAULT_PHRASES, "avoid_repeat_window": DEFAULT_AVOID_REPEAT}, source=source)


def script_to_json(script: Optional[Script] = None) -> str:
    script = script or default_script()
    payload = {
        "_comment": "垫话配置：用户刚开口、AI 还没开始回复时先播的短句。改完保存即可，热加载无需重启。",
        "avoid_repeat_window": script.avoid_repeat_window,
        "phrases": [{"text": p.text, "weight": p.weight, "tags": list(p.tags)} for p in script.phrases],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def ensure_file(path: str | Path) -> bool:
    """文件不存在时写一份默认脚本，方便用户直接改。返回是否新创建。"""
    p = Path(path)
    if p.exists():
        return False
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(script_to_json(), encoding="utf-8")
        logger.info(f"Backchannel script created: {p}")
        return True
    except Exception as e:
        logger.warning(f"Failed to create backchannel script at {p}: {e}")
        return False


def load_script(path: str | Path) -> Script:
    """读 JSON；文件缺失/损坏/为空时退回默认脚本（不抛异常）。"""
    p = Path(path)
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return parse_script(data, source=str(p))
    except FileNotFoundError:
        return default_script(source="default(missing)")
    except Exception as e:
        logger.warning(f"Failed to load backchannel script {p}: {e}; using defaults.")
        return default_script(source="default(error)")


def choose_phrase(script: Script, tag: str, recent: Sequence[str] = ()) -> Optional[Phrase]:
    """按场景标签筛选 + 避开最近用过的 + 按权重随机。"""
    if not script.phrases:
        return None
    candidates = [p for p in script.phrases if p.matches(tag)] or list(script.phrases)
    recent_set = set(recent)
    fresh = [p for p in candidates if p.text not in recent_set] or candidates
    weights = [max(0.01, p.weight) for p in fresh]
    return random.choices(fresh, weights=weights, k=1)[0]
