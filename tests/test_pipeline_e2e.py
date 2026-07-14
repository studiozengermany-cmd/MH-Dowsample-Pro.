import shutil
from pathlib import Path

from organize import process_file
from organizer import Organizer
from processor import AudioProcessor
from quality_gate import QualityGate


def test_dry_run_changes_nothing(full_band_wav: Path, tmp_path: Path, monkeypatch) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    gate = QualityGate()
    monkeypatch.setattr(gate, "analyze", lambda _path: {"passed": True, "issues": []})
    try:
        result = process_file(
            full_band_wav, "local", gate, AudioProcessor(), organizer, tmp_path / "stage", dry_run=True
        )
        assert result["status"] == "would_pass"
        assert full_band_wav.exists()
        assert organizer.get_stats()["total"] == 0
        assert not any((tmp_path / "out").rglob("*.wav"))
    finally:
        organizer.db.close_all()


def test_full_pipeline_analyze_process_organize(full_band_wav: Path, tmp_path: Path, monkeypatch) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    gate = QualityGate()

    # Mock analyze so it passes quickly and returns standard analysis
    monkeypatch.setattr(
        gate,
        "analyze",
        lambda _path: {
            "passed": True,
            "issues": [],
            "content_type": "loop",
            "genre_hint": "house",
            "bpm": 128,
        },
    )

    try:
        result = process_file(
            full_band_wav, "local", gate, AudioProcessor(), organizer, tmp_path / "stage", dry_run=False
        )
        assert result["status"] == "passed"
        assert organizer.get_stats()["total"] == 1
        output_files = list((tmp_path / "out").rglob("*.wav"))
        assert len(output_files) == 1
        assert "House" in str(output_files[0])
    finally:
        organizer.db.close_all()


def test_rejected_file_not_in_output(full_band_wav: Path, tmp_path: Path, monkeypatch) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    gate = QualityGate()

    # Mock analyze to reject the file
    monkeypatch.setattr(gate, "analyze", lambda _path: {"passed": False, "issues": ["Bitrate too low"]})

    try:
        result = process_file(
            full_band_wav, "local", gate, AudioProcessor(), organizer, tmp_path / "stage", dry_run=False
        )
        assert result["status"] == "rejected"
        assert organizer.get_stats()["total"] == 0
        assert not any((tmp_path / "out").rglob("*.wav"))
    finally:
        organizer.db.close_all()


def test_duplicate_file_skipped(full_band_wav: Path, tmp_path: Path, monkeypatch) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    gate = QualityGate()
    monkeypatch.setattr(gate, "analyze", lambda _path: {"passed": True, "issues": [], "content_type": "fx"})

    try:
        duplicate_wav = tmp_path / "duplicate.wav"
        shutil.copy(full_band_wav, duplicate_wav)

        res1 = process_file(
            full_band_wav, "local", gate, AudioProcessor(), organizer, tmp_path / "stage", dry_run=False
        )
        assert res1["status"] == "passed"

        res2 = process_file(
            duplicate_wav, "local", gate, AudioProcessor(), organizer, tmp_path / "stage", dry_run=False
        )
        assert res2["status"] == "duplicate"

        assert organizer.get_stats()["total"] == 1
    finally:
        organizer.db.close_all()
