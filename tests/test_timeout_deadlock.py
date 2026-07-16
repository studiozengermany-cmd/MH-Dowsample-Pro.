import asyncio
import multiprocessing
import os
from pathlib import Path

from organize import run_pipeline


def test_worker_timeout_and_batch_continues(tmp_path):
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    
    # Create two fake audio files
    file1 = input_dir / "hang.mp3"
    file1.write_text("fake audio data")
    file2 = input_dir / "fast.mp3"
    file2.write_text("fake audio data")

    # Inject a sitecustomize.py to monkeypatch QualityGate in the child process
    sitecustomize_path = tmp_path / "sitecustomize.py"
    sitecustomize_path.write_text("""
import os
import time
import sys

if os.environ.get("MOCK_HANG_ACTIVE") == "1":
    try:
        import quality_gate
        original_analyze = quality_gate.QualityGate.analyze
        def fake_analyze(self, path):
            if "hang" in str(path):
                time.sleep(10)
            return original_analyze(self, path)
        quality_gate.QualityGate.analyze = fake_analyze
    except Exception as e:
        pass
""")
    
    old_pythonpath = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = f"{tmp_path}{os.pathsep}{old_pythonpath}"
    os.environ["MOCK_HANG_ACTIVE"] = "1"
    
    try:
        # Run pipeline with a 2-second timeout
        # Using 2 workers so they run concurrently
        counts = asyncio.run(
            run_pipeline(
                input_dir,
                output_dir,
                timeout_sec=2,
                workers=2,
                batch_size=2
            )
        )
    finally:
        os.environ["PYTHONPATH"] = old_pythonpath
        del os.environ["MOCK_HANG_ACTIVE"]

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
