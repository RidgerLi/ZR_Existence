import devices.headless
from agent.custom_agent import CustomAgent
from agent.tool_agent import ToolAgent
from character.filter.strategy import FirstMatchedFilter
from common.generator.gradio_gen import DynamicConfigPage
from devices.headless import is_headless
from devices.speaker import Speaker
from manager.config_manager import get_config
from manager.llm_prompt_manager import LLMPromptManager
from manager.model_manager import ModelManager
from manager.tts_prompt_manager import TTSPromptManager
from devices.microphone import SmartMicrophone
from pipeline.asr.asr_sync import ASRSyncPipeline
from pipeline.db.milvus.milvus_sync import MilvusSyncPipeline
from pipeline.imgcap.imgcap_sync import ImgCapSyncPipeline
from pipeline.llm.llm_sync import LLMSyncPipeline
from pipeline.ocr.ocr_sync import OCRSyncPipeline
from pipeline.tts.tts_sync import TTSSyncPipeline
from pipeline.vidcap.vidcap_sync import VidCapSyncPipeline
from pipeline.vla.showui.showui_sync import ShowUISyncPipeline
from services.browser.browser import Browser
from services.game.config import PlatformEnum
from services.game.minecraft.app import KonekoMinecraftAIAgent
from services.live_stream.bilibili import BilibiliService
from services.live_stream.twitch import TwitchService
from services.live_stream.youtube import YouTubeService
from services.obs.client import ObsStudioWsClient
from services.playground.bridge import PlaygroundBridge
from services.playground.res.res_server import ResourceServer

_config = get_config()


