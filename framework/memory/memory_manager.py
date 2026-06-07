"""
分层记忆管理器（MemoryManager）
Author: ZerolanLiveRobot

承接 `LLMPromptManager` 滑动窗口裁掉的"真实对话"，做后台压缩与长期入库：

    L3b 会话摘要（轻压缩）：被滑出工作窗口的对话，由后台轻线程用 `summary_history` 增量
        折叠进一段 `session_summary`（关键节点/结论/承诺要点），回拼进 prompt（"之前聊过的内容"）。

    L2b 长期记忆（事实抽取 + 向量库，Phase 3b）：真实对话累计很多后，最旧的大块经
        `extract_durable_facts` 只抽"以后仍成立的耐久事实"写入向量库（Milvus history_collection）；
        prompt 构建时按当前输入检索 top-k 回拼。刻意与会话摘要分离：摘要记"发生了什么"，
        长期记忆记"以后还用得上的事实"，避免把一次性情节/瞬时状态当事实检索回来。

线程安全：
    - 被裁内容通过 `enqueue_evicted` 进入待压缩缓冲（加锁）。
    - 后台 `run(should_continue)` 轻线程周期性把缓冲折叠进 `session_summary`；LLM 调用在锁外进行。
    - `session_summary` 为不可变 str，读写都是原子赋值，读取无需加锁。
"""

import threading
import time
from typing import Callable, List

from loguru import logger
from zerolan.data.pipeline.llm import Conversation, RoleEnum

from agent.api import summary_history, light_digest, extract_durable_facts


