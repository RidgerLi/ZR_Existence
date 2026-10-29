"""
抗噪门（Noise Gate）
Author: ZerolanLiveRobot

背景
----
`devices/microphone.py` 原来的端点检测只依赖 webrtcvad 的帧级判定。它对"人声 vs 静音"
够用，但对**键盘敲击、咳嗽、桌面震动**这类"很响但不像人声"的瞬态很无力：只要累计 33 帧
被判 speech，这段音频就会被当成一句话送去 ASR；全双工下这些帧还会累计成"用户在插话"，
把正在说的话硬打断。

本模块在 VAD 之外补三类**额外否决**证据（只会减少误判，不会把一个非语音帧变成语音）：

1. 能量（level）
   帧 RMS 必须同时高于「绝对静音门」和「自适应底噪 × 倍数」。底噪由非语音帧慢速跟踪，
   所以换房间 / 开空调 / 换麦克风不必手改阈值。

2. 频谱（timbre）
   - ZCR（过零率）：人声（浊音）波形缓慢起伏，键盘点击是高频密集振荡。
   - 谱平坦度（flatness）：人声能量集中在基频与共振峰，平坦度低；宽带噪声/点击接近均匀。
   - 语音带能量比（band_ratio）：200–3800 Hz 能量占总能量的比例，人声高、点击低。
   句级用**中位数**判定，避免把 /s/ /sh/ 这类本身就像噪声的清辅音误杀。

3. 键盘活动（context）
   最近若干毫秒内有击键时直接否决该帧。键盘声与击键时间高度相关，这条几乎零成本且最准。

配套的两件工程设施
------------------
- 句尾静音裁剪：只把最后 `trailing_silence_ms` 写进 WAV（原先会把整整 1.2s 静音一起送 ASR）。
- 每句指标日志：`[VAD] dur=... rms=... snr=... zcr=... flat=... band=... -> ACCEPT/REJECT(原因)`，
  让阈值能按真实房间数据来调。详见 `docs/noise-robustness.md`。

本模块只做纯计算，不读配置单例、不碰音频设备，便于单元测试。
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

# 语音带：低于 200 Hz 多为桌面震动/低频轰鸣，高于 3800 Hz 在 16k 采样下主要是宽带噪声。
_BAND_LO_HZ = 200.0
_BAND_HI_HZ = 3800.0
_NFFT = 512
_EPS = 1e-12


# ----------------------------------------------------------------------------
# 基础特征
# ----------------------------------------------------------------------------
def _to_float(frame) -> np.ndarray:
    """把一帧 PCM（bytes 或 ndarray）转成 float32 数组。"""
    if isinstance(frame, (bytes, bytearray, memoryview)):
        return np.frombuffer(frame, dtype=np.int16).astype(np.float32)
    return np.asarray(frame, dtype=np.float32)


def rms_to_dbfs(rms: float) -> float:
    """int16 域的 RMS → dBFS（满幅 32768 记为 0 dB）。"""
    return 20.0 * math.log10(max(float(rms), 1e-9) / 32768.0)


def _zcr(x: np.ndarray) -> float:
    if x.size < 2:
        return 0.0
    return float(np.count_nonzero(np.diff(np.signbit(x))) / (x.size - 1))


def _flatness(power: np.ndarray) -> float:
    """谱平坦度 = 几何均值 / 算术均值，宽带噪声趋近 1，窄带/共振峰趋近 0。"""
    p = power + _EPS
    return float(np.exp(np.mean(np.log(p))) / (np.mean(p) + _EPS))


def _band_ratio(power: np.ndarray, freqs: np.ndarray) -> float:
    total = float(np.sum(power)) + _EPS
    band = float(np.sum(power[(freqs >= _BAND_LO_HZ) & (freqs <= _BAND_HI_HZ)]))
    return band / total


@dataclass
class FrameFeatures:
    """单帧特征。`spectral=False` 表示只算了能量（省一次 FFT）。"""
    rms: float
    zcr: float = 0.0
    flatness: float = 0.0
    band_ratio: float = 1.0
    spectral: bool = False

    @property
    def dbfs(self) -> float:
        return rms_to_dbfs(self.rms)


@dataclass
class UtteranceStats:
    """一句话的聚合指标，用于日志与调参。"""
    frames_speech: int = 0
    span_frames: int = 0
    speech_ratio: float = 0.0
    duration_ms: int = 0
    rms_median: float = 0.0
    dbfs_median: float = 0.0
    zcr_median: float = 0.0
    flatness_median: float = 0.0
    band_ratio_median: float = 0.0
    noise_floor: float = 0.0
    snr_db: float = 0.0

    def format(self) -> str:
        floor = f" floor={self.noise_floor:.0f} snr={self.snr_db:.1f}dB" if self.noise_floor > 0 else " floor=?"
        return (f"dur={self.duration_ms}ms speech={self.frames_speech} span={self.span_frames} "
                f"({self.speech_ratio * 100:.0f}%) rms={self.rms_median:.0f}({self.dbfs_median:.1f}dBFS)"
                f"{floor} zcr={self.zcr_median:.2f} flat={self.flatness_median:.2f} band={self.band_ratio_median:.2f}")


def _median(values: Sequence[float]) -> float:
    return float(np.median(values)) if len(values) else 0.0


# ----------------------------------------------------------------------------
# 抗噪门
# ----------------------------------------------------------------------------
class NoiseGate:
    """能量 + 频谱 +（外部注入的）键盘活动三重抗噪门。

    线程模型：所有方法都在麦克风采集线程调用，内部不加锁。
    """

    def __init__(self, cfg, sample_rate: int = 16000, frame_ms: int = 30):
        self._cfg = cfg
        self._sr = sample_rate
        self._frame_ms = frame_ms
        # 自适应底噪（int16 RMS）；None 表示还在冷启动学习期。
        self._floor: Optional[float] = None
        # 底噪冷启动采样：取前 N 个非语音帧的中位数，避免被开机噪声带偏。
        self._seed: List[float] = []
        self._seed_needed = 30
        self._need_spectral = bool(cfg.spectral_check or cfg.barge_in_frame_check)

    # ---- 只读属性（供日志 / 面板）------------------------------------------
    @property
    def cfg(self):
        return self._cfg

    @property
    def noise_floor(self) -> Optional[float]:
        return self._floor

    def snr_ratio(self, rms: float) -> float:
        floor = self._floor if self._floor is not None else 0.0
        return float(rms) / floor if floor > 0 else float("inf")

    # ---- 底噪学习 ------------------------------------------------------------
    def observe_noise(self, rms: float) -> None:
        """用一帧"确认非语音"的 RMS 更新底噪。调用方需保证：不在播放、不在句子中。"""
        rms = max(0.0, float(rms))
        hi = self._cfg.noise_floor_max
        if rms > hi * 2.0:
            # 明显是响动而不是底噪，丢弃，避免一次关门把底噪抬高。
            return
        if self._floor is None:
            self._seed.append(rms)
            if len(self._seed) >= self._seed_needed:
                self._floor = float(np.median(self._seed))
                self._floor = min(max(self._floor, self._cfg.noise_floor_min), hi)
            return
        a = self._cfg.noise_floor_alpha
        self._floor = (1.0 - a) * self._floor + a * rms
        self._floor = min(max(self._floor, self._cfg.noise_floor_min), hi)

    # ---- 能量门 --------------------------------------------------------------
    def energy_ok(self, rms: float) -> bool:
        """绝对静音门 + 信噪比门。底噪还没学到时只用绝对门（冷启动不误杀）。"""
        if rms < self._cfg.abs_floor_rms:
            return False
        if self._floor is None:
            return True
        return rms >= max(self._cfg.abs_floor_rms, self._floor * self._cfg.snr_margin)

    # ---- 帧特征 --------------------------------------------------------------
    def features(self, frame) -> FrameFeatures:
        x = _to_float(frame)
        if x.size == 0:
            return FrameFeatures(rms=0.0)
        rms = float(np.sqrt(np.mean(x * x)))
        if not self._need_spectral:
            return FrameFeatures(rms=rms)

        zcr = _zcr(x)
        window = np.hanning(x.size)
        power = np.abs(np.fft.rfft((x - x.mean()) * window, n=_NFFT)) ** 2
        freqs = np.fft.rfftfreq(_NFFT, d=1.0 / self._sr)
        return FrameFeatures(rms=rms, zcr=zcr, flatness=_flatness(power),
                             band_ratio=_band_ratio(power, freqs), spectral=True)

    # ---- 打断用的"确凿插话"帧 ------------------------------------------------
    def barge_in_frame_ok(self, f: FrameFeatures, speech_prob: Optional[float] = None) -> bool:
        """比普通 VAD 更严的一帧判定：只有像人声、够响、AEC 也认为是语音才算。

        打断是"立刻停止输出"的破坏性动作，宁可慢一点也不能被键盘/咳嗽误触发，
        所以这里用帧级频谱门（普通句子用的是句级中位数，容忍清辅音）。
        """
        cfg = self._cfg
        if f.rms < cfg.abs_floor_rms:
            return False
        if speech_prob is not None and cfg.barge_in_min_speech_prob > 0.0 \
                and speech_prob < cfg.barge_in_min_speech_prob:
            return False
        if not cfg.barge_in_frame_check or not f.spectral:
            return True
        if f.zcr > cfg.barge_in_max_zcr:
            return False
        if f.flatness > cfg.barge_in_max_flatness:
            return False
        if f.band_ratio < cfg.barge_in_min_band_ratio:
            return False
        return True

    # ---- 句级评分 ------------------------------------------------------------
    def score(self, feats: Sequence[FrameFeatures], span_frames: int) -> Tuple[bool, str, UtteranceStats]:
        """对一句话做最终判定。

        :param feats: 这句话里被判为语音的帧特征。
        :param span_frames: 从第一个语音帧到最后一个语音帧的帧数（不含句尾 hangover），
                            用来衡量"语音密度"——键盘连敲时语音帧稀疏散落，密度会很低。
        :return: (是否接受, 拒绝原因, 指标)
        """
        cfg = self._cfg
        n = len(feats)
        span = max(int(span_frames), n, 1)
        ratio = n / span

        spectral = [f for f in feats if f.spectral]
        stats = UtteranceStats(
            frames_speech=n,
            span_frames=span,
            speech_ratio=ratio,
            duration_ms=n * self._frame_ms,
            rms_median=_median([f.rms for f in feats]),
            zcr_median=_median([f.zcr for f in spectral]),
            flatness_median=_median([f.flatness for f in spectral]),
            band_ratio_median=_median([f.band_ratio for f in spectral]),
        )
        stats.dbfs_median = rms_to_dbfs(stats.rms_median)
        if self._floor is not None and self._floor > 0:
            stats.noise_floor = self._floor
            stats.snr_db = 20.0 * math.log10(max(stats.rms_median, 1e-9) / self._floor)

        if n == 0:
            return False, "no_speech", stats
        if ratio < cfg.min_speech_ratio:
            return False, "low_density", stats
        if cfg.spectral_check and spectral:
            if stats.zcr_median > cfg.max_zcr:
                return False, "zcr", stats
            if stats.flatness_median > cfg.max_flatness:
                return False, "flatness", stats
            if stats.band_ratio_median < cfg.min_band_ratio:
                return False, "band_ratio", stats
        return True, "ok", stats
