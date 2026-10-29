"""抗噪门（NoiseGate）单元测试：用合成音频验证三类证据的判定方向。

不依赖麦克风、不依赖配置文件，用例全部是数学构造的信号。
运行：pytest tests/devices/test_noise_gate.py -q
"""

import numpy as np
import pytest

from config import NoiseGateConfig
from devices.noise_gate import NoiseGate, rms_to_dbfs


SR = 16000
N = 480  # 30ms


def _tone(freq: float, amp: float = 3000.0, n: int = N) -> bytes:
    t = np.arange(n, dtype=np.float32) / SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16).tobytes()


def _harmonics(amp: float = 2500.0, n: int = N, f0: float = 180.0) -> bytes:
    """类人声：基频 + 谐波，带 -6dB/oct 的频谱倾斜和 700/1800Hz 两个共振峰。

    这样能量集中在 200–3800Hz、ZCR 低、平坦度低，与真实语音（实测 zcr≈0.06~0.17、
    flat≈0.01、band≈0.97）接近，而不是一条过于明亮的锯齿波。
    """
    t = np.arange(n, dtype=np.float32) / SR
    wave = np.zeros(n, dtype=np.float32)
    for f in np.arange(f0, min(3800.0, SR / 2), f0):
        tilt = 1.0 / (1.0 + (f / 800.0) ** 2)
        formant = (1.0
                   + 1.5 * np.exp(-((f - 700.0) / 300.0) ** 2)
                   + 0.8 * np.exp(-((f - 1800.0) / 500.0) ** 2))
        wave += tilt * formant * np.sin(2 * np.pi * f * t)
    wave *= 0.9 + 0.1 * np.sin(2 * np.pi * 4 * t)
    wave = wave / np.max(np.abs(wave)) * amp
    return wave.astype(np.int16).tobytes()


def _white_noise(amp: float = 3000.0, seed: int = 0, n: int = N) -> bytes:
    rng = np.random.default_rng(seed)
    x = rng.normal(0, 1, n)
    x = x / np.max(np.abs(x)) * amp
    return x.astype(np.int16).tobytes()


def _keyboard_click(seed: int = 0, n: int = N) -> bytes:
    """键盘/鼠标点击：极短冲击 + 指数衰减的宽带噪声。"""
    rng = np.random.default_rng(seed)
    t = np.arange(n, dtype=np.float32)
    env = np.exp(-t / 20.0)
    click = rng.normal(0, 1, n) * env
    click[:2] += 6.0  # 冲击前沿
    click = click / np.max(np.abs(click)) * 3000.0
    return click.astype(np.int16).tobytes()


def _silence(n: int = N) -> bytes:
    return np.zeros(n, dtype=np.int16).tobytes()


@pytest.fixture()
def gate() -> NoiseGate:
    return NoiseGate(NoiseGateConfig())


# ---------------------------------------------------------------- 基础特征
def test_silence_is_never_speech(gate):
    f = gate.features(_silence())
    assert not gate.energy_ok(f.rms)
    assert not gate.barge_in_frame_ok(f, speech_prob=1.0)


def test_voiced_like_frame_passes_both_gates(gate):
    for f0 in (110.0, 180.0, 220.0):  # 男声/中性/女声基频
        f = gate.features(_harmonics(f0=f0))
        assert gate.energy_ok(f.rms)
        assert f.zcr < 0.22, f"f0={f0} zcr={f.zcr}"
        assert f.flatness < 0.45, f"f0={f0} flatness={f.flatness}"
        assert f.band_ratio > 0.60, f"f0={f0} band={f.band_ratio}"
        assert gate.barge_in_frame_ok(f, speech_prob=0.9)


def test_keyboard_click_rejected_by_frame_gate(gate):
    rejected = 0
    for seed in range(10):
        f = gate.features(_keyboard_click(seed=seed))
        if not gate.barge_in_frame_ok(f, speech_prob=0.9):
            rejected += 1
    # 点击是宽带瞬态，绝大多数应被帧级频谱门挡住
    assert rejected >= 8, f"only {rejected}/10 clicks rejected"


def test_white_noise_rejected_by_utterance_score(gate):
    feats = [gate.features(_white_noise(seed=i)) for i in range(40)]
    ok, reason, stats = gate.score(feats, span_frames=40)
    assert not ok
    assert reason in {"zcr", "flatness", "band_ratio", "low_density"}, reason


# ---------------------------------------------------------------- 句级评分
def test_voiced_utterance_accepted(gate):
    feats = [gate.features(_harmonics()) for _ in range(50)]
    ok, reason, stats = gate.score(feats, span_frames=52)
    assert ok, (reason, stats.format())
    assert stats.duration_ms == 1500
    assert 0.9 < stats.speech_ratio <= 1.0


def test_low_density_rejected(gate):
    """语音帧稀疏地散落在很长的时间跨度里 = 典型的"边打字边零星触发"。"""
    feats = [gate.features(_harmonics()) for _ in range(40)]
    ok, reason, _ = gate.score(feats, span_frames=200)
    assert not ok and reason == "low_density"


def test_short_noise_burst_not_accepted(gate):
    feats = [gate.features(_keyboard_click(seed=i)) for i in range(40)]
    ok, _, _ = gate.score(feats, span_frames=40)
    assert not ok


# ---------------------------------------------------------------- 能量门 / 底噪
def test_noise_floor_adapts_and_raises_gate(gate):
    # 冷启动：未学到底噪时只受绝对门约束
    assert gate.energy_ok(400)
    for _ in range(120):
        gate.observe_noise(220.0)
    assert gate.noise_floor == pytest.approx(220.0, rel=0.05)
    # 底噪 220 → 门限约 550：轻微超过绝对门但没超过信噪比门的声音应被拒
    assert not gate.energy_ok(450)
    assert gate.energy_ok(900)


def test_noise_floor_ignores_loud_transients(gate):
    for _ in range(120):
        gate.observe_noise(200.0)
    floor_before = gate.noise_floor
    for _ in range(20):
        gate.observe_noise(20000.0)  # 关门/拍桌子级别的瞬态
    assert gate.noise_floor == pytest.approx(floor_before, rel=0.01)


def test_floor_clamped_to_max(gate):
    for _ in range(200):
        gate.observe_noise(5000.0)
    assert gate.noise_floor is not None
    assert gate.noise_floor <= NoiseGateConfig().noise_floor_max


# ---------------------------------------------------------------- 打断
def test_barge_in_requires_aec_probability(gate):
    f = gate.features(_harmonics())
    assert gate.barge_in_frame_ok(f, speech_prob=0.9)
    assert not gate.barge_in_frame_ok(f, speech_prob=0.2)


def test_barge_in_disabled_spectral_still_checks_energy():
    cfg = NoiseGateConfig(barge_in_frame_check=False)
    g = NoiseGate(cfg)
    f = g.features(_white_noise())
    assert g.barge_in_frame_ok(f, speech_prob=0.9)  # 关了频谱门就只看能量


def test_dbfs_math():
    assert rms_to_dbfs(32768) == pytest.approx(0.0, abs=1e-6)
    assert rms_to_dbfs(3276.8) == pytest.approx(-20.0, abs=0.01)
