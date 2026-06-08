"""
Author: Github@AkagawaTsurunaki

这些 API 是无记忆的。因此适合于单次使用。
These APIs are memory-free. Therefore, it is suitable for single use.
"""
import random
import re
from typing import List

from langchain_core.messages import AIMessage
from langchain_core.prompts import ChatPromptTemplate
from loguru import logger
from zerolan.data.pipeline.llm import Conversation
from zerolan.data.pipeline.ocr import RegionResult

from agent.adaptor import LangChainAdaptedLLM
from manager.config_manager import get_config
from services.playground.data import GameObject, ScaleOperationResponse
from common.decorator import log_run_time
from common.enumerator import Language
from common.utils.json_util import smart_load_json_like
from pipeline.ocr.ocr_sync import stringify

_config = get_config()
_model = LangChainAdaptedLLM(config=_config.pipeline.llm)


@log_run_time()
def find_file(files: List[dict], question: str) -> str | None:
    system_template = '你现在是文件搜索助手，请根据用户的提问，寻找出最匹配的文件，并返回它的ID。注意：你只需要返回ID，不要输出任何其他内容'

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user",
                                       "{files} \n\n【你的任务】现在根据上述文件信息，和用户提问```\n{question}\n```\n寻找出最匹配的文件，并返回它的ID\n注意：你只需要返回ID，不要输出任何其他内容")]
    )

    result = prompt_template.invoke({"files": files, "question": question})
    result.to_messages()
    response = _model.invoke(result)

    for file in files:
        if file["id"] in response.content:
            return file["id"]
    return None


@log_run_time()
def find_focus(region_results: List[RegionResult]) -> RegionResult:
    system_template = "你的任务：你需要在下列的OCR识别结果中找到最具有值得注意的信息，最后返回其标号 [i]。"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{region_results}")]
    )

    result = prompt_template.invoke({"region_results": stringify(region_results)})
    result.to_messages()
    response = _model.invoke(result)

    numbers = re.findall(r'\d+', response.content)

    if len(numbers) == 0:
        idx = random.randint(0, len(numbers) - 1)
    else:
        idx = int(numbers[0])

    logger.info(f"Location attention: [{idx}]{region_results[idx].content}")

    return region_results[idx]


@log_run_time()
def answer_question(text: str, question: str) -> str:
    system_template = "你现在是一个问答助手，请仔细阅读文章内容，基于你阅读的内容，充分、正确地回答用户提出的问题。"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text} \n\n【你的任务】现在根据上述文段，回答问题：\n{question}")]
    )

    result = prompt_template.invoke({"text": text, "question": question})
    result.to_messages()
    response = _model.invoke(result)

    return response.content


@log_run_time()
def sentiment_analyse(sentiments: List[str], text: str) -> str:
    # If you only set 1 tts prompt, then there is no need to analyse sentiment
    if len(sentiments) == 1:
        return sentiments[0]

    system_template = "你的任务：你现在是一个情感分析助手，你将要对所给的文句进行情感分析，你必须从以下情感标签中挑选一个作为答案 {sentiments}。\n输出格式：必须仅返回情感标签内容，不要输出多余内容。"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}")]
    )

    result = prompt_template.invoke({"sentiments": sentiments, "text": text})
    result.to_messages()
    response = _model.invoke(result)
    # Try to parse the results of the LLM analysis
    #   1. If the sentiment tag is first matched in response, it is returned
    #   2. If the sentiment tag is not found, try to return the first match in ["normal", "正常", "default", "默认"]
    #   3. If the default/normal sentiment tag is not found, try returning the first sentiment tag
    for sentiment in sentiments:
        if sentiment in response.content:
            return sentiment
    for default in ["Default", "Normal", "默认", "正常"]:
        if default in response.content:
            return sentiment
    return sentiments[0]


