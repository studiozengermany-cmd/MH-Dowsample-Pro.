import asyncio
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import TelegramError, TimedOut

from access_control import AccessStatus
from bot import (
    AudioBot,
    build_result_archive,
    build_result_archives,
    deliverable_paths,
    format_admin_welcome,
    format_counts,
    format_crawler_error,
    format_link_failure,
    format_link_progress,
    format_results,
    format_stats,
    format_usage_guide,
    format_welcome,
    main_menu,
    source_name_from_url,
)
from delivery_retry import DeliveryRetryStore
from exceptions import (
    CrawlLimitError,
    CrawlTimeoutError,
    HTTPError,
    NetworkError,
    NoAudioFoundError,
    PathTraversalError,
)


def test_empty_stats_are_presented_in_vietnamese() -> None:
    message = format_stats({"total": 0, "sites": [], "genres": []})
    assert "KHO ÂM THANH ĐANG TRỐNG" in message
    assert "'total'" not in message


def test_stats_are_formatted_for_people_not_as_raw_dict() -> None:
    message = format_stats(
        {
            "total": 3,
            "sites": [{"site": "web", "total": 3, "loops": 1, "oneshots": 1, "fx": 1}],
            "genres": [{"genre": "DnB", "total": 2}],
        }
    )
    assert "<b>Tổng số mẫu:</b> 3" in message
    assert "vòng lặp: 1" in message
    assert "DnB: 2 mẫu" in message
    assert "{'" not in message


def test_pipeline_counts_are_translated() -> None:
    message = format_counts({"passed": 2, "duplicate": 1, "error": 0})
    assert "Đã xử lý: <b>2</b>" in message
    assert "Bị trùng: <b>1</b>" in message


def test_crawl_results_show_saved_filename() -> None:
    message = format_results(
        [{"status": "passed", "output": "C:/audio/kick.wav"}],
        discovered_count=1,
    )
    assert "<b>Đã lưu:</b> 1" in message
    assert "kick.wav" in message


def test_crawler_failures_do_not_expose_english_internals() -> None:
    no_audio_message = format_crawler_error(NoAudioFoundError("internal discovery details"))
    assert "Chưa tìm thấy tệp âm thanh" in no_audio_message
    assert "đăng nhập" not in no_audio_message.lower()
    assert "internal discovery details" not in no_audio_message
    assert "giới hạn quét an toàn" in format_crawler_error(CrawlLimitError("51 pages"))
    assert "quá lâu" in format_crawler_error(CrawlTimeoutError("internal timeout"))
    assert "tạm giới hạn" in format_crawler_error(HTTPError(429))
    assert "Liên kết này chưa hợp lệ" in format_crawler_error(PathTraversalError("internal path"))
    assert "kết nối an toàn" in format_crawler_error(NetworkError("internal network"))


def test_progress_marks_completed_steps_and_only_current_step_as_pending() -> None:
    downloading = format_link_progress("downloading", discovered=5, downloaded=2)
    analyzing = format_link_progress("analyzing", discovered=5, downloaded=4, processed=1)
    completed = format_link_progress("complete", discovered=5, downloaded=4, processed=4)

    assert "✅ <b>Đã nhận liên kết</b>" in downloading
    assert "✅ <b>Đã tìm thấy:</b> 5" in downloading
    assert "⏳ Đang tải tệp gốc: 2/5" in downloading
    assert "✅ <b>Đã tải tệp gốc:</b> 4" in analyzing
    assert "⏳ Đang kiểm tra và phân loại: 1/4" in analyzing
    assert "✅ <b>Đã gửi file gốc:</b> 4" in completed
    assert "✅ <b>Hoàn tất</b>" in completed


def test_failed_progress_keeps_received_step_and_marks_failure() -> None:
    message = format_link_failure("Trang phản hồi quá lâu.")

    assert "✅ <b>Đã nhận liên kết</b>" in message
    assert "❌ <b>Chưa hoàn tất xử lý</b>" in message
    assert "Trang phản hồi quá lâu" in message


def test_source_name_uses_main_domain_not_page_subdomain() -> None:
    assert source_name_from_url("https://sounds.example.com/catalogue") == "example.com"
    assert source_name_from_url("https://cdn.example.co.uk/audio/file.mp3") == "example.co.uk"


