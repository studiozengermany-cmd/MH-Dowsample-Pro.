from pathlib import Path

from utils.cleanup import cleanup_run, setup_cleanup


def test_cleanup_removes_only_owned_run(tmp_path: Path) -> None:
    outside = tmp_path / "keep.txt"
    outside.write_text("keep")
    run = setup_cleanup(tmp_path / "temp")
    (run / "delete.txt").write_text("delete")
    cleanup_run(run)
    assert not run.exists()
    assert outside.exists()
