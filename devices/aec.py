"""
全双工声学回声消除（Acoustic Echo Cancellation, AEC）
Author: ZerolanLiveRobot

背景：
    半双工方案（devices/microphone.py 的 playback gate）在机器人说话时直接关闭麦克风采集，
    用户无法在机器人说话期间插话。要做"全双工 + 打断（barge-in）"，麦克风必须在播放期间
    持续采集，同时把机器人自己的声音从麦克风信号里减掉——这就是 AEC。

原理：
    AEC 需要两路时间对齐、等长的信号：
        - near-end：麦克风采集到的信号 = 用户语音 + 机器人回声 + 噪声
        - far-end ：扬声器正在播放的参考信号（机器人自己的声音）
    已知 far-end，自适应滤波器（这里用 WebRTC AEC3）就能估计并消除 near-end 里的回声分量。

参考信号来源（far-end）：
    Windows 下用 PyAudioWPatch 的 WASAPI loopback，把"扬声器正在播放的声音"作为输入流录下来。
    它覆盖一切本地播放（pygame TTS、系统提示音等），无需改动现有播放器。
    loopback 通常是设备原生采样率（如 48000）+ 立体声，需降混为单声道并重采样到 16000，
    再喂给与 near-end 对齐的环形缓冲。

依赖（缺失任一则 `available` 为 False，调用方应回退到半双工）：
    - pyaudiowpatch：WASAPI loopback 回采（仅 Windows）
    - pywebrtc_audio：WebRTC AEC3（AudioProcessor）
    - scipy：重采样
"""

import os
import threading
import time

import numpy as np
from loguru import logger

try:
    import pyaudiowpatch as _pyaudio_wp
except Exception:  # pragma: no cover - 取决于运行平台与是否安装
    _pyaudio_wp = None

try:
    from pywebrtc_audio import AudioProcessor as _AudioProcessor
except Exception:  # pragma: no cover
    _AudioProcessor = None

try:
    from scipy.signal import resample_poly as _resample_poly
except Exception:  # pragma: no cover
    _resample_poly = None


