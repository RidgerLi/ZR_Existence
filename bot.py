import asyncio
import os
import re
import threading
import time
from concurrent.futures.thread import ThreadPoolExecutor
from pathlib import Path
from queue import Queue
from typing import List

from loguru import logger
from zerolan.data.data.prompt import TTSPrompt
from zerolan.data.pipeline.asr import ASRStreamQuery
from zerolan.data.pipeline.img_cap import ImgCapQuery
from zerolan.data.pipeline.llm import LLMQuery, LLMPrediction, Conversation, RoleEnum
from zerolan.data.pipeline.milvus import MilvusInsert, InsertRow, MilvusQuery
from zerolan.data.pipeline.ocr import OCRQuery
from zerolan.data.pipeline.tts import TTSQuery
from zerolan.data.pipeline.vla import ShowUiQuery

from agent.api import sentiment_analyse, translate, summary_history, find_file, model_scale, sentiment_score, \
    memory_score, update_user_impression
from common.concurrent.abs_runnable import stop_all_runnable
from common.concurrent.killable_thread import KillableThread, kill_all_threads
from common.enumerator import Language
from common.io.api import save_audio
from common.io.file_type import AudioFileType
from common.utils import audio_util, math_util
from common.utils.img_util import is_image_uniform
from common.utils.str_util import split_by_punc, is_blank
from event.event_data import DeviceMicrophoneVADEvent, DeviceKeyboardPressEvent, DeviceScreenCapturedEvent, \
    PipelineOutputLLMEvent, \
    PipelineImgCapEvent, \
    QQMessageEvent, DeviceMicrophoneSwitchEvent, PipelineOutputTTSEvent, PipelineASREvent, \
    PipelineOCREvent, SecondEvent, ConfigFileModifiedEvent, LiveStreamDanmakuEvent, DeviceSpeakerPlayEvent
from event.event_emitter import emitter
from event.registry import EventKeyRegistry
from framework.base_bot import BaseBot
from framework.conversation_state import ConversationState
from framework.brain.brain import Brain
from framework.brain.drives import DriveSystem
from framework.brain.perception import (Perception, MicEnergySensor, VadSensor,
                                        KeyboardActivitySensor, TimeSilenceSensor,
                                        CameraMotionSensor, MemoryUrgeSensor)
from framework.memory.memory_manager import MemoryManager
from framework.memory.memory_store import MemoryStore, MemoryState
from framework.memory.self_state import SelfState
from framework.memory.self_update import ToolRegistry, extract_and_strip, MARKER_OPEN
from manager.config_manager import get_config
from manager.prompt_composer import PromptComposer
from pipeline.ocr.ocr_sync import avg_confidence, stringify

_config = get_config()


