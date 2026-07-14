"""Authenticated Telegram adapter for the shared organizer pipeline."""

from __future__ import annotations

import asyncio
import html
import logging
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from telegram import BotCommand, Update
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config import (
    ADMIN_USER_ID,
    CRAWL_TIMEOUT_SEC,
    DB_PATH,
    DOWNLOAD_DIR,
    OUTPUT_DIR,
    TELEGRAM_TOKEN,
    TEMP_ROOT,
    ensure_runtime_dirs,
    validate_bot_config,
)
from crawler import AudioCrawler
from exceptions import (
    AuthenticationRequiredError,
    BrowserUnavailableError,
    CrawlerError,
    CrawlTimeoutError,
    NetworkError,
    PathTraversalError,
)
from organize import process_file, run_pipeline
from organizer import Organizer
from processor import AudioProcessor
from quality_gate import QualityGate
from utils.cleanup import cleanup_run, setup_cleanup

logger = logging.getLogger(__name__)

_SECOND_LEVEL_SUFFIXES = {"co", "com", "net", "org", "gov", "edu", "ac"}


def source_name_from_url(url: str) -> str:
    """Use the registrable-looking domain, not a CDN/app subdomain, as the source name."""
    hostname = (urlparse(url).hostname or "web").lower().removeprefix("www.").strip(".")
    parts = hostname.split(".")
    if len(parts) >= 3 and parts[-2] in _SECOND_LEVEL_SUFFIXES and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return hostname or "web"


