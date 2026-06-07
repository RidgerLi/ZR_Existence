"""
内驱层（Drives）
Author: ZerolanLiveRobot

仿生内核：用"漏积分-发放（Leaky Integrate-and-Fire）"维护多个随时间演化的内部驱动量。
它们整合感知层送来的各路刺激，最终由 `expression_urge` 越过阈值触发"主动开口"。

三个内驱：
    arousal         唤醒度：任何刺激都拉高，随时间泄漏衰减。代表整体警觉/兴奋。
    social_need     社交需求（孤独）：随"距上次真实交流"的时间缓慢上升，每次真实对话后回落。
    expression_urge 表达欲：发放累加器。累积速率 = 基线 + 唤醒度 + 社交需求 + 即时刺激；
                    键鼠繁忙会抑制它。越过阈值即发放（主动开口），随后清零。

模式（focus/normal/companion）只是一组参数预设，不写任何分支逻辑——这正是这套模型的好处。
"""

from dataclasses import dataclass, field
from typing import Dict

from loguru import logger


@dataclass
class DriveParams:
    """一套内驱动力学参数。不同 proactivity 模式 = 不同的参数预设。"""
    # --- 表达欲基线（无聊驱动）。越大越话痨。focus 设 0 即几乎不主动。---
    base_drive: float = 0.05
    # 发放阈值：expression_urge 越过它就主动开口。越大越沉默。
    fire_threshold: float = 1.0

    # --- 表达欲的加权来源 ---
    w_arousal: float = 0.15          # 唤醒度对表达欲的贡献
    w_social: float = 0.35           # 社交需求对表达欲的贡献
    w_mic: float = 0.10              # 麦克风能量（环境有动静）对表达欲的贡献
    w_memory: float = 0.20           # 未完话题（Phase 3）对表达欲的贡献
    # 键鼠繁忙对表达欲的抑制强度 [0,1]：1 表示键盘拉满时表达欲完全不累积。
    keyboard_suppress: float = 0.9
    leak_urge: float = 0.10          # 表达欲自身泄漏

    # --- 唤醒度动力学 ---
    arousal_gain: float = 1.2        # 刺激对唤醒度的增益
    arousal_leak: float = 0.5        # 唤醒度泄漏（越大平复越快）
    wa_mic: float = 0.6
    wa_speech: float = 1.0
    wa_keyboard: float = 0.3
    wa_camera: float = 0.5

    # --- 社交需求动力学 ---
    social_rise_per_s: float = 0.004  # 每秒上升量（约 1/250s 涨满）
    social_reset_factor: float = 0.2  # 发生真实交流后乘以该系数（回落）

    # --- 防唠叨 ---
    refractory_s: float = 30.0        # 发放后的基础不应期
    refractory_backoff: float = 1.8   # 连续无人回应时不应期逐次乘以该系数
    max_consecutive_fires: int = 3    # 连续主动开口上限，达到后彻底安静直到用户开口


# 模式预设。auto 模式由 Brain 根据键鼠活跃度在这三档之间选择。
def default_mode_presets() -> Dict[str, DriveParams]:
    return {
        "focus": DriveParams(
            base_drive=0.0, fire_threshold=1.0,
            w_social=0.15, keyboard_suppress=1.0,
            social_rise_per_s=0.0015,
            refractory_s=120.0, max_consecutive_fires=1,
        ),
        "normal": DriveParams(
            base_drive=0.05, fire_threshold=1.0,
            social_rise_per_s=0.003,
            refractory_s=45.0, max_consecutive_fires=3,
        ),
        "companion": DriveParams(
            base_drive=0.08, fire_threshold=0.95,
            w_social=0.45, keyboard_suppress=0.6,
            social_rise_per_s=0.005,
            refractory_s=40.0, max_consecutive_fires=3,
        ),
    }


@dataclass
class DriveState:
    arousal: float = 0.0
    social_need: float = 0.0
    expression_urge: float = 0.0


class DriveSystem:
    """多内驱漏积分-发放系统。

    用法：每个 tick 由 Brain 调用 `update(dt, signals, gated, interaction)`，
    传入归一化感知信号、当前 effective 模式参数。返回 expression_urge 是否越过阈值。
    Brain 再结合不应期/上限决定是否真的发放，发放后调用 `on_fired()`。
    """

    def __init__(self):
        self.state = DriveState()

    def update(self, dt: float, signals: Dict[str, float], params: DriveParams,
               gated: bool, interaction: bool) -> bool:
        """推进一步动力学。

        :param dt: 距上次更新的秒数。
        :param signals: 归一化感知信号（mic_energy / user_speaking / keyboard / camera_motion / memory_urge ...）。
        :param params: 当前 effective 模式的动力学参数。
        :param gated: 硬闸门（AI 正忙 / 用户正在说话）。为真时表达欲只衰减、不向发放推进。
        :param interaction: 本 tick 是否刚发生真实交流（用户说话/AI 回应），用于回落社交需求。
        :return: expression_urge 是否 >= fire_threshold（仅作"想发放"的信号，是否真发由 Brain 决定）。
        """
        if dt <= 0:
            return False

        mic = signals.get("mic_energy", 0.0)
        speech = signals.get("user_speaking", 0.0)
        kb = signals.get("keyboard", 0.0)
        cam = signals.get("camera_motion", 0.0)
        mem = signals.get("memory_urge", 0.0)

        s = self.state

        # --- 唤醒度：刺激拉高、自身泄漏 ---
        stim = (params.wa_mic * mic + params.wa_speech * speech
                + params.wa_keyboard * kb + params.wa_camera * cam)
        s.arousal += dt * (params.arousal_gain * stim - params.arousal_leak * s.arousal)
        s.arousal = _clamp01(s.arousal)

        # --- 社交需求：随时间上升；真实交流后回落 ---
        if interaction:
            s.social_need *= params.social_reset_factor
        else:
            s.social_need += dt * params.social_rise_per_s
        s.social_need = _clamp01(s.social_need)

        # --- 表达欲：加权驱动 + 键鼠抑制门 + 泄漏 ---
        drive = (params.base_drive
                 + params.w_arousal * s.arousal
                 + params.w_social * s.social_need
                 + params.w_mic * mic
                 + params.w_memory * mem)
        # 键鼠繁忙抑制：你在专注干活时少打扰
        drive *= (1.0 - params.keyboard_suppress * kb)

        if gated:
            # 被硬闸门挡住时：只衰减，不向发放推进（不抢话）
            s.expression_urge -= dt * params.leak_urge * s.expression_urge
        else:
            s.expression_urge += dt * drive
            s.expression_urge -= dt * params.leak_urge * s.expression_urge
        s.expression_urge = _clamp01(s.expression_urge)

        return s.expression_urge >= params.fire_threshold

    def on_fired(self) -> None:
        """主动开口已发放：清零表达欲。"""
        self.state.expression_urge = 0.0

    def on_user_interaction(self) -> None:
        """用户主动开口：强烈回落社交需求（被陪伴了）。"""
        self.state.social_need *= 0.2

    def snapshot(self) -> Dict[str, float]:
        s = self.state
        return {
            "arousal": round(s.arousal, 4),
            "social_need": round(s.social_need, 4),
            "expression_urge": round(s.expression_urge, 4),
        }


def _clamp01(x: float) -> float:
    if x < 0.0:
        return 0.0
    if x > 1.0:
        return 1.0
    return x