def test_deliverable_paths_include_new_and_existing_files(tmp_path) -> None:
    first = tmp_path / "mau-moi.wav"
    second = tmp_path / "mau-da-co.wav"
    first.write_bytes(b"audio")
    second.write_bytes(b"audio")

    paths = deliverable_paths(
        [
            {"status": "passed", "output": str(first)},
            {"status": "duplicate", "output": str(second)},
            {"status": "rejected", "output": str(first)},
        ]
    )

    assert paths == [first, second]


def test_public_welcome_is_clear_and_does_not_expose_host_tools() -> None:
    message = format_welcome()

    assert "MH - DOWNSAMPLE PRO" in message
    assert "<b>CÁCH SỬ DỤNG</b>" in message
    assert "Bot tự tìm và tải" in message
    assert "kiểm tra chất lượng" in message
    assert "đăng nhập" not in message.lower()
    assert "/batdau" not in message
    assert "/thumuc" not in message
    assert "gia đình" not in message


def test_usage_guide_explains_one_linear_journey() -> None:
    message = format_usage_guide()

    assert "Dạ, anh/chị" in message
    assert "Sao chép liên kết" in message
    assert "Dán liên kết" in message
    assert "gửi lại tệp kết quả" in message
    assert "TRỢ LÝ SẼ TỰ ĐỘNG" not in message


def test_admin_actions_are_buttons_not_welcome_text() -> None:
    public_labels = [button.text for row in main_menu().inline_keyboard for button in row]
    admin_labels = [button.text for row in main_menu(is_admin=True).inline_keyboard for button in row]

    assert "📁 Xử lý thư mục" not in public_labels
    assert admin_labels[0] == "🔗 Gửi liên kết âm thanh"
    assert "📁 Xử lý thư mục" in admin_labels
    assert all("Đăng nhập" not in label for label in public_labels + admin_labels)


def test_admin_welcome_explains_the_automatic_pipeline() -> None:
    message = format_admin_welcome()

    assert "nhiều trang khác nhau" in message
    assert "không cần chọn trước một nền tảng cố định" in message
    assert "Bot tự tìm và tải" in message
    assert "chuẩn hóa và phân loại" in message
    assert "đăng nhập" not in message.lower()
    assert "Splice" not in message


@pytest.mark.asyncio
async def test_new_non_admin_user_only_sees_access_request() -> None:
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=1001),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.access_control = SimpleNamespace(status_for=lambda _user_id: None)

    await bot.cmd_start(update, None)

    reply_text.assert_awaited_once()
    sent_message = reply_text.await_args.args[0]
    assert "BOT CHỈ DÀNH CHO NGƯỜI ĐÃ ĐƯỢC DUYỆT" in sent_message
    assert "Bot tự tìm và tải" not in sent_message
    keyboard = reply_text.await_args.kwargs["reply_markup"]
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == ["🔐 Gửi yêu cầu sử dụng"]


@pytest.mark.asyncio
async def test_guide_button_opens_guide_instead_of_repeating_start() -> None:
    answer = AsyncMock()
    edit_message_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=-1),
        callback_query=SimpleNamespace(
            answer=answer,
            data="menu:huong_dan",
            edit_message_text=edit_message_text,
        ),
    )
    bot = AudioBot.__new__(AudioBot)

    await bot.handle_menu(update, None)

    answer.assert_awaited_once()
    edit_message_text.assert_awaited_once()
    opened_message = edit_message_text.await_args.args[0]
    assert "<b>HƯỚNG DẪN SỬ DỤNG</b>" in opened_message
    assert "<b>ANH/CHỊ MUỐN LÀM GÌ?</b>" not in opened_message


@pytest.mark.asyncio
async def test_second_url_job_is_rejected_while_pipeline_is_busy() -> None:
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.url_job_lock = asyncio.Lock()
    bot.handle_url = AsyncMock()

    with patch("bot.ADMIN_USER_ID", 42):
        async with bot.url_job_lock:
            await bot.handle_authorized_url(update, None)

    bot.handle_url.assert_not_awaited()
    assert "đang xử lý một job khác" in reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_empty_discovery_stops_without_starting_a_login_flow() -> None:
    status_message = SimpleNamespace(edit_text=AsyncMock())
    reply_text = AsyncMock(return_value=status_message)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=-2),
        effective_message=SimpleNamespace(
            text="https://sounds.example.com/protected-pack",
            reply_text=reply_text,
        ),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.crawler = SimpleNamespace(sniff_urls=AsyncMock(return_value=[]))

    await bot.handle_url(update, None)

    failure_message = status_message.edit_text.await_args.args[0]
    assert "Chưa tìm thấy tệp âm thanh" in failure_message
    assert "0 đường dẫn" not in failure_message
    assert "reply_markup" not in status_message.edit_text.await_args.kwargs


