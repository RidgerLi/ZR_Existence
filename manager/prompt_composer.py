"""
提示词分层装配器（PromptComposer）
Author: ZerolanLiveRobot

把发给 LLM 的 system 消息按"分层"拼装，而不是把人设、状态、记忆糊成一团。各层：

    L1 人设            角色设定/性格/说话风格      —— 静态，来自 config.system_prompt（作为 base）
    L2a 固定设定/长远计划  不变的目标、规则、长期设定   —— 静态，来自 config（手写一段文本）
    L2b 长期记忆        学到的关于对方的耐久事实      —— 动态，来自记忆库（Phase 3）
    L3b 会话摘要        被截断旧对话的浓缩           —— 动态，来自记忆整理（Phase 3）
    L3a 当前状态        心情/模式                  —— 动态，来自 brain（瞬态）

设计：
    - L1 是 base（system 消息原有的人设文本）。
    - 其余各层是一组有序的 provider 回调，每个返回"一个完整文本块"或空串（空则跳过）。
    - provider 自带小标题/格式，composer 只负责按顺序用空行拼接，并替换/插入 system 消息。
    - 这一切都作用在 current_history 的**拷贝**上，绝不污染持久化历史。

L2b / L3b 现在留空 provider（返回 ""），Phase 3 接上记忆库时只需替换 provider，无需改本类。
"""

import re
from typing import Callable, List

from loguru import logger
from zerolan.data.pipeline.llm import Conversation, RoleEnum

# 历史里曾用过两种逐轮时间戳：`06-07 22:44` 与 `06-07 周日 23:13`。渲染发送时统一抹掉
# “周X / 星期X”这类星期词，让模型看到的时间前缀格式一致（[MM-DD HH:MM]）。
_WEEKDAY_RE = re.compile(r"\s*(?:周|星期|礼拜)[一二三四五六日天]\s*")


class PromptComposer:
    def __init__(self, section_providers: List[Callable[[], str]]):
        """
        :param section_providers: 有序的层 provider 列表（L1 之后的各层）。
                                   每个返回一个完整文本块或空串。
        """
        self._providers = section_providers

    def compose_system(self, base_persona: str) -> str:
        """在人设(base)之后，依次拼接各非空层，组成最终 system 文本。"""
        blocks: List[str] = []
        base = (base_persona or "").strip()
        if base:
            blocks.append(base)
        for provider in self._providers:
            try:
                text = (provider() or "").strip()
            except Exception as e:
                logger.exception(e)
                text = ""
            if text:
                blocks.append(text)
        return "\n\n".join(blocks)

    @staticmethod
    def _normalize_ts(metadata: str) -> str:
        """把逐轮时间戳归一化为统一格式（抹掉“周X/星期X”等星期词，合并多余空格）。"""
        ts = _WEEKDAY_RE.sub(" ", str(metadata))
        return re.sub(r"\s+", " ", ts).strip()

    def build_query_history(self, current_history: List[Conversation]) -> List[Conversation]:
        """返回一份历史拷贝，其中 system 消息被替换为分层拼装后的内容。

        few-shot 示例与真实对话原样保留；持久化历史（传入的 current_history）不受影响。
        若某真实轮次带有时间戳（metadata），在其 content 前加紧凑时间前缀（如 `[06-07 14:30] …`），
        让 LLM 感知每句话是何时说的；few-shot 示例无 metadata，不受影响。

        另外做两步归一化，只作用于"发出去"的拷贝：
          1) 时间戳格式统一（见 _normalize_ts）；
          2) 合并相邻同角色轮次（system 除外）。主动开口被插入历史时会产生连续两条 assistant，
             部分严格要求 user/assistant 交替的服务端会报错或降质，这里在发送前折叠掉。
        """
        new: List[Conversation] = []
        for c in current_history:
            content = c.content
            if c.role != RoleEnum.system and c.metadata:
                content = f"[{self._normalize_ts(c.metadata)}] {content}"
            new.append(Conversation(role=c.role, content=content))

        if new and new[0].role == RoleEnum.system:
            new[0] = Conversation(role=RoleEnum.system,
                                  content=self.compose_system(new[0].content))
        else:
            composed = self.compose_system("")
            if composed:
                new.insert(0, Conversation(role=RoleEnum.system, content=composed))

        return self._merge_consecutive_roles(new)

    @staticmethod
    def _merge_consecutive_roles(history: List[Conversation]) -> List[Conversation]:
        """把相邻同角色的消息合并成一条（content 用换行拼接），保证 user/assistant 严格交替。
        system 不参与合并（它只会出现在开头）。"""
        merged: List[Conversation] = []
        for c in history:
            if (merged and c.role != RoleEnum.system
                    and merged[-1].role == c.role):
                prev = merged[-1]
                joined = f"{prev.content}\n{c.content}" if prev.content else c.content
                merged[-1] = Conversation(role=prev.role, content=joined)
            else:
                merged.append(c)
        return merged
