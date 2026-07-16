from __future__ import annotations

import asyncio
import hashlib
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from telegram.error import TelegramError, TimedOut

from delivery import (
    DeliveryService,
    build_delivery_manifest,
    build_original_archives,
    build_result_archive,
    build_result_archives,
)


def _service(
    tmp_path: Path,
    *,
    owner_mode: str = "telegram",
    max_part_bytes: int = 1024,
    retries: int = 2,
    archive_builder=build_result_archives,
) -> DeliveryService:
    return DeliveryService(
        output_root=tmp_path / "library",
        temp_root=tmp_path / "run",
        owner_mode=owner_mode,
        archive_part_bytes=max_part_bytes,
        upload_retries=retries,
        upload_timeout_sec=30,
        archive_builder=archive_builder,
    )


def test_manifest_distinguishes_every_core_status_and_only_keeps_existing_files(
    tmp_path: Path,
) -> None:
    new_file = tmp_path / "library" / "Loops" / "Techno" / "new.wav"
    duplicate = tmp_path / "library" / "FX" / "existing.wav"
    new_file.parent.mkdir(parents=True)
    duplicate.parent.mkdir(parents=True)
    new_file.write_bytes(b"new")
    duplicate.write_bytes(b"existing")

    manifest = build_delivery_manifest(
        [
            {"status": "passed", "output": str(new_file)},
            {"status": "duplicate", "output": str(duplicate)},
            {"status": "rejected", "issues": ["quality"]},
            {"status": "error", "error": "processing failed"},
            {"status": "passed", "output": str(tmp_path / "missing.wav")},
            {"status": "would_pass"},
        ]
    )

    assert manifest.paths == (new_file, duplicate)
    assert [item.status for item in manifest.items] == ["passed", "duplicate"]
    assert manifest.passed_count == 2
    assert manifest.duplicate_count == 1
    assert manifest.rejected_count == 1
    assert manifest.error_count == 1
    assert manifest.unavailable_count == 1
    assert manifest.other_count == 1


@pytest.mark.asyncio
async def test_owner_local_summary_distinguishes_statuses_without_building_zip(
    tmp_path: Path,
) -> None:
    new_file = tmp_path / "library" / "Loops" / "House" / "new.wav"
    duplicate = tmp_path / "library" / "One-Shots" / "existing.wav"
    new_file.parent.mkdir(parents=True)
    duplicate.parent.mkdir(parents=True)
    new_file.write_bytes(b"new")
    duplicate.write_bytes(b"existing")
    archive_builder = Mock(side_effect=AssertionError("local mode must not build ZIP"))
    service = _service(tmp_path, owner_mode="local", archive_builder=archive_builder)
    message = SimpleNamespace(reply_text=AsyncMock())

    report = await service.deliver(
        message,
        [
            {"status": "passed", "output": str(new_file)},
            {"status": "duplicate", "output": str(duplicate)},
            {"status": "rejected"},
            {"status": "error"},
        ],
        "splice.com",
        is_owner=True,
    )

    summary = message.reply_text.await_args.args[0]
    assert report.local_notified is True
    assert report.archive_count == 0
    assert "File mới đã xử lý: <b>1</b>" in summary
    assert "File trùng đã có sẵn: <b>1</b>" in summary
    assert "File bị loại: <b>1</b>" in summary
    assert "File lỗi: <b>1</b>" in summary
    archive_builder.assert_not_called()


def test_single_part_zip_preserves_library_folder(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    sample = output_root / "Loops" / "Techno" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF-audio")

    archives = build_result_archives(
        [sample], output_root, tmp_path / "run", "splice.com-samples", 1024
    )

    assert [path.name for path in archives] == ["splice.com-samples.zip"]
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["Loops/Techno/sample.wav"]


def test_original_archive_stores_source_audio_without_recompression(tmp_path: Path) -> None:
    output_root = tmp_path / "downloads"
    sample = output_root / "Original Source Name.mp3"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"already-compressed-audio")

    archives = build_original_archives(
        [sample], output_root, tmp_path / "run", "splice.com-samples", 1024
    )

    with zipfile.ZipFile(archives[0]) as archive:
        info = archive.getinfo("Original Source Name.mp3")
        assert info.compress_type == zipfile.ZIP_STORED
        assert archive.read(info) == sample.read_bytes()


