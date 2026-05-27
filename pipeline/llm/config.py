from pydantic import Field

from common.enumerator import BaseEnum
from common.utils.enum_util import enum_to_markdown
from pipeline.base.base_sync import AbstractPipelineConfig


#######
# LLM #
#######

class LLMModelIdEnum(BaseEnum):
    """
    Known good model IDs, kept here for documentation / quick-reference only.

    Since the refactor that turned `LLMPipelineConfig.model_id` into a free-form `str`,
    you can pass ANY model string supported by your chosen LLM service
    (OpenAI-compatible cloud API, or your own ZerolanCore deployment).
    The values listed below are not enforced — they only serve as a reference
    list displayed in the generated `config.yaml` comments and the WebUI tooltip.
    """
    DeepSeekAPI: str = "deepseek-chat"
    KimiAPI: str = "moonshot-v1-8k"
    DoubaoAPI: str = "doubao-seed-1-6-flash-250715"

    ChatGLM3_6B: str = "THUDM/chatglm3-6b"
    GLM4: str = "THUDM/GLM-4"
    Qwen_7B_Chat: str = "Qwen/Qwen-7B-Chat"
    Shisa_7b_V1: str = "augmxnt/shisa-7b-v1"
    Yi_6B_Chat: str = "01-ai/Yi-6B-Chat"
    DeepSeek_R1_Distill: str = "deepseek-ai/DeepSeek-R1-Distill-Llama-8B"


class LLMPipelineConfig(AbstractPipelineConfig):
    api_key: str | None = Field(default=None, description="The API key for accessing the LLM service.　\n"
                                                          "Kimi API supported: \n"
                                                          "Reference: https://platform.moonshot.cn/docs/guide/start-using-kimi-api \n"
                                                          "Deepseek API supported: \n"
                                                          "Reference: https://api-docs.deepseek.com/zh-cn/")
    openai_format: bool = Field(default=False,
                                description="Whether to call the LLM service via the OpenAI-compatible SDK.\n"
                                            "Set this to `True` when using ANY cloud LLM service that exposes an "
                                            "OpenAI-compatible API (DeepSeek / Kimi / Doubao / OpenAI / ...). \n"
                                            "Set to `False` to call a self-hosted ZerolanCore service via its native HTTP protocol.")
    model_id: str = Field(default="THUDM/GLM-4",
                          description="The model identifier sent to the LLM service. This is a FREE-FORM STRING.\n"
                                      "- When `openai_format=True`, this string is passed verbatim as the `model` parameter "
                                      "to the OpenAI SDK. You can freely use ANY model name that your chosen provider supports "
                                      "(e.g. `deepseek-chat`, `deepseek-v4-pro`, `gpt-4o`, `moonshot-v1-8k`, ...). "
                                      "No code change is required to use a new model — just write its name here.\n"
                                      "- When `openai_format=False`, this string is forwarded to your ZerolanCore service "
                                      "to select a locally hosted model.\n"
                                      f"\nKnown good values for quick reference:\n{enum_to_markdown(LLMModelIdEnum)}")
    predict_url: str = Field(default="http://127.0.0.1:11000/llm/predict",
                             description="The URL for LLM prediction requests.\n"
                                         "- When `openai_format=True`, this is the *base URL* of the OpenAI-compatible service "
                                         "(e.g. `https://api.deepseek.com`, `https://api.openai.com/v1`).\n"
                                         "- When `openai_format=False`, this is the full endpoint of your ZerolanCore LLM service.")
    stream_predict_url: str = Field(default="http://127.0.0.1:11000/llm/stream-predict",
                                    description="The URL for streaming LLM prediction requests. "
                                                "Same semantics as `predict_url`.")
