"""
对话轮次状态（Conversation turn-taking state）
Author: ZerolanLiveRobot

背景：
    `event.event_emitter.SyncTaskExecutor` 用的是无上限的线程池（max_workers=None），
    因此所有同步事件处理器是**并发**执行的。当 VAD 把用户连续的一句话切成多个片段时，
    每个片段都会并发地触发一次完整的 LLM + TTS 流水线，导致机器人"自言自语"、回复互相
    叠加、答非所问。

这个模块提供一个线程安全的组件来根治该问题：

    ConversationState
        一块集中的"状态白板"。记录机器人当前是否在思考（LLM 生成中）、是否还有 TTS
        任务在排队/播放，以及距离上次活动过了多久。所有输入源只往这里写状态、读状态，
        由调用方据此决定要不要现在开口、要不要把输入缓存到下一轮。

    延迟优先策略：不做发声去抖合并。ASR 片段一到就直接交给轮次锁——AI 空闲立刻回复，
    AI 正忙则缓存到 pending，等本轮说完后合并补发，从而避免并发触发多次回复。

该组件不依赖项目中的任何其它模块，可独立测试。
"""

import threading
import time
from typing import Callable, List, Optional

from loguru import logger

# 句末标点：合并片段时若前一段已自带句末标点，则不再额外补逗号。
_SENTENCE_END_PUNC = "，。！？,.!?、…；;："


def join_fragments(parts: List[str]) -> str:
    """把多个 ASR 片段拼成一句话。

    片段之间若没有自然的句末标点，则补一个逗号，避免两段文字粘连成一个词。
    """
    out = ""
    for p in parts:
        if p is None:
            continue
        p = p.strip()
        if not p:
            continue
        if out and out[-1] not in _SENTENCE_END_PUNC:
            out += "，"
        out += p
    return out


class ConversationState:
    """线程安全的集中对话状态。

    "AI 正忙"的判定由三部分组成，任意一个成立即视为忙：
        - 正在思考：LLM 正在生成回复（begin_thinking / end_thinking 之间）；
        - 有在途 TTS：已经派发给 TTS 但还没生成/播放完的子句计数 > 0；
        - 扬声器正在出声：通过外部注入的探针函数判定（见 set_speaker_busy_probe）。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._ai_thinking = False
        self._inflight_tts = 0
        self._pending: List[str] = []
        self._last_activity = time.monotonic()
        self._speaker_busy_probe: Optional[Callable[[], bool]] = None

    def set_speaker_busy_probe(self, probe: Optional[Callable[[], bool]]) -> None:
        """注入一个"扬声器是否正在播放"的探针。返回 True 表示仍在出声。"""
        self._speaker_busy_probe = probe

    # ---- 思考状态 ----------------------------------------------------------
    def begin_thinking(self) -> None:
        with self._lock:
            self._ai_thinking = True
            self._last_activity = time.monotonic()

    def end_thinking(self) -> None:
        with self._lock:
            self._ai_thinking = False

    # ---- 在途 TTS 计数 -----------------------------------------------------
    def tts_submitted(self) -> None:
        with self._lock:
            self._inflight_tts += 1
            self._last_activity = time.monotonic()

    def tts_done(self) -> None:
        with self._lock:
            if self._inflight_tts > 0:
                self._inflight_tts -= 1

    def reset_tts(self) -> None:
        """打断/清空 TTS 时复位在途计数（为后续 barge-in 预留）。"""
        with self._lock:
            self._inflight_tts = 0

    # ---- 综合忙碌判定 ------------------------------------------------------
    def is_ai_busy(self) -> bool:
        with self._lock:
            if self._ai_thinking or self._inflight_tts > 0:
                return True
        probe = self._speaker_busy_probe
        if probe is not None:
            try:
                if probe():
                    return True
            except Exception as e:
                logger.exception(e)
        return False

    # ---- 待处理输入缓冲 ----------------------------------------------------
    def push_pending(self, text: str) -> None:
        if text is None:
            return
        text = text.strip()
        if not text:
            return
        with self._lock:
            self._pending.append(text)
            self._last_activity = time.monotonic()

    def has_pending(self) -> bool:
        with self._lock:
            return len(self._pending) > 0

    def drain_pending(self) -> Optional[str]:
        """取出并清空所有缓冲输入，合并成一句返回。无缓冲时返回 None。"""
        with self._lock:
            if not self._pending:
                return None
            parts = self._pending
            self._pending = []
        return join_fragments(parts)

    # ---- 活动时间（供主动开口 / 记忆整理的空闲判定使用）-------------------
    def note_activity(self) -> None:
        with self._lock:
            self._last_activity = time.monotonic()

    def idle_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_activity