def test_multiple_zip_parts_are_named_and_partitioned_deterministically(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    first = output_root / "Loops" / "House" / "first.wav"
    second = output_root / "FX" / "second.wav"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")

    probe_first = build_result_archive([first], output_root, tmp_path / "first-probe.zip")
    probe_second = build_result_archive([second], output_root, tmp_path / "second-probe.zip")
    max_part_bytes = max(probe_first.stat().st_size, probe_second.stat().st_size)
    probe_first.unlink()
    probe_second.unlink()

    archives = build_result_archives(
        [first, second], output_root, tmp_path / "run", "splice.com-samples", max_part_bytes
    )

    assert [path.name for path in archives] == [
        "splice.com-samples-01-of-02.zip",
        "splice.com-samples-02-of-02.zip",
    ]
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["Loops/House/first.wav"]
    with zipfile.ZipFile(archives[1]) as archive:
        assert archive.namelist() == ["FX/second.wav"]


@pytest.mark.asyncio
async def test_network_timeout_is_retried_by_delivery_service(tmp_path: Path) -> None:
    archive = tmp_path / "part.zip"
    archive.write_bytes(b"zip")
    reply_document = AsyncMock(side_effect=[TimedOut("slow upload"), SimpleNamespace()])
    message = SimpleNamespace(reply_document=reply_document)
    service = _service(tmp_path, retries=2)

    with patch("delivery.asyncio.sleep", new=AsyncMock()) as sleep:
        sent = await service.send_archive_with_retry(message, archive, "caption")

    assert sent is True
    assert reply_document.await_count == 2
    sleep.assert_awaited_once()


@pytest.mark.asyncio
async def test_hung_telegram_upload_is_cut_off_and_retried(tmp_path: Path) -> None:
    archive = tmp_path / "part.zip"
    archive.write_bytes(b"zip")

    async def hang(**_kwargs):
        await asyncio.Event().wait()

    reply_document = AsyncMock(side_effect=hang)
    message = SimpleNamespace(reply_document=reply_document)
    service = DeliveryService(
        output_root=tmp_path / "library",
        temp_root=tmp_path / "run",
        owner_mode="telegram",
        archive_part_bytes=1024,
        upload_retries=2,
        upload_timeout_sec=300,
        upload_attempt_guard_sec=0.01,
    )

    with patch("delivery.asyncio.sleep", new=AsyncMock()):
        sent = await service.send_archive_with_retry(message, archive, "caption")

    assert sent is False
    assert reply_document.await_count == 2


@pytest.mark.asyncio
async def test_partial_upload_continues_and_cleans_every_temporary_zip(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    first = output_root / "Loops" / "House" / "first.wav"
    second = output_root / "FX" / "second.wav"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")
    source_hashes = {
        first: hashlib.sha256(first.read_bytes()).hexdigest(),
        second: hashlib.sha256(second.read_bytes()).hexdigest(),
    }
    reply_document = AsyncMock(side_effect=[SimpleNamespace(), TelegramError("rejected")])
    reply_text = AsyncMock()
    message = SimpleNamespace(reply_document=reply_document, reply_text=reply_text)
    first_probe = build_result_archive([first], output_root, tmp_path / "first-probe.zip")
    second_probe = build_result_archive([second], output_root, tmp_path / "second-probe.zip")
    max_part_bytes = max(first_probe.stat().st_size, second_probe.stat().st_size)
    first_probe.unlink()
    second_probe.unlink()
    service = _service(tmp_path, max_part_bytes=max_part_bytes, retries=1)

    report = await service.deliver(
        message,
        [
            {"status": "passed", "output": str(first)},
            {"status": "duplicate", "output": str(second)},
            {"status": "rejected"},
        ],
        "splice.com",
        is_owner=False,
    )

    assert report.archive_count == 2
    assert report.sent_parts == (1,)
    assert report.failed_parts == (2,)
    assert reply_document.await_count == 2
    assert "File mới đã xử lý: <b>1</b>" in reply_document.await_args_list[0].kwargs["caption"]
    assert "File trùng đã có sẵn: <b>1</b>" in reply_document.await_args_list[0].kwargs["caption"]
    assert "Các gói chưa gửi được: <b>2</b>" in reply_text.await_args.args[0]
    assert not list((tmp_path / "run").glob("*.zip"))
    for path, before_hash in source_hashes.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == before_hash


@pytest.mark.asyncio
async def test_one_unreadable_file_isolated_without_blocking_other_results(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "library"
    readable = output_root / "Loops" / "readable.wav"
    unreadable = output_root / "FX" / "unreadable.wav"
    readable.parent.mkdir(parents=True)
    unreadable.parent.mkdir(parents=True)
    readable.write_bytes(b"RIFF-readable")
    unreadable.write_bytes(b"RIFF-unreadable")
    real_builder = build_result_archives

    def fail_only_unreadable(paths, root, archive_dir, stem, max_part_bytes):
        if unreadable in paths:
            raise OSError("cannot read one output")
        return real_builder(paths, root, archive_dir, stem, max_part_bytes)

    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())
    service = _service(
        tmp_path,
        max_part_bytes=1024,
        archive_builder=fail_only_unreadable,
    )

    report = await service.deliver(
        message,
        [
            {"status": "passed", "output": str(readable)},
            {"status": "passed", "output": str(unreadable)},
        ],
        "splice.com",
        is_owner=False,
    )

    assert report.archive_count == 1
    assert report.sent_parts == (1,)
    assert report.build_failed is True
    assert report.manifest.ready_count == 1
    assert report.manifest.unavailable_count == 1
    assert message.reply_document.await_count == 1
    assert "<b>1</b> file" in message.reply_text.await_args.args[0]
    assert readable.read_bytes() == b"RIFF-readable"
    assert unreadable.read_bytes() == b"RIFF-unreadable"
    assert not list((tmp_path / "run").glob("*.zip"))


@pytest.mark.asyncio
async def test_each_batch_zip_is_removed_before_the_next_batch_is_built(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "library"
    first = output_root / "first.wav"
    second = output_root / "second.wav"
    output_root.mkdir(parents=True)
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")
    calls = 0

    def one_zip_builder(paths, root, archive_dir, stem, max_part_bytes):
        nonlocal calls
        calls += 1
        assert not list(archive_dir.glob("*.zip"))
        archive = archive_dir / f"{stem}.zip"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(paths[0].read_bytes())
        return [archive]

    message = SimpleNamespace(reply_document=AsyncMock(), reply_text=AsyncMock())
    service = _service(
        tmp_path,
        max_part_bytes=4,
        archive_builder=one_zip_builder,
    )

    report = await service.deliver(
        message,
        [
            {"status": "passed", "output": str(first)},
            {"status": "passed", "output": str(second)},
        ],
        "splice.com",
        is_owner=False,
    )

    assert calls == 2
    assert report.archive_count == 2
    assert report.sent_parts == (1, 2)
    assert not list((tmp_path / "run").glob("*.zip"))


def test_single_file_larger_than_final_zip_limit_fails_cleanly(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    sample = output_root / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(bytes(range(256)) * 8)

    with pytest.raises(ValueError, match="cannot fit"):
        build_result_archives(
            [sample],
            output_root,
            tmp_path / "run",
            "samples",
            max_part_bytes=32,
        )

    assert not list((tmp_path / "run").glob("*.zip"))


def test_archive_build_failure_cleans_completed_and_partial_parts(tmp_path: Path) -> None:
    output_root = tmp_path / "library"
    first = output_root / "Loops" / "first.wav"
    second = output_root / "FX" / "second.wav"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")
    real_builder = build_result_archive
    def fail_second_build(paths, root, archive_path):
        if "second" in paths[0].name:
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            archive_path.write_bytes(b"partial")
            raise OSError("disk full")
        return real_builder(paths, root, archive_path)

    first_probe = real_builder([first], output_root, tmp_path / "first-probe.zip")
    second_probe = real_builder([second], output_root, tmp_path / "second-probe.zip")
    max_part_bytes = max(first_probe.stat().st_size, second_probe.stat().st_size)
    first_probe.unlink()
    second_probe.unlink()

    with (
        patch("delivery.build_result_archive", side_effect=fail_second_build),
        pytest.raises(OSError, match="disk full"),
    ):
        build_result_archives(
            [first, second],
            output_root,
            tmp_path / "run",
            "splice.com-samples",
            max_part_bytes,
        )

    assert not list((tmp_path / "run").glob("*.zip"))


@pytest.mark.asyncio
async def test_zip_build_failure_keeps_library_file_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "library" / "Loops" / "Techno" / "sample.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"RIFF-library")
    before_hash = hashlib.sha256(output.read_bytes()).hexdigest()
    builder = Mock(side_effect=OSError("cannot build"))
    service = _service(tmp_path, archive_builder=builder)
    message = SimpleNamespace(reply_text=AsyncMock())

    report = await service.deliver(
        message,
        [{"status": "passed", "output": str(output)}],
        "splice.com",
        is_owner=False,
    )

    assert report.build_failed is True
    assert output.is_file()
    assert hashlib.sha256(output.read_bytes()).hexdigest() == before_hash
    assert "Kết quả đã xử lý vẫn được giữ" in message.reply_text.await_args.args[0]
