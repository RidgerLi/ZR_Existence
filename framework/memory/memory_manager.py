"""
分层记忆管理器（MemoryManager）—— 串行管线 / 水位滑动窗口版
Author: ZerolanLiveRobot

承接 `LLMPromptManager` 滑动窗口的"真实对话"，做一条**串行**的记忆流水（不再双写）：

    热区 hot（逐字）：窗口尾部、温区还没覆盖到的那些最近原文，由 bot 直接发给 LLM。
        热区大小在 [hot_low, hot_high] 之间浮动（滞回/水位）：每次只要逐字未压缩的轮次超过
        hot_high，后台就把最旧的那批折叠进温区，使热区回落到 hot_low。

    温区 warm（近期回顾 / recent_digest）：被折叠出热区、但仍在工作窗口内的较早原文，由后台线程用
        `light_digest` 整理成一份分时段、保真的客观回顾，常驻 prompt。这是 prompt 里唯一的
        "近期线性记忆"（已取消独立会话摘要）。recent_digest 是"温区当前覆盖段原文"的纯函数
        （重建、不漂移），只在触发折叠（逐字轮次越过 hot_high 水位）时才重新调 LLM。

    长期记忆 L2b（向量库，逐条原文）：被滑出整个工作窗口的原文，经"价值过滤"后**逐条原样**
        写入向量库（保真，便于精确语义召回），太短/无信息的发言直接丢弃。prompt 构建时按当前
        输入检索 top-k 回拼（由 bot 负责）。

无缝保证（热区 ↔ 温区不漏轮次）：
    `_digest_len` 精确记录"温区当前覆盖了工作窗口前多少条原文"。bot 拼 prompt 时，逐字段
    取 `live[_digest_len:]`（即未被温区覆盖的全部尾部轮次，至少 hot_low 条）。这样无论后台
    压缩是否及时跟上，任何一条都不会同时缺席热区与温区。窗口前端发生驱逐时，`_digest_len`
    同步递减、`light_digest` 折叠提交前做乐观并发校验，确保该计数始终准确。

线程安全：
    - 被裁内容通过 `enqueue_evicted` 即时过滤后写向量库（写库回调在锁外执行），同时同步 _digest_len。
    - 后台 `run_recent_digest(should_continue)` 由 `notify()`（提交对话即时唤醒）+ 安全周期驱动；
      LLM 调用在锁外进行，提交前用驱逐代次（_evict_gen）做乐观校验，期间若发生驱逐则本次作废。
    - `recent_digest` / `_digest_len` 的读写都在 `_lock` 内（LLM 调用除外）。
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
                 hot_low: int = 8,
                 hot_high: int = 12,
                 safety_interval_s: float = 30.0,
                 digester: Callable[[List[Conversation]], str] | None = None,
                 live_turns_provider: Callable[[], List[Conversation]] | None = None,
                 vec_line_sink: Callable[[Conversation], None] | None = None):
        """
        :param hot_low: 折叠后热区回落到的"低水位"逐字轮次数（也是 prompt 逐字段的下限）。<=0 表示关闭热/温分层。
        :param hot_high: 逐字未压缩轮次的"高水位"；越过它就触发一次后台折叠，把最旧的并进温区。
        :param safety_interval_s: 后台折叠循环的兜底检查周期（秒）；正常折叠由 notify() 即时驱动，此处仅自愈。
        :param digester: 温区回顾整理函数，默认用 agent.api.light_digest。
        :param live_turns_provider: 返回当前工作窗口真实对话（用于切出温区那段）。
        :param vec_line_sink: 把"被驱逐的单条原文"写入向量库的回调（由 bot 提供）。
        """
        self._hot_low = max(0, int(hot_low))
        # 高水位至少要比低水位大 1，否则折叠后立刻又越界。
        self._hot_high = max(self._hot_low + 1, int(hot_high)) if self._hot_low > 0 else max(1, int(hot_high))
        self._safety_interval_s = max(1.0, float(safety_interval_s))
        self._digester = digester or self._default_digest
        self._live_turns_provider = live_turns_provider
        self.vec_line_sink = vec_line_sink

        self._lock = threading.RLock()
        self._wake = threading.Event()      # 提交对话后唤醒后台折叠（事件驱动，替代 30s 轮询触发）
        self.recent_digest: str = ""        # 温区回顾（覆盖工作窗口前 _digest_len 条原文）
        self._digest_len = 0                # 温区当前覆盖了工作窗口前多少条原文（无缝拼接的关键）
        self._evict_gen = 0                 # 窗口前端驱逐代次，用于折叠提交时的乐观并发校验

    def set_live_turns_provider(self, provider: Callable[[], List[Conversation]]) -> None:
        self._live_turns_provider = provider

    def get_recent_digest(self) -> str:
        return self.recent_digest

    def digest_covered_count(self) -> int:
        """温区当前覆盖了工作窗口前多少条原文。bot 拼 prompt 时据此切出逐字段，保证无缝。"""
        with self._lock:
            return self._digest_len

    def seed_digest_len(self, n: int) -> None:
        """启动时用持久化的 recent_digest 还原覆盖计数（避免重启后逐字段把整窗重发一遍）。"""
        with self._lock:
            self._digest_len = max(0, int(n))

    def notify(self) -> None:
        """提交一轮对话后调用：即时唤醒后台折叠循环（无需等待安全周期）。"""
        self._wake.set()

    @staticmethod
    def _default_digest(items: List[Conversation]) -> str:
        return light_digest(items)

    def enqueue_evicted(self, turns: List[Conversation]) -> None:
        """接收被滑出整个工作窗口的真实对话：逐条做价值过滤后，原样写入向量库（长期记忆 L2b）。
        同时把 `_digest_len` 按驱逐条数同步前移（窗口前端被裁，温区覆盖的起点也随之左移），
        并递增驱逐代次，使正在进行的折叠提交能检测到窗口已变。由 LLMPromptManager 的裁剪回调调用。
        """
        if not turns:
            return
        with self._lock:
            self._evict_gen += 1
            if self._digest_len > 0:
                self._digest_len = max(0, self._digest_len - len(turns))
        if self.vec_line_sink is None:
            return
        for t in turns:
            try:
                if _is_low_value(getattr(t, "content", "")):
                    continue
                self.vec_line_sink(t)
            except Exception as e:
                logger.debug(f"vec line ingest skipped: {e}")

    def run_recent_digest(self, should_continue: Callable[[], bool]) -> None:
        """后台温区折叠循环：逐字未压缩轮次越过 hot_high 时，把最旧的一批折叠进 recent_digest。

        事件驱动（notify）+ 安全周期（safety_interval_s）双触发；recent_digest 始终是其覆盖段原文的纯函数。
        """
        logger.info("MemoryManager recent-digest loop started (watermark mode).")
        while should_continue():
            self._wake.wait(timeout=self._safety_interval_s)
            self._wake.clear()
            if not should_continue():
                break
            try:
                self._maybe_digest()
            except Exception as e:
                logger.exception(e)
        logger.info("MemoryManager recent-digest loop stopped.")

    def _maybe_digest(self) -> None:
        provider = self._live_turns_provider
        if provider is None or self._hot_low <= 0:
            return
        live = list(provider() or [])
        with self._lock:
            covered = max(0, min(self._digest_len, len(live)))
            gen0 = self._evict_gen
        tail = len(live) - covered   # 逐字未压缩的轮次数
        if tail <= self._hot_high:
            return  # 仍在水位带内，无需折叠
        fold_to = len(live) - self._hot_low  # 把热区收回到低水位，多出的折叠进温区
        if fold_to <= covered:
            return
        # LLM 调用在锁外：重建覆盖 live[:fold_to] 的回顾（纯函数，重建不漂移）。
        digest = (self._digester(live[:fold_to]) or "").strip()
        if not digest:
            return
        with self._lock:
            if self._evict_gen != gen0:
                return  # 折叠期间窗口前端发生过驱逐，本次作废，下个周期/唤醒重来
            self.recent_digest = digest
            self._digest_len = fold_to
        logger.debug(f"MemoryManager recent digest folded "
                     f"({len(digest)} chars, covering first {fold_to} of {len(live)} turns).")
