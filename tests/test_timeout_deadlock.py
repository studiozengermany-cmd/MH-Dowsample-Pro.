import asyncio
import multiprocessing
import time
from pathlib import Path

import organize
from organize import run_pipeline


def _controlled_worker(path, _site, _staging_dir, _dry_run, conn):
    try:
        if "hang" in str(path):
            time.sleep(10)
        else:
            conn.send({"status": "error", "error": "controlled fast failure"})
    finally:
        conn.close()


def test_worker_timeout_and_batch_continues(tmp_path, monkeypatch):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    
    # Create two fake audio files
    file1 = input_dir / "hang.mp3"
    file1.write_text("fake audio data")
    file2 = input_dir / "fast.mp3"
    file2.write_text("fake audio data")

    monkeypatch.setattr(organize, "_worker_process_file", _controlled_worker)

    # Run pipeline with a 2-second timeout and two concurrent workers.
    counts = asyncio.run(
        run_pipeline(
            input_dir,
            output_dir,
            timeout_sec=2,
            workers=2,
            batch_size=2,
        )
    )

    # The hang file should timeout
    assert counts["file_timeout"] == 1
    # The fast file should fail cleanly
    assert (counts["rejected"] + counts["error"]) == 1
    
    # Ensure no processes are left alive
    active_children = multiprocessing.active_children()
    assert len(active_children) == 0, f"Found leaked processes: {active_children}"
    print("Test passed: Worker was killed, next file continued, batch finished.")

if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_path:
        test_worker_timeout_and_batch_continues(Path(tmp_path))
