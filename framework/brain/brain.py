"""
大脑（Brain）—— LLM 之前的决策中枢
Author: ZerolanLiveRobot

认知循环：每个 tick 依次完成
    1. 感知：轮询所有传感器，刷新感知白板；
    2. 内驱：用归一化信号推进多个内驱量（漏积分-发放）；
    3. 决策：按优先级选择行为
         - 硬闸门（AI 忙 / 用户正在说话）→ 不行动；
         - 有 pending 用户输入 → 回应用户（最高优先级）；
         - 表达欲发放且通过不应期/上限校验 → 主动开口；
    4. 发布：把本 tick 的完整状态快照发布给可视化面板。

Brain 不直接调用 LLM。真正的"开口"通过注入的两个回调交给 bot 执行（由 bot 负责轮次锁
和历史维护）。回调返回 bool 表示本次是否真的派发了一轮。
"""

import time
from typing import Callable, Dict, Optional

from loguru import logger

from framework.brain import monitor
from framework.brain.drives import DriveParams, DriveSystem, default_mode_presets
from framework.brain.perception import Perception
from framework.conversation_state import ConversationState

_VALID_MODES = ("auto", "focus", "normal", "companion")


def _bucket(v: float, n: int) -> int:
    """把 [0,1] 连续值均匀离散成 0..n-1 共 n 档。"""
    if v <= 0.0:
        return 0
    if v >= 1.0:
        return n - 1
    return min(int(v * n), n - 1)


# 5 档自然语言映射（由弱到强）
_AROUSAL_LEVELS = [
    "有点困倦、没什么精神",
    "心情平静放松",
    "精神还不错",
    "挺来劲、很有精神",
    "非常兴奋、有点亢奋",
]
_SOCIAL_LEVELS = [
    "社交上很满足，独处也自在",
    "社交上挺满足",
    "有点想找人聊聊",
    "挺想找人说说话",
    "很孤单、特别渴望有人陪",
]
_URGE_LEVELS = [
    "",                       # 没什么特别想说的
    "",                       # 略有表达欲，无需点明
    "有点想开口",
    "挺有话想说",
    "很想插话、憋着一肚子话",
]


def describe_state(drive_state, effective_mode: str) -> str:
    """把内驱量翻译成一句简短的"此刻心情"自然语言（不含给 LLM 的指示）。"""
    arousal_text = _AROUSAL_LEVELS[_bucket(drive_state.arousal, len(_AROUSAL_LEVELS))]
    social_text = _SOCIAL_LEVELS[_bucket(drive_state.social_need, len(_SOCIAL_LEVELS))]
    urge_text = _URGE_LEVELS[_bucket(drive_state.expression_urge, len(_URGE_LEVELS))]
    parts = [arousal_text, social_text]
    if urge_text:
        parts.append(urge_text)
    return "，".join(parts)


def build_state_prompt(drive_state, effective_mode: str) -> str:
    """构造拼进 LLM 请求的"内部状态"system 片段（含"体现而非复述"的指示）。"""
    mood = describe_state(drive_state, effective_mode)
    mode_hint = {
        "focus": "（对方正在专注做事，请尽量简短、别打扰太多。）",
        "companion": "（现在是陪伴模式，可以更主动、多聊几句。）",
        "normal": "",
        "auto": "",
    }.get(effective_mode, "")
    return ("【你此刻的内部感受】" + mood + "。" + mode_hint
            + "\n请让这种状态自然地体现在你的语气和回应里，但不要直接复述或解释这些状态。")


