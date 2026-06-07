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

from typing import Callable, List

from loguru import logger
from zerolan.data.pipeline.llm import Conversation, RoleEnum


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

    def build_query_history(self, current_history: List[Conversation]) -> List[Conversation]:
        """返回一份历史拷贝，其中 system 消息被替换为分层拼装后的内容。

        few-shot 示例与真实对话原样保留；持久化历史（传入的 current_history）不受影响。
        若某真实轮次带有时间戳（metadata），在其 content 前加紧凑时间前缀（如 `[06-07 14:30] …`），
        让 LLM 感知每句话是何时说的；few-shot 示例无 metadata，不受影响。
        """
        new: List[Conversation] = []
        for c in current_history:
            content = c.content
            if c.role != RoleEnum.system and c.metadata:
                content = f"[{c.metadata}] {content}"
            new.append(Conversation(role=c.role, content=content))

        if new and new[0].role == RoleEnum.system:
            new[0] = Conversation(role=RoleEnum.system,
                                  content=self.compose_system(new[0].content))
        else:
            composed = self.compose_system("")
            if composed:
                new.insert(0, Conversation(role=RoleEnum.system, content=composed))
        return new