@pytest.mark.asyncio
async def test_empty_discovery_fallback_message_has_no_login_button() -> None:
    status_message = SimpleNamespace(
        edit_text=AsyncMock(side_effect=TelegramError("message cannot be edited"))
    )
    reply_text = AsyncMock(side_effect=[status_message, SimpleNamespace()])
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=-2),
        effective_message=SimpleNamespace(
            text="https://sounds.example.com/protected-pack",
            reply_text=reply_text,
        ),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.crawler = SimpleNamespace(
        sniff_urls=AsyncMock(side_effect=NoAudioFoundError("no public audio"))
    )

    await bot.handle_url(update, None)

    fallback_call = reply_text.await_args_list[-1]
    assert "Chưa tìm thấy tệp âm thanh" in fallback_call.args[0]
    assert "reply_markup" not in fallback_call.kwargs


@pytest.mark.asyncio
async def test_download_batches_send_original_files_immediately(tmp_path) -> None:
    status_message = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=77),
        effective_message=SimpleNamespace(
            text="https://sounds.example.com/pack",
            reply_text=AsyncMock(return_value=status_message),
        ),
    )
    urls = [f"https://cdn.example.com/{index}.mp3" for index in range(6)]

    def download(url, raw_dir, _title):
        path = tmp_path / Path(url).name
        path.write_bytes(b"audio")
        return path

    bot = AudioBot.__new__(AudioBot)
    bot.crawler = SimpleNamespace(
        sniff_urls=AsyncMock(return_value=urls),
        download=download,
        discovered_titles={},
    )
    bot.run_dir = tmp_path / "run"
    bot._send_downloaded_files = AsyncMock(return_value=True)

    with patch("bot.DOWNLOAD_DIR", tmp_path / "downloads"), patch(
        "bot.JOB_BATCH_FILES", 2
    ):
        await bot.handle_url(update, None)

    progress_messages = [call.args[0] for call in status_message.edit_text.await_args_list]
    assert any("Đang đóng gói và gửi" in text for text in progress_messages)
    assert bot._send_downloaded_files.await_count == 3
    assert [
        len(call.args[1]) for call in bot._send_downloaded_files.await_args_list
    ] == [2, 2, 2]


@pytest.mark.asyncio
async def test_url_delivery_never_calls_audio_processing(tmp_path) -> None:
    status_message = SimpleNamespace(edit_text=AsyncMock())
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=77),
        effective_message=SimpleNamespace(
            text="https://sounds.example.com/pack",
            reply_text=AsyncMock(return_value=status_message),
        ),
    )
    urls = [f"https://cdn.example.com/{index}.mp3" for index in range(11)]

    def download(url, _raw_dir, _title):
        path = tmp_path / Path(url).name
        path.write_bytes(b"audio")
        return path

    bot = AudioBot.__new__(AudioBot)
    bot.crawler = SimpleNamespace(
        sniff_urls=AsyncMock(return_value=urls),
        download=download,
        discovered_titles={},
    )
    bot.run_dir = tmp_path / "run"
    bot._send_downloaded_files = AsyncMock(return_value=True)

    with patch("bot.DOWNLOAD_DIR", tmp_path / "downloads"), patch(
        "bot.JOB_BATCH_FILES", 200
    ):
        await bot.handle_url(update, None)

    bot._send_downloaded_files.assert_awaited_once()
    delivered_paths = bot._send_downloaded_files.await_args.args[1]
    assert len(delivered_paths) == 11
    assert all(path.suffix == ".mp3" for path in delivered_paths)