class MemoryManager:
    def __init__(self,
                 light_interval_s: float = 60.0,
                 light_min_pending: int = 4,
                 heavy_interval_s: float = 120.0,
                 long_term_threshold: int = 40,
                 hot_window_size: int = 10,
                 recent_digest_interval_s: float = 30.0,
                 summarizer: Callable[[List[Conversation]], str] | None = None,
                 digester: Callable[[List[Conversation]], str] | None = None,
                 archiver: Callable[[List[Conversation]], str] | None = None,
                 live_turns_provider: Callable[[], List[Conversation]] | None = None,
                 archive_sink: Callable[[str], None] | None = None):
        """
        :param light_interval_s: 轻压缩线程的检查周期（秒）。
        :param light_min_pending: 待压缩缓冲累计到多少条才触发一次轻压缩（避免频繁调 LLM）。
        :param heavy_interval_s: 重压缩（长期入库）线程的检查周期（秒）。
        :param long_term_threshold: 长期缓冲累计到多少条真实对话才触发一次重压缩 + 入库。
        :param hot_window_size: 工作窗口中"逐字保留"的最近轮次数；其余在窗口内的较早轮次由温区摘要表示。
        :param recent_digest_interval_s: 温区"近期回顾"重建的检查周期（秒）。
        :param summarizer: 会话摘要函数（关键节点要点），默认用 agent.api.summary_history。
        :param digester: 温区轻压缩函数，默认用 agent.api.light_digest。
        :param archiver: 长期记忆抽取函数（只抽耐久事实），默认用 agent.api.extract_durable_facts。
                         与 summarizer 分离：会话摘要记"本次发生了什么"，长期记忆只记"以后仍成立的事实"。
        :param live_turns_provider: 返回当前工作窗口真实对话（用于切出温区那段）。
        :param archive_sink: 把重压缩后的长期记忆文本落库的回调（由 bot 提供，写入向量库）。
        """
        self._light_interval_s = light_interval_s
        self._light_min_pending = light_min_pending
        self._heavy_interval_s = heavy_interval_s
        self._long_term_threshold = long_term_threshold
        self._hot_window_size = hot_window_size
        self._recent_digest_interval_s = recent_digest_interval_s
        self._summarizer = summarizer or self._default_summarize
        self._digester = digester or self._default_digest
        self._archiver = archiver or self._default_archive
        self._live_turns_provider = live_turns_provider
        self.archive_sink = archive_sink

        self._lock = threading.Lock()
        self._pending: List[Conversation] = []      # 轻压缩缓冲（→ session_summary）
        self._lt_buffer: List[Conversation] = []     # 长期缓冲（→ 重压缩入库）
        self.session_summary: str = ""
        self.recent_digest: str = ""                  # 温区轻摘要（窗口内较早轮次的"近期回顾"）
        self._digest_sig = None                       # 上次温区内容签名，未变则不重复调 LLM

    def set_live_turns_provider(self, provider: Callable[[], List[Conversation]]) -> None:
        self._live_turns_provider = provider

    def get_recent_digest(self) -> str:
        return self.recent_digest

    @staticmethod
    def _default_summarize(items: List[Conversation]) -> str:
        return summary_history(items).content

    @staticmethod
    def _default_digest(items: List[Conversation]) -> str:
        return light_digest(items)

    @staticmethod
    def _default_archive(items: List[Conversation]) -> str:
        return extract_durable_facts(items)

    def enqueue_evicted(self, turns: List[Conversation]) -> None:
        """接收被滑出工作窗口的真实对话。同时进入轻压缩缓冲（→会话摘要）与长期缓冲（→重压缩入库）。
        由 LLMPromptManager 的裁剪回调调用。
        """
        if not turns:
            return
        with self._lock:
            self._pending.extend(turns)
            self._lt_buffer.extend(turns)

    def get_session_summary(self) -> str:
        return self.session_summary

    def run(self, should_continue: Callable[[], bool]) -> None:
        """后台轻压缩循环：周期性把待压缩缓冲增量折叠进 session_summary。"""
        logger.info("MemoryManager light-compression loop started.")
        while should_continue():
            time.sleep(self._light_interval_s)
            try:
                self._maybe_compress()
            except Exception as e:
                logger.exception(e)
        logger.info("MemoryManager light-compression loop stopped.")

    def _maybe_compress(self) -> None:
        with self._lock:
            if len(self._pending) < self._light_min_pending:
                return
            batch = self._pending[:]
            self._pending.clear()

        items: List[Conversation] = []
        old = self.session_summary
        if old:
            items.append(Conversation(role=RoleEnum.system, content="已有的对话摘要：" + old))
        items.extend(batch)

        new_summary = (self._summarizer(items) or "").strip()
        if new_summary:
            self.session_summary = new_summary
            logger.debug(f"MemoryManager session summary updated ({len(new_summary)} chars).")

    def run_recent_digest(self, should_continue: Callable[[], bool]) -> None:
        """后台温区循环：把"窗口内较早的那段对话"（除最近 hot_window_size 条外）轻压缩成 recent_digest。

        recent_digest 始终是温区当前内容的纯函数：温区为空则清空，温区内容变化才重新调 LLM。
        因此不会与更老的 session_summary 重复表示同一批对话。
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
            return
        sig = (len(older), older[-1].content, getattr(older[-1], "metadata", None))
        if sig == self._digest_sig:
            return  # 温区那段没变，不必重复压缩
        digest = (self._digester(older) or "").strip()
        if digest:
            self.recent_digest = digest
            self._digest_sig = sig
            logger.debug(f"MemoryManager recent digest rebuilt ({len(digest)} chars, from {len(older)} turns).")

    def run_archive(self, should_continue: Callable[[], bool]) -> None:
        """后台重压缩循环：长期缓冲累计够多时，把最旧的一大块重压缩并落入向量库（长期记忆 L2b）。"""
        logger.info("MemoryManager heavy-archive loop started.")
        while should_continue():
            time.sleep(self._heavy_interval_s)
            try:
                self._maybe_archive()
            except Exception as e:
                logger.exception(e)
        logger.info("MemoryManager heavy-archive loop stopped.")

    def _maybe_archive(self) -> None:
        if self.archive_sink is None:
            return
        with self._lock:
            if len(self._lt_buffer) < self._long_term_threshold:
                return
            # 取出最旧的一整块（阈值大小）做重压缩，剩余的留到下次。
            block = self._lt_buffer[:self._long_term_threshold]
            self._lt_buffer = self._lt_buffer[self._long_term_threshold:]

        # 长期记忆只抽"耐久事实"，与会话摘要（关键节点流水）分离，避免把过期情节当事实入库。
        digest = (self._archiver(block) or "").strip()
        if not digest:
            return
        try:
            self.archive_sink(digest)
            logger.info(f"MemoryManager archived a long-term memory ({len(digest)} chars).")
        except Exception as e:
            logger.exception(e)
