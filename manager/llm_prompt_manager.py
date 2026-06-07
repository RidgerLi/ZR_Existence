from copy import deepcopy
from typing import Callable

from loguru import logger
from zerolan.data.pipeline.llm import Conversation, RoleEnum

from character.config import ChatConfig


class LLMPromptManager:
    def __init__(self, config: ChatConfig):
        self.system_prompt: str = config.system_prompt
        self.injected_history: list[Conversation] = self._parse_history_list(config.injected_history,
                                                                             self.system_prompt)
        self.current_history: list[Conversation] = deepcopy(self.injected_history)
        self.max_history = config.max_history
        # 当滑动窗口裁掉最旧的真实对话时，通过该回调把被裁内容交出去做记忆整理（MemoryManager）。
        self._evict_callback: Callable[[list[Conversation]], None] | None = None

    def set_evict_callback(self, callback: Callable[[list[Conversation]], None] | None) -> None:
        """注册"真实对话被滑出窗口"时的回调，交给记忆系统做压缩/入库。"""
        self._evict_callback = callback

    def live_turns(self) -> list[Conversation]:
        """返回固定前缀（system + injected few-shot）之后的真实对话轮次。"""
        base = len(self.injected_history)
        return self.current_history[base:]

    def seed_live_turns(self, turns: list[Conversation]) -> None:
        """用持久化载入的真实对话填充工作窗口（接在固定前缀之后）。仅启动时调用一次。
        超过 max_history 的部分只保留最近的 max_history 轮，避免一次性塞超量。"""
        if not turns:
            return
        live = list(turns[-self.max_history:]) if len(turns) > self.max_history else list(turns)
        self.current_history = deepcopy(self.injected_history) + deepcopy(live)

    def reset_history(self, history: list[Conversation]) -> None:
        """
        以"滑动窗口"方式更新 current_history。

        固定前缀（system 人设 + injected few-shot 示例）永远保留；`max_history` 只约束其后的
        真实对话。当真实对话超过 `max_history` 时，裁掉最旧的轮次（成对裁，避免窗口以 assistant
        开头破坏 user/assistant 交替），并通过 `_evict_callback` 把被裁内容交给记忆系统整理，
        而不是像过去那样把整段对话清回 injected_history（金鱼脑 bug）。

        :param history: 完整历史（固定前缀 + 真实对话）。None 表示清回 injected_history。
        """
        if history is None:
            self.current_history = deepcopy(self.injected_history)
            return

        base = len(self.injected_history)
        prefix = deepcopy(self.injected_history)  # 始终用规范的固定前缀，避免被外部改动污染
        live = list(history[base:])               # 固定前缀之后的真实对话

        if len(live) > self.max_history:
            cut = len(live) - self.max_history
            evicted = live[:cut]
            live = live[cut:]
            # 避免窗口以 assistant 轮次开头：把开头多余的 assistant 也一并裁掉
            while live and live[0].role == RoleEnum.assistant:
                evicted.append(live.pop(0))
            if evicted and self._evict_callback is not None:
                try:
                    self._evict_callback(deepcopy(evicted))
                except Exception as e:
                    logger.exception(e)

        self.current_history = prefix + deepcopy(live)

    @staticmethod
    def _parse_history_list(history: list[str], system_prompt: str | None = None) -> list[Conversation]:
        result = []

        if system_prompt is not None:
            result.append(Conversation(role=RoleEnum.system, content=system_prompt))

        for idx, content in enumerate(history):
            role = RoleEnum.user if idx % 2 == 0 else RoleEnum.assistant
            conversation = Conversation(role=role, content=content)
            result.append(conversation)

        return result
