import asyncio
import os
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
    memory_score
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
from manager.config_manager import get_config
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
        # 轮次执行器：让"大脑"决定开口后把这一轮的 LLM 放到后台跑，大脑循环可继续 tick
        # （感知/内驱/面板不被一轮长回复阻塞）。单线程，保证轮次串行。
        self._turn_executor = ThreadPoolExecutor(max_workers=1)

        # LLM 之前的"大脑"：感知 → 多内驱 → 决策（被动响应 / 主动开口）。
        self._proactive_prompt = _config.system.brain.proactive_prompt
        self.brain: Brain | None = None
        if self.enable_turn_taking and _config.system.brain.enable:
            self._build_brain()

        self.init()
        logger.info("🤖 Zerolan Live Robot: Initialized services successfully.")

    async def start(self):
        logger.info("🤖 Zerolan Live Robot: Running...")
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
        """构造发给 LLM 的历史：在干净历史基础上，瞬态拼入大脑的"内部状态"system 片段。

        返回的是一份拷贝；持久化历史（current_history）不受影响，所以状态描述不会堆进历史。
        """
        history = self.llm_prompt_manager.current_history
        if self.brain is None or not _config.system.brain.inject_state_to_prompt:
            return history
        suffix = self.brain.state_prompt()
        if not suffix:
            return history
        new = [Conversation(role=c.role, content=c.content) for c in history]
        if new and new[0].role == RoleEnum.system:
            new[0] = Conversation(role=RoleEnum.system,
                                  content=new[0].content + "\n\n" + suffix)
        else:
            new.insert(0, Conversation(role=RoleEnum.system, content=suffix))
        return new

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
                logger.info(f"AI busy, buffer utterance for next turn: {text}")
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

        # Streaming voice path: stream LLM tokens, dispatch each clause to TTS as soon as
        # a punctuation mark is seen. This drastically lowers the time-to-first-speech
        # (typically from ~16s down to ~5s on cloud LLMs). Only used when:
        #   - the caller does NOT need the whole prediction back (no `direct_return`)
        #   - clause-level split is enabled (otherwise streaming buys nothing)
        #   - the streaming switch is enabled in config
        if (not direct_return) and self.enable_split_by_punc and self.enable_streaming_llm:
            return self._emit_llm_prediction_streaming(text)

        query = LLMQuery(text=text, history=self.llm_prompt_manager.current_history)
        prediction = self.llm.predict(query)

        # Filter applied here
        is_filtered = self.filter.filter(prediction.response)

        if is_filtered:
            logger.warning(f"LLM (Filtered): {prediction.response}")
            return None

        # Remove \n start
        if prediction.response[0] == '\n':
            prediction.response = prediction.response[1:]

        logger.info(f"Length of current history: {len(self.llm_prompt_manager.current_history)}")

        if self.enable_exp_memory:
            if self.exp_memory(text, is_filtered, prediction.response, len(prediction.response)):
                self.llm_prompt_manager.reset_history(prediction.history)
        else:
            # If experiment memory disabled, history should be updated for each chat commit.
            self.llm_prompt_manager.reset_history(prediction.history)

        if not direct_return:
            emitter.emit(PipelineOutputLLMEvent(prediction=prediction))
            logger.debug("LLMEvent emitted.")
        return prediction

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
        query = LLMQuery(text=text, history=self._history_for_query())
        tts_prompt = self.tts_prompt_manager.default_tts_prompt

        if self.cur_lang == Language.ZH:
            cut_punc = "，。！？"
        elif self.cur_lang == Language.JA:
            cut_punc = "、。！？"
        else:
            cut_punc = ",.!?"

        full_response = ""
        buffer = ""
        first_token_logged = False
        aborted = False

        for delta in self.llm.stream_predict(query):
            if not first_token_logged:
                logger.debug("LLM streaming: first delta received.")
                first_token_logged = True
            full_response += delta
            buffer += delta

            while True:
                cut_pos = -1
                for i, ch in enumerate(buffer):
                    if ch in cut_punc:
                        cut_pos = i
                        break
                if cut_pos < 0:
                    break

                clause = buffer[:cut_pos].strip().lstrip('\n')
                buffer = buffer[cut_pos + 1:]
                if not clause:
                    continue

                if self.filter.filter(clause):
                    logger.warning(f"LLM (Filtered clause, abort streaming): {clause}")
                    aborted = True
                    break
                if self.playground:
                    self.playground.add_history(role="assistant", text=clause, username=self.bot_name)
                self._tts_without_block(tts_prompt, clause)

            if aborted:
                break

        if aborted:
            return None

        # Flush any trailing text without a closing punctuation mark.
        tail = buffer.strip().lstrip('\n')
        if tail:
            if self.filter.filter(tail):
                logger.warning(f"LLM (Filtered tail): {tail}")
                return None
            if self.playground:
                self.playground.add_history(role="assistant", text=tail, username=self.bot_name)
            self._tts_without_block(tts_prompt, tail)

        full_response = full_response.lstrip('\n')
        logger.info(f"LLM (stream): {full_response}")
        logger.info(f"Length of current history: {len(self.llm_prompt_manager.current_history)}")

        # Update chat history with the full response. The streaming generator does NOT
        # touch `query.history`, so we have to do it here.
        new_history = list(self.llm_prompt_manager.current_history)
        if persist_user:
            new_history.append(Conversation(role=RoleEnum.user, content=text))
        new_history.append(Conversation(role=RoleEnum.assistant, content=full_response))

        if self.enable_exp_memory:
            if self.exp_memory(text, False, full_response, len(full_response)):
                self.llm_prompt_manager.reset_history(new_history)
        else:
            self.llm_prompt_manager.reset_history(new_history)

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
