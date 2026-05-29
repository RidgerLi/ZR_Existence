import io
import threading
import wave

import pyaudio
import webrtcvad
from loguru import logger

from common.concurrent.abs_runnable import ThreadRunnable
from common.io.file_type import AudioFileType
from event.event_data import DeviceMicrophoneVADEvent
from event.event_emitter import emitter


class SmartMicrophone(ThreadRunnable):
    def __init__(self, enable_vad: bool = False, vad_mode=3, frame_duration=30,
                 silence_hangover_ms: int = 700, min_speech_ms: int = 300):
        """
        初始化智能麦克风类
        :param enable_vad: 是否启用 webrtcvad 进行语音端点检测。开启后讲话间隔超过
                           `silence_hangover_ms` 才会判定为本句结束并发送 ASR。
        :param vad_mode: Optionally, set its aggressiveness mode, which is an integer between 0 and 3.
                         0 is the least aggressive about filtering out non-speech, 3 is the most aggressive.
        :param frame_duration: A frame must be either 10, 20, or 30 ms in duration.
        :param silence_hangover_ms: 连续多少毫秒静音后才认为一句话说完，默认 700ms。
        :param min_speech_ms: 一句话至少要有多少毫秒的语音帧才会被发出去，默认 300ms。
                              用来过滤 VAD 抖动产生的噪声小段。
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

        # self._pause_event = threading.Event()
        self._stop_flag = False

        # 初始默认麦克风 off
        if self._stream.is_active():
            self._stream.stop_stream()

        self._talk_enabled_event = threading.Event()
        self._talk_enabled_event.clear()

        # 外部环境可用的锁
        self._recording_lock = threading.Lock()

    @property
    def is_recording(self):
        # return self._pause_event.is_set() and (not self._stop_flag) and self._stream.is_active()
        return self._talk_enabled_event.is_set() and (not self._stop_flag) and self._stream.is_active()

    def start(self):
        super().start()
        # self._pause_event.set()
        self._stop_flag = False
        try:
            while not self._stop_flag:
                # self._pause_event.wait()
                self._stream_update()
                self._talk_enabled_event.wait()
                self._stream_update()
                
                if self._stop_flag:
                    break

                data = self._stream.read(self._chunk_size, exception_on_overflow=False)

                # 锁防止 hotkey 线程强制释放时同时读取
                with self._recording_lock:
                    self._vad_record(data)

        except Exception as e:
            logger.exception(e)
        finally:
            # Stop and close the microphone stream
            self._stream.stop_stream()
            self._stream.close()
            self._audio.terminate()

    def _vad_record(self, data: bytes):
        if self._enable_vad:
            is_speech = self._vad.is_speech(data, self._sample_rate)

            if is_speech:
                if not self._is_speaking:
                    logger.info("Voice detected: Beginning.")
                    self._is_speaking = True
                    self._speech_frame_count = 0
                self._audio_frames.append(data)
                self._speech_frame_count += 1
                self._silence_frame_count = 0
            else:
                if self._is_speaking:
                    # 已经在讲话中：把静音帧也保留进来当作 hangover
                    self._audio_frames.append(data)
                    self._silence_frame_count += 1
                    if self._silence_frame_count >= self._silence_hangover_frames:
                        logger.info(
                            f"Voice detected: Ending. (speech={self._speech_frame_count} frames, "
                            f"silence_hangover={self._silence_frame_count} frames)"
                        )
                        if self._speech_frame_count >= self._min_speech_frames:
                            self._emit_event()
                        else:
                            logger.debug(
                                f"Speech too short ({self._speech_frame_count} frames < "
                                f"{self._min_speech_frames}), drop."
                            )
                        self._reset_vad_state()
                # 还没开始讲话：直接丢掉静音帧
        else:
            if not self._is_speaking:
                self._is_speaking = True
            self._audio_frames.append(data)

    def _reset_vad_state(self):
        self._is_speaking = False
        self._speech_frame_count = 0
        self._silence_frame_count = 0
        self._audio_frames = []

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