@pytest.mark.asyncio
async def test_processed_file_is_returned_through_telegram(tmp_path) -> None:
    output_root = tmp_path / "organized"
    output = output_root / "Loops" / "House" / "mau-da-xu-ly.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"RIFF-test-audio")
    reply_text = AsyncMock()
    reply_document = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=-1),
        effective_message=SimpleNamespace(
            reply_text=reply_text,
            reply_document=reply_document,
        )
    )
    bot = AudioBot.__new__(AudioBot)
    bot.run_dir = tmp_path / "run"
    bot.output_dir = output_root
    bot.delivery_retry_store = DeliveryRetryStore(tmp_path / "delivery-retries.db")

    await bot._send_processed_files(
        update, [{"status": "passed", "output": str(output)}], "loopcloud.com"
    )

    reply_document.assert_awaited_once()
    assert reply_document.await_args.kwargs["filename"] == "loopcloud.com-samples.zip"
    assert reply_document.await_args.kwargs["parse_mode"] == "HTML"
    assert reply_document.await_args.kwargs["read_timeout"] >= 30
    assert reply_document.await_args.kwargs["write_timeout"] >= 30
    assert "gom gọn 1 sample" in reply_document.await_args.kwargs["caption"]
    assert not (bot.run_dir / "loopcloud.com-samples.zip").exists()
    retry_record = bot.delivery_retry_store.load(-1, output_root)
    assert retry_record is not None
    assert retry_record.site == "loopcloud.com"
    assert retry_record.results == (
        {"status": "passed", "output": str(output.resolve())},
    )


@pytest.mark.asyncio
async def test_owner_uses_local_library_delivery_by_default(tmp_path) -> None:
    output = tmp_path / "organized" / "FX" / "sample.wav"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"RIFF-test-audio")
    reply_text = AsyncMock()
    reply_document = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=reply_text, reply_document=reply_document),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.output_dir = tmp_path / "organized"
    bot.run_dir = tmp_path / "run"

    with patch("bot.ADMIN_USER_ID", 42), patch("bot.OWNER_DELIVERY_MODE", "local"):
        await bot._send_processed_files(
            update, [{"status": "passed", "output": str(output)}], "splice.com"
        )

    assert "ĐÃ LƯU KẾT QUẢ TRÊN MÁY" in reply_text.await_args.args[0]
    assert str(bot.output_dir) in reply_text.await_args.args[0]
    reply_document.assert_not_awaited()


@pytest.mark.asyncio
async def test_approved_user_can_retry_existing_library_without_reprocessing(tmp_path) -> None:
    output_root = tmp_path / "organized"
    first = output_root / "Loops" / "first.wav"
    second = output_root / "FX" / "second.wav"
    ignored = output_root / "notes.txt"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"RIFF-first")
    second.write_bytes(b"RIFF-second")
    ignored.write_text("not audio", encoding="utf-8")
    reply_text = AsyncMock()
    reply_document = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=77),
        effective_message=SimpleNamespace(
            reply_text=reply_text,
            reply_document=reply_document,
        ),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.output_dir = output_root
    bot.run_dir = tmp_path / "run"
    bot._has_access = lambda _update: True
    bot.delivery_retry_store = DeliveryRetryStore(tmp_path / "delivery-retries.db")
    bot.delivery_retry_store.save(
        77,
        "splice.com",
        [
            {"status": "passed", "output": str(first)},
            {"status": "passed", "output": str(second)},
        ],
        output_root,
    )

    await bot.cmd_retry_delivery(update, None)

    assert "<b>2</b> sample" in reply_text.await_args_list[0].args[0]
    assert "không tải hoặc xử lý lại" in reply_text.await_args_list[0].args[0]
    reply_document.assert_awaited_once()
    assert reply_document.await_args.kwargs["filename"] == "splice.com-samples.zip"
    assert first.read_bytes() == b"RIFF-first"
    assert second.read_bytes() == b"RIFF-second"
    assert not list((tmp_path / "run").glob("*.zip"))