class ZerolanLiveRobotContext:
    """
    ZerolanLiveRobotContext 应只包括初始化上下文资源的工作，
    事件监听器注册和各类事件的发送逻辑不应该体现在这里。
    ZerolanLiveRobotContext here should only include initializing the context resource,
    The logic for registering listeners and sending events shouldn't be reflected here.
    """

    def __init__(self):
        self.llm: LLMSyncPipeline | None = None
        self.asr: ASRSyncPipeline | None = None
        self.ocr: OCRSyncPipeline | None = None
        self.tts: TTSSyncPipeline | None = None
        self.img_cap: ImgCapSyncPipeline | None = None
        self.vid_cap: VidCapSyncPipeline | None = None
        self.showui: ShowUISyncPipeline | None = None
        self.vec_db: MilvusSyncPipeline | None = None

        self.filter: FirstMatchedFilter | None = None
        self.llm_prompt_manager: LLMPromptManager | None = None
        self.tts_prompt_manager: TTSPromptManager | None = None

        self.tool_agent: ToolAgent | None = None

        if not devices.headless.is_headless():
            from devices.screen.base_screen import Screen
            self.screen: Screen = Screen()
        else:
            self.screen = None

        self.browser: Browser | None = None
        self.speaker: Speaker | None = None
        self.playground: PlaygroundBridge | None = None
        self.qq = None

        self.bot_id: str | None = None
        self.bot_name: str | None = None
        self.master_name: str = "AkagawaTsurunaki"
        self.live2d_model: str | None = None
        self.live2d_viewer = None
        self.obs: ObsStudioWsClient | None = None
        self.res_server: ResourceServer | None = None
        self.game_agent = None

        self.bilibili: BilibiliService | None = None
        self.youtube: YouTubeService | None = None
        self.twitch: TwitchService | None = None

        assert _config.pipeline.llm.enable, f"At least LLMPipeline must be enabled in your config."
        self.llm = LLMSyncPipeline(_config.pipeline.llm)
        self.filter = FirstMatchedFilter(_config.character.chat.filter.bad_words)
        self.llm_prompt_manager = LLMPromptManager(_config.character.chat)
        self.speaker = Speaker()
        self.bot_name = _config.character.bot_name
        self.res_server = ResourceServer(_config.service.res_server.host, _config.service.res_server.port)
        self.model_manager = None

        if _config.pipeline.asr.enable:
            self.asr = ASRSyncPipeline(_config.pipeline.asr)
        if _config.pipeline.ocr.enable:
            self.ocr = OCRSyncPipeline(_config.pipeline.ocr)
        if _config.pipeline.tts.enable:
            self.tts_prompt_manager = TTSPromptManager(_config.character.speech)
            self.tts = TTSSyncPipeline(_config.pipeline.tts)
        if _config.pipeline.img_cap.enable:
            self.img_cap = ImgCapSyncPipeline(_config.pipeline.img_cap)
        if _config.pipeline.vid_cap.enable:
            self.vid_cap = VidCapSyncPipeline(_config.pipeline.vid_cap)
        if _config.pipeline.vla.enable:
            if _config.pipeline.vla.showui.enable:
                self.showui = ShowUISyncPipeline(_config.pipeline.vla.showui)
        if _config.service.browser.enable:
            self.browser = Browser(_config.service.browser)
        if _config.service.game.enable:
            if _config.service.game.platform == PlatformEnum.Minecraft:
                if self.tool_agent is None:
                    self.tool_agent = ToolAgent(_config.pipeline.llm)
                self.game_agent = KonekoMinecraftAIAgent(_config.service.game, self.tool_agent)
        if _config.service.live_stream.enable:
            if _config.service.live_stream.bilibili.enable:
                self.bilibili = BilibiliService(_config.service.live_stream.bilibili)
            if _config.service.live_stream.youtube.enable:
                self.youtube = YouTubeService(_config.service.live_stream.youtube)
            if _config.service.live_stream.twitch.enable:
                self.twitch = TwitchService(_config.service.live_stream.twitch)
        if _config.pipeline.vec_db.enable:
            if _config.pipeline.vec_db.backend == "milvus":
                self.vec_db = MilvusSyncPipeline(_config.pipeline.vec_db.milvus)
            else:
                from pipeline.db.milvus.chroma_local import ChromaLocalPipeline
                self.vec_db = ChromaLocalPipeline(_config.pipeline.vec_db)
        if _config.service.playground.enable:
            self.model_manager = ModelManager()
            self.bot_id = _config.service.playground.bot_id
            self.live2d_model = _config.service.playground.model_dir
            self.custom_agent = CustomAgent(config=_config.pipeline.llm)
            self.playground = PlaygroundBridge(config=_config.service.playground)
        if _config.service.qqbot.enable:
            from services.qqbot.napcat import QQBotService

            self.qq: QQBotService = QQBotService(_config.service.qqbot)
        # 全双工回声消除（AEC）：开启后机器人说话期间麦克风持续采集，用 WebRTC AEC3 + WASAPI
        # 回采参考信号把机器人自己的声音从麦克风信号里减掉，从而能听见用户插话（barge-in 的采集基础）。
        # 依赖缺失时 EchoCanceller.available 为 False，麦克风会自动退回半双工。
        echo_canceller = None
        if _config.system.enable_full_duplex:
            from devices.aec import EchoCanceller
            # 30ms@16k = 480 samples/frame，与麦克风一致。
            echo_canceller = EchoCanceller(
                sample_rate=16000,
                frame_samples=int(16000 * 30 / 1000),
                stream_delay_ms=_config.system.aec_stream_delay_ms,
                loopback_device_index=_config.system.aec_loopback_device_index,
            )

        self.mic = SmartMicrophone(
            enable_vad=True,
            vad_mode=_config.system.microphone_vad_mode,
            playback_tail_ms=_config.system.echo_suppression_tail_ms,
            energy_ref=_config.system.brain.mic_energy_ref,
            full_duplex=_config.system.enable_full_duplex,
            echo_canceller=echo_canceller,
            playback_speech_prob=_config.system.full_duplex_playback_speech_prob,
        )

        # 把扬声器的本地播放事件接到麦克风的播放状态门控上：
        # - 半双工（enable_echo_suppression）：播放期间丢弃麦克风帧，防止采集到自己的声音；
        # - 全双工（enable_full_duplex）：播放期间不丢帧，但用该状态在播放期对残余回声做更严的过滤。
        if _config.system.enable_full_duplex or _config.system.enable_echo_suppression:
            self.speaker.set_playback_hooks(self.mic.begin_playback, self.mic.end_playback)

        # Headless system can not load `pynput` and `pygame`
        self.keyboard = None
        if not is_headless():
            from devices.keyboard import SmartKeyboard
            hotkeys = [_config.system.microphone_hotkey]
            # 大脑启用时，额外监听切换主动开口模式的热键。
            if _config.system.brain.enable and _config.system.enable_turn_taking:
                mode_hotkey = _config.system.brain.mode_hotkey
                if mode_hotkey and mode_hotkey != _config.system.microphone_hotkey:
                    hotkeys.append(mode_hotkey)
            self.keyboard = SmartKeyboard(hotkeys=hotkeys)
        if _config.service.obs.enable:
            self.obs = ObsStudioWsClient(_config.service.obs)
        self.config_page = DynamicConfigPage(_config)
        # Agents
        self.tool_agent = ToolAgent(_config.pipeline.llm)
        if _config.service.live2d_viewer.enable:
            from services.live2d.live2d_viewer import Live2DViewer
            self.live2d_viewer = Live2DViewer(_config.service.live2d_viewer)
