import wave
from pathlib import Path

import pytest

from exceptions import ConversionError
from processor import AudioProcessor


def test_processor_preserves_source(full_band_wav: Path, tmp_path: Path) -> None:
    staged = AudioProcessor().process(
        full_band_wav, {"content_type": "fx", "genre_hint": "fx"}, tmp_path / "staging"
    )
    assert full_band_wav.exists()
    assert staged.exists()
    assert staged != full_band_wav


def test_to_wav_corrupt_file_raises_conversion_error(tmp_path: Path) -> None:
    """A corrupt/empty file should raise ConversionError, not an unhandled exception."""
    corrupt = tmp_path / "corrupt.mp3"
    corrupt.write_bytes(b"not audio data at all")
    dst = tmp_path / "output.wav"
    with pytest.raises(ConversionError):
        AudioProcessor().to_wav(corrupt, dst)
    # dst should be cleaned up on failure
    assert not dst.exists()


def test_to_wav_missing_file_raises_conversion_error(tmp_path: Path) -> None:
    """A non-existent source file should raise ConversionError."""
    missing = tmp_path / "does_not_exist.wav"
    dst = tmp_path / "output.wav"
    with pytest.raises(ConversionError):
        AudioProcessor().to_wav(missing, dst)


def test_tag_wav_with_full_metadata(full_band_wav: Path, tmp_path: Path) -> None:
    """All ID3 tags should be written when full metadata is provided."""
    from mutagen.wave import WAVE

    staged = tmp_path / "staging"
    processor = AudioProcessor()
    output = processor.process(
        full_band_wav,
        {
            "content_type": "loop",
            "genre_hint": "house",
            "bpm": 128,
            "bpm_confidence": "high",
            "key": "Cmaj",
            "site": "splice",
        },
        staged,
    )
    wav = WAVE(output)
    assert wav.tags is not None
    # Title tag exists
    assert wav.tags.getall("TIT2")
    # Artist/site tag exists
    tpe1 = wav.tags.getall("TPE1")
    assert tpe1 and "splice" in str(tpe1[0])
    # BPM tag
    tbpm = wav.tags.getall("TBPM")
    assert tbpm and "128" in str(tbpm[0])
    # Key tag
    tkey = wav.tags.getall("TKEY")
    assert tkey and "Cmaj" in str(tkey[0])


def test_tag_wav_missing_optional_fields(full_band_wav: Path, tmp_path: Path) -> None:
    """Tagging should succeed even when optional metadata fields are missing."""
    staged = tmp_path / "staging"
    processor = AudioProcessor()
    # Minimal metadata — no bpm, no key, no site
    output = processor.process(full_band_wav, {"content_type": "unknown"}, staged)
    assert output.exists()


def test_process_sets_correct_sample_rate(full_band_wav: Path, tmp_path: Path) -> None:
    """Processed WAV should have the target sample rate."""
    staged = tmp_path / "staging"
    target_sr = 44_100
    processor = AudioProcessor(target_sample_rate=target_sr)
    output = processor.process(full_band_wav, {"content_type": "fx"}, staged)
    with wave.open(str(output), "rb") as wav:
        assert wav.getframerate() == target_sr
