from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from organize import _print_summary, main, run_pipeline


def test_print_summary_includes_file_timeout(capsys):
    counts = Counter({"file_timeout": 5, "passed": 2})
    _print_summary(counts)
    captured = capsys.readouterr()
    assert "file_timeout" in captured.out
    assert "5" in captured.out

@patch("organize.run_pipeline")
@patch("organize.build_parser")
@patch("organize.configure_cli_logging")
def test_main_exit_code_on_file_timeout(mock_logging, mock_parser, mock_run):
    mock_args = MagicMock()
    mock_args.stats = False
    mock_args.rebuild_layout = False
    mock_args.input = Path(".")
    mock_args.output = Path("dummy_out")
    mock_parser.return_value.parse_args.return_value = mock_args

    async def mock_run_pipeline(*args, **kwargs):
        return Counter({"file_timeout": 1, "passed": 10})
    mock_run.side_effect = mock_run_pipeline
    assert main() == 1
    
    async def mock_run_pipeline2(*args, **kwargs):
        return Counter({"error": 1, "passed": 10})
    mock_run.side_effect = mock_run_pipeline2
    assert main() == 1
    
    async def mock_run_pipeline3(*args, **kwargs):
        return Counter({"passed": 10, "rejected": 2})
    mock_run.side_effect = mock_run_pipeline3
    assert main() == 0

@pytest.mark.asyncio
@patch("organize.Organizer")
@patch("organize.QualityGate")
@patch("organize.AudioProcessor")
async def test_run_pipeline_logging_file_timeout(mock_proc, mock_gate, mock_org, tmp_path, caplog):
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    dummy_audio = input_dir / "test.mp3"
    dummy_audio.touch()
    
    simulated_result = {
        "status": "file_timeout", 
        "file": str(dummy_audio), 
        "error": "Analysis timed out after 45s", 
        "source_hash": "dummy"
    }
    
    with patch("organize.process_file", return_value=simulated_result):
        counts = await run_pipeline(
            input_dir,
            output_dir,
            workers=1,
            batch_size=1
        )
    
    assert counts["file_timeout"] == 1
    assert "Organized test.mp3" not in caplog.text
    assert "Timed out test.mp3: Analysis timed out after 45s" in caplog.text
