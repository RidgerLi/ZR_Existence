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
    """分层记忆配置（串行管线；工作窗口由 character.chat.max_history 控制）。

    串行三层：
        热区 hot（hot_window_size 条逐字真实对话，直接发 LLM）
          → 温区 recent_digest（窗口内、热区之外的较早原文，整理成保真的"近期回顾"，常驻 prompt，唯一的近期线性记忆）
          → 长期记忆 L2b（被滑出整个窗口的原文，逐条原样写入向量库，按语义检索 top-k 回拼）。
    """
    enable: bool = Field(default=True,
                         description="Master switch for the layered memory system (recent digest + long-term vector memory). "
                                     "When False, only the sliding-window working memory (character.chat.max_history) is kept.")
    inject_timestamp: bool = Field(default=True,
                                   description="Stamp each real conversation turn with a local timestamp and inject a '当前时间' "
                                               "section + per-turn time prefixes into the LLM prompt, so the AI is time-aware "
                                               "(knows 'now' and how long ago each turn was said). Few-shot examples are not stamped.")
    # --- 长期记忆 / 向量库（逐条原文入库 + 语义检索）---
    retrieve_top_k: int = Field(default=2,
                                description="Number of long-term memory lines retrieved from the vector DB per turn and injected into the prompt.")
    collection_name: str = Field(default="history_collection",
                                 description="Vector-DB collection name used for long-term conversation memory.")
    # --- Phase 3c: 自我编辑 ---
    enable_self_edit: bool = Field(default=True,
                                   description="Allow the LLM to edit its own long-term goals / todolist via the <self_update> "
                                               "prompt-JSON tool. The tool markers are stripped from spoken output.")
    # --- 对用户的印象 ---
    impression_interval: int = Field(default=40,
                                     description="Every this many committed conversation turns, a background thread asks the LLM to "
                                                 "update the durable 'impression of the user' from recent turns + summaries. "
                                                 "Set to 0 to disable.")
    # --- 工作窗口两段式：热区逐字 + 温区轻摘要（水位滑动窗口）---
    hot_window_size: int = Field(default=8,
                                 description="LOW watermark of the verbatim hot zone: after a compaction the hot zone falls back to "
                                             "this many most-recent real turns (kept VERBATIM, with timestamps). It is also the floor "
                                             "of the prompt's verbatim window. Older in-window turns are represented by a light 'recent "
                                             "digest'. The full working window (max_history) is still kept in memory/disk; this only "
                                             "affects what is sent to the LLM. Set to 0 to disable the split (send all).")
    hot_high_watermark: int = Field(default=12,
                                    description="HIGH watermark of the verbatim hot zone. Whenever the number of verbatim (not-yet-"
                                                "digested) turns exceeds this, a background fold compresses the oldest of them into the "
                                                "recent digest, bringing the hot zone back down to hot_window_size. Must be > hot_window_size. "
                                                "The hot zone therefore floats within [hot_window_size, hot_high_watermark].")
    recent_digest_interval_s: float = Field(default=30.0,
                                            description="Safety re-check period (seconds) for the background digest-fold loop. Folds are "
                                                        "primarily event-driven (triggered immediately after each committed turn); this "
                                                        "interval is only a self-healing fallback, NOT the main trigger.")


