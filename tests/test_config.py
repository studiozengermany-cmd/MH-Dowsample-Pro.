from pathlib import Path

import pytest

import config
from exceptions import ConfigError


def test_quality_defaults_are_valid() -> None:
    assert config.QUALITY["min_oneshot_duration_sec"] < config.QUALITY["min_duration_sec"]
    assert 0 <= config.QUALITY["max_silence_ratio"] <= 1


def test_runtime_dirs_are_created(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    paths = [tmp_path / name for name in ("data", "reports", "output", "temp")]
    monkeypatch.setattr(config, "DATA_DIR", paths[0])
    monkeypatch.setattr(config, "REPORT_DIR", paths[1])
    monkeypatch.setattr(config, "OUTPUT_DIR", paths[2])
    monkeypatch.setattr(config, "TEMP_ROOT", paths[3])
    monkeypatch.setattr(config, "DB_PATH", paths[0] / "db.sqlite")
    config.ensure_runtime_dirs()
    assert all(path.is_dir() for path in paths)


def test_audio_tools_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config.shutil, "which", lambda _name: None)
    with pytest.raises(ConfigError):
        config.validate_audio_tools()
