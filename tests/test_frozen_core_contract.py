from __future__ import annotations

import hashlib
import inspect
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot import AudioBot
from organize import process_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]

FROZEN_FILE_HASHES = {
    "crawler.py": "813ee7036b66a623d4914b94c6645c5311ed3aba34287799f3b9827c96e86bf0",
    "quality_gate.py": "e69c9696685faba665bad469371060bb3797f5adb7601abe4a6f8ef8eaad5496",
    "processor.py": "58a7a2bc0405c58fc46a00ee3c2f2978c622d90ed77d415fecca015f9cad7315",
    "organizer.py": "611ae7ab0c00f60868cf3728b64a4ae2450fed4c4373af90b10f2457930f837f",
    "organize.py": "416fa4ec637324b93f7e1d876414925e4b555d77ee5a9767e9d7e2108ff69079",
}
FROZEN_HANDLE_URL_HASH = "31e1a0a1ddb31d74b0ed553124cf3d66fe64c9743b0e29f97999ebecc9bbe82e"


def _normalized_hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def test_frozen_core_fingerprints_match_confirmed_baseline() -> None:
    actual = {
        name: _normalized_hash((PROJECT_ROOT / name).read_text(encoding="utf-8"))
        for name in FROZEN_FILE_HASHES
    }
    handle_url_source = textwrap.dedent(inspect.getsource(AudioBot.handle_url))

    assert actual == FROZEN_FILE_HASHES
    assert _normalized_hash(handle_url_source) == FROZEN_HANDLE_URL_HASH


def test_passed_result_contract_contains_wav_hash_metadata_and_source(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"source-audio")
    output = tmp_path / "library" / "Loops" / "Techno" / "sample.wav"
    output.parent.mkdir(parents=True)
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    source_hash = "a" * 64
    analysis = {
        "passed": True,
        "issues": [],
        "content_type": "loop",
        "bpm": 128,
        "key": "A minor",
        "genre_hint": "techno",
    }

    def create_staged_file(_source: Path, _analysis: dict, target_dir: Path) -> Path:
        staged = target_dir / "normalized.wav"
        staged.write_bytes(b"RIFF-normalized")
        return staged

    gate = SimpleNamespace(analyze=Mock(return_value=analysis))
    processor = SimpleNamespace(process=Mock(side_effect=create_staged_file))
    organizer = SimpleNamespace(
        hash_file=Mock(return_value=source_hash),
        is_duplicate=Mock(return_value=False),
        organize=Mock(return_value=output),
    )

    result = process_file(
        source,
        "splice.com",
        gate,
        processor,
        organizer,
        staging_dir,
        delete_source=False,
    )

    assert result == {
        "status": "passed",
        "file": str(source),
        "output": str(output),
        "analysis": analysis,
        "source_hash": source_hash,
    }
    organizer.organize.assert_called_once_with(
        staging_dir / "normalized.wav", "splice.com", analysis, source_hash
    )
    assert source.exists()


def test_duplicate_result_contract_points_to_existing_wav(tmp_path: Path) -> None:
    source = tmp_path / "source.mp3"
    source.write_bytes(b"same-audio")
    existing = tmp_path / "library" / "One-Shots" / "sample.wav"
    source_hash = "b" * 64
    organizer = SimpleNamespace(
        hash_file=Mock(return_value=source_hash),
        is_duplicate=Mock(return_value=True),
        metadata_for_hash=Mock(return_value={"filepath": str(existing)}),
    )
    must_not_run = Mock(side_effect=AssertionError("duplicate must stop before audio processing"))

    result = process_file(
        source,
        "splice.com",
        SimpleNamespace(analyze=must_not_run),
        SimpleNamespace(process=must_not_run),
        organizer,
        tmp_path / "staging",
        delete_source=False,
    )

    assert result == {
        "status": "duplicate",
        "file": str(source),
        "output": str(existing),
        "source_hash": source_hash,
    }
    must_not_run.assert_not_called()


@pytest.mark.asyncio
async def test_handle_url_hands_completed_results_to_delivery_once(tmp_path: Path) -> None:
    audio_url = "https://cdn.example.test/sample.mp3"
    source = tmp_path / "downloads" / "example.test" / "sample.mp3"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"raw-audio")
    archived_raw = tmp_path / "raw-library" / "example.test" / "sample.mp3"
    archived_raw.parent.mkdir(parents=True)
    archived_raw.write_bytes(source.read_bytes())
    output = tmp_path / "library" / "Loops" / "Techno" / "sample.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"RIFF-processed")
    analysis = {
        "passed": True,
        "content_type": "loop",
        "bpm": 128,
        "key": "A minor",
        "genre_hint": "techno",
    }
    core_result = {
        "status": "passed",
        "file": str(source),
        "output": str(output),
        "analysis": analysis,
        "source_hash": "c" * 64,
    }
    status_message = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=7),
        effective_message=SimpleNamespace(
            text="https://sounds.example.test/catalogue",
            reply_text=AsyncMock(return_value=status_message),
        ),
    )
    archive_raw = Mock(return_value=archived_raw)
    instance = AudioBot.__new__(AudioBot)
    instance.run_dir = tmp_path / "run"
    instance.gate = object()
    instance.processor = object()
    instance.organizer = SimpleNamespace(archive_raw=archive_raw)
    instance.crawler = SimpleNamespace(
        sniff_urls=AsyncMock(return_value=[audio_url]),
        download=Mock(return_value=source),
        discovered_titles={audio_url: "Sample"},
    )
    instance._send_processed_files = AsyncMock()

    with (
        patch("bot.DOWNLOAD_DIR", tmp_path / "downloads"),
        patch("bot.process_file", return_value=core_result) as process,
    ):
        await instance.handle_url(update, None)

    expected = {**core_result, "raw": str(archived_raw)}
    instance._send_processed_files.assert_awaited_once_with(
        update,
        [expected],
        "example.test",
        retry_results=(expected,),
    )
    process.assert_called_once()
    assert process.call_args.args[0] == source
    assert process.call_args.args[1] == "example.test"
    assert process.call_args.kwargs == {
        "delete_source": False,
        "ephemeral": False,
        "timeout": 45,
    }
    archive_raw.assert_called_once()
    assert output.read_bytes() == b"RIFF-processed"


@pytest.mark.asyncio
async def test_delivery_failure_does_not_modify_processed_library_file(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    output = output_root / "Loops" / "Techno" / "sample.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"RIFF-confirmed-library-file")
    before_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=-1),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    instance = AudioBot.__new__(AudioBot)
    instance.output_dir = output_root
    instance.run_dir = tmp_path / "run"

    with patch("bot.build_result_archives", side_effect=OSError("cannot create archive")):
        await instance._send_processed_files(
            update,
            [{"status": "passed", "output": str(output)}],
            "splice.com",
        )

    assert output.is_file()
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before_hash
    assert "Kết quả đã xử lý vẫn được giữ" in reply_text.await_args.args[0]
