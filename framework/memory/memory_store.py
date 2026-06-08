"""
记忆持久化（MemoryStore）
Author: ZerolanLiveRobot

把"工作窗口对话历史 + 近期回顾(温区) + 对用户的印象 + 计数器"序列化成人类可读的 Markdown
（resources/memory/memory.md），启动时解析载入，运行中每次更新由后台 IO 线程异步写回
（合并写，避免频繁阻塞主流程）。

注意：
    - 只持久化工作窗口的真实对话（user/assistant 轮次）、温区近期回顾、对用户的印象，以及对话
      计数器；长期记忆已在向量库、自我目标/待办已在 self_state.json，各自独立持久化。
    - 文件格式可往返解析（见 _parse / _render）。请勿手动破坏标题格式。
"""

import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from loguru import logger
from zerolan.data.pipeline.llm import Conversation, RoleEnum

_ROLE_MAP = {"user": RoleEnum.user, "assistant": RoleEnum.assistant, "system": RoleEnum.system}
_EMPTY = "（空）"


@dataclass
class MemoryState:
    turns: List[Conversation] = field(default_factory=list)
    recent_digest: str = ""
    user_impression: str = ""
    turn_counter: int = 0
    last_impression_at: int = 0


class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._pending: Optional[MemoryState] = None
        self._dirty = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ---------------- 载入 ----------------
    def load(self) -> MemoryState:
        """解析 memory.md，返回 MemoryState。文件不存在或解析失败时返回空状态。"""
        if not os.path.exists(self.path):
            return MemoryState()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception as e:
            logger.warning(f"Failed to read memory file {self.path}: {e}")
            return MemoryState()
        state = self._parse(text)
        logger.info(f"Loaded memory: {len(state.turns)} turn(s), digest {len(state.recent_digest)} chars, "
                    f"impression {len(state.user_impression)} chars, turn_counter={state.turn_counter}.")
        return state

    @staticmethod
    def _parse(text: str) -> MemoryState:
        state = MemoryState()
        impression_lines: List[str] = []
        digest_lines: List[str] = []
        section: Optional[str] = None  # 'impression' | 'digest' | 'history'
        cur_role: Optional[str] = None
        cur_ts: Optional[str] = None
        cur_content: List[str] = []

        def flush_turn():
            nonlocal cur_role, cur_ts, cur_content
            if cur_role is not None:
                content = "\n".join(cur_content).strip()
                role = _ROLE_MAP.get(cur_role)
                if role is not None and content:
                    state.turns.append(Conversation(role=role, content=content, metadata=cur_ts))
            cur_role, cur_ts, cur_content = None, None, []

        # 计数器从元信息注释里解析
        m = re.search(r"turn_counter\s*=\s*(\d+)", text)
        if m:
            state.turn_counter = int(m.group(1))
        m = re.search(r"last_impression_at\s*=\s*(\d+)", text)
        if m:
            state.last_impression_at = int(m.group(1))

        for line in text.splitlines():
            if line.startswith("## "):
                flush_turn()
                head = line[3:].strip()
                if head.startswith("对用户的印象"):
                    section = "impression"
                elif head.startswith("近期回顾"):
                    section = "digest"
                elif head.startswith("对话历史"):
                    section = "history"
                else:
                    section = None  # 旧文件里的"会话摘要"段落会落到这里被忽略
                continue
            if section == "history" and line.startswith("### "):
                flush_turn()
                head = line[4:].strip()
                if "·" in head:
                    role_part, ts_part = head.split("·", 1)
                    cur_role = role_part.strip()
                    cur_ts = ts_part.strip() or None
                else:
                    cur_role = head.strip()
                    cur_ts = None
                continue
            if section == "impression":
                impression_lines.append(line)
            elif section == "digest":
                digest_lines.append(line)
            elif section == "history" and cur_role is not None:
                cur_content.append(line)
        flush_turn()

        impression = "\n".join(impression_lines).strip()
        digest = "\n".join(digest_lines).strip()
        state.user_impression = "" if impression == _EMPTY else impression
        state.recent_digest = "" if digest == _EMPTY else digest
        return state

    # ---------------- 渲染 ----------------
    @staticmethod
    def _render(state: MemoryState) -> str:
        impression = state.user_impression.strip() if state.user_impression and state.user_impression.strip() else _EMPTY
        digest = state.recent_digest.strip() if state.recent_digest and state.recent_digest.strip() else _EMPTY
        out: List[str] = [
            "# ZerolanLiveRobot 记忆",
            "",
            f"<!-- 自动保存，请勿手动修改标题格式。最后更新：{time.strftime('%Y-%m-%d %H:%M:%S')} -->",
            f"<!-- meta: turn_counter={state.turn_counter}; last_impression_at={state.last_impression_at} -->",
            "",
            "## 对用户的印象",
            "",
            impression,
            "",
            "## 近期回顾",
            "",
            digest,
            "",
            "## 对话历史",
            "",
        ]
        for c in state.turns:
            role = getattr(c.role, "value", None) or str(c.role)
            header = f"### {role} · {c.metadata}" if c.metadata else f"### {role}"
            out.append(header)
            out.append(c.content if c.content is not None else "")
            out.append("")
        return "\n".join(out).rstrip() + "\n"

    # ---------------- 异步写入 ----------------
    def start(self) -> None:
        """启动后台写线程。重复调用只生效一次。"""
        with self._lock:
            if self._running:
                return
            self._running = True
        self._thread = threading.Thread(target=self._run, name="MemoryStoreWriter", daemon=True)
        self._thread.start()
        logger.info("MemoryStore writer thread started.")

    def request_save(self, state: MemoryState) -> None:
        """提交一次保存请求（合并最新快照），由后台线程落盘。线程未启动时静默忽略。"""
        snapshot = MemoryState(
            turns=[Conversation(role=c.role, content=c.content, metadata=c.metadata) for c in state.turns],
            recent_digest=state.recent_digest or "",
            user_impression=state.user_impression or "",
            turn_counter=state.turn_counter,
            last_impression_at=state.last_impression_at,
        )
        with self._lock:
            self._pending = snapshot
        self._dirty.set()

    def _run(self) -> None:
        while True:
            self._dirty.wait()
            self._dirty.clear()
            with self._lock:
                snap = self._pending
                self._pending = None
                stopping = not self._running
            if snap is not None:
                self._write(snap)
            if stopping:
                break
        logger.info("MemoryStore writer thread stopped.")

    def _write(self, state: MemoryState) -> None:
        try:
            parent = os.path.dirname(self.path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(self._render(state))
            os.replace(tmp, self.path)  # 原子替换，避免半截文件
        except Exception as e:
            logger.warning(f"Failed to write memory file {self.path}: {e}")

    def stop(self) -> None:
        """停止写线程并把最后一次快照刷盘。"""
        with self._lock:
            if not self._running:
                return
            self._running = False
        self._dirty.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
