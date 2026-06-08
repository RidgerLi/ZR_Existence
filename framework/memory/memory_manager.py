"""
分层记忆管理器（MemoryManager）—— 串行管线版
Author: ZerolanLiveRobot

承接 `LLMPromptManager` 滑动窗口的"真实对话"，做一条**串行**的记忆流水（不再双写）：

    热区 hot（逐字）：最近 hot_window_size 条原文，由 bot 直接发给 LLM。

    温区 warm（近期回顾 / recent_digest）：工作窗口内、热区之外的较早原文，由后台线程用
        `light_digest` 整理成一份分时段、保真的客观回顾，常驻 prompt。这是 prompt 里唯一的
        "近期线性记忆"（已取消独立会话摘要）。recent_digest 始终是温区当前原文的纯函数
        （重建、不漂移），只在温区那段实际变化（且变化够一批）时才重新调 LLM。

    长期记忆 L2b（向量库，逐条原文）：被滑出整个工作窗口的原文，经"价值过滤"后**逐条原样**
        写入向量库（保真，便于精确语义召回），太短/无信息的发言直接丢弃。prompt 构建时按当前
        输入检索 top-k 回拼（由 bot 负责）。

线程安全：
    - 被裁内容通过 `enqueue_evicted` 即时过滤后写向量库（写库回调在锁外执行）。
    - 后台 `run_recent_digest(should_continue)` 周期性重建 recent_digest；LLM 调用在锁外进行。
    - `recent_digest` 为不可变 str，读写都是原子赋值，读取无需加锁。
"""

import re
import threading
import time
from typing import Callable, List

from loguru import logger
from zerolan.data.pipeline.llm import Conversation

from agent.api import light_digest


# 价值过滤：去掉首尾空白后长度过短、或整句就是这些无信息口头语的发言，不写入向量库。
_TRIVIAL_PHRASES = {
    "嗯", "嗯嗯", "哦", "噢", "哦哦", "啊", "哎", "哎呀", "唉", "诶",
    "好", "好的", "好好", "好啊", "行", "行吧", "可以", "对", "对啊", "是的", "是",
    "知道了", "知道啦", "懂了", "明白", "明白了", "没事", "没有", "嗯哼", "哈", "哈哈",
}
_VEC_MIN_LEN = 6  # 去标点空白后长度小于此值直接丢弃


def _is_low_value(text: str) -> bool:
    """判断一条发言是否"没有入库价值"（纯口头语/过短）。"""
    s = (text or "").strip()
    if not s:
        return True
    if s in _TRIVIAL_PHRASES:
        return True
    # 去掉标点/空白后再看长度，避免"好的。"这种被算成有效长度。
    core = re.sub(r"[\s，。！？、~…,.!?·\-—\"'（）()【】\[\]]+", "", s)
    if len(core) < _VEC_MIN_LEN and core in _TRIVIAL_PHRASES:
        return True
    if len(core) < _VEC_MIN_LEN:
        return True
    return False


class MemoryManager:
    def __init__(self,
                 hot_window_size: int = 8,
                 recent_digest_interval_s: float = 30.0,
                 digest_min_delta: int = 4,
                 digester: Callable[[List[Conversation]], str] | None = None,
                 live_turns_provider: Callable[[], List[Conversation]] | None = None,
                 vec_line_sink: Callable[[Conversation], None] | None = None):
        """
        :param hot_window_size: 工作窗口中"逐字保留"的最近轮次数；其余在窗口内的较早轮次由温区回顾表示。
        :param recent_digest_interval_s: 温区"近期回顾"重建的检查周期（秒）。
        :param digest_min_delta: 温区原文较上次重建至少新增这么多条，才重新调 LLM（批量，省调用）。
        :param digester: 温区回顾整理函数，默认用 agent.api.light_digest。
        :param live_turns_provider: 返回当前工作窗口真实对话（用于切出温区那段）。
        :param vec_line_sink: 把"被驱逐的单条原文"写入向量库的回调（由 bot 提供）。
        """
        self._hot_window_size = hot_window_size
        self._recent_digest_interval_s = recent_digest_interval_s
        self._digest_min_delta = max(1, digest_min_delta)
        self._digester = digester or self._default_digest
        self._live_turns_provider = live_turns_provider
        self.vec_line_sink = vec_line_sink

        self.recent_digest: str = ""        # 温区回顾（窗口内较早轮次的"近期回顾"）
        self._digest_sig = None             # 上次温区内容签名（条数 + 末条），未变则不重复调 LLM
        self._digest_len = 0                # 上次重建时温区的条数（用于 digest_min_delta 批量判断）

    def set_live_turns_provider(self, provider: Callable[[], List[Conversation]]) -> None:
        self._live_turns_provider = provider

    def get_recent_digest(self) -> str:
        return self.recent_digest

    @staticmethod
    def _default_digest(items: List[Conversation]) -> str:
        return light_digest(items)

    def enqueue_evicted(self, turns: List[Conversation]) -> None:
        """接收被滑出整个工作窗口的真实对话：逐条做价值过滤后，原样写入向量库（长期记忆 L2b）。
        由 LLMPromptManager 的裁剪回调调用。写库异常被吞掉，不影响主流程。
        """
        if not turns or self.vec_line_sink is None:
            return
        for t in turns:
            try:
                if _is_low_value(getattr(t, "content", "")):
                    continue
                self.vec_line_sink(t)
            except Exception as e:
                logger.debug(f"vec line ingest skipped: {e}")

    def run_recent_digest(self, should_continue: Callable[[], bool]) -> None:
        """后台温区循环：把"窗口内较早的那段对话"（除最近 hot_window_size 条外）整理成 recent_digest。

        recent_digest 始终是温区当前内容的纯函数：温区为空则清空，温区内容变化（且达批量阈值）才重新调 LLM。
        """
        logger.info("MemoryManager recent-digest loop started.")
        while should_continue():
            time.sleep(self._recent_digest_interval_s)
            try:
                self._maybe_digest()
            except Exception as e:
                logger.exception(e)
        logger.info("MemoryManager recent-digest loop stopped.")

    def _maybe_digest(self) -> None:
        if self._live_turns_provider is None or self._hot_window_size <= 0:
            return
        live = list(self._live_turns_provider() or [])
        older = live[:-self._hot_window_size] if len(live) > self._hot_window_size else []
        if not older:
            if self.recent_digest:
                self.recent_digest = ""
                self._digest_sig = None
                self._digest_len = 0
            return
        sig = (len(older), older[-1].content, getattr(older[-1], "metadata", None))
        if sig == self._digest_sig:
            return  # 温区那段没变，不必重复整理
        # 批量：温区内容虽变，但相比上次重建新增不足 digest_min_delta 条时，先攒着（除非是首次或缩短）。
        if self.recent_digest and 0 < (len(older) - self._digest_len) < self._digest_min_delta \
                and len(older) >= self._digest_len:
            return
        digest = (self._digester(older) or "").strip()
        if digest:
            self.recent_digest = digest
            self._digest_sig = sig
            self._digest_len = len(older)
            logger.debug(f"MemoryManager recent digest rebuilt ({len(digest)} chars, from {len(older)} turns).")
