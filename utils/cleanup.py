"""Run-scoped temporary directory cleanup."""

from __future__ import annotations

import atexit
import shutil
import signal
import tempfile
import threading
from pathlib import Path

_lock = threading.Lock()
_owned: set[Path] = set()
_registered = False


def _remove(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def cleanup_all() -> None:
    with _lock:
        paths = list(_owned)
        _owned.clear()
    for path in paths:
        _remove(path)


def _signal_handler(signum: int, _frame: object) -> None:
    cleanup_all()
    raise SystemExit(128 + signum)


def setup_cleanup(temp_root: Path) -> Path:
    global _registered
    temp_root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="run-", dir=temp_root)).resolve()
    with _lock:
        _owned.add(run_dir)
        if not _registered:
            atexit.register(cleanup_all)
            if threading.current_thread() is threading.main_thread():
                for name in ("SIGINT", "SIGTERM"):
                    sig = getattr(signal, name, None)
                    if sig is not None:
                        signal.signal(sig, _signal_handler)
            _registered = True
    return run_dir


def cleanup_run(run_dir: Path) -> None:
    resolved = run_dir.resolve()
    with _lock:
        if resolved not in _owned:
            return
        _owned.remove(resolved)
    _remove(resolved)
