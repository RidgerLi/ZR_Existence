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


@log_run_time()
def summary_history(history: List[Conversation]) -> AIMessage:
    system_template = "将这段用户与你的对话总结成一段话，需要包含重要细节。"
    text = ""
    for conversation in history:
        text += f"[{conversation.role}]\n{conversation.content}"
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}")]
    )
    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)

    return response


@log_run_time()
def light_digest(history: List[Conversation]) -> str:
    """温区轻压缩：把"较早一些的窗口内对话"压成简洁回顾。不过分压缩——只去口语冗余/重复，
    把逐句时间戳折叠成时间范围，保留关键信息/决定/情绪。用于缩短 prompt、抑制幻觉。"""
    system_template = (
        "你是对话压缩助手。把下面这段较早的对话压成极简要点，要求：\n"
        "1) 最多 3 条要点，总共不超过 150 字；只保留真正重要的事实/决定/情绪，其余全部丢弃；\n"
        "2) 不要逐句时间戳，开头用一个时间范围概括（如“06-07 14:00–14:40：”）即可；\n"
        "3) 第三人称、电报式短句，不要复述对话、不要寒暄、不要细节铺陈；\n"
        "4) 只输出要点本身，不要解释、不要加任何新的时间戳标记、不要编造。"
    )
    text = ""
    for c in history:
        role = getattr(c.role, "value", None) or str(c.role)
        ts = f"[{c.metadata}] " if getattr(c, "metadata", None) else ""
        text += f"{ts}[{role}] {c.content}\n"
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", "{text}\n\n任务：把以上对话压成简洁回顾。")]
    )
    result = prompt_template.invoke({"text": text})
    result.to_messages()
    response = _model.invoke(result)
    return (response.content or "").strip()


@log_run_time()
def update_user_impression(prior_impression: str, history: List[Conversation],
                           session_summary: str = "", long_term: str = "") -> str:
    """基于已有印象 + 最近对话 + 记忆摘要，更新并返回对用户的整体印象画像。"""
    system_template = (
        "你是一个长期陪伴用户的 AI。请基于【已有印象】【会话摘要】【更早的长期记忆】"
        "和【最近的对话】，更新你对哥哥的整体印象画像：性格、习惯、当前状态、在意的事、关系动态、"
        "需要注意的点等。要求：用中文；保留仍然成立的旧印象，融合新信息，修正过时内容；"
        "高度凝练，只保留最稳定、最关键的特征，总共不超过150字；可以是几条短要点或一小段话；"
        "只输出印象本身，不要解释、不要寒暄、不要加时间戳。"
    )
    convo = ""
    for c in history:
        convo += f"[{c.role}] {c.content}\n"
    user_template = (
        "【已有印象】\n{prior}\n\n【会话摘要】\n{summary}\n\n【更早的长期记忆】\n{long_term}\n\n"
        "【最近的对话】\n{convo}\n\n【你的任务】综合以上，输出更新后的“对用户的印象”。"
    )
    prompt_template = ChatPromptTemplate.from_messages(
        [("system", system_template), ("user", user_template)]
    )
    result = prompt_template.invoke({"prior": prior_impression or "（暂无）",
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
