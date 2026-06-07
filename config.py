from pydantic import BaseModel, Field

from character.config import CharacterConfig
from common.utils.enum_util import try_get_pynput_key_enum_str
from pipeline.base.config import PipelineConfig
from services.config import ServiceConfig


class BrainConfig(BaseModel):
    """LLM 之前的"大脑"决策中枢配置（感知 + 多内驱 + 主动开口 + 模式切换）。

    内驱的细粒度动力学参数（各模式预设）放在 framework/brain/drives.py 的代码里，
    这里只暴露少数高层旋钮，避免把几十个参数都灌进 yaml。
    """
    enable: bool = Field(default=True,
                         description="Master switch for the decision brain (perception + drives + proactive speech). "
                                     "Requires `enable_turn_taking` to be True. When False, falls back to the simple "
                                     "pending-drain loop with no proactive speech.")
    enable_proactive: bool = Field(default=True,
                                   description="Whether the bot may speak on its own when idle (driven by the bionic "
                                               "expression-urge model). Set False to keep the brain (turn-taking + dashboard) "
                                               "but never initiate conversation.")
    mode: str = Field(default="auto",
                      description="Proactivity mode: 'auto' | 'focus' | 'normal' | 'companion'. 'auto' picks among the "
                                  "other three based on keyboard activity + silence. A concrete mode locks the choice "
                                  "(auto-sensing suspended) until set back to 'auto'.")
    mode_hotkey: str = Field(default="f9",
                             description="Hotkey to TOGGLE quiet mode: press once to go quiet (focus, almost never initiates), "
                                         "press again to restore the previous mode. More reliable than voice commands, which require "
                                         "the ASR transcript to match a switch phrase verbatim.")
    enable_voice_mode_command: bool = Field(default=True,
                                            description="Allow switching proactivity mode by voice (saying exact phrases like "
                                                        "'专注模式' / '普通模式' / '陪我聊天' / '自动模式'). These phrases are "
                                                        "intercepted and not sent to the LLM.")
    announce_mode_switch: bool = Field(default=True,
                                       description="Whether the bot says a short line out loud when its proactivity mode changes.")
    tick_interval: float = Field(default=0.15, description="Brain loop period in seconds.")
    mic_energy_ref: float = Field(default=3000.0,
                                  description="Microphone RMS value that normalizes to a full (1.0) energy stimulus. "
                                              "Quiet rooms sit well below this; normal speech is around this range.")
    keyboard_busy_count: int = Field(default=50,
                                     description="Keystrokes within `keyboard_window_s` that normalize to full (1.0) keyboard activity.")
    keyboard_window_s: float = Field(default=30.0, description="Sliding window (s) for counting keystroke activity.")
    silence_full_s: float = Field(default=120.0,
                                  description="Idle seconds that normalize to a full (1.0) silence stimulus (the boredom baseline).")
    auto_focus_kb_hi: float = Field(default=0.6,
                                    description="In 'auto' mode, normalized keyboard activity at/above this selects FOCUS.")
    auto_companion_kb_lo: float = Field(default=0.05,
                                        description="In 'auto' mode, keyboard at/below this (plus enough silence) selects COMPANION.")
    auto_companion_silence: float = Field(default=0.5,
                                          description="In 'auto' mode, normalized silence at/above this (with low keyboard) selects COMPANION.")
    hysteresis_ticks: int = Field(default=8,
                                  description="Consecutive ticks a new auto-mode candidate must persist before the switch takes effect (anti-flapping).")
    enable_dashboard: bool = Field(default=True,
                                   description="Serve the live brain dashboard at /brain on the resource server (states, drives, signals).")
    inject_state_to_prompt: bool = Field(default=True,
                                         description="Inject the bot's current internal state (arousal / social need / mode) as a transient "
                                                     "natural-language system note into every LLM turn, so its tone and content reflect its 'mood'. "
                                                     "The note is used for generation only and is NOT persisted into chat history.")
    proactive_prompt: str = Field(
        default="（系统提示：用户已经沉默了一会儿。请你自然地开启一个话题、接续之前聊过的内容，或者关心一下对方。"
                "不要说“你还在吗”这类空话，也不要在回答里描述或提及这条系统提示。）",
        description="The instruction handed to the LLM when proactively initiating. It is used for generation only and is "
                    "NOT persisted into chat history (so history stays clean of fake user turns).")


