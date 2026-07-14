from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from exceptions import DuplicateFileError
from organizer import Organizer


def test_concurrent_organize_no_duplicates(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    db_path = tmp_path / "db.sqlite"
    organizer = Organizer(out_dir, db_path, pool_size=4)

    # Create 4 unique files
    files = []
    for i in range(4):
        f = tmp_path / f"source_{i}.wav"
        f.write_bytes(f"data_{i}".encode())
        files.append(f)

    analysis = {
        "genre_hint": "house",
        "content_type": "loop",
        "bpm": 128,
        "bpm_confidence": "high",
        "key": "Cmin",
        "bitrate_kbps": 320,
        "duration_sec": 4,
        "silence_ratio": 0,
    }

    def worker(f_path):
        digest = organizer.hash_file(f_path)
        return organizer.organize(f_path, "local", analysis, digest)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(worker, files))

        assert len(results) == 4
        for r in results:
            assert r.exists()
        assert organizer.get_stats()["total"] == 4
    finally:
        organizer.db.close_all()


def test_concurrent_same_hash_raises_duplicate(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    db_path = tmp_path / "db.sqlite"
    organizer = Organizer(out_dir, db_path, pool_size=4)

    source = tmp_path / "source.wav"
    source.write_bytes(b"duplicate_data")

    analysis = {
        "genre_hint": "house",
        "content_type": "loop",
        "bpm": 128,
        "bpm_confidence": "high",
        "key": "Cmin",
        "bitrate_kbps": 320,
        "duration_sec": 4,
        "silence_ratio": 0,
    }
    digest = organizer.hash_file(source)

    def worker(_):
        return organizer.organize(source, "local", analysis, digest)

    try:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker, i) for i in range(4)]
            successes = 0
            duplicates = 0
            for future in futures:
                try:
                    future.result()
                    successes += 1
                except DuplicateFileError:
                    duplicates += 1

        assert successes == 1
        assert duplicates == 3
        assert organizer.get_stats()["total"] == 1
    finally:
        organizer.db.close_all()


def test_pool_exhaustion_timeout(tmp_path: Path) -> None:
    out_dir = tmp_path / "out"
    db_path = tmp_path / "db.sqlite"
    organizer = Organizer(out_dir, db_path, pool_size=1)

    files = []
    for i in range(2):
        f = tmp_path / f"exhaust_{i}.wav"
        f.write_bytes(f"data_exhaust_{i}".encode())
        files.append(f)

    analysis = {"genre_hint": "house", "content_type": "loop"}

    def worker(f_path):
        digest = organizer.hash_file(f_path)
        return organizer.organize(f_path, "local", analysis, digest)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(worker, files))
        assert len(results) == 2
        assert organizer.get_stats()["total"] == 2
    finally:
        organizer.db.close_all()