@log_run_time()
def translate(text: str, src_lang: str | Language, tgt_lang: str | Language) -> str:
    if isinstance(src_lang, Language):
        src_lang = src_lang.name()
    if isinstance(tgt_lang, Language):
        tgt_lang = tgt_lang.name()

    system_template = "Translate the following from {src_lang} into {tgt_lang}"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}")]
    )

    result = prompt_template.invoke({"src_lang": src_lang, "tgt_lang": tgt_lang, "text": text})
    result.to_messages()
    response = _model.invoke(result)

    return response.content


@log_run_time()
def summary(text: str, max_len: int = 100) -> AIMessage:
    """
    Summary the provided text within `max_len`.
    Args:
        text: The long text to summary.
        max_len: The maximum number of text of the summary.

    Returns:

    """
    system_template = "请仔细阅读文章内容，根据你认为最有价值和信息量的重要内容，总结这段文本为一段话。"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text} \n任务：总结以上文本，不超过{max_len}字。")]
    )

    result = prompt_template.invoke({"text": text, "max_len": max_len})
    result.to_messages()
    response = _model.invoke(result)

    return response


def _speaker_label(role) -> str:
    """把会话角色映射成中性的身份标签，避免出现“用户/AI”这类技术性身份词。

    - “对方”：角色所陪伴/对话的那个人（即 user 轮），其发言是事实依据；
    - “角色”：对话里那个被陪伴方扮演的角色本身（即 assistant 轮），其发言只作辅助参考。
    """
    r = getattr(role, "value", None) or str(role)
    return "对方" if r == "user" else "角色"


# 各记忆压缩任务共用的“事实依据”说明：事实只取【对方】所言，【角色】发言仅作辅助参考。
_FACT_BASIS_RULE = (
    "【事实依据】对话里标【对方】的发言才是事实依据；标【角色】的发言只作辅助参考"
    "（补全语境、消解指代），绝不把【角色】的发挥、设定或承诺当成既成事实。"
)


@log_run_time()
def summary_history(history: List[Conversation]) -> AIMessage:
    """会话摘要（L3b）：把一段被滑出窗口的对话压成"关键节点/结论/承诺"的要点列表。

    刻意去台词化、去文采：只记发生了什么、定下了什么、答应了什么，绝不复述对话过程或
    模仿角色口吻。这样既省 token，又避免把旧情节当成"刚发生的事"诱导模型复述。
    """
    system_template = (
        "你是对话归档助手。把下面这段对话压成关键节点要点，要求：\n"
        f"0) {_FACT_BASIS_RULE}\n"
        "1) 用要点列表（每行以“- ”开头），最多 5 条，总共不超过 120 字；\n"
        "2) 只记真正重要的事实/决定/承诺/状态变化（如“日历缺货→改买本子”“答应明早在本子第一页写字”），"
        "其余寒暄、过程、动作描写、玩笑全部丢弃；\n"
        "3) 第三人称、电报式短句，绝对不要复述对话原文、不要模仿角色口吻、不要加动作括号；\n"
        "4) 不要写任何时间戳/日期；只输出要点本身，不要解释、不要编造。"
    )
    text = ""
    for conversation in history:
        text += f"[{_speaker_label(conversation.role)}]\n{conversation.content}\n"
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}\n\n任务：把以上对话压成关键节点要点。")]
    )
    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)

    return response