class SystemConfig(BaseModel):
    default_enable_microphone: bool = Field(default=False,
                                            description="For safety, do not open your microphone by default. \n"
                                                        "You can set it `True` to enable your microphone")
    microphone_vad_mode: int = Field(default=3,
                                     description="Optionally, set its aggressiveness mode, which is an integer between 0 and 3. " \
                                                 "0 is the least aggressive about filtering out non-speech, 3 is the most aggressive.")
    vad_silence_hangover_ms: int = Field(default=1200,
                                         description="End-of-utterance silence delay (ms): AFTER you have started speaking, the VAD waits for "
                                                     "this many milliseconds of CONTINUOUS silence before deciding your sentence is finished and "
                                                     "sending it to ASR. Raise it (e.g. 1500-2000) if the bot keeps cutting in while you are still "
                                                     "talking / pausing mid-sentence; lower it (e.g. 600-800) for snappier turn-taking. Short pauses "
                                                     "(breaths, hesitations) shorter than this do NOT end the turn.")
    vad_min_speech_ms: int = Field(default=1000,
                                   description="Minimum total speech (ms) an utterance must contain to be sent to ASR. Utterances shorter than this "
                                               "are treated as VAD jitter/noise and dropped. Lower it (e.g. 300-500) if short replies like '等一下' "
                                               "are being ignored.")
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
    enable_full_duplex: bool = Field(default=False,
                                     description="Full-duplex acoustic echo cancellation (AEC). When `True`, the microphone keeps capturing "
                                                 "WHILE the bot is talking, and a WebRTC AEC3 canceller removes the bot's own voice from the "
                                                 "mic signal using a WASAPI loopback of the speaker output as the reference signal. This is the "
                                                 "foundation for barge-in (interrupting the bot by talking) and natural full-duplex chat. "
                                                 "Overrides the half-duplex `enable_echo_suppression` gating (frames are cleaned instead of dropped). "
                                                 "Requires `PyAudioWPatch` (Windows WASAPI loopback) and `pywebrtc-audio`; if either is missing it "
                                                 "automatically falls back to half-duplex.")
    aec_stream_delay_ms: int = Field(default=0,
                                     description="AEC delay hint (ms): the delay between audio being written to the speaker and its echo appearing "
                                                 "in the mic capture. 0 lets AEC3's internal estimator figure it out; providing a rough value "
                                                 "(typically 80-200ms for speaker+room) helps the canceller converge faster. "
                                                 "Only effective when `enable_full_duplex` is True.")
    aec_loopback_device_index: int = Field(default=-1,
                                           description="WASAPI loopback input-device index used as the AEC reference (run `python -m pyaudiowpatch` "
                                                       "to list devices). -1 picks the loopback of the default system speakers. "
                                                       "Only effective when `enable_full_duplex` is True.")
    full_duplex_playback_speech_prob: float = Field(default=0.7,
                                                    description="While the bot is playing audio in full-duplex mode, a captured frame is only treated "
                                                                "as user speech when the AEC's speech probability is at/above this threshold (0.0-1.0). "
                                                                "Rejects residual echo that survives cancellation so the bot does not transcribe its own "
                                                                "trailing voice. Set to 0 to disable this extra gate. Only effective when "
                                                                "`enable_full_duplex` is True.")
    enable_barge_in: bool = Field(default=True,
                                  description="Barge-in (interrupting the bot by talking). When `True` and `enable_full_duplex` is on, the bot HARD-CUTS "
                                              "its current speech the moment it confidently detects the user starting to talk: it stops playback, "
                                              "clears the queued TTS, and cancels the in-flight LLM/TTS generation. The user's interrupting utterance is "
                                              "then transcribed and answered as a fresh turn. Requires full-duplex (the mic must stay open while the bot "
                                              "talks); has no effect in half-duplex.")
    barge_in_min_ms: int = Field(default=1200,
                                 description="How many milliseconds of CONFIDENT accumulated user speech (while the bot is playing) are required before a "
                                             "barge-in hard-cut fires. Short silences (consonants/breaths) shorter than the VAD silence hangover do NOT "
                                             "reset the count; a sustained silence does. Higher values are more robust against residual echo / coughs but "
                                             "make interruption feel slower; lower values interrupt faster but risk false cuts. Only effective when "
                                             "`enable_barge_in` and `enable_full_duplex` are True.")
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
                                 description='Layered conversation memory (serial pipeline): verbatim hot window -> warm "recent digest" '
                                             '-> long-term vector-DB memory (per-line raw ingest + semantic retrieval), '
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
