"""Human-readable paths for the local sample library."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from utils.paths import sanitize_component, sanitize_filename

_OPAQUE_STEM = re.compile(r"(?:[0-9a-f]{24,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})", re.IGNORECASE)
_LEGACY_NAME = re.compile(r"^(?:\[[^]]+\]){3}_(.+)$")
_KEY = re.compile(r"^([A-Ga-g])([#sb]?)(maj|min)$", re.IGNORECASE)


def display_label(value: str, fallback: str = "Other") -> str:
    """Turn a machine slug into a folder/file label a producer can scan."""
    text = re.sub(r"[_-]+", " ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not text or text.lower() in {"unknown", "other", "none"}:
        return fallback
    words = (
        word.upper() if word.lower() in {"fx", "edm", "rnb", "dnb"} else word.capitalize()
        for word in text.split()
    )
    return " ".join(words)


def content_label(content_type: str) -> str:
    return {
        "loop": "Loops",
        "one-shot": "One-Shots",
        "fx": "FX",
    }.get(str(content_type or "").lower(), "Unsorted")


def library_parts(analysis: dict[str, Any]) -> tuple[str, ...]:
    """Content is the primary browse axis; genre is meaningful for loops only."""
    content = str(analysis.get("content_type") or "unknown").lower()
    category = sanitize_component(content_label(content))
    if content != "loop":
        return (category,)
    genre_value = analysis.get("genre_hint") or analysis.get("genre") or "other"
    genre = sanitize_component(display_label(str(genre_value)))
    return category, genre


def source_name_from_legacy(filename: str) -> str:
    stem = Path(filename).stem
    match = _LEGACY_NAME.match(stem)
    return match.group(1) if match else stem


def is_opaque_name(value: str) -> bool:
    stem = source_name_from_legacy(Path(value).name)
    compact = re.sub(r"[^0-9a-f]", "", stem, flags=re.IGNORECASE)
    return bool(_OPAQUE_STEM.fullmatch(stem)) or (len(compact) >= 24 and compact.lower() == stem.lower())


def _clean_source_label(source_name: str) -> str:
    stem = source_name_from_legacy(Path(source_name).name)
    text = re.sub(r"[_-]+", " ", stem)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return text


def _key_label(raw: object) -> str | None:
    value = str(raw or "").strip()
    if not value or value.lower() == "unknown":
        return None
    match = _KEY.match(value)
    if not match:
        return value.replace("s", "#", 1) if len(value) > 1 else value
    note, accidental, mode = match.groups()
    accidental = (
        " sharp" if accidental.lower() in {"s", "#"} else " flat" if accidental.lower() == "b" else ""
    )
    return f"{note.upper()}{accidental} {'major' if mode.lower() == 'maj' else 'minor'}"


def friendly_filename(
    source_name: str,
    analysis: dict[str, Any],
    source_hash: str,
    *,
    extension: str = ".wav",
) -> str:
    """Build a readable, collision-resistant name without exposing a full CDN hash."""
    content = str(analysis.get("content_type") or "unknown").lower()
    readable_source = not is_opaque_name(source_name)
    if readable_source:
        lead = _clean_source_label(source_name)
    elif content == "loop":
        genre = analysis.get("genre_hint") or analysis.get("genre") or "other"
        lead = f"{display_label(str(genre))} Loop"
    elif content == "one-shot":
        lead = "One Shot"
    elif content == "fx":
        lead = "FX"
    else:
        lead = "Audio"

    details: list[str] = []
    bpm = int(analysis.get("bpm") or 0)
    if bpm:
        prefix = "~" if str(analysis.get("bpm_confidence") or "none") == "low" else ""
        details.append(f"{prefix}{bpm} BPM")
    key = _key_label(analysis.get("key"))
    if key:
        details.append(key)
    if not readable_source:
        details.append((source_hash or "unknown")[:8])

    suffix = extension if extension.startswith(".") else f".{extension}"
    filename = " - ".join([lead, *details]) + suffix.lower()
    return sanitize_filename(filename, fallback=f"Audio - {(source_hash or 'unknown')[:8]}{suffix.lower()}")
