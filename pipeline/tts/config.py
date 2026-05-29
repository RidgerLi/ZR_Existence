from pydantic import Field, BaseModel

from common.enumerator import BaseEnum
from common.utils.enum_util import enum_to_markdown
from pipeline.base.base_sync import AbstractPipelineConfig


#######
# TTS #
#######

class TTSModelIdEnum(BaseEnum):
    GPT_SoVITS = "AkagawaTsurunaki/GPT-SoVITS"  # Forked repo
    BaiduTTS = "BaiduTTS"
    GPT_SoVITS_V2_API = "GPT-SoVITS-V2-API"


# Config for BaiduTTS and should
class BaiduTTSConfig(BaseModel):
    api_key: str = Field(default="", description="The API key for Baidu TTS service.")
    secret_key: str = Field(default="", description="The secret key for Baidu TTS service.")


class GPTSoVITSConfig(BaseModel):
    url: str = Field(default="http://127.0.0.1:9880/tts", description="Where can i request GPTSoVITS service.")
    

# Config for ZerolanCore
class TTSPipelineConfig(AbstractPipelineConfig):
    model_id: TTSModelIdEnum = Field(default=TTSModelIdEnum.GPT_SoVITS,
                                     description=f"The ID of the model used for text-to-speech. \n"
                                                 f"{enum_to_markdown(TTSModelIdEnum)}")
    predict_url: str = Field(default="http://127.0.0.1:11000/tts/predict",
                             description="The URL for TTS prediction requests.")
    stream_predict_url: str = Field(default="http://127.0.0.1:11000/tts/stream-predict",
                                    description="The URL for streaming TTS prediction requests.")
    baidu_tts_config: BaiduTTSConfig = Field(default=BaiduTTSConfig(),
                                             description=f"Baidu TTS config. \n"
                                                         f"Only edit it when you set `model_id` to `{TTSModelIdEnum.BaiduTTS.value}`.\n"
                                                         f"For more details please see the [documents](https://cloud.baidu.com/doc/SPEECH/s/mlbxh7xie).")
    gpt_sovits_config: GPTSoVITSConfig = Field(default=GPTSoVITSConfig(),
                                               description=f"GPTSoVITS TTS config. \n"
                                                           f"Only edit it when you set `model_id` to `{TTSModelIdEnum.GPT_SoVITS_V2_API.value}`.\n"
                                                           f"For more details please see the [documents](https://github.com/RVC-Boss/GPT-SoVITS).")