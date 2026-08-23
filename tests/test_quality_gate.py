from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from quality_gate import QualityGate


@pytest.fixture(autouse=True)
def _use_mock_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    def request(client, method, url, **kwargs):
        kwargs.pop("validator", None)
        return getattr(client, method.lower())(url, **kwargs)

    monkeypatch.setattr("quality_gate.request_with_safe_redirects", request)


def test_pcm_wav_bitrate_is_calculated(full_band_wav: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "quality_gate.subprocess.run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError())
    )
    assert QualityGate()._get_bitrate(full_band_wav) == 706


def test_short_signal_is_one_shot() -> None:
    result = QualityGate()._classify_content(np.ones(1000), 44_100)
    assert result["content_type"] == "one-shot"
    assert sum(result[key] for key in ("is_loop", "is_oneshot", "is_fx")) == 1


def test_analyze_returns_stable_contract(full_band_wav: Path, monkeypatch) -> None:
    gate = QualityGate()
    monkeypatch.setattr(gate, "_get_bitrate", lambda _path, *args, **kwargs: 320)
    result = gate.analyze(full_band_wav)
    expected = {
        "passed",
        "bitrate_kbps",
        "duration_sec",
        "silence_ratio",
        "sample_rate",
        "channels",
        "rms_db",
        "spectral_centroid_hz",
        "bpm",
        "bpm_confidence",
        "key",
        "genre_hint",
        "content_type",
        "issues",
        "warnings",
    }
    assert set(result) == expected
    assert result["bitrate_kbps"] == 320


def test_pre_download_rejects_non_audio() -> None:
    response = SimpleNamespace(status_code=200, headers={"Content-Type": "text/html"}, close=lambda: None)
    session = SimpleNamespace(head=lambda *args, **kwargs: response)
    assert QualityGate().pre_download_ok("https://example.com", session)[0] is False


def test_pre_download_allows_unknown_binary_type_for_post_download_analysis() -> None:
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "application/x-download"},
        close=lambda: None,
    )
    session = SimpleNamespace(head=lambda *args, **kwargs: response)
    assert QualityGate().pre_download_ok("https://example.com/file", session) == (True, "ok")


def test_audio_suffix_rechecks_with_get_when_head_lies_about_content_type() -> None:
    head_response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "text/html"},
        close=lambda: None,
    )
    get_response = SimpleNamespace(
        status_code=206,
        headers={"Content-Type": "audio/mpeg", "Content-Range": "bytes 0-0/4096"},
        close=lambda: None,
    )
    session = SimpleNamespace(
        head=lambda *args, **kwargs: head_response,
        get=lambda *args, **kwargs: get_response,
    )
    assert QualityGate().pre_download_ok("https://example.com/sample.mp3", session) == (True, "ok")


def test_pre_download_falls_back_to_ranged_get_when_signed_url_rejects_head() -> None:
    head_response = SimpleNamespace(status_code=403, headers={}, close=lambda: None)
    get_response = SimpleNamespace(
        status_code=206,
        headers={
            "Content-Type": "audio/mp3",
            "Content-Length": "1",
            "Content-Range": "bytes 0-0/504220",
        },
        close=lambda: None,
    )
    session = SimpleNamespace(
        head=lambda *args, **kwargs: head_response,
        get=lambda *args, **kwargs: get_response,
    )

    assert QualityGate().pre_download_ok("https://cdn.example/audio.mp3", session) == (True, "ok")


def test_frequency_ratios_warn_but_do_not_fail_passed(full_band_wav: Path, monkeypatch) -> None:
    gate = QualityGate()
    # Mock _get_bitrate to pass
    monkeypatch.setattr(gate, "_get_bitrate", lambda _path, *args, **kwargs: 320)
    # Mock mono audio to be a pure 100Hz sine wave (No high-freq content)
    # 44100 Hz sample rate, 1 second duration
    t = np.linspace(0, 1.0, 44100, endpoint=False)
    pure_bass = np.sin(2 * np.pi * 100 * t) # 100 Hz (Bass only)
    
    # Mock _load_audio to return this pure bass
    monkeypatch.setattr(gate, "_load_audio", lambda _path: (pure_bass, 44100))
    
    result = gate.analyze(full_band_wav)
    assert "No high-freq content" in result["warnings"]
    assert "No high-freq content" not in result["issues"]
    assert result["passed"] is True
