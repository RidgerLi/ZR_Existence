import io
import threading
import time
import wave
from typing import Callable, Optional

import numpy as np
import pyaudio
import webrtcvad
from loguru import logger

from common.concurrent.abs_runnable import ThreadRunnable
from common.io.file_type import AudioFileType
from devices.noise_gate import FrameFeatures, NoiseGate
from event.event_data import DeviceMicrophoneVADEvent
from event.event_emitter import emitter


class SmartMicrophone(ThreadRunnable):
    def __init__(self, enable_vad: bool = False, vad_mode=3, frame_duration=30,
                 silence_hangover_ms: int = 800, min_speech_ms: int = 1000,
                 playback_tail_ms: int = 400, energy_ref: float = 3000.0,
                 full_duplex: bool = False, echo_canceller=None,
                 playback_speech_prob: float = 0.7,
                 enable_barge_in: bool = False, barge_in_min_ms: int = 180,
                 noise_gate: Optional[NoiseGate] = None,
                 keyboard_probe: Optional[Callable[[], bool]] = None,
                 trailing_silence_ms: int = 300,
                 log_utterances: bool = True,
                 barge_in_gap_ms: int = 350,
                 barge_in_grace_ms: int = 300):
        """
        初始化智能麦克风类
        :param enable_vad: 是否启用 webrtcvad 进行语音端点检测。开启后讲话间隔超过
                           `silence_hangover_ms` 才会判定为本句结束并发送 ASR。
        :param vad_mode: Optionally, set its aggressiveness mode, which is an integer between 0 and 3.
                         0 is the least aggressive about filtering out non-speech, 3 is the most aggressive.
        :param frame_duration: A frame must be either 10, 20, or 30 ms in duration.
        :param silence_hangover_ms: 用户开始说话后，连续多少毫秒静音才认为这句话说完并发送 ASR，
                                    默认 800ms（实际由 config.system.vad_silence_hangover_ms 配置注入）。
                                    句中停顿短于该值不会结束本轮，调大可避免"话没说完就被抢话"。
        :param min_speech_ms: 一句话至少要有多少毫秒的语音帧才会被发出去，默认 1000ms
                              （由 config.system.vad_min_speech_ms 配置注入）。用来过滤 VAD 抖动产生的噪声小段。
        :param playback_tail_ms: 半双工回声抑制的"尾巴"时长。机器人通过扬声器播放完音频后，
                                 还要再额外抑制麦克风这么多毫秒，用来覆盖混响和音频缓冲的残留，
                                 避免机器人自己的尾音被 VAD 当作用户输入重新采集。
        :param full_duplex: 全双工模式。开启后机器人说话期间麦克风**不再丢帧**，而是用
                            `echo_canceller` 对每帧做回声消除后继续 VAD，从而能听到用户插话。
                            覆盖半双工的 playback gate。
        :param echo_canceller: `devices.aec.EchoCanceller` 实例（或 None）。仅在 `full_duplex`
                               且其 `available` 为真时生效；否则自动退回半双工丢帧。
        :param playback_speech_prob: 全双工下，机器人正在播放时，AEC 语音概率需达到该阈值
                                     (0~1) 才把该帧当作用户语音，用来拒绝消不干净的残余回声。
                                     设为 0 关闭该额外过滤。
        :param enable_barge_in: 是否启用插话打断。仅在全双工下有效：机器人播放期间检测到用户
                                连续说话达到 `barge_in_min_ms` 时，触发 `barge_in_cb` 回调（由
                                上层执行停播+取消生成的硬打断）。
        :param barge_in_min_ms: 触发打断所需的"播放期间连续语音"时长（毫秒）。越大越抗残余回声
                                误触发但打断越慢，越小打断越快但可能误切。
        :param noise_gate: `devices.noise_gate.NoiseGate` 实例（或 None）。提供能量门、句级频谱评分与
                           更严的插话帧判定；None 时退回仅 webrtcvad 的旧行为。
        :param keyboard_probe: 返回 bool 的探针：最近一段时间内是否发生过击键。为真时该帧直接判为非语音
                               （键盘声与击键时间高度相关，这是最准的一条抗噪证据）。
        :param trailing_silence_ms: 句尾保留多少毫秒静音写进 WAV。VAD 仍按完整 hangover 判句尾，
                                    这里只裁剪发给 ASR 的音频长度，减少长静音导致的 ASR 幻听。
        :param log_utterances: 是否每句话打印一行 `[VAD] ... -> ACCEPT/REJECT(原因)` 指标日志（调参用）。
        :param barge_in_gap_ms: 打断证据计数器的静音重置时长。原先沿用 1.2s hangover，导致整段播放里
                                稀疏的误判帧能一直累加；改短后必须"持续在说话"才能打断。
        :param barge_in_grace_ms: 每段播放开始后的免计时间，等回声消除器收敛，避免起始瞬态误触发打断。
        """
        super().__init__()
        self._enable_vad = enable_vad
        assert frame_duration in [10, 20, 30], f"A frame must be either 10, 20, or 30 ms in duration!"

        # Audio parameters
        self._format = pyaudio.paInt16
        self._channels = 1
        self._sample_rate = 16000
        self._chunk_size = int(self._sample_rate * frame_duration / 1000)  # Bytes
        self._frame_duration = frame_duration

        # 帧粒度的静音容忍 / 最低语音长度
        self._silence_hangover_frames = max(1, silence_hangover_ms // frame_duration)
        self._min_speech_frames = max(1, min_speech_ms // frame_duration)

        # Initialize microphone
        self._audio = pyaudio.PyAudio()
        self._vad = webrtcvad.Vad(vad_mode)
        self._stream = self._audio.open(format=self._format,
                                        channels=self._channels,
                                        rate=self._sample_rate,
                                        input=True,
                                        frames_per_buffer=self._chunk_size)

        self._audio_frames = []
        self._is_speaking = False
        # VAD 计数：当前句中的 speech 帧数 / 末尾连续静音帧数
        self._speech_frame_count = 0
        self._silence_frame_count = 0

        # 麦克风能量 EMA（供大脑感知层的 MicEnergySensor 轮询）。
        # 在非播放抑制帧上计算 RMS 并做指数滑动平均，捕捉"环境有动静"（含非人声）。
        self._energy_ref = max(1.0, energy_ref)
        self._energy_ema = 0.0
        self._energy_alpha = 0.25

        # self._pause_event = threading.Event()
        self._stop_flag = False

        # 初始默认麦克风 off
        if self._stream.is_active():
            self._stream.stop_stream()

        self._talk_enabled_event = threading.Event()
        self._talk_enabled_event.clear()

        # 外部环境可用的锁
        self._recording_lock = threading.Lock()

        # 半双工回声抑制（playback gate）：
        # 当机器人通过扬声器播放 TTS 音频时，麦克风会把这段音频重新采集进来，
        # VAD 会把它当成用户语音发给 ASR，从而出现"AI 截获自己输出的音频"的自循环。
        # 这里用一道门把播放期间（及结束后的尾巴时间）采集到的帧直接丢弃。
        self._playback_tail_s = max(0.0, playback_tail_ms / 1000.0)
        self._playback_lock = threading.Lock()
        # 当前正在播放的音频片段计数，> 0 表示正在播放
        self._playback_count = 0
        # 播放结束后允许恢复采集的单调时间戳（monotonic seconds）
        self._suppress_until = 0.0

        # 全双工回声消除（AEC）：开启后播放期间不丢帧，而是对每帧做回声消除后继续 VAD，
        # 使机器人能在自己说话时听见用户插话（barge-in 的采集基础）。
        self._aec = echo_canceller
        self._full_duplex = bool(full_duplex) and (self._aec is not None) and self._aec.available
        self._playback_speech_prob = max(0.0, min(1.0, playback_speech_prob))
        if full_duplex and not self._full_duplex:
            logger.warning("Full-duplex requested but echo canceller unavailable; "
                           "falling back to half-duplex echo suppression.")

        # 插话打断（barge-in）：全双工下机器人说话时，检测到用户连续说话即触发回调，
        # 由上层执行"停播 + 取消在途生成"的硬打断。回调由外部通过 set_barge_in_callback 注入。
        self._enable_barge_in = bool(enable_barge_in)
        # 触发打断所需的"累计语音帧"（短静音不清零，见 _detect_barge_in）。直接按配置时长换算。
        self._barge_in_min_frames = max(1, barge_in_min_ms // frame_duration)
        if self._enable_barge_in:
            logger.info(
                f"Barge-in min speech: {self._barge_in_min_frames} frames "
                f"(~{self._barge_in_min_frames * frame_duration}ms)."
            )
        self._barge_in_cb = None
        # 本次播放期内累计语音帧 / 末尾连续静音帧 / 是否已触发过（避免一次播放里重复触发）。
        # 与主 VAD 同口径：统计"语音帧总数"，短于 hangover 的静音不清零，只有持续静音超过
        # hangover 才认为这段插话结束并清零——否则 2000ms 阈值在真实说话的辅音/换气间隙下永远凑不满。
        self._pb_speech_frames = 0
        self._pb_silence_frames = 0
        self._barge_in_fired = False
        # 打断证据的新口径：短静音（默认 350ms）就清零；播放开始后有一小段免计时等 AEC 收敛。
        self._barge_gap_frames = max(1, int(barge_in_gap_ms) // frame_duration)
        self._barge_grace_s = max(0.0, float(barge_in_grace_ms) / 1000.0)
        self._playback_started_at = 0.0

        # 抗噪门（能量 / 频谱 / 键盘活动）：见 devices/noise_gate.py
        self._noise = noise_gate
        self._kb_probe = keyboard_probe
        self._trailing_silence_frames = max(0, int(trailing_silence_ms) // frame_duration)
        self._log_utterances = bool(log_utterances)
        self._last_rms = 0.0
        # 当前句子的统计：语音帧特征、总帧数、最后一个语音帧的位置（用于算"语音密度"）。
        self._utterance_feats: list[FrameFeatures] = []
        self._utterance_frames = 0
        self._last_speech_frame_at = 0

    @property
    def is_recording(self):
        # return self._pause_event.is_set() and (not self._stop_flag) and self._stream.is_active()
        return self._talk_enabled_event.is_set() and (not self._stop_flag) and self._stream.is_active()

    def start(self):
        super().start()
        # self._pause_event.set()
        self._stop_flag = False
        # 全双工：启动回声消除器（含 WASAPI 回采线程）。启动失败则自动退回半双工。
        if self._full_duplex:
            if not self._aec.start():
                self._full_duplex = False
        try:
            while not self._stop_flag:
                # self._pause_event.wait()
                self._stream_update()
                self._talk_enabled_event.wait()
                self._stream_update()
                
                if self._stop_flag:
                    break

                data = self._stream.read(self._chunk_size, exception_on_overflow=False)

                # 全双工：先做回声消除，把机器人自己的声音从这一帧里减掉，再交给 VAD。
                # （process 必须在采集线程调用——这里正是采集线程。）
                if self._full_duplex:
                    data = self._aec.process(data)

                # 锁防止 hotkey 线程强制释放时同时读取
                with self._recording_lock:
                    self._vad_record(data)

        except Exception as e:
            logger.exception(e)
        finally:
            if self._aec is not None:
                try:
                    self._aec.stop()
                except Exception as e:
                    logger.exception(e)
            # Stop and close the microphone stream
            self._stream.stop_stream()
            self._stream.close()
            self._audio.terminate()

    def _is_playback_suppressed(self) -> bool:
        with self._playback_lock:
            if self._playback_count > 0:
                return True
            return time.monotonic() < self._suppress_until

    def begin_playback(self):
        """扬声器开始播放本地音频时调用：立即关闭麦克风采集（半双工）。"""
        with self._playback_lock:
            self._playback_count += 1
            self._playback_started_at = time.monotonic()

    def end_playback(self):
        """扬声器播放完一段本地音频时调用：在尾巴时间过后再恢复采集。"""
        with self._playback_lock:
            if self._playback_count > 0:
                self._playback_count -= 1
            if self._playback_count == 0:
                self._suppress_until = time.monotonic() + self._playback_tail_s

    def set_barge_in_callback(self, cb):
        """注入插话打断回调：在全双工播放期间检测到用户连续说话时被调用（无参）。"""
        self._barge_in_cb = cb

    def _detect_barge_in(self, playback_active: bool, is_speech: bool):
        if not (self._enable_barge_in and self._full_duplex):
            return
        if not playback_active:
            # 没在播放：重置计数与触发标记，让下一次播放可以重新被打断。
            self._pb_speech_frames = 0
            self._pb_silence_frames = 0
            self._barge_in_fired = False
            return
        if time.monotonic() - self._playback_started_at < self._barge_grace_s:
            # 播放刚开始的一小段：回声消除器还在收敛，这期间的帧不计入打断证据。
            return
        if is_speech:
            self._pb_speech_frames += 1
            self._pb_silence_frames = 0
            if (not self._barge_in_fired) and self._pb_speech_frames >= self._barge_in_min_frames \
                    and self._barge_in_cb is not None:
                self._barge_in_fired = True
                logger.info(f"Barge-in detected ({self._pb_speech_frames} speech frames during playback).")
                try:
                    self._barge_in_cb()
                except Exception as e:
                    logger.exception(e)
        else:
            # 短静音（辅音/换气）不清零；超过 barge_in_gap_ms 就认为这段插话断了，重新计数。
            # 原先用 1.2s hangover 重置，导致整段播放里稀疏的误判帧能一直累加，最终凑成一次假打断。
            self._pb_silence_frames += 1
            if self._pb_silence_frames >= self._barge_gap_frames:
                self._pb_speech_frames = 0
                self._pb_silence_frames = 0

    def _vad_record(self, data: bytes):
        playback_active = self._is_playback_suppressed()

        # 半双工：播放期间（及尾巴时间内）采集到的都是机器人自己的声音，直接丢弃，
        # 并清空已经累积的片段，避免把自己的输出当成用户输入发给 ASR。
        # 全双工：`data` 已在采集循环里做过回声消除，不丢帧，照常走 VAD。
        if playback_active and not self._full_duplex:
            if self._is_speaking or self._audio_frames:
                self._reset_vad_state()
            return

        # 更新能量 EMA（半双工下仅在非抑制帧上；全双工下用的是已消回声的帧）。
        self._update_energy(data)
        rms = self._last_rms

        if not self._enable_vad:
            if not self._is_speaking:
                self._is_speaking = True
            self._audio_frames.append(data)
            return

        is_speech = self._vad.is_speech(data, self._sample_rate)

        # 全双工 + 正在播放：用 AEC 的语音概率再过一道门，拒绝消不干净的残余回声，
        # 避免机器人把自己的尾音当成用户输入。
        aec_prob = getattr(self._aec, "last_speech_prob", None) if self._full_duplex and self._aec else None
        if is_speech and playback_active and self._full_duplex \
                and self._playback_speech_prob > 0.0 and aec_prob is not None:
            if aec_prob < self._playback_speech_prob:
                is_speech = False

        # 抗噪门 A：键盘活动。最近有击键 → 这帧不可能是"用户在说话"。
        # 键盘声与击键时间高度相关，这是最准、最便宜的一条证据。
        if is_speech and self._kb_probe is not None:
            try:
                if self._kb_probe():
                    is_speech = False
            except Exception as e:
                logger.debug(f"Keyboard probe failed: {e}")

        # 抗噪门 B：能量。低于绝对静音门、或没比自适应底噪高出足够倍数 → 不是语音。
        # 特征每帧最多算一次；不需要频谱时 NoiseGate.features 只算 RMS。
        feats: FrameFeatures | None = None
        if self._noise is not None:
            if is_speech:
                feats = self._noise.features(data)
                if not self._noise.energy_ok(feats.rms):
                    is_speech = False
            elif not playback_active and not self._is_speaking:
                # 只在"VAD 判非语音 + 不在播放 + 不在句子中"时学习底噪，
                # 否则会把用户自己的声音学成底噪，门限越抬越高。
                self._noise.observe_noise(rms)

        # 插话打断：只有"确凿的人声帧"才计数（键盘/咳嗽/宽带噪声在这里被挡掉）。
        clean_for_barge_in = is_speech
        if is_speech and playback_active and self._noise is not None:
            feats = feats if feats is not None else self._noise.features(data)
            clean_for_barge_in = self._noise.barge_in_frame_ok(feats, speech_prob=aec_prob)
        self._detect_barge_in(playback_active, clean_for_barge_in)

        if is_speech:
            if not self._is_speaking:
                logger.info("Voice detected: Beginning.")
                self._is_speaking = True
                self._speech_frame_count = 0
            self._audio_frames.append(data)
            self._speech_frame_count += 1
            self._silence_frame_count = 0
            # 句子密度统计：记录语音帧数与最后一个语音帧的位置。
            self._utterance_frames += 1
            self._last_speech_frame_at = self._utterance_frames
            if self._noise is not None:
                self._utterance_feats.append(feats if feats is not None else self._noise.features(data))
        else:
            if self._is_speaking:
                self._utterance_frames += 1
                # 句尾静音只保留前 trailing_silence_ms：VAD 仍按完整 hangover 判句尾，
                # 但发给 ASR 的 WAV 不再拖着整整 1.2 秒静音（那是幻觉文本的常见来源）。
                if self._silence_frame_count < self._trailing_silence_frames:
                    self._audio_frames.append(data)
                self._silence_frame_count += 1
                if self._silence_frame_count >= self._silence_hangover_frames:
                    self._finish_utterance()
            # 还没开始讲话：直接丢掉静音帧

    def _finish_utterance(self):
        """一句话结束：先过最短时长与抗噪门，通过才发 ASR。"""
        n_speech = self._speech_frame_count
        logger.info(
            f"Voice detected: Ending. (speech={n_speech} frames, "
            f"silence_hangover={self._silence_frame_count} frames)"
        )

        accept, reason, stats = True, "ok", None
        if self._noise is not None:
            span = max(self._last_speech_frame_at, n_speech)
            accept, reason, stats = self._noise.score(self._utterance_feats, span)
        if n_speech < self._min_speech_frames:
            # 时长不够：即使频谱像人声也丢（阈值 = vad_min_speech_ms）。
            # 仍然算指标并打印，方便看清"到底差多少帧"。
            accept, reason = False, "too_short"

        if self._log_utterances:
            if stats is not None:
                verdict = "ACCEPT" if accept else f"REJECT({reason})"
                logger.info(f"[VAD] {stats.format()} -> {verdict}")
            elif not accept:
                logger.info(f"[VAD] speech={n_speech} frames "
                            f"(< {self._min_speech_frames}) -> REJECT({reason})")

        if accept:
            self._emit_event()
        self._reset_vad_state()

    def _update_energy(self, data: bytes):
        try:
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size == 0:
                return
            rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            self._last_rms = rms
            self._energy_ema = self._energy_alpha * rms + (1 - self._energy_alpha) * self._energy_ema
        except Exception as e:
            logger.exception(e)

    def current_energy(self):
        """供大脑感知层轮询：返回归一化 [0,1] 的麦克风能量；麦克风未采集时返回 None。"""
        if not self.is_recording:
            return None
        v = self._energy_ema / self._energy_ref
        if v < 0.0:
            return 0.0
        if v > 1.0:
            return 1.0
        return v

    def is_user_speaking(self):
        """供大脑感知层轮询：用户当前是否正在说话（VAD 判定中）。麦克风未采集时返回 None。"""
        if not self.is_recording:
            return None
        return self._is_speaking

    def _reset_vad_state(self):
        self._is_speaking = False
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._audio_frames = []
        self._utterance_feats = []
        self._utterance_frames = 0
        self._last_speech_frame_at = 0

    def _emit_event(self):
        if self._audio_frames:
            # 创建一个BytesIO对象来存储WAV文件
            file = io.BytesIO()
            wf = wave.open(file, 'wb')
            wf.setnchannels(self._channels)
            wf.setsampwidth(self._audio.get_sample_size(self._format))
            wf.setframerate(self._sample_rate)
            wf.writeframes(b''.join(self._audio_frames))
            wf.close()

            # 将BytesIO对象的指针移到开始位置
            file.seek(0)
            emitter.emit(DeviceMicrophoneVADEvent(
                speech=file.read(),
                audio_type=AudioFileType.WAV,
                channels=self._channels,
                sample_rate=self._sample_rate,
            ))

    def _stream_update(self):
        if self._talk_enabled_event.is_set():
            if not self._stream.is_active():
                self._stream.start_stream()
        else:
            if self._stream.is_active():
                self._stream.stop_stream()

    def pause(self):
        # self._pause_event.clear()
        self._talk_enabled_event.clear()
        logger.info("Paused smart microphone.")

    def resume(self):
        # self._pause_event.set()
        self._talk_enabled_event.set()
        logger.info("Resumed smart microphone.")

    def stop(self):
        self._stop_flag = True
        # self._pause_event.set()
        # self._talk_enabled_event.clear()
        # Fix: Set it to true to avoid the deadlock!
        self._talk_enabled_event.set()
        logger.info("Stopped smart microphone.")

    def name(self):
        return "SmartMicrophone"
    
    def is_set_talk_enabled_event(self):
        return self._talk_enabled_event.is_set()
    
    def set_talk_enabled_event(self):
        self._talk_enabled_event.set()

    def unset_talk_enabled_event(self):
        self._talk_enabled_event.clear()

    def force_commit(self, is_emit=False):
        with self._recording_lock:
            if self._is_speaking and self._audio_frames and is_emit:
                self._emit_event()
            self._reset_vad_state()

"""
# 备份代码，以免 self._vad 作用不佳，作用于 bot.py - on_service_vad_speech_chunk 函数中
# 检查音频数据是否超过最低响度阈值
# threshold: 响度阈值, 一般安静房间的 RMS 可能在 100 以下, 正常说话在 1000-5000 左右
import numpy as np
threshold: float = 2200.0
audio_array = np.frombuffer(speech, dtype=np.int16)
rms = np.sqrt(np.mean(audio_array.astype(np.float32)**2))
logger.info(f'Microphone Voice: {rms}.')
if rms < threshold:
    logger.debug(f'Microphone Voice too low to be accepted, already been filtered.')
    return
"""