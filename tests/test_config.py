from pathlib import Path

import pytest

import config
from exceptions import ConfigError


def test_quality_defaults_are_valid() -> None:
    assert config.QUALITY["min_oneshot_duration_sec"] < config.QUALITY["min_duration_sec"]
    assert 0 <= config.QUALITY["max_silence_ratio"] <= 1


def test_delivery_defaults_are_safe_for_telegram() -> None:
    assert config.OWNER_DELIVERY_MODE in {"local", "telegram", "both"}
    assert 1 <= config.TELEGRAM_ARCHIVE_PART_MB <= 45
    assert config.TELEGRAM_UPLOAD_TIMEOUT_SEC >= 30
    assert config.TELEGRAM_UPLOAD_RETRIES >= 1


def test_integer_setting_honors_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_LIMIT", "46")
    with pytest.raises(ConfigError, match="<= 45"):
        config._env_int("TEST_LIMIT", 20, 1, 45)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_float_setting_rejects_non_finite_values(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("TEST_FLOAT", value)
    with pytest.raises(ConfigError, match="finite"):
        config._env_float("TEST_FLOAT", 1.0)


def test_browser_detection_prefers_the_windows_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = tmp_path / "browser.exe"
    browser.write_bytes(b"exe")
    monkeypatch.delenv("BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setattr(config, "_windows_default_browser", lambda: browser)

    assert config.find_browser_executable() == browser


def test_browser_detection_supports_coc_coc_as_a_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    browser = tmp_path / "CocCoc" / "Browser" / "Application" / "browser.exe"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"exe")
    monkeypatch.delenv("BROWSER_EXECUTABLE_PATH", raising=False)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "Program Files"))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "Program Files (x86)"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(config, "_windows_default_browser", lambda: None)

    assert config.find_browser_executable() == browser


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
