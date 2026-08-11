"""Environment-backed configuration without import-time filesystem effects."""

from __future__ import annotations

import math
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from exceptions import ConfigError

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent


def _env_int(name: str, default: int, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be >= {minimum}{suffix}")
    return value


def _env_float(name: str, default: float, minimum: float = 0.0, maximum: float | None = None) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ConfigError(f"{name} must be finite")
    if value < minimum or (maximum is not None and value > maximum):
        suffix = f" and <= {maximum}" if maximum is not None else ""
        raise ConfigError(f"{name} must be >= {minimum}{suffix}")
    return value


def _env_path(name: str, default: Path) -> Path:
    return Path(os.getenv(name) or default).expanduser()


DATA_DIR = _env_path("DATA_DIR", BASE_DIR / "data")
DOWNLOAD_DIR = _env_path("DOWNLOAD_DIR", BASE_DIR / "downloads")
OUTPUT_DIR = _env_path("OUTPUT_DIR", BASE_DIR / "organized")
TEMP_ROOT = _env_path("TEMP_DIR", Path(tempfile.gettempdir()) / "audio-organizer")
DB_PATH = _env_path("DB_PATH", DATA_DIR / "database.db")
REPORT_DIR = _env_path("REPORT_DIR", DATA_DIR / "reports")
BROWSER_PROFILE_DIR = _env_path("BROWSER_PROFILE_DIR", DATA_DIR / "browser-profile")

AUDIO_EXTS = frozenset({".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"})
QUALITY = {
    "min_bitrate_kbps": _env_int("MIN_BITRATE_KBPS", 96),
    "min_duration_sec": _env_float("MIN_DURATION_SEC", 1.0),
    "min_oneshot_duration_sec": _env_float("MIN_ONESHOT_DURATION_SEC", 0.1),
    "max_silence_ratio": _env_float("MAX_SILENCE_RATIO", 0.8, maximum=1.0),
    "max_file_mb": _env_int("CRAWL_MAX_FILE_MB", 0),
    "target_sample_rate": _env_int("TARGET_SAMPLE_RATE", 44_100, 8_000),
}
DEFAULT_WORKERS = _env_int("WORKERS", 4, 1)
DEFAULT_BATCH_SIZE = _env_int("BATCH_SIZE", 50, 1)
JOB_BATCH_FILES = _env_int("JOB_BATCH_FILES", 200, 1, 500)
FILE_PROCESS_TIMEOUT_SEC = _env_int("FILE_PROCESS_TIMEOUT_SEC", 45, 5, 600)
TELEGRAM_PROCESS_GUARD_SEC = _env_int(
    "TELEGRAM_PROCESS_GUARD_SEC",
    FILE_PROCESS_TIMEOUT_SEC + 15,
    FILE_PROCESS_TIMEOUT_SEC,
    900,
)
CRAWL_WAIT_SEC = _env_float("CRAWL_WAIT_SEC", 8.0)
CRAWL_TIMEOUT_SEC = _env_float("CRAWL_TIMEOUT_SEC", 300.0, 5.0)
CRAWL_LAUNCH_TIMEOUT_MS = _env_int("CRAWL_LAUNCH_TIMEOUT_MS", 15_000, 1_000)
LOGIN_TIMEOUT_SEC = _env_float("LOGIN_TIMEOUT_SEC", 900.0, 60.0)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_USER_ID = _env_int("ADMIN_USER_ID", 0)
OWNER_DELIVERY_MODE = os.getenv("OWNER_DELIVERY_MODE", "local").strip().lower()
if OWNER_DELIVERY_MODE not in {"local", "telegram", "both"}:
    raise ConfigError("OWNER_DELIVERY_MODE must be local, telegram, or both")
TELEGRAM_ARCHIVE_PART_MB = _env_int("TELEGRAM_ARCHIVE_PART_MB", 20, 1, 45)
TELEGRAM_ARCHIVE_PART_BYTES = TELEGRAM_ARCHIVE_PART_MB * 1024 * 1024
TELEGRAM_UPLOAD_TIMEOUT_SEC = _env_float("TELEGRAM_UPLOAD_TIMEOUT_SEC", 300.0, 30.0)
TELEGRAM_UPLOAD_RETRIES = _env_int("TELEGRAM_UPLOAD_RETRIES", 3, 1, 10)


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, DOWNLOAD_DIR, REPORT_DIR, OUTPUT_DIR, TEMP_ROOT, DB_PATH.parent):
        directory.mkdir(parents=True, exist_ok=True)


def configure_playwright_runtime() -> None:
    """Bypass a broken bundled Node launcher when a system Node is available."""
    if os.name != "nt" or os.getenv("PLAYWRIGHT_NODEJS_PATH"):
        return
    node_path = shutil.which("node")
    if node_path:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = node_path


def _windows_default_browser() -> Path | None:
    """Return the executable registered for HTTPS links when it is Chromium-based."""
    if sys.platform != "win32":
        return None
    try:
        import winreg

        user_choice = (
            r"Software\Microsoft\Windows\Shell\Associations"
            r"\UrlAssociations\https\UserChoice"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, user_choice) as key:
            prog_id = str(winreg.QueryValueEx(key, "ProgId")[0])
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command = str(winreg.QueryValueEx(key, "")[0])
    except (ImportError, OSError):
        return None

    match = re.match(r'^\s*(?:"([^"]+\.exe)"|([^\s]+\.exe))', command, re.IGNORECASE)
    if not match:
        return None
    path = Path(match.group(1) or match.group(2)).expanduser()
    supported_names = {"chrome.exe", "browser.exe", "brave.exe", "msedge.exe"}
    return path if path.is_file() and path.name.lower() in supported_names else None


def find_browser_executable() -> Path | None:
    configured = os.getenv("BROWSER_EXECUTABLE_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    if os.name != "nt":
        return None
    default_browser = _windows_default_browser()
    if default_browser:
        return default_browser
    program_files = Path(os.getenv("PROGRAMFILES", "C:/Program Files"))
    program_files_x86 = Path(os.getenv("PROGRAMFILES(X86)", "C:/Program Files (x86)"))
    local_app_data = Path(os.getenv("LOCALAPPDATA", "~/AppData/Local")).expanduser()
    candidates = (
        program_files / "Google/Chrome/Application/chrome.exe",
        program_files_x86 / "Google/Chrome/Application/chrome.exe",
        local_app_data / "Google/Chrome/Application/chrome.exe",
        local_app_data / "CocCoc/Browser/Application/browser.exe",
        program_files / "BraveSoftware/Brave-Browser/Application/brave.exe",
        local_app_data / "BraveSoftware/Brave-Browser/Application/brave.exe",
        program_files / "Microsoft/Edge/Application/msedge.exe",
        program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
    )
    return next((path for path in candidates if path.is_file()), None)


def validate_audio_tools() -> None:
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise ConfigError(f"Missing required audio tools: {', '.join(missing)}")


def validate_bot_config() -> None:
    if not TELEGRAM_TOKEN:
        raise ConfigError("TELEGRAM_TOKEN is required")
    if ADMIN_USER_ID <= 0:
        raise ConfigError("ADMIN_USER_ID must be a positive Telegram user ID")