class ZerolanLiveRobot(BaseBot):
    def __init__(self):
        super().__init__()
        self.cur_lang = Language.ZH
        if self.tts_prompt_manager is not None:
            self.tts_prompt_manager.set_lang(self.cur_lang)
        self._timer_flag = True
        self.tts_thread_pool = ThreadPoolExecutor(max_workers=1)
        self.enable_exp_memory = _config.system.enable_intelligent_memory
        self.enable_sentiment_analysis = _config.system.enable_sentiment_analysis
        self.enable_split_by_punc = _config.system.enable_clause_split
        self.enable_streaming_llm = _config.system.enable_streaming_llm
        self.subtitles_queue = Queue()

        # 对话轮次管理：把"AI 是否在忙"的状态集中起来，避免常开语音时一句话被 VAD 切碎后
        # 并发触发多次回复（详见 framework/conversation_state.py）。延迟优先：ASR 文字一出来
        # 就直接进轮次锁，不做去抖等待——空闲立刻回，忙则缓存到下一轮。
        self.enable_turn_taking = _config.system.enable_turn_taking
        self.conv_state = ConversationState()
        self.conv_state.set_speaker_busy_probe(self.speaker.is_busy)
        # 同一时刻只允许一个轮次进入 LLM；其余输入被缓存到下一轮。
        self._turn_lock = threading.Lock()

        # 插话打断（barge-in）取消令牌：被 `_on_barge_in` 置位后，在途的流式 LLM 生成与
        # TTS 派发会尽快停止；每一轮回复开始时清除。仅在全双工 + enable_barge_in 时由麦克风触发。
        self._cancel_event = threading.Event()
        if self.mic is not None and _config.system.enable_barge_in:
            self.mic.set_barge_in_callback(self._on_barge_in)
        # 轮次执行器：让"大脑"决定开口后把这一轮的 LLM 放到后台跑，大脑循环可继续 tick
        # （感知/内驱/面板不被一轮长回复阻塞）。单线程，保证轮次串行。
        self._turn_executor = ThreadPoolExecutor(max_workers=1)

        # LLM 之前的"大脑"：感知 → 多内驱 → 决策（被动响应 / 主动开口）。
        self._proactive_prompt = _config.system.brain.proactive_prompt
        # 主动开口（proactive）产生的 AI 发言先暂存这里，不直接进短期历史；只有当用户随后真的
        # 回复时，才把"这条主动发言 + 用户回复"一起补进历史。否则会被无人应答的自言自语填满。
        self._pending_proactive: Conversation | None = None
        self.brain: Brain | None = None
        if self.enable_turn_taking and _config.system.brain.enable:
            self._build_brain()

        # 分层记忆：工作窗口（current_history，由 max_history 滑动窗口控制）之外，
        # 被裁掉的真实对话交给 MemoryManager 做后台会话摘要（L3b）与长期入库（L2b）。
        self._long_term_memory: str = ""
        self._long_term_query: str = ""    # 上次用于检索的 query（监控用）
        self._long_term_hits: list = []    # 上次检索命中：[{text, distance}]（监控用）
        self._current_user_text: str = ""  # 最近一轮用户输入，供 L2b 长期记忆按语义检索
        self._mem_cfg = _config.system.memory
        self.memory = MemoryManager(
            hot_low=self._mem_cfg.hot_window_size,
            hot_high=self._mem_cfg.hot_high_watermark,
            safety_interval_s=self._mem_cfg.recent_digest_interval_s,
            vec_line_sink=self._ingest_line_to_vecdb,
        )
        if self._mem_cfg.enable:
            # 被滑出整个工作窗口的原文 → 逐条过滤后入向量库（长期记忆 L2b）。
            self.llm_prompt_manager.set_evict_callback(self.memory.enqueue_evicted)
            # 温区"近期回顾"按当前工作窗口的较早段重建。
            self.memory.set_live_turns_provider(self.llm_prompt_manager.live_turns)

        # 对话历史 + 会话摘要 + 对用户的印象 + 计数器的磁盘持久化（resources/memory/memory.md）：
        # 启动时载入工作窗口，运行中每轮由后台 IO 线程异步写回。
        self.memory_store = MemoryStore(os.path.join("resources", "memory", "memory.md"))
        _loaded = self.memory_store.load()
        if _loaded.turns:
            self.llm_prompt_manager.seed_live_turns(_loaded.turns)
        if _loaded.recent_digest:
            self.memory.recent_digest = _loaded.recent_digest
            # 还原温区覆盖计数：假定持久化时温区盖到了"低水位之前"的全部轮次，避免重启后逐字段把整窗重发。
            _seeded_live = len(self.llm_prompt_manager.live_turns())
            self.memory.seed_digest_len(max(0, _seeded_live - self._mem_cfg.hot_window_size))
        # 对用户的印象（顶层 syspromt 的一块）+ 触发计数器，均随 memory.md 持久化。
        self._user_impression: str = _loaded.user_impression
        self._turn_counter: int = _loaded.turn_counter
        self._last_impression_at: int = _loaded.last_impression_at
        self._impression_interval: int = self._mem_cfg.impression_interval
        self._impression_updating: bool = False

        # 可被 LLM 自我编辑的长期目标 / todolist（L2a 的可变部分），独立持久化到 json。
        self.self_state = SelfState(os.path.join("resources", "memory", "self_state.json"))
        self.tool_registry = ToolRegistry()
        self.tool_registry.register("update_self", self.self_state.apply_update)

        # 提示词分层装配：当前时间 + 人设(L1) + 固定长远计划&目标&待办(L2a) + 长期记忆(L2b)
        #                + 会话摘要(L3b) + 当前状态(L3a) + 自我管理工具说明。
        self.prompt_composer = PromptComposer([
            self._section_current_time,
            self._section_directives,
            self._section_user_impression,
            self._section_long_term_memory,
            self._section_recent_digest,
            self._section_state,
            self._section_tool_instructions,
        ])

        self.init()
        self._publish_memory_snapshot()  # 面板初始就有目标/待办/已注入历史可看
        logger.info("🤖 Zerolan Live Robot: Initialized services successfully.")

    async def start(self):
        logger.info("🤖 Zerolan Live Robot: Running...")
        self.memory_store.start()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(emitter.start())
            if self.model_manager is not None:
                self.model_manager.scan()

            threads = []
            if _config.system.default_enable_microphone:
                vad_thread = KillableThread(target=self.mic.start, daemon=True, name="VADThread")
                threads.append(vad_thread)

            if self.enable_turn_taking:
                if self.brain is not None:
                    # 大脑决策循环：感知 → 多内驱 → 决策（被动响应 + 主动开口）。
                    brain_thread = KillableThread(
                        target=lambda: self.brain.run(lambda: self._timer_flag),
                        daemon=True, name="BrainLoop")
                    threads.append(brain_thread)
                else:
                    # 退化：仅做 pending 补发，无主动开口。
                    turn_thread = KillableThread(target=self._turn_dispatch_loop, daemon=True,
                                                 name="TurnDispatchLoop")
                    threads.append(turn_thread)

            if self._mem_cfg.enable:
                # 温区"近期回顾"：把窗口内较早的那段对话整理成保真回顾，作为 prompt 里唯一的近期线性记忆。
                # （被滑出整个窗口的原文则在驱逐回调里逐条入向量库，无需独立线程。）
                if self._mem_cfg.hot_window_size > 0:
                    digest_thread = KillableThread(
                        target=lambda: self.memory.run_recent_digest(lambda: self._timer_flag),
                        daemon=True, name="RecentDigestThread")
                    threads.append(digest_thread)

            if self.keyboard is not None:
                keyboard_thread = KillableThread(target=self.keyboard.start, daemon=True, name="KeyboardThread")
                threads.append(keyboard_thread)

            speaker_thread = KillableThread(target=self.speaker.start, daemon=True, name="SpeakerThread")
            threads.append(speaker_thread)

            if self.playground:
                playground_thread = KillableThread(target=self.playground.start, daemon=True, name="PlaygroundThread")
                threads.append(playground_thread)

            if self.res_server:
                res_server_thread = KillableThread(target=self.res_server.start, daemon=True, name="ResServerThread")
                threads.append(res_server_thread)

            if self.obs is not None:
                obs_client_thread = KillableThread(target=self.obs.start, daemon=True, name="ObsClientThread")
                threads.append(obs_client_thread)

            if self.live2d_viewer is not None:
                live2d_viewer_thread = KillableThread(target=self.live2d_viewer.start, daemon=True,
                                                      name="Live2DViewerThread")
                threads.append(live2d_viewer_thread)

            if self.game_agent:
                game_agent_thread = KillableThread(target=self.game_agent.start, daemon=True, name="GameAgentThread")
                threads.append(game_agent_thread)

            for thread in threads:
                thread.start()

            # tg.create_task(emitter.start())
            if self.bilibili:
                def start_bili():
                    asyncio.run(self.bilibili.start())

                bili_thread = KillableThread(target=start_bili, daemon=True, name="BilibiliThread")
                bili_thread.start()
            if self.youtube:
                tg.create_task(self.youtube.start())
            if self.twitch:
                tg.create_task(self.twitch.start())
            if self.config_page:
                tg.create_task(self.config_page.start())
            elapsed = 0
            while self._timer_flag:
                await asyncio.sleep(1)
                emitter.emit(SecondEvent(elapsed=elapsed))
                elapsed += 1

        for thread in threads:
            thread.join()

    async def stop(self):
        self.tts_thread_pool.shutdown()
        emitter.stop()
        kill_all_threads()
        await stop_all_runnable()
        self.memory_store.stop()  # 把最后一次对话快照刷盘
        logger.info("Good Bye!")

    def init(self):
        @emitter.on(EventKeyRegistry.Playground.CONNECTED)
        def on_playground_connected(_):
            self.mic.pause()
            logger.info("Because ZerolanPlayground client connected, close the local microphone.")
            if self.playground:
                self.playground.load_live2d_model(
                    bot_id=self.bot_id,
                    bot_display_name=self.bot_name,
                    model_dir=self.live2d_model
                )
            logger.info(f"Live 2D model loaded: {self.live2d_model}")

        @emitter.on(EventKeyRegistry.Playground.DISCONNECTED)
        def on_playground_disconnected(_):
            # self.vad.resume()
            # logger.info("Because ZerolanPlayground client disconnected, open the local microphone.")
            pass

        @emitter.on(EventKeyRegistry.Device.KEYBOARD_HOTKEY_PRESS)
        def hotkey_handler(event: DeviceKeyboardPressEvent):
            logger.info(f'Hotkey toggle: {event.hotkey}')
            # 判断 hotkey 内容
            try:
                if event.hotkey == _config.system.microphone_hotkey:
                    if _config.system.default_enable_microphone:
                        # 麦克风对象锁
                        with self.keyboard.microphone_state_lock:
                            if self.mic.is_set_talk_enabled_event():
                                logger.debug(f'Hotkey toggled: MIC OFF')

                                # 关麦
                                self.mic.unset_talk_enabled_event()

                                # 强制 emit 已经收集的片段
                                self.mic.force_commit(is_emit=True)

                                # TODO: 播放停止提示音
                                pass
                            else:
                                logger.debug(f'Hotkey toggled: MIC ON')

                                # 仅清空可能遗留的音频
                                self.mic.force_commit(is_emit=False)

                                # TODO: 播放开始提示音，block=True
                                pass

                                # 开麦
                                self.mic.set_talk_enabled_event()
                    else:
                        logger.info(f'Microphone is disabled at config.yaml')
                elif self.brain is not None and event.hotkey == _config.system.brain.mode_hotkey:
                    # 切换"安静模式"：按一下进安静(focus)，再按一下恢复之前的模式。
                    # 比语音命令可靠——语音拦截要求 ASR 输出与短语逐字匹配，现实中难以保证。
                    new_mode = self.brain.toggle_quiet()
                    self._announce_mode(new_mode)
            except Exception as e:
                logger.exception(e)

        @emitter.on(EventKeyRegistry.Device.MICROPHONE_SWITCH)
        def on_open_microphone(event: DeviceMicrophoneSwitchEvent):
            if self.mic.is_recording:
                if event.switch:
                    logger.warning("The microphone has already resumed.")
                    return
                self.mic.pause()
            else:
                if not event.switch:
                    logger.warning("The microphone has already paused.")
                    return
                self.mic.resume()

        @emitter.on(EventKeyRegistry.Device.MICROPHONE_VAD)
        def on_service_vad_speech_chunk(event: DeviceMicrophoneVADEvent):
            logger.debug("`SpeechEvent` received.")
            speech, channels, sample_rate = event.speech, event.channels, event.sample_rate
            query = ASRStreamQuery(is_final=True, audio_data=speech, channels=channels, sample_rate=sample_rate,
                                   media_type=event.audio_type.value)

            for prediction in self.asr.stream_predict(query):
                logger.info(f"ASR: {prediction.transcript}")
                if is_blank(prediction.transcript):
                    continue
                emitter.emit(PipelineASREvent(prediction=prediction))
                logger.debug("ASREvent emitted.")

        @emitter.on(EventKeyRegistry.Pipeline.ASR)
        def asr_handler(event: PipelineASREvent):
            logger.debug("`ASREvent` received.")
            prediction = event.prediction
            if self.enable_turn_taking:
                # 延迟优先：文字一出来就交给轮次锁统一调度——空闲立刻触发 LLM，忙则缓存到下一轮。
                self._dispatch_user_utterance(prediction.transcript)
            else:
                self.emit_llm_prediction(prediction.transcript)

            # TODO 关闭额外功能，只保留llm回复功能
            # if self.playground:
            #     self.playground.add_history(role="user", text=prediction.transcript, username=self.master_name)
            # if "打开浏览器" in prediction.transcript:
            #     if self.browser is not None:
            #         self.browser.open("https://www.bing.com")
            # elif "关闭浏览器" in prediction.transcript:
            #     if self.browser is not None:
            #         self.browser.close()
            # elif "网页搜索" in prediction.transcript:
            #     if self.browser is not None:
            #         self.browser.move_to_search_box()
            #         text = prediction.transcript[4:]
            #         self.browser.send_keys_and_enter(text)
            # elif "游戏" in prediction.transcript:
            #     self.game_agent.exec_instruction(prediction.transcript)
            # elif "看见" in prediction.transcript:
            #     img, img_save_path = self.screen.safe_capture(k=0.99)
            #     if not self.check_img(img):
            #         return
            #     emitter.emit(DeviceScreenCapturedEvent(img_path=img_save_path, is_camera=False))
            # elif "点击" in prediction.transcript:
            #     # If there is no display, then can not use this feature
            #     if os.environ.get('DISPLAY', None) is None:
            #         return
            #     img, img_save_path = self.screen.safe_capture(k=0.99)
            #     if not self.check_img(img):
            #         return

            #     query = ShowUiQuery(query=prediction.transcript, env="web", img_path=img_save_path)
            #     prediction = self.showui.predict(query)
            #     logger.debug("ShowUI: " + prediction.model_dump_json())
            #     action = prediction.actions[0]
            #     if action.action == "CLICK":
            #         import pyautogui
            #         logger.info("Click action triggered.")
            #         x, y = action.position[0] * img.width, action.position[1] * img.height
            #         pyautogui.moveTo(x, y)
            #         pyautogui.click()
            # elif "记得" in prediction.transcript:
            #     query = MilvusQuery(collection_name="history_collection", limit=2, output_fields=['history', 'text'],
            #                         query=prediction.transcript)
            #     result = self.vec_db.search(query)
            #     memory = result.result[0][0]
            #     memory = memory.entity["text"]
            #     logger.debug(f"Memory found: {memory}")
            #     self.emit_llm_prediction(f"{memory}\n\n请根据上文回答：{prediction.transcript} \n")
            # elif "加载模型" in prediction.transcript:
            #     file_id = find_file(self.model_manager.get_files(), prediction.transcript)
            #     file_info = self.model_manager.get_file_by_id(file_id)
            #     if self.playground:
            #         self.playground.load_3d_model(file_info)
            # elif "调整模型" in prediction.transcript:
            #     if self.playground:
            #         info = self.playground.get_gameobjects_info()
            #         if not info:
            #             logger.warning("No gameobjects info")
            #             return
            #         so = model_scale(info, prediction.transcript)
            #         self.playground.modify_game_object_scale(so)
            # else:
                # if self.playground:
                #     assert self.custom_agent is not None
                #     tool_called = self.custom_agent.run(prediction.transcript)
                #     if tool_called:
                #         logger.debug("Tool called.")

            if self.playground:
                if self.playground.is_connected:
                    self.playground.show_user_input_text(prediction.transcript)
            if self.obs:
                self.obs.subtitle(prediction.transcript, which="user")

        @emitter.on(EventKeyRegistry.LiveStream.DANMAKU)
        def on_danmaku(event: LiveStreamDanmakuEvent):
            text = f"你收到了一条弹幕，用户“{event.danmaku.username}”说：\n{event.danmaku.content}"
            if self.enable_turn_taking:
                # 弹幕也走轮次锁：AI 忙时缓存到下一轮，避免和语音回复互相打架。
                self._dispatch_user_utterance(text)
            else:
                self.emit_llm_prediction(text)

        # @emitter.on(EventKeyRegistry.System.SECOND)
        # async def on_second_danmaku_check(event: SecondEvent):
        #     # Try select danmaku every 5 seconds.
        #     if event.elapsed % 5 == 0:
        #         danmaku = await self.bilibili.select_max_long_one()
        #         if danmaku:
        #             logger.info(f"Selected danmaku: [{danmaku.username}] {danmaku.content}")
        #             text = f"你收到了一条弹幕，用户“{danmaku.username}”说：\n{danmaku.content}"
        #             self.emit_llm_prediction(text)

        @emitter.on(EventKeyRegistry.Device.SCREEN_CAPTURED)
        def on_device_screen_captured(event: DeviceScreenCapturedEvent):
            img_path = event.img_path
            if isinstance(event.img_path, Path):
                img_path = str(event.img_path)

            ocr_prediction = self.ocr.predict(OCRQuery(img_path=img_path))
            # TODO: 0.6 is a hyperparameter that indicates the average confidence of the text contained in the image.
            if avg_confidence(ocr_prediction) > 0.6:
                logger.info("OCR: " + stringify(ocr_prediction.region_results))
                emitter.emit(PipelineOCREvent(prediction=ocr_prediction))
            else:
                img_cap_prediction = self.img_cap.predict(ImgCapQuery(prompt="There", img_path=img_path))
                src_lang = Language.value_of(img_cap_prediction.lang)
                caption = translate(src_lang, self.cur_lang, img_cap_prediction.caption)
                img_cap_prediction.caption = caption
                logger.info("ImgCap: " + caption)
                emitter.emit(PipelineImgCapEvent(prediction=img_cap_prediction))

        def predict_image_modal(images: List[Path]):
            results = []
            for image in images:
                if image.exists():
                    ocr_prediction = self.ocr.predict(OCRQuery(img_path=str(image)))
                    ocr_text = stringify(ocr_prediction.region_results)
                    img_cap_prediction = self.img_cap.predict(ImgCapQuery(prompt="There", img_path=str(image)))
                    img_cap_text = img_cap_prediction.caption
                    results.append({
                        "ocr": ocr_text,
                        "sentiment": img_cap_text
                    })
            return results

        @emitter.on(EventKeyRegistry.QQBot.QQ_MESSAGE)
        def on_qq_message(event: QQMessageEvent):
            if "语音" in event.message:
                prediction = self.emit_llm_prediction(event.message, direct_return=True)
                if prediction is None:
                    logger.warning("No response from LLM remote service and will not send QQ message.")
                    return
                tts_prompt = self.tts_prompt_manager.default_tts_prompt
                query = TTSQuery(
                    text=prediction.response,
                    text_language="auto",
                    refer_wav_path=tts_prompt.audio_path,
                    prompt_text=tts_prompt.prompt_text,
                    prompt_language=tts_prompt.lang,
                    audio_type="wav"
                )
                prediction = self.tts.predict(query=query)
                file_path = save_audio(prediction.wave_data, prefix="tts")
                self.qq.send_speech(event.group_id, str(file_path))
            elif event.images is not None and len(event.images) > 0:
                result = predict_image_modal(event.images)
                query_text = "你看见群友给你发了张图片，内容是：" + str(result)
                logger.info(f"OCR + ImgCap: {result}")
                prediction = self.emit_llm_prediction(query_text, direct_return=True)
                if prediction is None:
                    logger.warning("No response from LLM remote service and will not send QQ message.")
                    return
                self.qq.send_plain_message(group_id=event.group_id, receiver_id=event.sender_id,
                                           text=prediction.response)
            else:
                prediction = self.emit_llm_prediction(event.message, direct_return=True)
                if prediction is None:
                    logger.warning("No response from LLM remote service and will not send QQ message.")
                    return
                self.qq.send_plain_message(group_id=event.group_id, receiver_id=event.sender_id,
                                           text=prediction.response)

        @emitter.on(EventKeyRegistry.Pipeline.OCR)
        def on_pipeline_ocr(event: PipelineOCREvent):
            prediction = event.prediction
            text = "你看见了" + stringify(prediction.region_results) + "\n请总结一下"
            self.emit_llm_prediction(text)

        @emitter.on(EventKeyRegistry.Pipeline.IMG_CAP)
        def on_pipeline_img_cap(event: PipelineImgCapEvent):
            prediction = event.prediction
            text = "你看见了" + prediction.caption
            self.emit_llm_prediction(text)

        @emitter.on(EventKeyRegistry.Pipeline.LLM)
        def llm_query_handler(event: PipelineOutputLLMEvent):
            # NOTE: This handler only runs on the BLOCKING (non-streaming) LLM path,
            # i.e. when the caller of `emit_llm_prediction` requested `direct_return`,
            # or when `enable_streaming_llm`/`enable_clause_split` is False.
            # The streaming voice path drives TTS directly inside
            # `_emit_llm_prediction_streaming` and never emits this event.
            prediction = event.prediction
            text = prediction.response
            logger.info("LLM: " + text)
            if self.enable_sentiment_analysis:
                sentiment = sentiment_analyse(sentiments=self.tts_prompt_manager.sentiments, text=text)
                tts_prompt = self.tts_prompt_manager.get_tts_prompt(sentiment)
            else:
                tts_prompt = self.tts_prompt_manager.default_tts_prompt
            if self.playground:
                self.playground.add_history(role="assistant", text=text, username=self.bot_name)

            if self.enable_split_by_punc:
                transcripts = split_by_punc(text, self.cur_lang)
                # Note that transcripts may be [] because we can not apply split in some cases.
                if len(transcripts) > 0:
                    for idx, transcript in enumerate(transcripts):
                        self._tts_without_block(tts_prompt, transcript)
            else:
                self._tts_without_block(tts_prompt, text)

        @emitter.on(EventKeyRegistry.System.CONFIG_FILE_MODIFIED)
        def on_config_modified(_: ConfigFileModifiedEvent):
            config = get_config()

        @emitter.on(EventKeyRegistry.Device.SPEAKER_PLAY)
        def on_speaker_play(event: DeviceSpeakerPlayEvent):
            if self.obs is not None:
                assert event.audio_path.exists()
                sample_rate, num_channels, duration = audio_util.get_audio_info(event.audio_path)
                text = self.subtitles_queue.get()
                self.obs.subtitle(text, which="assistant", duration=math_util.clamp(0, 5, duration - 1))

    def _tts_without_block(self, tts_prompt: TTSPrompt, text: str):
        # 在途 TTS 计数 +1：让 `is_ai_busy()` 在子句还没生成/播放完之前持续返回 True，
        # 这样轮次锁不会在机器人话还没说完时就放下一轮进来。
        self.conv_state.tts_submitted()

        def wrapper():
            # Runs inside a thread pool: any exception raised here is captured by the
            # Future and would otherwise be swallowed silently (no log, no crash). We
            # log it explicitly so TTS failures are always visible.
            try:
                # 被插话打断：丢弃本子句，不再合成/排播。
                if self._cancel_event.is_set():
                    logger.debug(f"TTS skipped (barge-in): {text!r}")
                    return
                query = TTSQuery(
                    text=text,
                    text_language="auto",
                    refer_wav_path=tts_prompt.audio_path,
                    prompt_text=tts_prompt.prompt_text,
                    prompt_language=tts_prompt.lang,
                    audio_type="wav"
                )
                prediction = self.tts.predict(query=query)
                logger.info(f"TTS: {query.text}")

                # 合成期间可能发生打断：合成完也别再排播。
                if self._cancel_event.is_set():
                    logger.debug(f"TTS discarded after synth (barge-in): {text!r}")
                    return
                self.play_tts(PipelineOutputTTSEvent(prediction=prediction, transcript=text))
            except Exception:
                logger.exception(f"TTS failed for text: {text!r}")
            finally:
                # 子句已派发到本地扬声器队列（或已失败）。此时再 -1；剩余的播放时长由
                # ConversationState 的扬声器探针（speaker.is_busy）继续覆盖。
                self.conv_state.tts_done()

        # To sync audio playing and subtitle
        self.tts_thread_pool.submit(wrapper)

    def exp_memory(self, text: str, is_filtered: bool, response: str, len_history: int):

        l_max = get_config().character.chat.max_history
        try:
            s = sentiment_score(text)
        except Exception as e:
            logger.exception(e)
            s = 1

        if not is_filtered:
            b = 0
        else:
            b = self.filter.match(response)

        try:
            r = memory_score(response)
        except Exception as e:
            logger.exception(e)
            r = 1
        t_memory = 0.3 * (l_max - len_history) / l_max + 0.2 * s + 0.2 * b + 0.1 * r
        return t_memory > 0.5

    def _history_for_query(self):
        """构造发给 LLM 的历史：用分层装配器在干净历史上重写 system 消息（人设+长远计划+记忆+状态）。

        工作窗口两段式：把"温区还没覆盖到的尾部轮次"逐字发给 LLM（带时间戳，至少 hot_low 条、随
        折叠水位在 [hot_low, hot_high] 间浮动），更早的在窗口内的轮次改由"近期回顾(温区轻摘要)"层
        表示，以缩短 prompt、抑制幻觉。逐字段始终衔接温区覆盖点，保证两层无缝、不漏轮次。完整窗口
        仍留在 current_history 与磁盘上，本处只影响"发出去"的内容。返回拷贝，持久化历史不受影响。
        """
        hist = self.llm_prompt_manager.current_history
        base = len(self.llm_prompt_manager.injected_history)
        prefix, live = list(hist[:base]), list(hist[base:])
        low = self._mem_cfg.hot_window_size
        if self._mem_cfg.enable and low > 0 and len(live) > low:
            # 逐字段必须覆盖"温区还没盖到的全部尾部轮次"，否则刚滑出热区、还没并进温区的
            # 那几条会两头落空。keep = max(low, 未被温区覆盖的轮次数)，保证热区↔温区无缝。
            covered = min(max(0, self.memory.digest_covered_count()), len(live))
            keep = max(low, len(live) - covered)
            if len(live) > keep:
                live = live[-keep:]
        # 把"已说出口但还没被应答"的主动发言作为上下文带上，让 AI 应答时知道自己刚说了什么。
        # 它此时还没进 current_history（要等用户真的回复才落历史），所以只在这份发出去的拷贝里补上。
        if self._pending_proactive is not None:
            live = live + [self._pending_proactive]
        return self.prompt_composer.build_query_history(prefix + live)

    def _section_recent_digest(self) -> str:
        """温区：窗口内较早轮次的"近期回顾"（轻压缩，去时间戳/冗余）。由后台线程维护。"""
        if not self._mem_cfg.enable:
            return ""
        text = (self.memory.get_recent_digest() or "").strip()
        if not text:
            return ""
        return "# 最近聊天回顾\n" + text

    def _section_directives(self) -> str:
        """L2a：固定长远计划（config 自由文本，不可变）+ 可自我编辑的长期目标/todolist（self_state）。"""
        blocks = []
        fixed = (_config.character.chat.long_term_directives or "").strip()
        if fixed:
            blocks.append("# 固定设定与长远计划\n" + fixed)
        rendered = self.self_state.render()
        if rendered:
            blocks.append(rendered)
        return "\n\n".join(blocks)

    def _section_tool_instructions(self) -> str:
        """告知 LLM 如何用 <self_update> 工具自我编辑长期目标 / todolist。"""
        if not self._mem_cfg.enable_self_edit:
            return ""
        return (
            "# 自我管理工具\n"
            "你可以维护自己的长期目标和待办清单。当确实需要新增/完成/删除目标或待办时，"
            "在你这次回复的【最后】单独追加一行：\n"
            '<self_update>{"add_goal": "...", "add_todo": "...", "done_todo": "...", '
            '"remove_goal": "...", "remove_todo": "..."}</self_update>\n'
            "各字段都可选、可只给需要的，值可以是字符串或字符串数组。没有变更时就不要输出这一行。"
            "这一行不会被读出来，也不要在朗读内容里提及它或它的格式。"
        )

    def _section_long_term_memory(self) -> str:
        """L2b 长期记忆：按当前输入从向量库检索 top-k 的耐久记忆，拼进 prompt。失败静默降级为空。"""
        if not self._mem_cfg.enable or self.vec_db is None:
            return ""
        q = (self._current_user_text or "").strip()
        if not q:
            return ""
        try:
            query = MilvusQuery(collection_name=self._mem_cfg.collection_name,
                                limit=self._mem_cfg.retrieve_top_k,
                                output_fields=['text'],
                                query=q)
            result = self.vec_db.search(query)
            texts = []
            hits_dbg = []  # 供监控：命中文本 + 距离分数（越小越相似）
            for hits in (result.result or []):
                for hit in hits:
                    t = None
                    entity = getattr(hit, "entity", None)
                    if isinstance(entity, dict):
                        t = entity.get("text")
                    elif entity is not None:
                        t = getattr(entity, "text", None)
                    if t and str(t).strip():
                        texts.append(str(t))
                        hits_dbg.append({"text": str(t), "distance": getattr(hit, "distance", None)})
            self._long_term_query = q
            self._long_term_hits = hits_dbg
            if not texts:
                self._long_term_memory = ""
                return ""
            self._long_term_memory = "\n".join(texts)  # 供 WebUI 展示上一次检索结果
            return "# 关于对方你记得的事\n" + "\n".join(f"- {t}" for t in texts)
        except Exception as e:
            logger.debug(f"Long-term memory retrieval skipped: {e}")
            return ""

    def _vec_db_count(self) -> int:
        """向量库当前记录条数（监控用）。后端无 count 能力或出错时返回 -1。"""
        if self.vec_db is None:
            return -1
        try:
            counter = getattr(self.vec_db, "count", None)
            if callable(counter):
                return counter(self._mem_cfg.collection_name)
        except Exception as e:
            logger.debug(f"vec_db count skipped: {e}")
        return -1

    def _ingest_line_to_vecdb(self, turn) -> None:
        """把"被滑出整个工作窗口的单条原文"原样写入向量库（长期记忆 L2b，MemoryManager 驱逐回调）。
        保留 谁说的 + 日期 + 原话，便于精确语义召回；带 None 与异常保护。
        """
        if self.vec_db is None or turn is None:
            return
        content = (getattr(turn, "content", "") or "").strip()
        if not content:
            return
        try:
            role = getattr(turn.role, "value", None) or str(turn.role)
            who = "哥哥" if role == "user" else "你"
            ts = (getattr(turn, "metadata", None) or "").strip()
            day = ts.split(" ")[0] if ts else ""  # 只留日期，去掉具体时刻
            text = f"{day} {who}：{content}".strip()
            self._vec_seq = getattr(self, "_vec_seq", 0) + 1
            row = InsertRow(id=int(time.time() * 1000) * 1000 + (self._vec_seq % 1000),
                            text=text, subject="history")
            insert = MilvusInsert(collection_name=self._mem_cfg.collection_name, texts=[row])
            self.vec_db.insert(insert)
            logger.debug(f"Long-term line inserted into '{self._mem_cfg.collection_name}': {text[:40]}")
        except Exception as e:
            logger.warning(f"Failed to insert long-term line: {e}")

    def _section_user_impression(self) -> str:
        """对用户的印象：由后台线程每隔若干轮用 LLM 凝练，作为顶层人设的一块拼进 prompt。"""
        text = (self._user_impression or "").strip()
        if not text:
            return ""
        return "# 你对哥哥的印象\n" + text

    def _persist_memory(self) -> None:
        """把工作窗口对话 + 会话摘要 + 对用户的印象 + 计数器交给 MemoryStore 异步落盘。"""
        try:
            self.memory_store.request_save(MemoryState(
                turns=self.llm_prompt_manager.live_turns(),
                recent_digest=self.memory.get_recent_digest(),
                user_impression=self._user_impression,
                turn_counter=self._turn_counter,
                last_impression_at=self._last_impression_at,
            ))
        except Exception as e:
            logger.debug(f"Persist memory skipped: {e}")

    def _on_turn_committed(self, num_new: int) -> None:
        """每次提交对话后累加计数器；满 impression_interval 条就后台触发"对用户的印象"更新。"""
        self._turn_counter += max(0, int(num_new))
        # 即时唤醒温区折叠：逐字未压缩轮次一旦越过高水位就尽快折叠，无需等安全周期。
        self.memory.notify()
        if (self._mem_cfg.enable and self._impression_interval > 0
                and not self._impression_updating
                and self._turn_counter - self._last_impression_at >= self._impression_interval):
            self._last_impression_at = self._turn_counter  # 立即推进，避免后台运行期间重复触发
            self._spawn_impression_update()

    def _spawn_impression_update(self) -> None:
        """后台线程：用最近若干轮对话 + 会话摘要 + 长期记忆，请 LLM 更新对用户的印象并落盘。"""
        self._impression_updating = True
        recent = list(self.llm_prompt_manager.live_turns())[-self._impression_interval:]
        summary = self.memory.get_recent_digest()  # 已无独立会话摘要，用温区回顾作为补充上下文
        long_term = self._long_term_memory
        prior = self._user_impression

        persona = self.llm_prompt_manager.system_prompt

        def work():
            try:
                new_imp = update_user_impression(prior, recent, summary, long_term, persona)
                if new_imp and new_imp.strip():
                    self._user_impression = new_imp.strip()
                    logger.info(f"User impression updated ({len(self._user_impression)} chars).")
                    self._persist_memory()
                    self._publish_memory_snapshot()
            except Exception as e:
                logger.warning(f"Update user impression failed: {e}")
            finally:
                self._impression_updating = False

        KillableThread(target=work, daemon=True, name="ImpressionUpdater").start()

    def _publish_memory_snapshot(self) -> None:
        """把当前记忆状态发布给 Brain WebUI（/brain/memory）。仅在面板开启时执行；全程吞异常。"""
        if not (_config.system.brain.enable and _config.system.brain.enable_dashboard):
            return
        try:
            from framework.memory import monitor as memory_monitor
            history = self.llm_prompt_manager.current_history
            turns = []
            for c in history[-14:]:
                role = getattr(c.role, "value", None) or str(c.role)
                turns.append({"role": role, "content": c.content, "ts": c.metadata})
            memory_monitor.publish({
                "recent_digest": self.memory.get_recent_digest(),
                "user_impression": self._user_impression,
                "long_term": self._long_term_memory,
                "vec_count": self._vec_db_count(),
                "vec_query": self._long_term_query,
                "vec_hits": self._long_term_hits,
                "goals": list(self.self_state.goals),
                "todolist": list(self.self_state.todolist),
                "working_window": turns,
                "history_len": len(history),
                "turn_counter": self._turn_counter,
            })
        except Exception as e:
            logger.debug(f"Publish memory snapshot skipped: {e}")

    @staticmethod
    def _cn_weekday(t=None) -> str:
        """中文星期（周一~周日）。"""
        return "周" + "一二三四五六日"[(t or time.localtime()).tm_wday]

    def _section_current_time(self) -> str:
        """当前时间：让 AI 感知"现在几点、星期几"。每次构建查询时刷新。年份暂不展示。"""
        if not self._mem_cfg.inject_timestamp:
            return ""
        t = time.localtime()
        return "# 当前时间\n" + time.strftime(f"%m-%d {self._cn_weekday(t)} %H:%M:%S", t)

    @staticmethod
    def _now_ts() -> str:
        """轮次时间戳（紧凑本地时间，含星期），写进 Conversation.metadata。"""
        t = time.localtime()
        return time.strftime(f"%m-%d {ZerolanLiveRobot._cn_weekday(t)} %H:%M", t)

    def _section_state(self) -> str:
        """L3a 当前状态：大脑的瞬态"内部感受"（心情/模式）。"""
        if self.brain is None or not _config.system.brain.inject_state_to_prompt:
            return ""
        return self.brain.state_prompt() or ""

    def _build_brain(self) -> None:
        """构建大脑：装配感知层（现有硬件传感器）、多内驱系统与决策循环。"""
        bcfg = _config.system.brain

        perception = Perception()
        # 现有硬件传感器
        perception.add(MicEnergySensor(energy_probe=self.mic.current_energy))
        perception.add(VadSensor(speaking_probe=self.mic.is_user_speaking))
        if self.keyboard is not None:
            perception.add(KeyboardActivitySensor(
                count_probe=lambda: self.keyboard.recent_keystroke_count(bcfg.keyboard_window_s),
                busy_count=bcfg.keyboard_busy_count,
            ))
        else:
            # 无键盘（如 headless）：通道占位但离线，自动感知会退回 normal 档。
            perception.add(KeyboardActivitySensor(count_probe=lambda: None,
                                                  busy_count=bcfg.keyboard_busy_count,
                                                  enabled=False))
        perception.add(TimeSilenceSensor(idle_probe=self.conv_state.idle_seconds,
                                         full_silence_s=bcfg.silence_full_s))
        # 预留接口：摄像头 / 记忆（现在离线，Phase 3 / 接摄像头时替换 probe 即可）
        perception.add(CameraMotionSensor(enabled=False))
        perception.add(MemoryUrgeSensor(enabled=False))

        self.brain = Brain(
            conv_state=self.conv_state,
            perception=perception,
            drive_system=DriveSystem(),
            reactive_cb=self._brain_reactive,
            proactive_cb=self._brain_proactive,
            mode=bcfg.mode,
            tick_interval=bcfg.tick_interval,
            auto_focus_kb_hi=bcfg.auto_focus_kb_hi,
            auto_companion_kb_lo=bcfg.auto_companion_kb_lo,
            auto_companion_silence=bcfg.auto_companion_silence,
            hysteresis_ticks=bcfg.hysteresis_ticks,
            enable_proactive=bcfg.enable_proactive,
        )
        logger.info(f"🧠 Brain initialized (mode={bcfg.mode}, proactive={bcfg.enable_proactive}).")

    def _brain_reactive(self) -> bool:
        """大脑回调：AI 空闲时把缓存的用户输入合并补发一轮。返回是否真的派发。"""
        with self._turn_lock:
            if self.conv_state.is_ai_busy():
                return False
            merged = self.conv_state.drain_pending()
            if not merged:
                return False
            self.conv_state.begin_thinking()
        logger.info(f"Brain reactive: flush buffered utterance(s): {merged}")
        self._turn_executor.submit(self._run_turn, merged)
        return True

    def _brain_proactive(self) -> bool:
        """大脑回调：主动开口。返回是否真的派发。"""
        with self._turn_lock:
            if self.conv_state.is_ai_busy():
                return False
            self.conv_state.begin_thinking()
        logger.info("Brain proactive: initiating conversation.")
        self._turn_executor.submit(self._run_proactive_turn)
        return True

    def _run_proactive_turn(self) -> None:
        """执行一轮主动开口。用 proactive prompt 生成，但不把该提示写进对话历史。"""
        try:
            self._emit_llm_prediction_streaming(self._proactive_prompt, persist_user=False)
        except Exception as e:
            logger.exception(e)
        finally:
            self.conv_state.end_thinking()

    def _flush_pending_proactive(self, new_history: list) -> int:
        """若有"未被应答的主动发言"，在用户回复入历史前先把它补进去。返回补入的条数（0/1）。

        只保留最近一条 pending：若用户始终不回复，新的主动发言会覆盖旧的，旧的被丢弃，
        从而避免短期历史被无人应答的自言自语填满。
        """
        pending = self._pending_proactive
        self._pending_proactive = None
        if pending is None:
            return 0
        new_history.append(pending)
        return 1

    def _try_switch_mode_by_voice(self, text: str) -> bool:
        """语音模式命令拦截。命中则切换模式并返回 True（该句不再送 LLM）。"""
        if self.brain is None or not _config.system.brain.enable_voice_mode_command:
            return False
        t = text.strip()
        mapping = {
            "专注模式": "focus", "进入专注模式": "focus", "安静一点": "focus", "安静一会儿": "focus",
            "普通模式": "normal", "正常模式": "normal",
            "陪我聊天": "companion", "陪聊模式": "companion", "陪伴模式": "companion", "活跃一点": "companion",
            "自动模式": "auto", "你自己看着办": "auto",
        }
        target = mapping.get(t)
        if target is None:
            return False
        self.brain.set_mode(target)
        self._announce_mode(target)
        return True

    def _announce_mode(self, mode: str) -> None:
        if not _config.system.brain.announce_mode_switch:
            return
        lines = {
            "focus": "好的，我先安静会儿，你忙完叫我。",
            "normal": "好，恢复普通模式啦。",
            "companion": "好耶，那我多陪你聊聊天~",
            "auto": "好的，我自己看情况啦。",
        }
        line = lines.get(mode)
        if not line or self.tts_prompt_manager is None:
            return
        try:
            self._tts_without_block(self.tts_prompt_manager.default_tts_prompt, line)
        except Exception as e:
            logger.exception(e)

    def _on_barge_in(self) -> None:
        """插话硬打断：用户在机器人说话时开口，立即停播 + 清空 TTS 队列 + 取消在途 LLM/TTS 生成。

        由麦克风线程在全双工播放期间检测到用户连续语音时回调。被打断的这一轮 AI 回复直接丢弃，
        不写入历史；用户这句插话随后由 VAD→ASR 正常识别，并作为新的一轮立刻应答。
        """
        if not self.conv_state.is_ai_busy():
            # 机器人其实没在说话/思考，无需打断（避免误触发）。
            return
        logger.info("⛔ Barge-in: interrupting current speech.")
        # 1) 置取消令牌：让流式 LLM 循环与 TTS 派发尽快停下、不再排新音频。
        self._cancel_event.set()
        # 2) 停掉本地扬声器当前播放并清空待播队列。
        try:
            self.speaker.stop_now()
        except Exception as e:
            logger.exception(e)
        # 3) 丢弃可能与已清空音频错位的字幕条目，避免后续字幕与语音对不上。
        try:
            while not self.subtitles_queue.empty():
                self.subtitles_queue.get_nowait()
        except Exception:
            pass
        # 4) 复位忙碌状态：在途 TTS 计数清零、结束思考态，让这句插话能作为新一轮立即应答。
        self.conv_state.reset_tts()
        self.conv_state.end_thinking()

    def _dispatch_user_utterance(self, text: str) -> None:
        """轮次锁入口：决定"现在这句话能不能马上回复"。

        - AI 正忙（思考中 / 还有 TTS 在播）：把这句话缓存进 pending，等当前一轮说完后由
          `_turn_dispatch_loop` 合并补发，绝不并发开第二个 LLM。
        - AI 空闲：原子地占据本轮（begin_thinking），然后真正调用 LLM。
        """
        if not self.enable_turn_taking:
            self.emit_llm_prediction(text)
            return

        text = (text or "").strip()
        if not text:
            return

        # 语音模式命令拦截（"专注模式""陪我聊天"等），命中则不送 LLM。
        if self._try_switch_mode_by_voice(text):
            return

        with self._turn_lock:
            if self.conv_state.is_ai_busy():
                bd = self.conv_state.busy_breakdown()
                logger.info(
                    f"AI busy, buffer utterance for next turn: {text} "
                    f"(thinking={bd['thinking']}, inflight_tts={bd['inflight_tts']}, "
                    f"speaker_busy={bd['speaker_busy']})"
                )
                self.conv_state.push_pending(text)
                return
            # 占据本轮：在锁内置 thinking，确保并发到来的其它输入会看到"忙"。
            self.conv_state.begin_thinking()

        self._run_turn(text)

    def _run_turn(self, text: str) -> None:
        """真正执行一轮回复。调用方必须已经通过 `begin_thinking` 占据了本轮。"""
        try:
            self.emit_llm_prediction(text)
        except Exception as e:
            logger.exception(e)
        finally:
            # LLM 生成结束。注意此时 TTS 可能仍在生成/播放：is_ai_busy 会因为在途 TTS 计数
            # 和扬声器探针继续为真，pending 输入会等到真正播完后才在调度循环里补发。
            self.conv_state.end_thinking()

    def _turn_dispatch_loop(self) -> None:
        """轮次调度循环（Phase 2 决策循环的雏形）。

        每 ~150ms 看一眼：AI 是否已经彻底空闲？若空闲且有缓存输入，则合并补发一轮。
        """
        while self._timer_flag:
            time.sleep(0.15)
            try:
                if self.conv_state.is_ai_busy():
                    continue
                if not self.conv_state.has_pending():
                    continue
                with self._turn_lock:
                    # 双重检查：拿到锁后再确认仍然空闲且仍有 pending。
                    if self.conv_state.is_ai_busy():
                        continue
                    merged = self.conv_state.drain_pending()
                    if not merged:
                        continue
                    self.conv_state.begin_thinking()
                logger.info(f"Turn dispatch: flush buffered utterance(s): {merged}")
                self._run_turn(merged)
            except Exception as e:
                logger.exception(e)

    def emit_llm_prediction(self, text, direct_return: bool = False) -> None | LLMPrediction:
        logger.debug("`emit_llm_prediction` called")
        # 新一轮开始：清除上一轮可能残留的打断取消令牌。
        if not direct_return:
            self._cancel_event.clear()

        # Streaming voice path: stream LLM tokens, dispatch each clause to TTS as soon as
        # a punctuation mark is seen. This drastically lowers the time-to-first-speech
        # (typically from ~16s down to ~5s on cloud LLMs). Only used when:
        #   - the caller does NOT need the whole prediction back (no `direct_return`)
        #   - clause-level split is enabled (otherwise streaming buys nothing)
        #   - the streaming switch is enabled in config
        if (not direct_return) and self.enable_split_by_punc and self.enable_streaming_llm:
            return self._emit_llm_prediction_streaming(text)

        # Go through the layered composer (persona + memory + time + state) on a transient copy.
        self._current_user_text = text
        query = LLMQuery(text=text, history=self._history_for_query())
        prediction = self.llm.predict(query)

        # Filter applied here
        is_filtered = self.filter.filter(prediction.response)

        if is_filtered:
            logger.warning(f"LLM (Filtered): {prediction.response}")
            return None

        # Parse & apply any <self_update> tool calls; use the cleaned text for speaking/persistence.
        prediction.response = self._apply_self_updates(prediction.response)

        # Remove \n start
        if prediction.response and prediction.response[0] == '\n':
            prediction.response = prediction.response[1:]

        # 模型偶尔无视提示在开头加时间戳，剥掉再朗读/持久化。
        prediction.response = self._strip_leading_ts(prediction.response)

        logger.info(f"Length of current history: {len(self.llm_prompt_manager.current_history)}")

        # Persist cleanly from the canonical current_history (NOT prediction.history, which is the
        # transient composed copy with injected sections/timestamps), so layered content never
        # leaks into the persistent history.
        ts = self._now_ts() if self._mem_cfg.inject_timestamp else None
        new_history = list(self.llm_prompt_manager.current_history)
        extra = self._flush_pending_proactive(new_history)  # 把上次未应答的主动发言补到用户回复之前
        new_history.append(Conversation(role=RoleEnum.user, content=text, metadata=ts))
        new_history.append(Conversation(role=RoleEnum.assistant, content=prediction.response, metadata=ts))

        committed = False
        if self.enable_exp_memory:
            if self.exp_memory(text, is_filtered, prediction.response, len(prediction.response)):
                self.llm_prompt_manager.reset_history(new_history)
                committed = True
        else:
            # If experiment memory disabled, history should be updated for each chat commit.
            self.llm_prompt_manager.reset_history(new_history)
            committed = True

        if committed:
            self._on_turn_committed(2 + extra)  # (主动发言?) + user + assistant
        self._publish_memory_snapshot()
        self._persist_memory()

        if not direct_return:
            # 被插话打断：放弃这次（非流式）回复的朗读派发。
            if self._cancel_event.is_set():
                logger.info("Blocking LLM reply aborted by barge-in.")
                return None
            emitter.emit(PipelineOutputLLMEvent(prediction=prediction))
            logger.debug("LLMEvent emitted.")
        return prediction

    @staticmethod
    def _speakable_split(buf: str) -> tuple[str, str, bool]:
        """把流式缓冲拆成（可朗读, 暂留, 是否进入工具区）。

        - 若出现完整的 <self_update> 开标记：可朗读=标记前文本，暂留=""，进入工具区=True（其后一律不读）。
        - 否则把结尾处"可能是标记前缀"的一小段暂留，避免读出半个 "<self_up"。
        """
        i = buf.find(MARKER_OPEN)
        if i != -1:
            return buf[:i], "", True
        maxk = min(len(buf), len(MARKER_OPEN) - 1)
        for k in range(maxk, 0, -1):
            if MARKER_OPEN.startswith(buf[-k:]):
                return buf[:-k], buf[-k:], False
        return buf, "", False

    def _dispatch_clauses(self, work: str, cut_punc: str, tts_prompt) -> tuple[str, bool]:
        """从 work 中按标点切出完整子句并派发 TTS，返回（无标点的剩余文本, 是否被过滤中止）。"""
        while True:
            cut_pos = -1
            for i, ch in enumerate(work):
                if ch in cut_punc:
                    cut_pos = i
                    break
            if cut_pos < 0:
                break
            clause = work[:cut_pos].strip().lstrip('\n')
            work = work[cut_pos + 1:]
            if not clause:
                continue
            if self.filter.filter(clause):
                logger.warning(f"LLM (Filtered clause, abort streaming): {clause}")
                return work, True
            if self.playground:
                self.playground.add_history(role="assistant", text=clause, username=self.bot_name)
            self._tts_without_block(tts_prompt, clause)
        return work, False

    # 模型偶尔无视提示在开头吐时间戳（如 [06-07 18:19]、(14:30)、2026-06-07 14:30、【14:30】）。
    # prompt 约束不够可靠，这里在代码层把"开头连续的时间戳标记"强行剥掉，避免被朗读/被存进历史
    # （存进去下一轮会再被加一层前缀，越滚越多）。
    _LEAD_TS_RE = re.compile(
        r'^\s*(?:'
        r'[\[(（【]\s*\d{1,4}[\d\s\-:：月日年/周星期一二三四五六]*\d\s*[\])）】]'  # 括号包裹：[06-07 周日 18:19] (14:30) 【14:30】
        r'|\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:\s+\d{1,2}[:：]\d{2}(?:[:：]\d{2})?)?'  # 2026-06-07 14:30:00
        r'|\d{1,2}[-/月]\d{1,2}[日]?\s+\d{1,2}[:：]\d{2}(?:[:：]\d{2})?'  # 06-07 14:30
        r')\s*'
    )

    @classmethod
    def _strip_leading_ts(cls, text: str) -> str:
        """剥掉字符串开头连续的时间戳标记（可能有多个）。无则原样返回。"""
        if not text:
            return text
        prev = None
        while prev != text:
            prev = text
            text = cls._LEAD_TS_RE.sub('', text, count=1)
        return text

    def _apply_self_updates(self, full_response: str) -> str:
        """解析回复中的 <self_update> 工具块并应用，返回剥离了标记的干净文本（用于朗读/持久化）。"""
        cleaned, updates = extract_and_strip(full_response)
        if updates and self._mem_cfg.enable_self_edit:
            for u in updates:
                if self.tool_registry.dispatch("update_self", u):
                    logger.info(f"Self-update applied: {u}")
        return cleaned

    def _emit_llm_prediction_streaming(self, text: str, persist_user: bool = True) -> None:
        """
        Streaming voice path. Dispatches TTS as soon as a clause boundary is seen so that
        speaking can start while the LLM is still generating later tokens.

        `persist_user=False` is used by proactive speech: the prompt handed to the LLM is a
        system-style nudge, so we generate from it but do NOT store it as a user turn in the
        chat history (only the assistant's spontaneous reply is persisted).

        Trade-offs vs. the blocking path:
          - The TTS prompt is ALWAYS the default one. Per-utterance sentiment-based
            prompt selection is intentionally skipped because it would gate the entire
            pipeline on an extra synchronous LLM call.
          - Filtering is applied per clause; if any clause matches a filter rule, the
            already-dispatched clauses are NOT recalled (they may already be playing),
            but no further clauses are queued and the chat history is NOT updated.
          - `PipelineOutputLLMEvent` is NOT emitted: the streaming path drives TTS
            directly to avoid the duplicate split/dispatch logic in `llm_query_handler`.
        """
        # 新一轮开始：清除上一轮可能残留的打断取消令牌。
        self._cancel_event.clear()
        self._current_user_text = text
        query = LLMQuery(text=text, history=self._history_for_query())
        logger.info(f"LLM send: {query}")
        tts_prompt = self.tts_prompt_manager.default_tts_prompt

        if self.cur_lang == Language.ZH:
            cut_punc = "，。！？"
        elif self.cur_lang == Language.JA:
            cut_punc = "、。！？"
        else:
            cut_punc = ",.!?"

        full_response = ""
        pending = ""        # 待朗读累积区（已剥离工具标记）
        in_tool = False     # 一旦进入 <self_update> 工具区，后续内容只累积不朗读
        first_token_logged = False
        aborted = False

        for delta in self.llm.stream_predict(query):
            # 被插话打断：立刻停止消费 LLM 流并放弃本轮（不写历史）。
            if self._cancel_event.is_set():
                logger.info("LLM streaming aborted by barge-in.")
                return None
            if not first_token_logged:
                logger.debug("LLM streaming: first delta received.")
                first_token_logged = True
            full_response += delta
            if in_tool:
                # 工具标记之后的内容（JSON 体/闭合标记）只进 full_response，绝不朗读。
                continue
            pending += delta
            pending = self._strip_leading_ts(pending)  # 开头若被模型加了时间戳，剥掉再朗读（锚定 ^，正文领头时为空操作）

            speakable, held, entered = self._speakable_split(pending)
            if entered:
                in_tool = True
            leftover, aborted = self._dispatch_clauses(speakable, cut_punc, tts_prompt)
            # 未切出的剩余 + 暂留的（半个）标记前缀，留到下一轮；进入工具区后 held 为空。
            pending = leftover + held

            if aborted:
                break

        if aborted:
            return None

        # 收尾前再查一次打断：若刚被插话打断，放弃尾句与历史提交。
        if self._cancel_event.is_set():
            logger.info("LLM streaming aborted by barge-in (before tail flush).")
            return None

        # Flush any trailing speakable text (strip a possible partial tool marker).
        tail, _, _ = self._speakable_split(pending)
        tail = tail.strip().lstrip('\n')
        if tail:
            if self.filter.filter(tail):
                logger.warning(f"LLM (Filtered tail): {tail}")
                return None
            if self.playground:
                self.playground.add_history(role="assistant", text=tail, username=self.bot_name)
            self._tts_without_block(tts_prompt, tail)

        # Parse & apply any <self_update> tool calls, and use the cleaned text for history.
        full_response = self._strip_leading_ts(self._apply_self_updates(full_response).lstrip('\n'))
        logger.info(f"LLM (stream): {full_response}")
        logger.info(f"Length of current history: {len(self.llm_prompt_manager.current_history)}")

        # Update chat history with the full response. The streaming generator does NOT
        # touch `query.history`, so we have to do it here.
        ts = self._now_ts() if self._mem_cfg.inject_timestamp else None

        # 主动开口：先不写进历史，仅暂存。等用户真的回复了，下一轮再连同用户回复一起补进去。
        if not persist_user:
            self._pending_proactive = Conversation(role=RoleEnum.assistant, content=full_response, metadata=ts)
            logger.debug("Proactive utterance held pending a user reply (not yet persisted).")
            self._publish_memory_snapshot()
            return None

        new_history = list(self.llm_prompt_manager.current_history)
        extra = self._flush_pending_proactive(new_history)  # 把上次未应答的主动发言补到用户回复之前
        new_history.append(Conversation(role=RoleEnum.user, content=text, metadata=ts))
        new_history.append(Conversation(role=RoleEnum.assistant, content=full_response, metadata=ts))

        committed = False
        if self.enable_exp_memory:
            if self.exp_memory(text, False, full_response, len(full_response)):
                self.llm_prompt_manager.reset_history(new_history)
                committed = True
        else:
            self.llm_prompt_manager.reset_history(new_history)
            committed = True

        if committed:
            self._on_turn_committed(2 + extra)
        self._publish_memory_snapshot()
        self._persist_memory()

        return None

    def change_lang(self, lang: Language):
        self.cur_lang = lang.name()
        if self.tts_prompt_manager is not None:
            self.tts_prompt_manager.set_lang(self.cur_lang)

    def check_img(self, img) -> bool:
        if is_image_uniform(img):
            logger.warning("Are you sure you capture the screen properly? The screen is black!")
            self.emit_llm_prediction("你忽然什么都看不见了！请向你的开发者求助！")
            return False
        return True

    def save_memory(self):
        start = len(self.llm_prompt_manager.injected_history)
        history = self.llm_prompt_manager.current_history[start:]
        ai_msg = summary_history(history)
        row = InsertRow(id=1, text=ai_msg.content, subject="history")
        insert = MilvusInsert(collection_name="history_collection", texts=[row])
        try:
            insert_res = self.vec_db.insert(insert)
            if insert_res.insert_count == 1:
                logger.info(f"Add a history memory: {row.text}")
            else:
                logger.warning(f"Failed to add a history memory.")
        except Exception as e:
            logger.warning("Milvus pipeline failed!")

    def play_tts(self, event: PipelineOutputTTSEvent):
        prediction = event.prediction
        text = event.transcript
        self.subtitles_queue.put(text)
        audio_path = save_audio(wave_data=prediction.wave_data, format=AudioFileType(prediction.audio_type),
                                prefix='tts')
        if self.live2d_viewer:
            self.live2d_viewer.sync_lip(audio_path)
        if self.playground:
            if self.playground.is_connected:
                self.playground.play_speech(bot_id=self.bot_id, audio_path=audio_path,
                                            transcript=text, bot_name=self.bot_name)
                logger.debug("Remote speaker enqueue speech data")
        else:
            # `playsound(audio_path, block=True)` will block the thread, use `enqueue_sound(audio_path)` instead
            self.speaker.enqueue_sound(audio_path)
            logger.debug("Local speaker enqueue speech data")