@log_run_time()
def extract_durable_facts(history: List[Conversation]) -> str:
    """长期记忆（L2b）：从一段对话里只抽取"跨会话仍然成立的耐久事实"，写入向量库。

    与 `summary_history` 的区别：长期记忆不是流水账，而是关于对方的、未来还用得上的事实——
    偏好、习惯、关系、长期目标、立下的承诺。刻意剔除一次性情节与瞬时状态（如“现在在洗澡”
    “趁热吃出门”），避免检索时把过期状态当成当下事实塞回 prompt 造成时间线矛盾。
    """
    system_template = (
        "你是长期记忆抽取助手。从下面这段对话里，只抽取“以后仍然成立、值得长期记住”的关于【对方】的事实，"
        "写成要点。要求：\n"
        f"0) {_FACT_BASIS_RULE}所有耐久事实都必须来自【对方】亲口说的话，"
        "绝不把【角色】的设定、推测或承诺当作关于【对方】的事实写入；\n"
        "1) 用要点列表（每行以“- ”开头），最多 5 条，总共不超过 120 字；\n"
        "2) 只保留耐久信息：偏好/口味、习惯、关系与称呼、长期目标、明确许下的承诺、值得记住的设定；\n"
        "3) 必须丢弃一次性情节、当下动作、瞬时状态（如“正在洗澡”“现在要出门”“刚吃了西瓜”），"
        "这些是临时状态，不属于长期记忆；\n"
        "4) 第三人称、客观陈述句，不要复述对话、不要角色口吻、不要动作括号、不要时间戳；\n"
        "5) 若这段对话没有任何值得长期记住的事实，只输出空字符串。只输出要点本身。"
    )
    text = ""
    for c in history:
        text += f"[{_speaker_label(c.role)}] {c.content}\n"
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}\n\n任务：抽取关于【对方】的耐久事实要点。")]
    )
    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)
    return (response.content or "").strip()


@log_run_time()
def light_digest(history: List[Conversation]) -> str:
    """温区"近期回顾"：把"工作窗口内、热区之外的较早对话"压成一份信息保真的回顾。
    这是 prompt 里唯一的"近期线性记忆"（已无独立会话摘要），所以要保留较多细节——
    谁做了什么、聊了什么、决定/承诺/情绪/约定，按时间段分段，方便 AI 接上下文。
    但**不要复述原话、不要照抄语气和括号动作**，只客观转述，避免把说话风格也喂回去。"""
    system_template = (
        "你是对话回顾整理助手。把下面这段较早的对话整理成一份客观、保真的回顾，要求：\n"
        f"0) {_FACT_BASIS_RULE}回顾里的事实、决定、承诺、约定都以【对方】说的话为准，"
        "不要把【角色】单方面的发挥或设定当成已发生的事实记录；\n"
        "1) 按时间先后分成若干段落，每段开头用时间范围概括（如“06-07 14:00–14:20：”）；"
        "时间相近、话题连续的归一段；\n"
        "2) 第三人称客观转述：谁说了什么、做了什么决定/承诺/约定、情绪如何、有哪些待办；"
        "保留对后续对话有用的具体信息（地点、物品、计划、约定时间等）；\n"
        "3) 绝对不要复述原话、不要照抄说话语气，也不要写括号里的动作/神态描写——只记事实与要点；\n"
        "4) 总长控制在 300 字以内；不寒暄、不解释、不编造、不要在正文里加任何新的时间戳标记；\n"
        "5) 只输出回顾正文本身。"
    )
    text = ""
    for c in history:
        ts = f"[{c.metadata}] " if getattr(c, "metadata", None) else ""
        text += f"{ts}[{_speaker_label(c.role)}] {c.content}\n"
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}\n\n任务：把以上对话整理成分时段的客观回顾。")]
    )
    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)
    return (response.content or "").strip()


