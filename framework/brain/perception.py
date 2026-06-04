"""
感知层（Perception）
Author: ZerolanLiveRobot

把所有异构输入（麦克风能量、人声、键鼠、时间、未来的摄像头/记忆）统一抽象成归一化
到 [0, 1] 的"刺激强度"，写进一块共享的感知白板（Perception）。大脑只读白板，不关心
信号来自哪一路设备，从而做到新增模态零侵入。

设计要点：
    - Sensor 只负责"采样并归一化"，不做任何决策。
    - 每个 Sensor 输出一个 SignalReading（强度 + 时间戳 + 是否在线）。
    - Perception 聚合所有读数，提供给大脑按通道名取值。
    - 采样方式统一为"轮询"（poll）：大脑每个 tick 主动调用 sample()，避免为高频信号
      （如每 30ms 一帧的音频）滥发事件、压垮事件线程池。
"""

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from loguru import logger


def clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x


@dataclass
class SignalReading:
    """单路传感器的一次归一化读数。"""
    channel: str
    value: float = 0.0           # 归一化强度 [0, 1]
    online: bool = True          # 该传感器当前是否可用（设备缺失则 False）
    raw: Optional[float] = None  # 原始值（仅用于面板展示/调试）
    ts: float = field(default_factory=time.monotonic)


class Sensor(ABC):
    """传感器基类。子类只需实现 `sample()` 返回当前归一化强度。

    `channel` 是该传感器在感知白板上的通道名（大脑据此取值）。
    `enabled` 为 False 时大脑会跳过它（但仍在面板上显示为离线）。
    """

    def __init__(self, channel: str, enabled: bool = True):
        self.channel = channel
        self.enabled = enabled

    @abstractmethod
    def sample(self) -> SignalReading:
        ...

    def _reading(self, value: float, online: bool = True,
                 raw: Optional[float] = None) -> SignalReading:
        return SignalReading(channel=self.channel, value=clamp01(value),
                             online=online, raw=raw)


class Perception:
    """感知白板：聚合所有传感器的最新读数。"""

    def __init__(self):
        self._sensors: Dict[str, Sensor] = {}
        self._readings: Dict[str, SignalReading] = {}

    def add(self, sensor: Sensor) -> None:
        self._sensors[sensor.channel] = sensor
        self._readings[sensor.channel] = SignalReading(channel=sensor.channel,
                                                       value=0.0, online=False)

    def sample_all(self) -> Dict[str, SignalReading]:
        """轮询所有启用的传感器，刷新白板。返回最新读数字典。"""
        for channel, sensor in self._sensors.items():
            if not sensor.enabled:
                self._readings[channel] = SignalReading(channel=channel, value=0.0,
                                                        online=False)
                continue
            try:
                self._readings[channel] = sensor.sample()
            except Exception as e:
                logger.exception(e)
                self._readings[channel] = SignalReading(channel=channel, value=0.0,
                                                        online=False)
        return self._readings

    def value(self, channel: str, default: float = 0.0) -> float:
        r = self._readings.get(channel)
        if r is None or not r.online:
            return default
        return r.value

    def readings(self) -> Dict[str, SignalReading]:
        return dict(self._readings)


# --------------------------------------------------------------------------- #
# 具体传感器：现有硬件
# --------------------------------------------------------------------------- #

class MicEnergySensor(Sensor):
    """麦克风能量传感器。读取麦克风维护的 RMS 能量 EMA（含非人声/环境变化）。

    `energy_probe` 返回当前归一化能量 [0, 1]；麦克风离线/关闭时返回 None。
    """

    def __init__(self, energy_probe: Callable[[], Optional[float]], enabled: bool = True):
        super().__init__(channel="mic_energy", enabled=enabled)
        self._probe = energy_probe

    def sample(self) -> SignalReading:
        v = self._probe()
        if v is None:
            return self._reading(0.0, online=False)
        return self._reading(v, online=True, raw=v)


class VadSensor(Sensor):
    """人声在场传感器。用户正在说话时为 1，否则为 0。"""

    def __init__(self, speaking_probe: Callable[[], Optional[bool]], enabled: bool = True):
        super().__init__(channel="user_speaking", enabled=enabled)
        self._probe = speaking_probe

    def sample(self) -> SignalReading:
        v = self._probe()
        if v is None:
            return self._reading(0.0, online=False)
        return self._reading(1.0 if v else 0.0, online=True, raw=1.0 if v else 0.0)


class KeyboardActivitySensor(Sensor):
    """键鼠活跃度传感器。把最近窗口内的敲击次数归一化到 [0, 1]。

    `count_probe` 返回最近 `window_s` 秒内的敲击次数（由 SmartKeyboard 维护）。
    `busy_count` 是"算满活跃"的敲击次数（达到即归一化为 1）。
    """

    def __init__(self, count_probe: Callable[[], Optional[int]],
                 busy_count: int = 50, enabled: bool = True):
        super().__init__(channel="keyboard", enabled=enabled)
        self._probe = count_probe
        self._busy_count = max(1, busy_count)

    def sample(self) -> SignalReading:
        c = self._probe()
        if c is None:
            return self._reading(0.0, online=False)
        return self._reading(c / self._busy_count, online=True, raw=float(c))


class TimeSilenceSensor(Sensor):
    """静默时长传感器。距上次活动越久，强度越接近 1（提供"无聊"基线）。

    `idle_probe` 返回距上次活动的秒数；`full_silence_s` 是"算满静默"的秒数。
    """

    def __init__(self, idle_probe: Callable[[], float],
                 full_silence_s: float = 120.0, enabled: bool = True):
        super().__init__(channel="silence", enabled=enabled)
        self._probe = idle_probe
        self._full = max(1.0, full_silence_s)

    def sample(self) -> SignalReading:
        s = self._probe()
        return self._reading(s / self._full, online=True, raw=s)


# --------------------------------------------------------------------------- #
# 预留接口：未来模态。现在恒返回离线/0，但通道已经在白板上占位，
# 大脑的加权项可以直接引用，将来只需替换 probe 即可接入真实信号。
# --------------------------------------------------------------------------- #

class CameraMotionSensor(Sensor):
    """（预留）摄像头动静/人脸在场传感器。"""

    def __init__(self, motion_probe: Optional[Callable[[], Optional[float]]] = None,
                 enabled: bool = False):
        super().__init__(channel="camera_motion", enabled=enabled)
        self._probe = motion_probe

    def sample(self) -> SignalReading:
        if self._probe is None:
            return self._reading(0.0, online=False)
        v = self._probe()
        if v is None:
            return self._reading(0.0, online=False)
        return self._reading(v, online=True, raw=v)


class MemoryUrgeSensor(Sensor):
    """（预留 / Phase 3）未完话题/约定带来的好奇心刺激。

    将来记忆层会维护"悬而未决度"，转成一路刺激提升表达欲。现在留空接口。
    """

    def __init__(self, urge_probe: Optional[Callable[[], Optional[float]]] = None,
                 enabled: bool = False):
        super().__init__(channel="memory_urge", enabled=enabled)
        self._probe = urge_probe

    def sample(self) -> SignalReading:
        if self._probe is None:
            return self._reading(0.0, online=False)
        v = self._probe()
        if v is None:
            return self._reading(0.0, online=False)
        return self._reading(v, online=True, raw=v)