@pytest.mark.asyncio
async def test_admin_can_assign_pre_manifest_library_to_one_approved_customer(
    tmp_path,
) -> None:
    output_root = tmp_path / "organized"
    sample = output_root / "Loops" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF-sample")
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=AsyncMock()),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.output_dir = output_root
    bot.access_control = SimpleNamespace(
        status_for=lambda user_id: AccessStatus.APPROVED
        if user_id == 77
        else AccessStatus.PENDING
    )
    bot.delivery_retry_store = DeliveryRetryStore(tmp_path / "delivery-retries.db")

    with patch("bot.ADMIN_USER_ID", 42):
        await bot.cmd_assign_retry(update, SimpleNamespace(args=["77"]))

    record = bot.delivery_retry_store.load(77, output_root)
    assert record is not None
    assert record.site == "library-recovery"
    assert record.results == (
        {"status": "passed", "output": str(sample.resolve())},
    )
    assert "ĐÃ GÁN JOB CŨ CHO KHÁCH" in update.effective_message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_telegram_upload_timeout_is_retried(tmp_path) -> None:
    archive = tmp_path / "part.zip"
    archive.write_bytes(b"zip")
    reply_document = AsyncMock(side_effect=[TimedOut("slow upload"), SimpleNamespace()])
    message = SimpleNamespace(reply_document=reply_document)
    bot = AudioBot.__new__(AudioBot)

    with patch("bot.TELEGRAM_UPLOAD_RETRIES", 2), patch("bot.asyncio.sleep", new=AsyncMock()):
        sent = await bot._send_archive_with_retry(message, archive, "caption")

    assert sent is True
    assert reply_document.await_count == 2


@pytest.mark.asyncio
async def test_admin_can_change_and_persist_output_directory(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OUTPUT_DIR=./organized\n", encoding="utf-8")
    new_output = tmp_path / "new-library"
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.output_dir = tmp_path / "organized"
    bot.organizer = SimpleNamespace(output_dir=bot.output_dir)

    with patch("bot.ADMIN_USER_ID", 42), patch("bot.BASE_DIR", tmp_path):
        await bot.cmd_set_output(update, SimpleNamespace(args=[str(new_output)]))

    assert bot.output_dir == new_output.resolve()
    assert bot.organizer.output_dir == new_output.resolve()
    assert str(new_output.resolve()) in env_file.read_text(encoding="utf-8")
    assert "ĐÃ ĐỔI NƠI LƯU SAMPLE" in reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_cannot_set_output_inside_temporary_run_directory(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("OUTPUT_DIR=./organized\n", encoding="utf-8")
    reply_text = AsyncMock()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(reply_text=reply_text),
    )
    bot = AudioBot.__new__(AudioBot)
    bot.output_dir = tmp_path / "organized"
    bot.organizer = SimpleNamespace(output_dir=bot.output_dir)
    bot.run_dir = tmp_path / "run"
    bot.run_dir.mkdir()

    with patch("bot.ADMIN_USER_ID", 42), patch("bot.BASE_DIR", tmp_path):
        await bot.cmd_set_output(update, SimpleNamespace(args=[str(bot.run_dir / "library")]))

    assert bot.output_dir == tmp_path / "organized"
    assert bot.organizer.output_dir == tmp_path / "organized"
    assert env_file.read_text(encoding="utf-8") == "OUTPUT_DIR=./organized\n"
    assert "Không thể tạo hoặc ghi" in reply_text.await_args.args[0]


def test_result_archive_preserves_organized_sample_folders(tmp_path) -> None:
    output_root = tmp_path / "organized"
    sample = output_root / "Loops" / "Tech House" / "sample.wav"
    sample.parent.mkdir(parents=True)
    sample.write_bytes(b"RIFF-test-audio")
    archive_path = tmp_path / "result.zip"

    build_result_archive([sample], output_root, archive_path)

    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["Loops/Tech House/sample.wav"]


def test_large_result_is_split_into_telegram_safe_archives(tmp_path) -> None:
    output_root = tmp_path / "organized"
    first = output_root / "Loops" / "House" / "first.wav"
    second = output_root / "FX" / "second.wav"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"1234")
    second.write_bytes(b"5678")

    first_probe = build_result_archive([first], output_root, tmp_path / "first-probe.zip")
    second_probe = build_result_archive([second], output_root, tmp_path / "second-probe.zip")
    max_part_bytes = max(first_probe.stat().st_size, second_probe.stat().st_size)
    first_probe.unlink()
    second_probe.unlink()

    archives = build_result_archives(
        [first, second],
        output_root,
        tmp_path / "run",
        "splice.com-samples",
        max_part_bytes=max_part_bytes,
    )

    assert [path.name for path in archives] == [
        "splice.com-samples-01-of-02.zip",
        "splice.com-samples-02-of-02.zip",
    ]
    with zipfile.ZipFile(archives[0]) as archive:
        assert archive.namelist() == ["Loops/House/first.wav"]
    with zipfile.ZipFile(archives[1]) as archive:
        assert archive.namelist() == ["FX/second.wav"]
