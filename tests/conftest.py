from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def full_band_wav(tmp_path: Path) -> Path:
    sr = 44_100
    t = np.arange(sr * 2) / sr
    y = (
        0.25 * np.sin(2 * np.pi * 120 * t)
        + 0.35 * np.sin(2 * np.pi * 440 * t)
        + 0.08 * np.sin(2 * np.pi * 8000 * t)
    )
    path = tmp_path / "clean.wav"
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sr)
        output.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    return path
