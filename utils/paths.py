"""Path validation and conservative name sanitization."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from exceptions import PathTraversalError

_UNSAFE = re.compile(r"[^\w.()\[\] -]+", re.UNICODE)


def sanitize_component(value: str, fallback: str = "unknown", max_length: int = 100) -> str:
    if not value or "\x00" in value:
        raise PathTraversalError("Empty or NUL-containing path component")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise PathTraversalError(f"Absolute paths are not allowed: {value!r}")
    if any(part == ".." for part in (*posix.parts, *windows.parts)) or "/" in value or "\\" in value:
        raise PathTraversalError(f"Path separators are not allowed: {value!r}")
    cleaned = _UNSAFE.sub("_", value).strip(" ._")[:max_length].rstrip(" .")
    return cleaned or fallback


def sanitize_filename(value: str, fallback: str = "download.bin", max_length: int = 200) -> str:
    cleaned = sanitize_component(value, fallback=fallback, max_length=max_length)
    if cleaned in {".", ".."}:
        raise PathTraversalError("Invalid filename")
    return cleaned


def safe_child(root: Path, *components: str) -> Path:
    root_resolved = root.resolve()
    candidate = root.joinpath(*components).resolve()

    # Strip Windows extended-length path prefix (\\?\) for comparison
    root_cmp = Path(str(root_resolved).removeprefix("\\\\?\\"))
    cand_cmp = Path(str(candidate).removeprefix("\\\\?\\"))

    if not cand_cmp.is_relative_to(root_cmp):
        raise PathTraversalError(f"Destination escapes root: {candidate}")
    return candidate
