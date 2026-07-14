"""Loss-aware conversion and WAV tagging on staged files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from mutagen.id3 import COMM, TBPM, TCON, TIT2, TKEY, TPE1
from mutagen.wave import WAVE
from pydub import AudioSegment, effects

from config import QUALITY
from exceptions import ConversionError, TaggingError

log = logging.getLogger(__name__)


class AudioProcessor:
    def __init__(self, target_sample_rate: int | None = None) -> None:
        self.target_sample_rate = target_sample_rate or int(QUALITY["target_sample_rate"])

    def to_wav(self, src: Path, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            audio = AudioSegment.from_file(src)
            audio = effects.normalize(audio, headroom=1.0)
            audio = audio.set_frame_rate(self.target_sample_rate).set_sample_width(2)
            audio.export(dst, format="wav", codec="pcm_s16le")
            return dst
        except Exception as exc:
            dst.unlink(missing_ok=True)
            raise ConversionError(f"Cannot convert {src}: {exc}") from exc

    def tag_wav(self, filepath: Path, meta: dict[str, Any], is_preview: bool) -> None:
        try:
            wav = WAVE(filepath)
            if wav.tags is None:
                wav.add_tags()
            tags = wav.tags
            if tags is None:
                raise TaggingError(f"Cannot create WAV tags for {filepath}")
            title = str(meta.get("title") or filepath.stem)
            bpm = int(meta.get("bpm") or 0)
            quality = "SOURCE_LOSSY_TRANSCODED" if is_preview else "SOURCE_PCM_PROCESSED"
            tags.setall("TIT2", [TIT2(encoding=3, text=title)])
            tags.setall("TPE1", [TPE1(encoding=3, text=str(meta.get("site") or "unknown"))])
            tags.setall("TBPM", [TBPM(encoding=3, text=str(bpm))])
            tags.setall("TKEY", [TKEY(encoding=3, text=str(meta.get("key") or "Unknown"))])
            tags.setall("TCON", [TCON(encoding=3, text=str(meta.get("genre_hint") or "other"))])
            tags.setall(
                "COMM",
                [
                    COMM(
                        encoding=3,
                        lang="eng",
                        desc="",
                        text=f"{quality}; content={meta.get('content_type', 'unknown')}; Audio Organizer 4.1",
                    )
                ],
            )
            wav.save()
        except Exception as exc:
            raise TaggingError(f"Cannot tag {filepath}: {exc}") from exc

    def process(self, src: Path, meta: dict[str, Any], staging_dir: Path) -> Path:
        staged = staging_dir / f"{src.stem}.processed.wav"
        counter = 1
        while staged.exists():
            staged = staging_dir / f"{src.stem}.processed-{counter}.wav"
            counter += 1
        self.to_wav(src, staged)
        try:
            self.tag_wav(staged, {**meta, "title": src.stem}, src.suffix.lower() != ".wav")
        except TaggingError as exc:
            log.warning("%s", exc)
        return staged