class EchoCanceller:
    """WASAPI 回采 + WebRTC AEC3 的回声消除器。

    线程模型：
        - 一个独立的"回采线程"持续从 WASAPI loopback 读取扬声器输出，降混 + 重采样后
          追加到 far-end 环形缓冲（`_push_far`）。
        - 麦克风采集线程对每一帧调用 `process(near_bytes)`：从 far-end 缓冲取等长样本，
          用 AEC3 消回声后返回。
    注意：pywebrtc_audio 的 AudioProcessor 非线程安全，`process` 必须始终在同一个线程
    （麦克风采集线程）调用；回采线程只往 far-end 缓冲写，与 `process` 用独立的锁。
    """

    def __init__(self, sample_rate: int = 16000, frame_samples: int = 480,
                 stream_delay_ms: int = 0, loopback_device_index: int = -1,
                 max_buffer_frames: int = 8, noise_suppression: bool = True,
                 debug: bool = False):
        """
        :param sample_rate: AEC 工作采样率，必须与麦克风一致（16000）。
        :param frame_samples: 麦克风单帧样本数（30ms@16k = 480），用于估算 far-end 缓冲上限与对齐。
        :param stream_delay_ms: 扬声器→麦克风的回声延迟提示，0 表示让 AEC3 自行估计。
        :param loopback_device_index: WASAPI loopback 输入设备索引，-1 表示默认扬声器的 loopback。
        :param max_buffer_frames: far-end 缓冲最多保留多少帧（默认 8≈240ms）。

            ⚠️ 对齐是 AEC 能否生效的关键：参考信号必须"超前"于回声、且偏差在 AEC3 的延迟搜索范围
            （约几百毫秒）内。如果缓冲堆积太深，取到的参考会严重滞后于真实回声，导致完全无法对消。
            麦克风暂停（未开麦）时回采线程仍在推数据，缓冲会被堆满，因此这里把上限收紧，并在
            `process()` 检测到消费停顿后清空缓冲重新对齐（见 `_max_gap_s`）。
        :param noise_suppression: 是否在 AEC 之后顺带做降噪（同一遍处理，几乎零额外开销）。
        :param debug: 开启后每隔约 1s 打印一次 near/far/out 的 RMS，便于现场确认回声是否被消除。
        """
        self._sample_rate = sample_rate
        self._frame_samples = max(1, frame_samples)
        self._stream_delay_ms = max(0, stream_delay_ms)
        self._loopback_device_index = loopback_device_index
        self._max_far_samples = max(1, max_buffer_frames) * self._frame_samples
        self._noise_suppression = noise_suppression
        self._debug = debug or os.environ.get("AEC_DEBUG", "").lower() in ("1", "true", "yes")

        # 消费停顿检测：两次 process() 间隔超过该值，说明麦克风曾暂停/卡顿，缓冲里的参考已过时，
        # 清空后从最新的回采数据重新对齐。
        self._max_gap_s = 0.12
        self._last_process_t = 0.0
        self._dbg_count = 0

        # 依赖可用性：任一缺失则禁用，调用方回退到半双工。
        missing = []
        if _pyaudio_wp is None:
            missing.append("PyAudioWPatch")
        if _AudioProcessor is None:
            missing.append("pywebrtc-audio")
        if _resample_poly is None:
            missing.append("scipy")
        self._available = len(missing) == 0
        if not self._available:
            logger.warning(
                f"EchoCanceller disabled: missing dependencies {missing}. "
                f"Full-duplex will fall back to half-duplex echo suppression."
            )

        self._apm = None
        self._pa = None
        self._lb_stream = None
        self._lb_rate = sample_rate
        self._lb_channels = 1

        # far-end 环形缓冲（单声道 int16，已重采样到 sample_rate）
        self._far = np.zeros(0, dtype=np.int16)
        self._far_lock = threading.Lock()

        # 回采线程
        self._ref_thread: threading.Thread | None = None
        self._stop_flag = False
        self._started = False

        # 最近一次 process 的语音概率（0~1），供麦克风在播放期间过滤残余回声。
        self._last_speech_prob = 0.0

    @property
    def available(self) -> bool:
        return self._available

    @property
    def last_speech_prob(self) -> float:
        return self._last_speech_prob

    # ---- 生命周期 ----------------------------------------------------------
    def start(self) -> bool:
        """初始化 AEC 引擎并启动回采线程。返回是否成功启动（失败则禁用）。"""
        if not self._available or self._started:
            return self._started
        try:
            self._apm = _AudioProcessor(
                sample_rate=self._sample_rate,
                num_channels=1,
                echo_cancellation=True,
                noise_suppression=self._noise_suppression,
                stream_delay_ms=self._stream_delay_ms,
            )
            self._open_loopback()
        except Exception as e:
            logger.exception(e)
            logger.warning("EchoCanceller failed to start; disabling AEC (fall back to half-duplex).")
            self._available = False
            self._cleanup()
            return False

        self._stop_flag = False
        self._ref_thread = threading.Thread(target=self._reference_loop, daemon=True,
                                            name="AECReferenceLoop")
        self._ref_thread.start()
        self._started = True
        logger.info(
            f"EchoCanceller started (loopback rate={self._lb_rate}Hz ch={self._lb_channels}, "
            f"target={self._sample_rate}Hz, stream_delay={self._stream_delay_ms}ms)."
        )
        return True

    def stop(self):
        self._stop_flag = True
        if self._ref_thread is not None:
            self._ref_thread.join(timeout=1.0)
            self._ref_thread = None
        self._cleanup()
        self._started = False
        logger.info("EchoCanceller stopped.")

    def reset(self):
        """打断 / 切换对话后复位 AEC 内部状态，避免陈旧滤波系数影响下一段。"""
        with self._far_lock:
            self._far = np.zeros(0, dtype=np.int16)
        if self._apm is not None:
            try:
                self._apm.reset()
            except Exception as e:
                logger.exception(e)

    # ---- 核心处理 ----------------------------------------------------------
    def process(self, near_bytes: bytes) -> bytes:
        """对一帧麦克风 PCM（int16 单声道）做回声消除，返回消回声后的 PCM。

        必须始终从同一个线程（麦克风采集线程）调用。AEC 不可用时原样返回。
        """
        if not self._available or self._apm is None:
            return near_bytes
        try:
            near = np.frombuffer(near_bytes, dtype=np.int16)
            if near.size == 0:
                return near_bytes

            # 消费停顿（麦克风曾暂停/卡顿）后，缓冲里的参考已严重过时：清空，从最新数据重对齐。
            now = time.monotonic()
            if self._last_process_t and (now - self._last_process_t) > self._max_gap_s:
                self._flush_far()
            self._last_process_t = now

            far = self._pop_far(near.size)
            clean = self._apm.process(near, far)
            try:
                self._last_speech_prob = float(self._apm.speech_probability)
            except Exception:
                self._last_speech_prob = 0.0
            out = np.asarray(clean, dtype=np.int16)

            if self._debug:
                self._debug_log(near, far, out)

            return out.tobytes()
        except Exception as e:
            logger.exception(e)
            return near_bytes

    def _debug_log(self, near: np.ndarray, far: np.ndarray, out: np.ndarray):
        self._dbg_count += 1
        # 16k 下每 ~1s（约 33 帧 @30ms）打印一次
        if self._dbg_count % 33 != 0:
            return

        def _rms(x):
            return float(np.sqrt(np.mean(x.astype(np.float32) ** 2))) if x.size else 0.0

        logger.debug(
            f"AEC near={_rms(near):.0f} far={_rms(far):.0f} out={_rms(out):.0f} "
            f"speech_prob={self._last_speech_prob:.2f}"
        )

    # ---- far-end 缓冲 ------------------------------------------------------
    def _flush_far(self):
        # 只保留最新 2 帧：清掉过时堆积让参考重新贴近当前回声，同时留一点缓冲防欠载。
        keep = 2 * self._frame_samples
        with self._far_lock:
            if self._far.size > keep:
                self._far = self._far[-keep:].copy()

    def _push_far(self, samples: np.ndarray):
        with self._far_lock:
            if self._far.size:
                self._far = np.concatenate([self._far, samples])
            else:
                self._far = samples.copy()
            # 防漂移：只保留最近的一段，丢弃最旧的。
            if self._far.size > self._max_far_samples:
                self._far = self._far[-self._max_far_samples:]

    def _pop_far(self, n: int) -> np.ndarray:
        # 关键：始终取"最新"的 n 个样本，丢弃更旧的堆积。
        # loopback 在数字渲染端取信号，天然超前于"扬声器→空气→麦克风"的声学回声；
        # AEC3 要求参考超前于回声才能对消。若缓冲堆积，参考会被反推成滞后而完全失效。
        # 实时稳态下每帧只攒约 1 帧、不会丢弃；只有播放起始的突发/抖动时丢掉旧帧，保持对齐。
        with self._far_lock:
            if self._far.size >= n:
                out = self._far[-n:].copy()
                # 丢弃本次取用之前的所有旧样本，避免堆积造成参考滞后。
                self._far = np.zeros(0, dtype=np.int16)
                return out
            # far-end 还没攒够（刚开始 / 没在播放）：用静音补齐，AEC 此时近似透传。
            out = np.zeros(n, dtype=np.int16)
            if self._far.size:
                out[-self._far.size:] = self._far
                self._far = np.zeros(0, dtype=np.int16)
            return out

    # ---- WASAPI loopback ---------------------------------------------------
    def _open_loopback(self):
        self._pa = _pyaudio_wp.PyAudio()
        if self._loopback_device_index is not None and self._loopback_device_index >= 0:
            dev = self._pa.get_device_info_by_index(self._loopback_device_index)
        else:
            # 默认扬声器的 loopback 镜像设备
            dev = self._pa.get_default_wasapi_loopback()
        self._lb_rate = int(dev["defaultSampleRate"])
        self._lb_channels = int(dev["maxInputChannels"])
        self._lb_stream = self._pa.open(
            format=_pyaudio_wp.paInt16,
            channels=self._lb_channels,
            rate=self._lb_rate,
            input=True,
            input_device_index=int(dev["index"]),
            frames_per_buffer=max(1, int(self._lb_rate * 0.03)),
        )

    def _reference_loop(self):
        chunk = max(1, int(self._lb_rate * 0.03))  # 30ms @ 原生采样率
        while not self._stop_flag:
            try:
                data = self._lb_stream.read(chunk, exception_on_overflow=False)
            except Exception as e:
                logger.exception(e)
                continue
            arr = np.frombuffer(data, dtype=np.int16)
            if arr.size == 0:
                continue
            # 多声道交错 -> 单声道（取平均）
            if self._lb_channels > 1:
                arr = arr.reshape(-1, self._lb_channels).mean(axis=1)
            # 重采样到目标采样率
            if self._lb_rate != self._sample_rate:
                arr = _resample_poly(arr.astype(np.float32), self._sample_rate, self._lb_rate)
            arr = np.clip(arr, -32768, 32767).astype(np.int16)
            self._push_far(arr)

    def _cleanup(self):
        if self._lb_stream is not None:
            try:
                self._lb_stream.stop_stream()
                self._lb_stream.close()
            except Exception as e:
                logger.exception(e)
            self._lb_stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception as e:
                logger.exception(e)
            self._pa = None
        self._apm = None