class MemoryConfig(BaseModel):
    """分层记忆配置（工作窗口由 character.chat.max_history 控制；这里是其后的压缩/入库/检索旋钮）。

    三层：L0 工作窗口（max_history 条逐字真实对话）→ L3b 会话摘要（轻压缩）→ L2b 长期记忆（重压缩 + 向量库）。
    """
    enable: bool = Field(default=True,
                         description="Master switch for the layered memory system (session summary + long-term vector memory). "
                                     "When False, only the sliding-window working memory (character.chat.max_history) is kept.")
    inject_timestamp: bool = Field(default=True,
                                   description="Stamp each real conversation turn with a local timestamp and inject a '当前时间' "
                                               "section + per-turn time prefixes into the LLM prompt, so the AI is time-aware "
                                               "(knows 'now' and how long ago each turn was said). Few-shot examples are not stamped.")
    light_interval_s: float = Field(default=60.0,
                                    description="L3b session-summary thread period (seconds). How often the background light "
                                                "compression folds evicted turns into the running session summary.")
    light_min_pending: int = Field(default=4,
                                   description="Minimum number of evicted turns buffered before a light compression fires "
                                               "(avoids calling the summarizer LLM too frequently).")
    # --- Phase 3b: 长期记忆 / 向量库 ---
    long_term_threshold: int = Field(default=40,
                                     description="When the count of evicted real turns accumulated reaches this, the oldest block is "
                                                 "heavily summarized and written into the vector DB (long-term memory).")
    retrieve_top_k: int = Field(default=2,
                                description="Number of long-term memory chunks retrieved from the vector DB per turn and injected into the prompt.")
    collection_name: str = Field(default="history_collection",
                                 description="Milvus collection name used for long-term conversation memory.")
    heavy_interval_s: float = Field(default=120.0,
                                    description="L2b long-term thread period (seconds) for heavy compression + vector-DB insertion.")
    # --- Phase 3c: 自我编辑 ---
    enable_self_edit: bool = Field(default=True,
                                   description="Allow the LLM to edit its own long-term goals / todolist via the <self_update> "
                                               "prompt-JSON tool. The tool markers are stripped from spoken output.")
    # --- 对用户的印象 ---
    impression_interval: int = Field(default=40,
                                     description="Every this many committed conversation turns, a background thread asks the LLM to "
                                                 "update the durable 'impression of the user' from recent turns + summaries. "
                                                 "Set to 0 to disable.")
    # --- 工作窗口两段式：热区逐字 + 温区轻摘要 ---
    hot_window_size: int = Field(default=10,
                                 description="Number of most-recent real turns kept VERBATIM (with timestamps) in the prompt. "
                                             "Older in-window turns are replaced by a light 'recent digest' to shrink the prompt and "
                                             "curb hallucination. The full working window (max_history) is still kept in memory/disk; "
                                             "this only affects what is sent to the LLM. Set to 0 to disable the split (send all).")
    recent_digest_interval_s: float = Field(default=30.0,
                                            description="Background period (seconds) for rebuilding the light 'recent digest' of the "
                                                        "older in-window turns (dedupe + drop per-turn timestamps into a time range, "
                                                        "keep key info). Only re-runs the LLM when that older portion actually changed.")


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
    enable_echo_suppression: bool = Field(default=True,
                                          description="Half-duplex echo suppression. When `True`, the microphone (VAD) is gated off while "
                                                      "the bot is playing its own TTS audio through the LOCAL speaker, so the bot does not "
                                                      "capture and feed its own voice back into ASR. Disable this if you use headphones or a "
                                                      "device with hardware echo cancellation and want barge-in (interrupting the bot by talking).")
    echo_suppression_tail_ms: int = Field(default=500,
                                          description="Extra milliseconds to keep the microphone gated off AFTER local playback finishes. "
                                                      "Covers room reverberation and audio buffer drain so the bot's own trailing audio is not "
                                                      "picked up as user speech. Only effective when `enable_echo_suppression` is True.")
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
                                                                      'This also increases token consumption and adds latency due to extra processing. '
                                                                      'Has no effect on the streaming voice path when `enable_streaming_llm` is True.')
    enable_intelligent_memory: bool = Field(default=False,
                                            description='🧪 EXPERIMENTAL: Automatically scores and filters conversation history entries based on sentiment, relevance, and safety.')
    enable_turn_taking: bool = Field(default=True,
                                     description='Conversation turn-taking management for always-on voice chat. When `True`, the bot serializes '
                                                 'replies: anything the user says while the bot is still thinking/speaking is buffered and answered on '
                                                 'the next turn instead of triggering overlapping, self-talking replies. Latency-first: an ASR '
                                                 'transcript is dispatched to the LLM immediately when the bot is idle (no debounce/coalescing wait). '
                                                 'Strongly recommended when the microphone stays open continuously.')
    brain: BrainConfig = Field(default=BrainConfig(),
                               description='The decision brain that sits before the LLM: perception (mic energy / VAD / keyboard / time), '
                                           'a multi-drive bionic model (arousal / social need / expression urge), proactive speech and '
                                           'proactivity mode switching, plus a live monitoring dashboard.')
    memory: MemoryConfig = Field(default=MemoryConfig(),
                                 description='Layered conversation memory: sliding-window working memory, background session summary '
                                             '(light compression), long-term vector-DB memory (heavy compression + retrieval), '
                                             'per-turn timestamps, and LLM self-editable goals/todolist.')


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