@log_run_time()
def update_user_impression(prior_impression: str, history: List[Conversation],
                           session_summary: str = "", long_term: str = "",
                           system_prompt: str = "") -> str:
    """基于已有印象 + 最近对话 + 记忆摘要，更新并返回对【对方】的整体印象画像。

    会把【任务设定/system_prompt】一并喂入：一来用它界定“角色/对方”身份，二来要求 LLM
    **不要重复任务设定里已经写过的内容**，只记录从真实相处中"学到的、设定里没有的"新信息。

    职责切分：本函数只画"性格/行为模式"画像（形容词性的概括，如“有方向就肯行动、容易拖延”），
    具体的事件性事实、偏好、承诺归长期记忆（向量库）负责，这里不要重复记那些。
    """
    system_template = (
        "你的工作是更新对【对方】的【性格与行为模式】画像。下面会给你一份【任务设定】，"
        "它界定了“角色”（被陪伴方所对话的那个角色）和“对方”（角色长期陪伴/对话的人）各自的身份与口吻。\n"
        "【最重要】【任务设定】里已经写过的设定、性格、背景，绝对不要再写进印象里。\n"
        "印象只写从真实相处中**新观察到的、概括性的行为模式与脾性**（形容词性的描述，如"
        "“出门后执行力比在家高”“规划时犹豫但有方向就肯走”），帮助更懂怎么跟【对方】相处。\n"
        "【事实依据】画像必须基于【对方】本人说的话与表现来推断；【角色】说的话只作辅助参考，"
        "不要把【角色】的回应口吻或设定当成对【对方】的观察写进画像。\n"
        "不要写具体的一次性事件、偏好清单或承诺（那些由长期记忆单独负责），也不要复述对话流水。\n"
        "要求：用中文；保留仍成立的旧印象、融合新信息、删掉过时或与任务设定重复的内容；"
        "极度凝练，最多 3 条短要点、总共不超过 100 字；只输出印象本身，不要解释、不要寒暄、不要加时间戳。"
    )
    convo = ""
    for c in history:
        convo += f"[{_speaker_label(c.role)}] {c.content}\n"
    user_template = (
        "【任务设定（不要重复其中内容）】\n{persona}\n\n"
        "【已有印象】\n{prior}\n\n【会话摘要】\n{summary}\n\n【更早的长期记忆】\n{long_term}\n\n"
        "【最近的对话】\n{convo}\n\n"
        "【你的任务】综合以上，输出更新后的“对【对方】的印象”，且不得包含任务设定里已有的信息。"
    )
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", user_template)]
    )
    result = prompt_template.invoke({"persona": (system_prompt or "（无）").strip(),
                                     "prior": prior_impression or "（暂无）",
                                     "summary": session_summary or "（暂无）",
                                     "long_term": long_term or "（暂无）",
                                     "convo": convo or "（暂无）"})
    result.to_messages()
    response = _model.invoke(result)
    return (response.content or "").strip()


@log_run_time()
def model_scale(info: List[GameObject], question: str) -> ScaleOperationResponse | None:
    config = get_config()
    model = LangChainAdaptedLLM(config=config.pipeline.llm)
    format = {
        "instance_id": int,
        "target_scale": float
    }

    system_template = '你现在需要根据用户的指令，修改游戏对象的一些参数。返回的JSON格式为{format}'
    user_template = '{info}【用户输入】{question}\n【你的任务】根据上面游戏对象的信息，识别用户需要操作的模型指令，将结果以JSON的形式返回。'

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", user_template)]
    )

    result = prompt_template.invoke({"format": format, "info": info, "question": question})
    result.to_messages()
    response = model.invoke(result)
    json = smart_load_json_like(response.content)
    return ScaleOperationResponse.model_validate(json)


@log_run_time()
def sentiment_score(text: str) -> float:
    system_template = "你的任务：你现在是一个情感分析助手，你将要对所给的文句进行情感分析，从-1到1内的一个浮点数，数字越小代表情感越负面，数字越大代表情感越正面\n输出格式：必须仅返回数字，不要输出多余内容。"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}")]
    )

    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)

    try:
        return float(response)
    except Exception:
        return 0.0


@log_run_time()
def memory_score(text: str) -> float:
    system_template = "你的任务：你现在是一个文本是否存储为记忆的价值分析助手，你将要对所给的文句进行分析，从-1到1内的一个浮点数，数字越小代表该段文字越没有价值，应该被丢弃，数字越大代表该段文字越有价值，应该被保留\n输出格式：必须仅返回数字，不要输出多余内容。\n"

    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}")]
    )

    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)

    try:
        return float(response)
    except Exception:
        return 0.0