def configure_bot_logging() -> None:
    """Use UTF-8 plain logs and suppress HTTP URLs that contain the bot token."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)


def format_stats(stats: Mapping[str, Any]) -> str:
    total = int(stats.get("total", 0))
    if total == 0:
        return (
            "📊 <b>Kho âm thanh đang trống</b>\n\n"
            "Anh gửi URL âm thanh hoặc liên kết từ một trang catalogue để em bắt đầu thu thập."
        )

    lines = ["📊 <b>Thống kê kho âm thanh</b>", "", f"• Tổng số mẫu: <b>{total}</b>"]
    sites = stats.get("sites", [])
    if sites:
        lines.extend(["", "<b>Theo nguồn</b>"])
        for site in sites:
            name = html.escape(str(site.get("site") or "Không rõ"))
            lines.append(
                f"• {name}: {int(site.get('total', 0))} mẫu "
                f"(vòng lặp: {int(site.get('loops', 0))}, "
                f"âm đơn: {int(site.get('oneshots', 0))}, "
                f"hiệu ứng: {int(site.get('fx', 0))})"
            )

    genres = stats.get("genres", [])
    if genres:
        lines.extend(["", "<b>Thể loại nổi bật</b>"])
        for genre in genres:
            name = html.escape(str(genre.get("genre") or "Chưa phân loại"))
            lines.append(f"• {name}: {int(genre.get('total', 0))} mẫu")
    return "\n".join(lines)


def format_counts(counts: Mapping[str, int]) -> str:
    labels = {
        "passed": "Đã xử lý",
        "duplicate": "Bị trùng",
        "rejected": "Không đạt chất lượng",
        "error": "Gặp lỗi",
        "would_pass": "Đạt trong lần chạy thử",
    }
    lines = ["✅ <b>Đã xử lý xong thư mục</b>", ""]
    for status, label in labels.items():
        if counts.get(status, 0):
            lines.append(f"• {label}: <b>{int(counts[status])}</b>")
    if len(lines) == 2:
        lines.append("Không tìm thấy tệp âm thanh phù hợp.")
    return "\n".join(lines)


def format_results(results: Sequence[Mapping[str, Any]], discovered_count: int) -> str:
    if not results:
        if discovered_count:
            return (
                "⚠️ <b>Chưa lưu được mẫu âm thanh nào</b>\n\n"
                "Em đã thấy luồng âm thanh nhưng tệp không đạt kiểm tra chất lượng hoặc không thể tải xuống."
            )
        return (
            "⚠️ <b>Không tìm thấy âm thanh trong trang</b>\n\n"
            "Trang có thể yêu cầu đăng nhập, chưa phát bản nghe thử hoặc đang chặn trình duyệt tự động."
        )

    counts = Counter(str(result.get("status", "error")) for result in results)
    lines = ["✅ <b>Đã quét xong</b>", ""]
    labels = {
        "passed": "Đã lưu",
        "duplicate": "Đã có sẵn",
        "rejected": "Không đạt chất lượng",
        "error": "Gặp lỗi",
    }
    for status, label in labels.items():
        if counts[status]:
            lines.append(f"• {label}: <b>{counts[status]}</b>")

    outputs = [result.get("output") for result in results if result.get("status") == "passed"]
    if outputs:
        lines.extend(["", "<b>Tệp vừa lưu</b>"])
        for output in outputs[:5]:
            lines.append(f"• {html.escape(Path(str(output)).name)}")
        if len(outputs) > 5:
            lines.append(f"• Và {len(outputs) - 5} tệp khác")
    return "\n".join(lines)


def format_crawler_error(exc: CrawlerError) -> str:
    if isinstance(exc, AuthenticationRequiredError):
        return (
            "🔐 <b>Trang này cần đăng nhập</b>\n\n"
            "Anh gửi <code>/dangnhap URL_TRANG</code>, đăng nhập một lần trong cửa sổ Brave "
            "rồi gửi lại liên kết mẫu."
        )
    if isinstance(exc, CrawlTimeoutError):
        return "⏱️ Trang phản hồi quá lâu. Em đã dừng lượt quét để Bot không bị treo."
    if isinstance(exc, BrowserUnavailableError):
        return "🧩 Trình duyệt tự động chưa sẵn sàng. Anh báo em kiểm tra lại phần Chromium nhé."
    if isinstance(exc, PathTraversalError):
        return "🔗 Liên kết chưa hợp lệ. Anh hãy gửi liên kết đầy đủ bắt đầu bằng http:// hoặc https://."
    if isinstance(exc, NetworkError):
        return "🌐 Em chưa thể kết nối an toàn tới liên kết này. Anh kiểm tra lại link rồi gửi lại giúp em."
    return "⚠️ Em chưa thể quét trang này. Anh thử gửi lại sau một lúc nhé."


async def send_chunked(update: Update, text: str) -> None:
    if not update.effective_message:
        return
    for start in range(0, len(text), 3900):
        await update.effective_message.reply_text(text[start : start + 3900], parse_mode="HTML")


def _authorized(update: Update) -> bool:
    return bool(update.effective_user and update.effective_user.id == ADMIN_USER_ID)


class AudioBot:
    def __init__(self) -> None:
        validate_bot_config()
        ensure_runtime_dirs()
        self.run_dir = setup_cleanup(TEMP_ROOT)
        self.gate = QualityGate()
        self.processor = AudioProcessor()
        self.organizer = Organizer(OUTPUT_DIR, DB_PATH)
        migration = self.organizer.ensure_layout(DOWNLOAD_DIR)
        if migration["organized"] or migration["raw"] or migration["missing"]:
            logger.info("Đã tự nâng cấp bố cục thư viện: %s", migration)
        self.crawler = AudioCrawler(DOWNLOAD_DIR, self.gate)
        self.login_task: asyncio.Task[None] | None = None

    async def cmd_start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if _authorized(update):
            await send_chunked(
                update,
                "🎧 <b>MH - Dowsample Pro đã sẵn sàng</b>\n\n"
                "Anh gửi cho em một URL âm thanh hoặc liên kết từ bất kỳ trang catalogue nào. "
                "Em sẽ tự động:\n"
                "• Tìm và giữ toàn bộ tệp âm thanh gốc vào kho raw\n"
                "• Kiểm tra, chuẩn hóa thành WAV\n"
                "• Phân loại theo nhịp độ, tông và thể loại\n"
                "• Lưu vào thư viện của anh\n\n"
                "<b>Lệnh nhanh</b>\n"
                "/stats — Xem thống kê thư viện\n"
                "/path — Xem thư mục lưu nhạc\n"
                "/dangnhap URL — Đăng nhập website cần dùng\n"
                "/organize ĐƯỜNG_DẪN — Xử lý một thư mục trên máy",
            )

    async def cmd_stats(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if _authorized(update):
            await send_chunked(update, format_stats(self.organizer.get_stats()))

    async def cmd_path(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if _authorized(update):
            raw_path = html.escape(str(DOWNLOAD_DIR.resolve()))
            organized_path = html.escape(str(OUTPUT_DIR.resolve()))
            await send_chunked(
                update,
                "📁 <b>Thư mục lưu âm thanh</b>\n\n"
                f"Bản gốc đã tải: <code>{raw_path}</code>\n"
                f"Bản đã phân loại: <code>{organized_path}</code>",
            )

    async def cmd_organize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not context.args:
            await send_chunked(
                update,
                "⚠️ Anh chưa nhập đường dẫn thư mục.\n\nVí dụ: <code>/organize D:\\Thu-vien-am-thanh</code>",
            )
            return
        path = Path(" ".join(context.args)).expanduser()
        if not path.is_dir():
            await send_chunked(update, "❌ Không tìm thấy thư mục này. Anh kiểm tra lại đường dẫn nhé.")
            return
        await send_chunked(update, "⏳ Em đang quét và xử lý thư mục. Anh chờ em một chút nhé...")
        try:
            counts = await run_pipeline(path, OUTPUT_DIR, "telegram", delete_source=False)
            await send_chunked(update, format_counts(counts))
        except Exception:
            logger.exception("Không thể xử lý thư mục %s", path)
            await send_chunked(
                update, "❌ Em chưa xử lý được thư mục này. Anh kiểm tra lại tệp và thử lại nhé."
            )

    async def handle_url(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update) or not update.effective_message:
            return
        if self.login_task and not self.login_task.done():
            await send_chunked(
                update,
                "🔐 <b>Phiên đăng nhập website vẫn đang mở</b>\n\n"
                "Nếu anh đã đăng nhập xong, hãy đóng toàn bộ cửa sổ Brave riêng. "
                "Chờ Bot xác minh thành công rồi gửi lại liên kết; em chưa bắt đầu quét lúc này.",
            )
            return
        url = (update.effective_message.text or "").strip()
        status_message = await update.effective_message.reply_text(
            f"⏳ Em đã nhận liên kết. Đang tìm âm thanh, tối đa {CRAWL_TIMEOUT_SEC:g} giây...",
            parse_mode="HTML",
        )
        try:
            urls = await self.crawler.sniff_urls(url)
            site = source_name_from_url(url)
            raw_dir = DOWNLOAD_DIR / site
            await status_message.edit_text(
                f"🎯 Đã tìm thấy <b>{len(urls)}</b> URL âm thanh. Đang tải toàn bộ về kho raw...",
                parse_mode="HTML",
            )
            results = []
            downloaded_files: list[Path] = []
            loop = asyncio.get_running_loop()
            semaphore = asyncio.Semaphore(4)

            async def download_one(audio_url: str) -> Path | None:
                async with semaphore:
                    return await loop.run_in_executor(
                        None,
                        partial(
                            self.crawler.download,
                            audio_url,
                            raw_dir,
                            self.crawler.discovered_titles.get(audio_url),
                        ),
                    )

            tasks = [asyncio.create_task(download_one(audio_url)) for audio_url in urls]
            failures = 0
            for completed, task in enumerate(asyncio.as_completed(tasks), start=1):
                try:
                    downloaded = await task
                    if downloaded is not None:
                        downloaded_files.append(downloaded)
                    else:
                        failures += 1
                except CrawlerError as exc:
                    failures += 1
                    logger.warning("Không tải được một URL âm thanh: %s", exc)
                if completed % 10 == 0 or completed == len(tasks):
                    try:
                        await status_message.edit_text(
                            f"⬇️ Đang tải: <b>{completed}/{len(tasks)}</b> URL — "
                            f"đã lưu <b>{len(downloaded_files)}</b> tệp raw...",
                            parse_mode="HTML",
                        )
                    except TelegramError:
                        pass

            for processed, downloaded in enumerate(downloaded_files, start=1):
                result = await loop.run_in_executor(
                    None,
                    partial(
                        process_file,
                        downloaded,
                        site,
                        self.gate,
                        self.processor,
                        self.organizer,
                        self.run_dir,
                        delete_source=False,
                        ephemeral=False,
                    ),
                )
                source_hash = str(result.get("source_hash") or "")
                if downloaded.exists() and source_hash:
                    try:
                        archived = await loop.run_in_executor(
                            None,
                            partial(
                                self.organizer.archive_raw,
                                downloaded,
                                DOWNLOAD_DIR,
                                site,
                                result.get("analysis"),
                                source_hash,
                            ),
                        )
                        result["raw"] = str(archived)
                    except OSError:
                        logger.exception("Không thể sắp xếp tệp raw %s", downloaded)
                results.append(result)
                if processed % 10 == 0 or processed == len(downloaded_files):
                    try:
                        await status_message.edit_text(
                            f"🎛️ Đã giữ đủ raw. Đang phân tích: "
                            f"<b>{processed}/{len(downloaded_files)}</b> tệp...",
                            parse_mode="HTML",
                        )
                    except TelegramError:
                        pass
            raw_path = html.escape(str(raw_dir.resolve()))
            await status_message.edit_text(
                "✅ <b>Đã hoàn tất thu thập</b>\n\n"
                f"• URL âm thanh tìm thấy: <b>{len(urls)}</b>\n"
                f"• Tệp raw đã giữ lại: <b>{len(downloaded_files)}</b>\n"
                f"• URL tải lỗi: <b>{failures}</b>\n"
                f"• Thư mục raw: <code>{raw_path}</code>",
                parse_mode="HTML",
            )
            await send_chunked(update, format_results(results, len(urls)))
        except CrawlerError as exc:
            logger.warning("Không thể quét liên kết: %s", exc)
            await send_chunked(update, format_crawler_error(exc))
        except Exception:
            logger.exception("Lỗi ngoài dự kiến khi xử lý liên kết")
            await send_chunked(
                update, "❌ Có lỗi ngoài dự kiến. Em đã dừng an toàn; anh thử lại giúp em nhé."
            )

    async def cmd_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _authorized(update):
            return
        if not self.crawler.interactive_login_supported:
            await send_chunked(
                update,
                "❌ Máy chưa có Brave hoặc Edge để mở cửa sổ đăng nhập website.",
            )
            return
        if self.login_task and not self.login_task.done():
            await send_chunked(update, "⏳ Cửa sổ đăng nhập đang mở. Anh hoàn tất đăng nhập giúp em nhé.")
            return
        login_url = " ".join(context.args).strip() if context.args else "https://splice.com/accounts/sign-in"
        try:
            site = source_name_from_url(login_url)
            parsed_login = urlparse(login_url)
            if parsed_login.scheme not in {"http", "https"} or not parsed_login.hostname:
                raise ValueError
        except ValueError:
            await send_chunked(
                update,
                "⚠️ URL đăng nhập chưa hợp lệ. Ví dụ: <code>/dangnhap https://example.com</code>",
            )
            return
        await send_chunked(
            update,
            "🔐 Em đang mở cửa sổ Brave riêng cho MH-Dowsample.\n\n"
            f"Website: <b>{html.escape(site)}</b>\n"
            "Anh tự đăng nhập trong cửa sổ đó. Em không đọc hoặc lưu mật khẩu của anh.\n\n"
            "Sau khi đăng nhập xong, <b>anh tự đóng cửa sổ Brave</b>. "
            "Bot sẽ chờ tối đa 15 phút rồi mới tự dừng.",
        )
        self.login_task = context.application.create_task(self._finish_login(update, login_url, site))

    async def _finish_login(self, update: Update, login_url: str, site: str) -> None:
        try:
            logged_in = await self.crawler.login_site(login_url)
            if logged_in:
                await send_chunked(
                    update,
                    f"✅ <b>Đã lưu phiên đăng nhập {html.escape(site)}</b>\n\n"
                    "Anh gửi lại liên kết mẫu âm thanh để em quét nhé.",
                )
            else:
                await send_chunked(
                    update,
                    f"⚠️ <b>Chưa xác minh được phiên {html.escape(site)}</b>\n\n"
                    "Cửa sổ đã đóng hoặc hết thời gian chờ, nhưng Bot chưa mở lại được website. "
                    "Anh gửi lại lệnh /dangnhap kèm URL để thử lại nhé.",
                )
        except CrawlerError as exc:
            logger.warning("Không thể đăng nhập %s: %s", site, exc)
            await send_chunked(update, format_crawler_error(exc))
        except Exception:
            logger.exception("Lỗi ngoài dự kiến khi đăng nhập %s", site)
            await send_chunked(update, "❌ Không mở được phiên đăng nhập website. Anh thử lại giúp em nhé.")
        finally:
            self.login_task = None

    async def configure_profile(self, application: Application) -> None:
        name = "MH - Dowsample Pro"
        short_description = "Trợ lý thu thập, chuẩn hóa và phân loại mẫu âm thanh cho Minh Hiếu Producer."
        description = (
            "🎧 Trợ lý âm thanh riêng của Minh Hiếu Producer.\n\n"
            "Anh chỉ cần gửi URL âm thanh hoặc liên kết từ một trang catalogue. Em sẽ tìm mẫu, "
            "kiểm tra chất lượng, chuẩn hóa WAV, phân loại và lưu vào thư viện."
        )
        commands = [
            BotCommand("start", "Mở hướng dẫn sử dụng"),
            BotCommand("stats", "Xem thống kê thư viện âm thanh"),
            BotCommand("path", "Xem thư mục lưu âm thanh"),
            BotCommand("dangnhap", "Đăng nhập website theo URL"),
            BotCommand("organize", "Quét một thư mục trên máy"),
        ]
        try:
            if (await application.bot.get_my_name()).name != name:
                await application.bot.set_my_name(name)
            if (await application.bot.get_my_short_description()).short_description != short_description:
                await application.bot.set_my_short_description(short_description)
            if (await application.bot.get_my_description()).description != description:
                await application.bot.set_my_description(description)
            current_commands = await application.bot.get_my_commands()
            current_values = [(item.command, item.description) for item in current_commands]
            wanted_values = [(item.command, item.description) for item in commands]
            if current_values != wanted_values:
                await application.bot.set_my_commands(commands)
        except TelegramError as exc:
            logger.warning("Chưa thể đồng bộ hồ sơ Telegram; Bot vẫn tiếp tục chạy: %s", exc)

    async def shutdown(self, _application: Application) -> None:
        if self.login_task and not self.login_task.done():
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
        self.crawler.close()
        self.organizer.db.checkpoint()
        self.organizer.db.close_all()
        cleanup_run(self.run_dir)

    def run(self) -> None:
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .post_init(self.configure_profile)
            .post_shutdown(self.shutdown)
            .build()
        )
        application.add_handler(CommandHandler("start", self.cmd_start))
        application.add_handler(CommandHandler("stats", self.cmd_stats))
        application.add_handler(CommandHandler("path", self.cmd_path))
        application.add_handler(CommandHandler("dangnhap", self.cmd_login))
        application.add_handler(CommandHandler("organize", self.cmd_organize))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_url))
        application.run_polling()


if __name__ == "__main__":
    configure_bot_logging()
    AudioBot().run()