class Brain:
    def __init__(self,
                 conv_state: ConversationState,
                 perception: Perception,
                 drive_system: DriveSystem,
                 reactive_cb: Callable[[], bool],
                 proactive_cb: Callable[[], bool],
                 mode: str = "auto",
                 mode_presets: Optional[Dict[str, DriveParams]] = None,
                 tick_interval: float = 0.15,
                 auto_focus_kb_hi: float = 0.6,
                 auto_companion_kb_lo: float = 0.05,
                 auto_companion_silence: float = 0.5,
                 hysteresis_ticks: int = 8,
                 enable_proactive: bool = True):
        self.conv_state = conv_state
        self.perception = perception
        self.drives = drive_system
        self._reactive_cb = reactive_cb
        self._proactive_cb = proactive_cb

        self._presets = mode_presets or default_mode_presets()
        self._mode = mode if mode in _VALID_MODES else "auto"
        self._tick_interval = tick_interval
        self._max_dt = 1.0  # dt 上限，防止某轮被 LLM 阻塞后内驱出现大跳变
        self.enable_proactive = enable_proactive

        # auto 模式的档位判定阈值 + 滞回
        self._auto_focus_kb_hi = auto_focus_kb_hi
        self._auto_companion_kb_lo = auto_companion_kb_lo
        self._auto_companion_silence = auto_companion_silence
        self._hysteresis_ticks = max(1, hysteresis_ticks)
        self._effective_mode = "normal"
        self._pending_mode_candidate = "normal"
        self._pending_mode_count = 0
        # 进入安静模式前的模式，用于 F9 再按一次时恢复。
        self._mode_before_quiet = "auto"

        # 防唠叨：不应期 + 连续发放计数 + 退避
        self._refractory_until = 0.0
        self._consecutive_fires = 0

        self._last_tick = time.monotonic()
        self._stop = False

    # ---- 模式控制（供热键 / 语音命令调用）-------------------------------
    def set_mode(self, mode: str) -> bool:
        """设置主动开口模式。设为具体档位 = 锁定（auto 感知暂停）；设为 auto = 解锁。"""
        if mode not in _VALID_MODES:
            logger.warning(f"Unknown proactivity mode: {mode}")
            return False
        self._mode = mode
        logger.info(f"Proactivity mode set to: {mode}")
        return True

    def cycle_mode(self) -> str:
        """在 auto → focus → normal → companion → auto 间循环（备用）。"""
        idx = _VALID_MODES.index(self._mode)
        self._mode = _VALID_MODES[(idx + 1) % len(_VALID_MODES)]
        logger.info(f"Proactivity mode cycled to: {self._mode}")
        return self._mode

    def toggle_quiet(self) -> str:
        """切换"安静模式"（供 F9）：

        - 当前不在 focus → 记住当前模式并切到 focus（安静，几乎不主动开口）；
        - 当前已在 focus → 恢复到进安静前的模式。
        返回切换后的模式名。
        """
        if self._mode == "focus":
            restore = self._mode_before_quiet or "auto"
            self._mode = restore if restore in _VALID_MODES else "auto"
            logger.info(f"Quiet mode OFF, restored to: {self._mode}")
        else:
            self._mode_before_quiet = self._mode
            self._mode = "focus"
            logger.info("Quiet mode ON (focus).")
        return self._mode

    @property
    def mode(self) -> str:
        return self._mode

    def state_prompt(self) -> str:
        """供 bot 拼进 LLM 请求的"内部状态"system 片段（瞬态，不入历史）。"""
        return build_state_prompt(self.drives.state, self._effective_mode)

    def describe_state(self) -> str:
        """简短的"此刻心情"自然语言（供面板/调试）。"""
        return describe_state(self.drives.state, self._effective_mode)

    # ---- 主循环 ----------------------------------------------------------
    def run(self, should_continue: Callable[[], bool]) -> None:
        logger.info("Brain loop started.")
        self._last_tick = time.monotonic()
        while (not self._stop) and should_continue():
            time.sleep(self._tick_interval)
            try:
                self.tick()
            except Exception as e:
                logger.exception(e)
        logger.info("Brain loop stopped.")

    def stop(self) -> None:
        self._stop = True

    # ---- 单步 ------------------------------------------------------------
    def tick(self) -> None:
        now = time.monotonic()
        dt = min(now - self._last_tick, self._max_dt)
        self._last_tick = now

        readings = self.perception.sample_all()
        signals = {ch: r.value for ch, r in readings.items()}

        busy = self.conv_state.is_ai_busy()
        user_speaking = signals.get("user_speaking", 0.0) >= 0.5
        gated = busy or user_speaking
        # 是否刚发生真实交流（用于回落社交需求）
        interaction = user_speaking or busy or self.conv_state.has_pending()

        effective_mode, params = self._resolve_mode(signals)

        wants_fire = self.drives.update(dt, signals, params, gated, interaction)

        action = "idle"
        if not gated:
            if self.conv_state.has_pending():
                # 最高优先级：回应用户。真实交流 → 复位防唠叨计数与社交需求。
                if self._reactive_cb():
                    action = "reactive"
                    self._consecutive_fires = 0
                    self.drives.on_user_interaction()
            elif self.enable_proactive and wants_fire and self._may_fire(now, params):
                if self._proactive_cb():
                    action = "proactive"
                    self.drives.on_fired()
                    self._consecutive_fires += 1
                    backoff = params.refractory_backoff ** max(0, self._consecutive_fires - 1)
                    self._refractory_until = now + params.refractory_s * backoff

        # 用户开口（reactive）以外，检测到用户正在说话也复位连续发放计数
        if user_speaking and self._consecutive_fires > 0:
            self._consecutive_fires = 0

        self._publish(now, signals, readings, effective_mode, params, action,
                      busy, user_speaking, gated)

    # ---- 内部 ------------------------------------------------------------
    def _may_fire(self, now: float, params: DriveParams) -> bool:
        if now < self._refractory_until:
            return False
        if self._consecutive_fires >= params.max_consecutive_fires:
            return False
        return True

    def _resolve_mode(self, signals: Dict[str, float]):
        if self._mode != "auto":
            self._effective_mode = self._mode
            return self._mode, self._presets[self._mode]

        # auto：根据键鼠活跃度 + 静默时长选档，带滞回防抖
        kb = signals.get("keyboard", 0.0)
        silence = signals.get("silence", 0.0)
        if kb >= self._auto_focus_kb_hi:
            candidate = "focus"
        elif kb <= self._auto_companion_kb_lo and silence >= self._auto_companion_silence:
            candidate = "companion"
        else:
            candidate = "normal"

        if candidate == self._pending_mode_candidate:
            self._pending_mode_count += 1
        else:
            self._pending_mode_candidate = candidate
            self._pending_mode_count = 1

        if (self._pending_mode_count >= self._hysteresis_ticks
                and candidate != self._effective_mode):
            self._effective_mode = candidate
            logger.info(f"Auto proactivity switched to: {candidate}")

        return self._effective_mode, self._presets[self._effective_mode]

    def _publish(self, now: float, signals: Dict[str, float], readings,
                 effective_mode: str, params: DriveParams, action: str,
                 busy: bool, user_speaking: bool, gated: bool) -> None:
        if user_speaking:
            fsm_state = "LISTENING"
        elif self.conv_state.is_thinking():
            fsm_state = "THINKING"
        elif busy:
            fsm_state = "SPEAKING"
        else:
            fsm_state = "IDLE"

        snapshot = {
            "t": round(now, 3),
            "fsm_state": fsm_state,
            "mode": self._mode,
            "effective_mode": effective_mode,
            "action": action,
            "ai_busy": busy,
            "user_speaking": user_speaking,
            "gated": gated,
            "drives": self.drives.snapshot(),
            "mood": describe_state(self.drives.state, effective_mode),
            "fire_threshold": params.fire_threshold,
            "signals": {ch: round(v, 4) for ch, v in signals.items()},
            "signal_online": {ch: r.online for ch, r in readings.items()},
            "idle_seconds": round(self.conv_state.idle_seconds(), 1),
            "refractory_remaining": round(max(0.0, self._refractory_until - now), 1),
            "consecutive_fires": self._consecutive_fires,
        }
        monitor.publish(snapshot)
