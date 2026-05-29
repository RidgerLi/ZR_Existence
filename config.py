from pydantic import BaseModel, Field

from character.config import CharacterConfig
from common.utils.enum_util import try_get_pynput_key_enum_str
from pipeline.base.config import PipelineConfig
from services.config import ServiceConfig


class SystemConfig(BaseModel):
    default_enable_microphone: bool = Field(default=False,
                                            description="For safety, do not open your microphone by default. \n"
                                                        "You can set it `True` to enable your microphone")
    microphone_vad_mode: int = Field(default=3,
                                     description="Optionally, set its aggressiveness mode, which is an integer between 0 and 3. " \
                                                 "0 is the least aggressive about filtering out non-speech, 3 is the most aggressive.")
    microphone_hotkey: str = Field(default='f8',
                                   description="Your microphone is set to be off when the program starts. One tap on this hotkey will change its status between on and off.\n" \
                                               "You can pick your own hotkey on Key names like: {} ...".format(
                                       try_get_pynput_key_enum_str()))
    enable_clause_split: bool = Field(default=True,
                                      description='If `True`, splits LLM responses into smaller clauses before sending to TTS service. '
                                                  'This enables faster audio generation and reduced latency for real-time applications. \n'
                                                  'Set to `False` to send full sentences as a single unit for more natural speech flow at the cost of longer wait times.')
    enable_streaming_llm: bool = Field(default=True,
                                       description='If `True`, the LLM response is streamed and clauses are dispatched to TTS as soon as a punctuation mark is seen. '
                                                   'This drastically reduces the time-to-first-speech in voice-chat scenarios. '
                                                   'When enabled, `enable_sentiment_analysis` is ignored on the streaming voice path '
                                                   '(the default TTS prompt is used) because the sentiment LLM call would otherwise re-serialize the whole pipeline. \n'
                                                   'Set to `False` to keep the legacy blocking behavior (slower but compatible with sentiment-based prompt selection).')
    enable_sentiment_analysis: bool = Field(default=False, description='Automatically analyzes sentiment to select appropriate TTS prompts. '
                                                                      'This also increases token consumption and adds slight latency due to extra processing. '
                                                                      'Has no effect on the streaming voice path when `enable_streaming_llm` is True.')
    enable_intelligent_memory: bool = Field(default=False,
                                            description='🧪 EXPERIMENTAL: Automatically scores and filters conversation history entries based on sentiment, relevance, and safety.')


class ZerolanLiveRobotConfig(BaseModel):
    pipeline: PipelineConfig = Field(default=PipelineConfig(),
                                     description="Configuration for the pipeline settings. \n"
                                                 "The pipeline is the key to connecting to `ZerolanCore`, \n"
                                                 "which typically accesses the model via HTTP or HTTPS requests and gets a response from the model. \n"
                                                 "> [!NOTE] \n"
                                                 "> 1. At a minimum, you need to enable the LLMPipeline. \n"
                                                 "> 2. ZerolanCore is distributed, and you can deploy different models to different servers. Just set different url to connect to your models. \n"
                                                 "> 3. If your server can only open one port, try forwarding your network requests using [Nginx](https://nginx.org/en/).")
    service: ServiceConfig = Field(default=ServiceConfig(),
                                   description="Configuration for the service settings. \n"
                                               "The services are usually opened locally, \n"
                                               "and instances of other projects establish WebSocket or HTTP connections with the service, \n"
                                               "and the service controls the behavior of its sub-project instances.")
    character: CharacterConfig = Field(default=CharacterConfig(),
                                       description="Configuration for the character settings.")
    system: SystemConfig = Field(default=SystemConfig(), description="Configuration for the system settings.")
