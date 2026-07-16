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
    monkeypatch.setattr(gate, "_get_bitrate", lambda _path: 320)
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
