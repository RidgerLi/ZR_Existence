"""
自我状态（SelfState）：可被 LLM 自我编辑的长期目标 + todolist
Author: ZerolanLiveRobot

对应提示词分层的 L2a。把 sysprompt 的"可变部分"从固定人设(L1)里拆出来，独立持久化到
`resources/memory/self_state.json`（不回写大 config.yaml，避免 get_config 单例/重载问题）：

    goals    : 长期目标（字符串列表）
    todolist : 待办（{text, done} 列表）

LLM 通过 <self_update> 工具协议增删改这些内容（见 self_update.py）。`render()` 把它们渲染成
system 提示词的一个小节；`apply_update()` 应用一组操作并落盘。
"""

import json
import os
import threading
from typing import List

from loguru import logger


class SelfState:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self.goals: List[str] = []
        self.todolist: List[dict] = []  # 每项 {"text": str, "done": bool}
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        with self._lock:
            if not os.path.exists(self.path):
                return
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.goals = [str(g) for g in data.get("goals", []) if str(g).strip()]
                todos = []
                for t in data.get("todolist", []):
                    if isinstance(t, dict) and t.get("text"):
                        todos.append({"text": str(t["text"]), "done": bool(t.get("done", False))})
                    elif isinstance(t, str) and t.strip():
                        todos.append({"text": t, "done": False})
                self.todolist = todos
            except Exception as e:
                logger.warning(f"Failed to load self_state from {self.path}: {e}")

    def save(self) -> None:
        with self._lock:
            try:
                os.makedirs(os.path.dirname(self.path), exist_ok=True)
                with open(self.path, "w", encoding="utf-8") as f:
                    json.dump({"goals": self.goals, "todolist": self.todolist},
                              f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"Failed to save self_state to {self.path}: {e}")

    # ---------- 渲染（L2a） ----------
    def render(self) -> str:
        with self._lock:
            blocks = []
            if self.goals:
                lines = "\n".join(f"- {g}" for g in self.goals)
                blocks.append("# 你的长期目标\n" + lines)
            if self.todolist:
                lines = "\n".join(("- [x] " if t["done"] else "- [ ] ") + t["text"] for t in self.todolist)
                blocks.append("# 你的待办清单\n" + lines)
            return "\n\n".join(blocks)

    # ---------- 编辑 ----------
    def _has_todo(self, text: str) -> int:
        for i, t in enumerate(self.todolist):
            if t["text"] == text:
                return i
        return -1

    def apply_update(self, update: dict) -> bool:
        """应用一组操作。支持的键（值为 str 或 str 列表）：
        add_goal / remove_goal / add_todo / done_todo / remove_todo。
        返回是否发生了实际改动。
        """
        if not isinstance(update, dict):
            return False

        def as_list(v):
            if v is None:
                return []
            return v if isinstance(v, list) else [v]

        changed = False
        with self._lock:
            for g in as_list(update.get("add_goal")):
                g = str(g).strip()
                if g and g not in self.goals:
                    self.goals.append(g)
                    changed = True
            for g in as_list(update.get("remove_goal")):
                g = str(g).strip()
                if g in self.goals:
                    self.goals.remove(g)
                    changed = True
            for t in as_list(update.get("add_todo")):
                t = str(t).strip()
                if t and self._has_todo(t) < 0:
                    self.todolist.append({"text": t, "done": False})
                    changed = True
            for t in as_list(update.get("done_todo")):
                t = str(t).strip()
                idx = self._has_todo(t)
                if idx >= 0 and not self.todolist[idx]["done"]:
                    self.todolist[idx]["done"] = True
                    changed = True
            for t in as_list(update.get("remove_todo")):
                t = str(t).strip()
                idx = self._has_todo(t)
                if idx >= 0:
                    self.todolist.pop(idx)
                    changed = True

            if changed:
                self.save()
        return changed
