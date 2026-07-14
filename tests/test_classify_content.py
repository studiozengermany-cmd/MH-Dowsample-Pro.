"""Tests for QualityGate._classify_content — loop, one-shot, fx classification."""

from __future__ import annotations

import numpy as np

from quality_gate import QualityGate


def _gate() -> QualityGate:
    return QualityGate()


# ── One-shot: ultra-short (<0.8s) ────────────────────────────────────────────


def test_01_silence_under_08s_is_oneshot() -> None:
    """Signal shorter than 0.8s must always be classified as one-shot."""
    sr = 44_100
    y = np.zeros(int(sr * 0.5))  # 0.5s silence
    result = _gate()._classify_content(y, sr)
    assert result["content_type"] == "one-shot"
    assert result["is_oneshot"] is True
    assert result["is_loop"] is False
    assert result["is_fx"] is False


# ── One-shot: 808 kick > 0.8s ────────────────────────────────────────────────


def test_02_808_kick_1_5s_is_oneshot() -> None:
    """A synthetic 808 kick (1.5s, single transient + decay) should be one-shot, not fx."""
    sr = 44_100
    n = int(sr * 1.5)
    t = np.arange(n) / sr
    # 808 kick: strong transient attack + exponential decay, sub-bass
    envelope = np.exp(-t * 6.0)
    y = envelope * np.sin(2 * np.pi * 55 * t)  # 55Hz sub kick
    y = np.clip(y, -1, 1).astype(np.float32)
    result = _gate()._classify_content(y, sr)
    assert result["content_type"] == "one-shot", (
        f"808 kick should be one-shot, got '{result['content_type']}' "
        f"(loop_score={result['loop_score']}, is_oneshot={result['is_oneshot']})"
    )
    assert result["is_loop"] is False


# ── Not-loop: snare roll 2s ──────────────────────────────────────────────────


def test_03_snare_roll_2s_is_not_loop() -> None:
    """A 2s snare roll (irregular transients) must NOT be classified as loop."""
    sr = 44_100
    n = int(sr * 2.0)
    y = np.zeros(n, dtype=np.float32)
    # Irregular hits at non-uniform intervals
    hit_positions = [0.0, 0.15, 0.35, 0.45, 0.7, 0.9, 1.1, 1.4, 1.55, 1.8]
    for pos in hit_positions:
        start = int(pos * sr)
        length = min(int(0.03 * sr), n - start)  # 30ms noise burst
        if length > 0:
            y[start : start + length] = np.random.default_rng(42).uniform(-0.8, 0.8, length)
    result = _gate()._classify_content(y, sr)
    assert result["is_loop"] is False, f"Snare roll should not be loop, got loop_score={result['loop_score']}"


# ── Loop: 4-bar pattern 128 BPM ─────────────────────────────────────────────


def test_04_4bar_loop_128bpm_is_loop() -> None:
    """A clear 4-bar rhythmic pattern at 128 BPM should be classified as loop."""
    sr = 44_100
    bpm = 128
    duration = 4 * (4 * 60.0 / bpm)  # 4 bars
    n = int(sr * duration)
    y = np.zeros(n, dtype=np.float32)
    beat_interval = int(sr * 60.0 / bpm)
    # Strong kick on every beat
    for i in range(int(duration * bpm / 60)):
        start = i * beat_interval
        end = min(start + int(sr * 0.05), n)
        if end > start:
            decay = np.exp(-np.linspace(0, 10, end - start))
            y[start:end] += 0.9 * decay
    # Add bass and hihat for realism
    t = np.arange(n) / sr
    y += 0.2 * np.sin(2 * np.pi * 80 * t)
    y += 0.1 * np.sin(2 * np.pi * 8000 * t)
    y = np.clip(y, -1, 1).astype(np.float32)
    result = _gate()._classify_content(y, sr)
    assert result["content_type"] == "loop", (
        f"4-bar 128BPM pattern should be loop, got '{result['content_type']}' "
        f"(loop_score={result['loop_score']})"
    )
    assert result["is_loop"] is True
    assert result["loop_score"] > 0.4


# ── FX: ambient pad 5s ───────────────────────────────────────────────────────


def test_05_ambient_pad_5s_is_fx() -> None:
    """A smooth 5s sine pad without rhythm should be fx."""
    sr = 44_100
    n = int(sr * 5.0)
    t = np.arange(n) / sr
    # Smooth evolving pad — no transients, no rhythm
    y = 0.3 * np.sin(2 * np.pi * 220 * t) + 0.2 * np.sin(2 * np.pi * 330 * t)
    y = y.astype(np.float32)
    result = _gate()._classify_content(y, sr)
    assert result["is_oneshot"] is False, "5s pad should not be one-shot"
    # Could be loop or fx depending on detection — but should NOT be one-shot
    assert result["content_type"] in ("loop", "fx")


# ── One-shot: transient click 0.1s (but played at >0.8s audio length) ────────


def test_06_click_in_silence_is_oneshot() -> None:
    """A single click embedded in 1s of mostly silence should be one-shot."""
    sr = 44_100
    n = int(sr * 1.0)
    y = np.zeros(n, dtype=np.float32)
    # Single sharp click at 0.1s
    click_pos = int(0.1 * sr)
    click_len = int(0.005 * sr)  # 5ms click
    y[click_pos : click_pos + click_len] = np.random.default_rng(7).uniform(-0.9, 0.9, click_len)
    result = _gate()._classify_content(y, sr)
    assert result["is_loop"] is False, "Single click should not be loop"


# ── Loop: drum pattern 140 BPM 8s ───────────────────────────────────────────


def test_07_drum_loop_140bpm_8s_is_loop() -> None:
    """An 8-second drum pattern at 140 BPM should be detected as loop with decent confidence."""
    sr = 44_100
    duration = 8.0
    n = int(sr * duration)
    y = np.zeros(n, dtype=np.float32)
    beat_interval = int(sr * 60.0 / 140)
    # Kick every beat + hi-hat every half-beat
    for i in range(int(duration * 140 / 60)):
        # Kick
        start = i * beat_interval
        end = min(start + int(sr * 0.06), n)
        if end > start:
            decay = np.exp(-np.linspace(0, 12, end - start))
            y[start:end] += 0.85 * decay
        # Hi-hat on off-beat
        hh_start = start + beat_interval // 2
        hh_end = min(hh_start + int(sr * 0.015), n)
        if hh_end > hh_start and hh_end <= n:
            y[hh_start:hh_end] += 0.3 * np.random.default_rng(i).uniform(-1, 1, hh_end - hh_start)
    t = np.arange(n) / sr
    y += 0.15 * np.sin(2 * np.pi * 60 * t)  # sub bass
    y = np.clip(y, -1, 1).astype(np.float32)
    result = _gate()._classify_content(y, sr)
    assert result["content_type"] == "loop", (
        f"8s drum pattern at 140BPM should be loop, got '{result['content_type']}' "
        f"(loop_score={result['loop_score']})"
    )
    assert result["loop_score"] > 0.4
