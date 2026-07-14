from datetime import UTC, datetime
from pathlib import Path

import pytest

from exceptions import DuplicateFileError
from organizer import Organizer


def analysis() -> dict:
    return {
        "genre_hint": "house",
        "content_type": "loop",
        "bpm": 128,
        "bpm_confidence": "high",
        "key": "C#min",
        "bitrate_kbps": 320,
        "duration_sec": 4,
        "silence_ratio": 0,
    }


def test_organize_and_duplicate(full_band_wav: Path, tmp_path: Path) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    try:
        digest = organizer.hash_file(full_band_wav)
        target = organizer.organize(full_band_wav, "local", analysis(), digest)
        assert target.exists()
        assert target.parent.parts[-2:] == ("Loops", "House")
        assert target.name == "clean - 128 BPM - C sharp minor.wav"
        assert organizer.is_duplicate(digest)
        with pytest.raises(DuplicateFileError):
            organizer.organize(full_band_wav, "local", analysis(), digest)
        assert organizer.get_stats()["total"] == 1
    finally:
        organizer.db.close_all()


def test_migrate_legacy_layout_and_raw_is_idempotent(tmp_path: Path) -> None:
    output = tmp_path / "organized"
    raw_root = tmp_path / "downloads"
    legacy_output = output / "splice" / "house" / "placeholder.wav"
    legacy_output.parent.mkdir(parents=True)
    legacy_output.write_bytes(b"same audio")
    digest = Organizer.hash_file(legacy_output)
    legacy_output_with_hash = legacy_output.with_name(f"[128BPM][Cmin][loop]_{digest}.wav")
    legacy_output.rename(legacy_output_with_hash)

    raw = raw_root / "splice" / f"{digest}.mp3"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"same audio")
    organizer = Organizer(output, tmp_path / "db.sqlite", pool_size=1)
    try:
        with organizer.db.connection(transaction=True) as conn:
            conn.execute(
                """INSERT INTO files
                (filepath, site, genre, content_type, bpm, bpm_confidence, key,
                 bitrate_kbps, duration_sec, silence_ratio, file_hash, processed_at)
                VALUES (?, 'splice', 'house', 'loop', 128, 'high', 'Cmin',
                        320, 4, 0, ?, ?)""",
                (str(legacy_output_with_hash), digest, datetime.now(UTC).isoformat()),
            )

        first = organizer.migrate_layout(raw_root)
        automatic = organizer.ensure_layout(raw_root)
        second = organizer.migrate_layout(raw_root)

        assert first == {"organized": 1, "raw": 1, "missing": 0}
        assert automatic == {"organized": 0, "raw": 0, "missing": 0}
        assert second == {"organized": 0, "raw": 0, "missing": 0}
        stored = organizer.metadata_for_hash(digest)
        assert stored is not None
        migrated = Path(str(stored["filepath"]))
        assert migrated.parent.parts[-2:] == ("Loops", "House")
        assert migrated.name == f"House Loop - 128 BPM - C minor - {digest[:8]}.wav"
        assert next(raw_root.rglob("*.mp3")).parent.parts[-3:] == ("splice", "Loops", "House")
    finally:
        organizer.db.close_all()
