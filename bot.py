"""Authenticated Telegram adapter for the shared organizer pipeline."""

from __future__ import annotations

import asyncio
import html
import logging
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import set_key
from telegram import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from access_control import AccessControlStore, AccessStatus, AccessUser, RequestOutcome
from config import (
    ADMIN_USER_ID,
    AUDIO_EXTS,
    BASE_DIR,
    DATA_DIR,
    DB_PATH,
    DOWNLOAD_DIR,
    JOB_BATCH_FILES,
    OUTPUT_DIR,
    OWNER_DELIVERY_MODE,
    TELEGRAM_ARCHIVE_PART_BYTES,
    TELEGRAM_TOKEN,
    TELEGRAM_UPLOAD_RETRIES,
    TELEGRAM_UPLOAD_TIMEOUT_SEC,
    TEMP_ROOT,
    ensure_runtime_dirs,
    validate_bot_config,
)
from crawler import AudioCrawler
from delivery import DeliveryService, build_original_archives, build_result_archives
from delivery import build_result_archive as build_result_archive
from delivery import deliverable_paths as deliverable_paths
from delivery_retry import DeliveryRetryStore
from exceptions import (
    BrowserUnavailableError,
    ConfigError,
    CrawlerError,
    CrawlLimitError,
    CrawlTimeoutError,
    HTTPError,
    NetworkError,
    NoAudioFoundError,
    PathTraversalError,
)
from organize import run_pipeline
from organizer import Organizer
from processor import AudioProcessor
from quality_gate import QualityGate
from utils.cleanup import cleanup_run, setup_cleanup

logger = logging.getLogger(__name__)
ACCESS_DB_PATH = DATA_DIR / "access-control.db"

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
            "📊 <b>KHO ÂM THANH ĐANG TRỐNG</b>\n\n"
            "Dạ, anh/chị vui lòng gửi một liên kết có âm thanh. "
            "Em sẽ hỗ trợ xử lý ngay khi nhận được liên kết."
        )

    lines = ["📊 <b>THỐNG KÊ THƯ VIỆN</b>", "", f"• <b>Tổng số mẫu:</b> {total}"]
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
            "Trang có thể chưa phát bản nghe thử, vừa đổi cấu trúc hoặc đang chặn trình duyệt tự động."
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
            lines.append(f"• <b>{label}:</b> {counts[status]}")

    outputs = [result.get("output") for result in results if result.get("status") == "passed"]
    if outputs:
        lines.extend(["", "<b>TỆP ĐÃ XỬ LÝ</b>"])
        for output in outputs[:5]:
            lines.append(f"• {html.escape(Path(str(output)).name)}")
        if len(outputs) > 5:
            lines.append(f"• Và {len(outputs) - 5} tệp khác")
    return "\n".join(lines)


def format_link_progress(
    stage: str,
    *,
    discovered: int = 0,
    downloaded: int = 0,
    processed: int = 0,
    failed_downloads: int = 0,
) -> str:
    """Show completed work with green checks and only the current operation as pending."""
    lines = ["🎧 <b>TIẾN TRÌNH XỬ LÝ</b>", "", "✅ <b>Đã nhận liên kết</b>"]
    if stage == "searching":
        lines.append("⏳ Đang tìm âm thanh...")
        return "\n".join(lines)

    lines.append(f"✅ <b>Đã tìm thấy:</b> {discovered} đường dẫn âm thanh")
    if stage == "downloading":
        lines.append(f"⏳ Đang tải tệp gốc: {downloaded}/{discovered}")
        return "\n".join(lines)

    lines.append(f"✅ <b>Đã tải tệp gốc:</b> {downloaded}")
    if failed_downloads:
        lines.append(f"⚠️ <b>Không tải được:</b> {failed_downloads} đường dẫn")
    if stage == "analyzing":
        lines.append(f"⏳ Đang kiểm tra và phân loại: {processed}/{downloaded}")
        return "\n".join(lines)
    if stage == "packaging":
        lines.append(f"⏳ Đang đóng gói và gửi {downloaded} file gốc...")
        return "\n".join(lines)
    if stage == "complete":
        lines.append(f"✅ <b>Đã gửi file gốc:</b> {processed} tệp")
        lines.append("✅ <b>Hoàn tất</b>")
        return "\n".join(lines)

    lines.append(f"✅ <b>Đã kiểm tra và phân loại:</b> {processed} tệp")
    lines.append("✅ <b>Hoàn tất xử lý</b>")
    return "\n".join(lines)


def format_link_failure(error_text: str) -> str:
    return (
        "🎧 <b>TIẾN TRÌNH XỬ LÝ</b>\n\n"
        "✅ <b>Đã nhận liên kết</b>\n"
        "❌ <b>Chưa hoàn tất xử lý</b>\n\n"
        f"{error_text}"
    )


def format_crawler_error(exc: CrawlerError) -> str:
    if isinstance(exc, NoAudioFoundError):
        return (
            "🔎 <b>Chưa tìm thấy tệp âm thanh trong liên kết này</b>\n\n"
            "Trang có thể vừa đổi cấu trúc hoặc chưa cung cấp bản nghe thử công khai. "
            "Bot đã dừng lượt này mà không chuyển sang thao tác khác."
        )
    if isinstance(exc, CrawlLimitError):
        return "📚 Catalogue vượt giới hạn quét an toàn. Em đã dừng để tránh trả kết quả bị thiếu."
    if isinstance(exc, CrawlTimeoutError):
        return "⏱️ Trang phản hồi quá lâu. Em đã dừng lượt quét để trợ lý không bị treo."
    if isinstance(exc, BrowserUnavailableError):
        return "🧩 Trình duyệt xử lý chưa sẵn sàng. Mong anh/chị vui lòng thử lại sau ít phút."
    if isinstance(exc, PathTraversalError):
        return (
            "🔗 Liên kết này chưa hợp lệ. Anh/chị vui lòng gửi liên kết đầy đủ "
            "bắt đầu bằng http:// hoặc https://."
        )
    if isinstance(exc, HTTPError) and exc.status_code == 429:
        return (
            "⏳ <b>Trang đang tạm giới hạn lượt truy cập</b>\n\n"
            "Bot đã tự thử lại nhưng trang vẫn yêu cầu chờ thêm. Không cần đăng nhập; "
            "anh/chị chỉ cần gửi lại liên kết sau ít phút."
        )
    if isinstance(exc, NetworkError):
        return (
            "🌐 Dạ, em chưa thể kết nối an toàn tới liên kết này. "
            "Anh/chị vui lòng kiểm tra lại liên kết giúp em."
        )
    return "⚠️ Dạ, em chưa thể xử lý trang này. Mong anh/chị vui lòng thử lại sau."


async def send_chunked(update: Update, text: str) -> None:
    if not update.effective_message:
        return
    for start in range(0, len(text), 3900):
        await update.effective_message.reply_text(text[start : start + 3900], parse_mode="HTML")


def format_welcome() -> str:
    """Explain the same adaptive workflow to every user."""
    return format_admin_welcome()


def format_admin_welcome() -> str:
    """Explain the download-first flow shown to approved users."""
    return (
        "🎧 <b>MH - DOWNSAMPLE PRO</b>\n\n"
        "Bot hỗ trợ tìm và tải âm thanh từ nhiều trang khác nhau. Anh không cần chọn "
        "trước một nền tảng cố định.\n\n"
        "<b>CÁCH SỬ DỤNG</b>\n"
        "1. Gửi liên kết của trang hoặc gói âm thanh cần tải.\n"
        "2. Bot tự tìm và tải các tệp âm thanh công khai trong liên kết.\n"
        "3. Bot giữ tên file nguồn và chia ZIP dưới 20 MB.\n"
        "4. Xong mỗi lô, bot gửi ngay rồi mới tải lô tiếp theo."
    )


def format_usage_guide() -> str:
    """Explain the one primary user journey without repeating product features."""
    return (
        "📖 <b>HƯỚNG DẪN SỬ DỤNG</b>\n\n"
        "Dạ, anh/chị chỉ cần thực hiện ba bước sau:\n\n"
        "1. Sao chép liên kết của trang hoặc tệp âm thanh cần xử lý.\n"
        "2. Dán liên kết vào ô tin nhắn trong cuộc trò chuyện này rồi bấm gửi.\n"
        "3. Em sẽ giữ file gốc, chia ZIP và gửi lại ngay trên Telegram."
    )


def main_menu(*, is_admin: bool = False) -> InlineKeyboardMarkup:
    """Build the public action menu and append private host actions for the administrator."""
    public_rows = [
        [InlineKeyboardButton("🔗 Gửi liên kết âm thanh", callback_data="menu:gui_lien_ket")],
        [
            InlineKeyboardButton("📖 Xem hướng dẫn", callback_data="menu:huong_dan"),
            InlineKeyboardButton("📊 Xem thống kê", callback_data="menu:thong_ke"),
        ],
    ]
    if is_admin:
        return InlineKeyboardMarkup(
            [
                *public_rows,
                [
                    InlineKeyboardButton("📁 Xử lý thư mục", callback_data="menu:sap_xep"),
                    InlineKeyboardButton("⚙️ Nơi lưu", callback_data="menu:cai_dat_thu_muc"),
                ],
                [
                    InlineKeyboardButton("🔑 Tạo mã mời", callback_data="menu:tao_ma"),
                    InlineKeyboardButton(
                        "👥 Xét duyệt người dùng", callback_data="menu:xet_duyet"
                    ),
                ],
            ]
        )
    return InlineKeyboardMarkup(public_rows)


def back_to_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Quay lại", callback_data="menu:quay_lai")]])


ACCESS_STATUS_LABELS = {
    AccessStatus.PENDING: "Đang chờ duyệt",
    AccessStatus.APPROVED: "Đã được duyệt",
    AccessStatus.REJECTED: "Đã bị từ chối",
    AccessStatus.BLOCKED: "Đã bị chặn",
    AccessStatus.REVOKED: "Đã bị thu hồi",
}


def access_gate_text(status: AccessStatus | None) -> str:
    if status is AccessStatus.PENDING:
        return (
            "⏳ <b>YÊU CẦU ĐANG CHỜ DUYỆT</b>\n\n"
            "Anh/chị chưa thể gửi job cho đến khi quản trị viên phê duyệt."
        )
    if status is AccessStatus.BLOCKED:
        return "⛔ <b>TÀI KHOẢN ĐÃ BỊ CHẶN</b>\n\nTài khoản này không thể gửi yêu cầu mới."
    if status is AccessStatus.REJECTED:
        title = "❌ <b>YÊU CẦU ĐÃ BỊ TỪ CHỐI</b>"
    elif status is AccessStatus.REVOKED:
        title = "🔒 <b>QUYỀN SỬ DỤNG ĐÃ BỊ THU HỒI</b>"
    else:
        title = "🔐 <b>BOT CHỈ DÀNH CHO NGƯỜI ĐÃ ĐƯỢC DUYỆT</b>"
    return (
        f"{title}\n\n"
        "Để gửi yêu cầu sử dụng, anh/chị cần mã mời dùng một lần từ quản trị viên."
    )


def access_request_keyboard(status: AccessStatus | None) -> InlineKeyboardMarkup | None:
    if status in {None, AccessStatus.REJECTED, AccessStatus.REVOKED}:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔐 Gửi yêu cầu sử dụng", callback_data="access:request")]]
        )
    return None


def admin_access_keyboard(user_id: int, status: AccessStatus) -> InlineKeyboardMarkup:
    prefix = f"access:admin:{{action}}:{user_id}:{status.value}"
    if status is AccessStatus.PENDING:
        actions = [("✅ Duyệt", "approve"), ("❌ Từ chối", "reject"), ("⛔ Chặn", "block")]
    elif status is AccessStatus.APPROVED:
        actions = [("🔒 Thu hồi", "revoke"), ("⛔ Chặn", "block")]
    elif status is AccessStatus.BLOCKED:
        actions = [("✅ Duyệt lại", "approve")]
    else:
        actions = [("✅ Duyệt", "approve"), ("⛔ Chặn", "block")]
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(label, callback_data=prefix.format(action=action))
                for label, action in actions
            ]
        ]
    )


def pending_access_keyboard(users: Sequence[AccessUser]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for user in users:
        label = user.full_name or (
            f"@{user.username}" if user.username else str(user.telegram_user_id)
        )
        short_label = label[:24]
        prefix = f"access:admin:{{action}}:{user.telegram_user_id}:pending"
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ {short_label}",
                    callback_data=prefix.format(action="approve"),
                ),
                InlineKeyboardButton(
                    "❌", callback_data=prefix.format(action="reject")
                ),
                InlineKeyboardButton(
                    "⛔", callback_data=prefix.format(action="block")
                ),
            ]
        )
    rows.append([InlineKeyboardButton("⬅️ Quay lại", callback_data="menu:quay_lai")])
    return InlineKeyboardMarkup(rows)


def format_pending_access(users: Sequence[AccessUser]) -> str:
    if not users:
        return (
            "👥 <b>XÉT DUYỆT NGƯỜI DÙNG</b>\n\n"
            "✅ Hiện không có yêu cầu nào đang chờ duyệt."
        )
    lines = ["👥 <b>XÉT DUYỆT NGƯỜI DÙNG</b>", ""]
    for index, user in enumerate(users, start=1):
        name = html.escape(user.full_name or "Không có tên")
        username = html.escape(f"@{user.username}" if user.username else "Không có")
        lines.append(
            f"{index}. <b>{name}</b> — "
            f"<code>{user.telegram_user_id}</code> — {username}"
        )
    lines.append("\nBấm ✅ để duyệt, ❌ để từ chối hoặc ⛔ để chặn.")
    return "\n".join(lines)


class AudioBot:
    def __init__(self) -> None:
        validate_bot_config()
        ensure_runtime_dirs()
        if ACCESS_DB_PATH.resolve() == DB_PATH.resolve():
            raise ConfigError("Access-control database must differ from audio-library database")
        self.access_control = AccessControlStore(ACCESS_DB_PATH)
        self.delivery_retry_store = DeliveryRetryStore(DATA_DIR / "delivery-retries.db")
        self.run_dir = setup_cleanup(TEMP_ROOT)
        self.gate = QualityGate()
        self.processor = AudioProcessor()
        self.output_dir = OUTPUT_DIR.resolve()
        self.organizer = Organizer(self.output_dir, DB_PATH)
        migration = self.organizer.ensure_layout(DOWNLOAD_DIR)
        if migration["organized"] or migration["raw"] or migration["missing"]:
            logger.info("Đã tự nâng cấp bố cục thư viện: %s", migration)
        self.crawler = AudioCrawler(DOWNLOAD_DIR, self.gate)
        self.url_job_lock = asyncio.Lock()
        self.profile_retry_task: asyncio.Task[None] | None = None

    @staticmethod
    def _is_admin(update: Update) -> bool:
        user = getattr(update, "effective_user", None)
        return bool(user and user.id == ADMIN_USER_ID)

    def _access_status(self, update: Update) -> AccessStatus | None:
        user = getattr(update, "effective_user", None)
        if not user:
            return None
        if user.id == ADMIN_USER_ID:
            return AccessStatus.APPROVED
        return self.access_control.status_for(user.id)

    def _has_access(self, update: Update) -> bool:
        return self._access_status(update) is AccessStatus.APPROVED

    async def _reply_access_gate(self, update: Update) -> None:
        message = update.effective_message
        if not message:
            return
        status = self._access_status(update)
        await message.reply_text(
            access_gate_text(status),
            parse_mode="HTML",
            reply_markup=access_request_keyboard(status),
        )

    async def cmd_start(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        if not self._has_access(update):
            await self._reply_access_gate(update)
            return
        is_admin = self._is_admin(update)
        await update.effective_message.reply_text(
            format_welcome(),
            parse_mode="HTML",
            reply_markup=main_menu(is_admin=is_admin),
        )

    async def cmd_request_access(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        if self._is_admin(update):
            await message.reply_text("✅ Tài khoản quản trị luôn có quyền sử dụng.")
            return
        invite_code = "".join(context.args).strip() if context.args else ""
        if not invite_code:
            await message.reply_text(
                "🔐 <b>GỬI YÊU CẦU SỬ DỤNG</b>\n\n"
                "Nhập lệnh <code>/yeucau MA_MOI</code> bằng mã dùng một lần do quản trị viên cấp.",
                parse_mode="HTML",
            )
            return
        outcome = self.access_control.submit_request(
            telegram_user_id=user.id,
            username=getattr(user, "username", None),
            full_name=getattr(user, "full_name", None),
            invite_code=invite_code,
        )
        if outcome is RequestOutcome.ALREADY_APPROVED:
            await message.reply_text("✅ Tài khoản đã được duyệt và có thể gửi liên kết.")
            return
        if outcome is RequestOutcome.ALREADY_PENDING:
            await message.reply_text("⏳ Yêu cầu của anh/chị đang chờ quản trị viên duyệt.")
            return
        if outcome is RequestOutcome.BLOCKED:
            await message.reply_text("⛔ Tài khoản đã bị chặn và không thể gửi yêu cầu mới.")
            return
        if outcome is RequestOutcome.INVALID_INVITE:
            await message.reply_text("❌ Mã mời không hợp lệ, đã được dùng hoặc đã hết hạn.")
            return

        await message.reply_text(
            "✅ <b>ĐÃ GỬI YÊU CẦU</b>\n\n"
            "Quản trị viên sẽ xem xét. Anh/chị chỉ có thể gửi job sau khi được duyệt.",
            parse_mode="HTML",
        )
        username = html.escape(f"@{user.username}" if getattr(user, "username", None) else "Không có")
        full_name = html.escape(str(getattr(user, "full_name", None) or "Không có"))
        try:
            await context.bot.send_message(
                chat_id=ADMIN_USER_ID,
                text=(
                    "🔐 <b>YÊU CẦU QUYỀN MỚI</b>\n\n"
                    f"• Telegram ID: <code>{user.id}</code>\n"
                    f"• Tên: {full_name}\n"
                    f"• Username: {username}"
                ),
                parse_mode="HTML",
                reply_markup=admin_access_keyboard(user.id, AccessStatus.PENDING),
            )
            await self.backup_database_to_telegram(context)
        except TelegramError:
            logger.exception("Không thể gửi thông báo yêu cầu quyền cho quản trị viên")

    async def cmd_create_invite(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_admin(update) or not update.effective_message:
            return
        hours = 24
        if context.args:
            try:
                hours = int(context.args[0])
            except ValueError:
                hours = 0
        if not 1 <= hours <= 168:
            await update.effective_message.reply_text(
                "⚠️ Thời hạn mã mời phải từ 1 đến 168 giờ. Ví dụ: <code>/taoma 24</code>",
                parse_mode="HTML",
            )
            return
        code = self.access_control.create_invite(
            created_by=ADMIN_USER_ID,
            ttl=timedelta(hours=hours),
        )
        await self.backup_database_to_telegram(context)
        await update.effective_message.reply_text(
            "🔑 <b>MÃ MỜI DÙNG MỘT LẦN</b>\n\n"
            f"<code>{code}</code>\n\n"
            f"Hết hạn sau {hours} giờ. Người dùng gửi: <code>/yeucau {code}</code>",
            parse_mode="HTML",
        )

    async def cmd_access_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        is_admin = self._is_admin(update)
        target_id = user.id
        if is_admin and context.args:
            try:
                target_id = int(context.args[0])
            except ValueError:
                await message.reply_text("⚠️ Telegram ID phải là một số nguyên.")
                return
        record = self.access_control.get_user(target_id)
        if record is None:
            text = "ℹ️ Tài khoản chưa gửi yêu cầu sử dụng."
            if is_admin and target_id != user.id:
                text += f"\nTelegram ID: <code>{target_id}</code>"
            await message.reply_text(text, parse_mode="HTML")
            return
        label = ACCESS_STATUS_LABELS[record.status]
        if is_admin:
            await message.reply_text(
                "🔐 <b>TRẠNG THÁI QUYỀN</b>\n\n"
                f"• Telegram ID: <code>{record.telegram_user_id}</code>\n"
                f"• Trạng thái: <b>{label}</b>",
                parse_mode="HTML",
                reply_markup=admin_access_keyboard(record.telegram_user_id, record.status),
            )
        else:
            await message.reply_text(
                "🔐 <b>TRẠNG THÁI QUYỀN CỦA BẠN</b>\n\n"
                f"Trạng thái: <b>{label}</b>",
                parse_mode="HTML",
            )

    async def cmd_pending_access(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if not self._is_admin(update) or not update.effective_message:
            return
        users = self.access_control.list_users(status=AccessStatus.PENDING, limit=20)
        await update.effective_message.reply_text(
            format_pending_access(users),
            parse_mode="HTML",
            reply_markup=pending_access_keyboard(users),
        )

    async def handle_access_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if not query:
            return
        data = query.data or ""
        if data == "access:request":
            await query.answer()
            status = self._access_status(update)
            if status is AccessStatus.BLOCKED:
                await query.edit_message_text(access_gate_text(status), parse_mode="HTML")
                return
            if status is AccessStatus.APPROVED:
                await query.edit_message_text(
                    format_welcome(),
                    parse_mode="HTML",
                    reply_markup=main_menu(is_admin=self._is_admin(update)),
                )
                return
            await query.edit_message_text(
                "🔐 <b>GỬI YÊU CẦU SỬ DỤNG</b>\n\n"
                "Nhập <code>/yeucau MA_MOI</code> bằng mã dùng một lần do quản trị viên cấp.",
                parse_mode="HTML",
            )
            return
        if not data.startswith("access:admin:"):
            await query.answer()
            return
        if not self._is_admin(update):
            await query.answer("Bạn không có quyền quản trị.", show_alert=True)
            return
        await query.answer()
        parts = data.split(":")
        if len(parts) != 5:
            return
        action = parts[2]
        try:
            target_id = int(parts[3])
            expected_status = AccessStatus(parts[4])
        except ValueError:
            return
        status_by_action = {
            "approve": AccessStatus.APPROVED,
            "reject": AccessStatus.REJECTED,
            "block": AccessStatus.BLOCKED,
            "revoke": AccessStatus.REVOKED,
        }
        new_status = status_by_action.get(action)
        if new_status is None:
            return
        changed = self.access_control.set_status(
            target_id,
            new_status,
            decided_by=ADMIN_USER_ID,
            expected_status=expected_status,
        )
        if not changed:
            current = self.access_control.get_user(target_id)
            if current is None:
                await query.edit_message_text("⚠️ Không tìm thấy yêu cầu quyền này.")
                return
            await query.edit_message_text(
                "⚠️ <b>TRẠNG THÁI ĐÃ THAY ĐỔI</b>\n\n"
                f"• Telegram ID: <code>{target_id}</code>\n"
                f"• Trạng thái hiện tại: <b>{ACCESS_STATUS_LABELS[current.status]}</b>",
                parse_mode="HTML",
                reply_markup=admin_access_keyboard(target_id, current.status),
            )
            return
        label = ACCESS_STATUS_LABELS[new_status]
        await query.edit_message_text(
            "🔐 <b>ĐÃ CẬP NHẬT QUYỀN</b>\n\n"
            f"• Telegram ID: <code>{target_id}</code>\n"
            f"• Trạng thái: <b>{label}</b>",
            parse_mode="HTML",
            reply_markup=admin_access_keyboard(target_id, new_status),
        )
        await self.backup_database_to_telegram(context)
        try:
            await context.bot.send_message(
                chat_id=target_id,
                text=f"🔐 Trạng thái quyền sử dụng của bạn: <b>{label}</b>",
                parse_mode="HTML",
            )
        except TelegramError:
            logger.warning("Không thể báo trạng thái quyền cho Telegram ID %s", target_id)

    async def handle_authorized_menu(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._has_access(update):
            await self.handle_menu(update, context)
            return
        if update.callback_query:
            await update.callback_query.answer("Quyền sử dụng chưa được duyệt.", show_alert=True)

    async def cmd_authorized_stats(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if self._has_access(update):
            await self.cmd_stats(update, context)
            return
        await self._reply_access_gate(update)

    async def handle_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if not query:
            return
        await query.answer()
        action = (query.data or "").removeprefix("menu:")
        is_admin = self._is_admin(update)

        if action == "quay_lai":
            await query.edit_message_text(
                format_welcome(),
                parse_mode="HTML",
                reply_markup=main_menu(is_admin=is_admin),
            )
            return
        if action == "gui_lien_ket":
            await query.edit_message_text(
                "🔗 <b>GỬI LIÊN KẾT ÂM THANH</b>\n\n"
                "Dạ, anh/chị vui lòng dán liên kết vào ô tin nhắn bên dưới rồi bấm gửi nhé.\n\n"
                "Liên kết hợp lệ bắt đầu bằng <code>http://</code> hoặc <code>https://</code>.",
                parse_mode="HTML",
                reply_markup=back_to_menu(),
            )
            return
        if action == "huong_dan":
            await query.edit_message_text(
                format_usage_guide(),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [
                        [InlineKeyboardButton("🔗 Gửi liên kết ngay", callback_data="menu:gui_lien_ket")],
                        [InlineKeyboardButton("⬅️ Quay lại", callback_data="menu:quay_lai")],
                    ]
                ),
            )
            return
        if action == "thong_ke":
            await query.edit_message_text(
                format_stats(self.organizer.get_stats()),
                parse_mode="HTML",
                reply_markup=back_to_menu(),
            )
            return
        if action == "tao_ma" and is_admin:
            code = self.access_control.create_invite(
                created_by=ADMIN_USER_ID,
                ttl=timedelta(hours=24),
            )
            await self.backup_database_to_telegram(context)
            await query.edit_message_text(
                "🔑 <b>MÃ MỜI DÙNG MỘT LẦN</b>\n\n"
                f"<code>{code}</code>\n\n"
                "Hết hạn sau 24 giờ. Gửi nguyên dòng này cho người dùng:\n"
                f"<code>/yeucau {code}</code>",
                parse_mode="HTML",
                reply_markup=back_to_menu(),
            )
            return
        if action == "xet_duyet" and is_admin:
            users = self.access_control.list_users(
                status=AccessStatus.PENDING,
                limit=20,
            )
            await query.edit_message_text(
                format_pending_access(users),
                parse_mode="HTML",
                reply_markup=pending_access_keyboard(users),
            )
            return
        if action == "sap_xep" and is_admin:
            await query.edit_message_text(
                "📁 <b>XỬ LÝ THƯ MỤC TRÊN MÁY CHỦ</b>\n\n"
                "Gửi lệnh sau, thay phần đường dẫn bằng thư mục cần xử lý:\n\n"
                "<code>/sapxep D:\\Thu-muc-am-thanh</code>",
                parse_mode="HTML",
                reply_markup=back_to_menu(),
            )
            return
        if action == "cai_dat_thu_muc" and is_admin:
            current = html.escape(str(self.output_dir))
            await query.edit_message_text(
                "⚙️ <b>CÀI ĐẶT NƠI LƯU SAMPLE</b>\n\n"
                f"Thư mục hiện tại: <code>{current}</code>\n\n"
                "Để đổi nơi lưu, gửi lệnh sau kèm đường dẫn tuyệt đối:\n"
                "<code>/datthumuc D:\\Sample-Library</code>\n\n"
                "Thiết lập mới được áp dụng ngay và lưu lại cho lần mở bot sau.",
                parse_mode="HTML",
                reply_markup=back_to_menu(),
            )
            return
        return

    async def cmd_stats(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        await send_chunked(update, format_stats(self.organizer.get_stats()))

    async def cmd_path(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if self._is_admin(update):
            raw_path = html.escape(str(DOWNLOAD_DIR.resolve()))
            organized_path = html.escape(str(self.output_dir))
            await send_chunked(
                update,
                "📁 <b>Thư mục lưu âm thanh</b>\n\n"
                f"Bản gốc đã tải: <code>{raw_path}</code>\n"
                f"Bản đã phân loại: <code>{organized_path}</code>",
            )

    async def cmd_set_output(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        raw_path = " ".join(context.args).strip() if context.args else ""
        if not raw_path:
            await send_chunked(
                update,
                "⚙️ <b>CHỌN NƠI LƯU SAMPLE</b>\n\n"
                f"Hiện tại: <code>{html.escape(str(self.output_dir))}</code>\n\n"
                "Ví dụ: <code>/datthumuc D:\\Sample-Library</code>",
            )
            return
        target = Path(raw_path).expanduser()
        if not target.is_absolute():
            await send_chunked(update, "⚠️ Anh hãy nhập đường dẫn đầy đủ, ví dụ D:\\Sample-Library.")
            return
        previous = self.output_dir
        try:
            resolved = target.resolve()
            run_dir = getattr(self, "run_dir", None)
            if run_dir:
                resolved_run_dir = Path(run_dir).resolve()
                if resolved == resolved_run_dir or resolved.is_relative_to(resolved_run_dir):
                    raise OSError("output directory cannot be inside temporary run directory")
            resolved.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=resolved, prefix=".write-test-", delete=True):
                pass
            set_key(BASE_DIR / ".env", "OUTPUT_DIR", str(resolved), quote_mode="always")
        except OSError:
            logger.exception("Không thể thiết lập thư mục đầu ra %s", target)
            await send_chunked(update, "❌ Không thể tạo hoặc ghi vào thư mục này.")
            return
        self.output_dir = resolved
        self.organizer.output_dir = resolved
        await send_chunked(
            update,
            "✅ <b>ĐÃ ĐỔI NƠI LƯU SAMPLE</b>\n\n"
            f"Thư mục mới: <code>{html.escape(str(resolved))}</code>\n\n"
            f"Tệp cũ vẫn được giữ nguyên tại <code>{html.escape(str(previous))}</code>.",
        )

    async def cmd_organize(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_admin(update):
            return
        if not context.args:
            await send_chunked(
                update,
                "⚠️ Anh chưa nhập đường dẫn thư mục.\n\nVí dụ: <code>/sapxep D:\\Thu-vien-am-thanh</code>",
            )
            return
        path = Path(" ".join(context.args)).expanduser()
        if not path.is_dir():
            await send_chunked(update, "❌ Không tìm thấy thư mục này. Anh kiểm tra lại đường dẫn nhé.")
            return
        await send_chunked(update, "⏳ Em đang quét và xử lý thư mục. Anh chờ em một chút nhé...")
        try:
            counts = await run_pipeline(path, self.output_dir, "telegram", delete_source=False)
            await send_chunked(update, format_counts(counts))
        except Exception:
            logger.exception("Không thể xử lý thư mục %s", path)
            await send_chunked(
                update, "❌ Em chưa xử lý được thư mục này. Anh kiểm tra lại tệp và thử lại nhé."
            )

    async def cmd_retry_delivery(
        self, update: Update, _context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Re-send only the requesting user's latest job manifest."""
        if not self._has_access(update):
            await self._reply_access_gate(update)
            return
        message = update.effective_message
        user = update.effective_user
        if not message or not user:
            return
        try:
            record = self.delivery_retry_store.load(user.id, self.output_dir)
        except Exception:
            logger.exception("Không thể đọc manifest delivery của Telegram ID %s", user.id)
            await send_chunked(
                update,
                "❌ Chưa thể đọc lịch sử giao file của anh/chị. Vui lòng thử lại sau.",
            )
            return
        if record is None:
            await send_chunked(
                update,
                "⚠️ Chưa có job nào của tài khoản này để tải lại. "
                "Bot không lấy file từ thư viện của người dùng khác.",
            )
            return

        await send_chunked(
            update,
            f"⏳ Đang đóng gói lại <b>{len(record.results)}</b> sample từ job gần nhất. "
            "Bot không tải hoặc xử lý lại từ đầu.",
        )
        await self._delivery_service(owner_mode="telegram").deliver(
            message,
            record.results,
            record.site,
            is_owner=False,
        )

    async def cmd_assign_retry(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Let the admin recover a pre-manifest library for one explicit customer."""
        if not self._is_admin(update):
            return
        if not context.args:
            await send_chunked(
                update,
                "⚠️ Nhập Telegram ID của khách. Ví dụ: <code>/ganjob 123456789</code>",
            )
            return
        try:
            target_id = int(context.args[0])
        except (TypeError, ValueError):
            await send_chunked(update, "❌ Telegram ID không hợp lệ.")
            return
        if target_id <= 0:
            await send_chunked(update, "❌ Telegram ID phải là số dương.")
            return
        if (
            target_id != ADMIN_USER_ID
            and self.access_control.status_for(target_id) is not AccessStatus.APPROVED
        ):
            await send_chunked(
                update,
                "❌ Tài khoản đích chưa được duyệt. Bot chưa gán file.",
            )
            return

        try:
            paths = sorted(
                (
                    path
                    for path in self.output_dir.rglob("*")
                    if path.is_file() and path.suffix.lower() in AUDIO_EXTS
                ),
                key=lambda path: path.as_posix().lower(),
            )
        except OSError:
            logger.exception("Không thể quét thư viện để cứu job cũ")
            await send_chunked(update, "❌ Không thể đọc thư viện hiện tại.")
            return
        if not paths:
            await send_chunked(update, "⚠️ Thư viện hiện tại không còn file audio.")
            return

        results = [{"status": "passed", "output": str(path)} for path in paths]
        try:
            saved = self.delivery_retry_store.save(
                target_id,
                "library-recovery",
                results,
                self.output_dir,
            )
        except Exception:
            logger.exception("Không thể gán job cũ cho Telegram ID %s", target_id)
            await send_chunked(update, "❌ Chưa thể lưu manifest cứu hộ.")
            return
        await send_chunked(
            update,
            "✅ <b>ĐÃ GÁN JOB CŨ CHO KHÁCH</b>\n\n"
            f"• Telegram ID: <code>{target_id}</code>\n"
            f"• Số file: <b>{saved}</b>\n\n"
            "Khách gửi <code>/taigoi</code> để nhận file.",
        )

    def _delivery_service(self, *, owner_mode: str | None = None) -> DeliveryService:
        return DeliveryService(
            output_root=self.output_dir,
            temp_root=self.run_dir,
            owner_mode=owner_mode or OWNER_DELIVERY_MODE,
            archive_part_bytes=TELEGRAM_ARCHIVE_PART_BYTES,
            upload_retries=TELEGRAM_UPLOAD_RETRIES,
            upload_timeout_sec=TELEGRAM_UPLOAD_TIMEOUT_SEC,
            archive_builder=build_result_archives,
        )

    async def _send_archive_with_retry(
        self, message: Any, archive_path: Path, caption: str
    ) -> bool:
        """Compatibility adapter; retry ownership lives in DeliveryService."""
        service = DeliveryService(
            output_root=Path(),
            temp_root=archive_path.parent,
            owner_mode=OWNER_DELIVERY_MODE,
            archive_part_bytes=TELEGRAM_ARCHIVE_PART_BYTES,
            upload_retries=TELEGRAM_UPLOAD_RETRIES,
            upload_timeout_sec=TELEGRAM_UPLOAD_TIMEOUT_SEC,
        )
        return await service.send_archive_with_retry(message, archive_path, caption)

    async def _send_processed_files(
        self,
        update: Update,
        results: Sequence[Mapping[str, Any]],
        site: str,
        *,
        retry_results: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        message = update.effective_message
        if not message:
            return
        user = update.effective_user
        retry_store = getattr(self, "delivery_retry_store", None)
        if user and retry_store:
            try:
                retry_store.save(
                    user.id,
                    site,
                    retry_results if retry_results is not None else results,
                    self.output_dir,
                )
            except Exception:
                logger.exception(
                    "Không thể lưu manifest delivery cho Telegram ID %s", user.id
                )
        await self._delivery_service().deliver(
            message,
            results,
            site,
            is_owner=self._is_admin(update),
        )

    async def _send_downloaded_files(
        self,
        update: Update,
        paths: Sequence[Path],
        site: str,
        raw_root: Path,
    ) -> bool:
        """Send original downloads immediately without analysis, conversion, or renaming."""
        message = update.effective_message
        if not message or not paths:
            return False
        results = [{"status": "passed", "output": str(path)} for path in paths]
        service = DeliveryService(
            output_root=raw_root,
            temp_root=self.run_dir,
            owner_mode="telegram",
            archive_part_bytes=min(TELEGRAM_ARCHIVE_PART_BYTES, 10 * 1024 * 1024),
            upload_retries=min(TELEGRAM_UPLOAD_RETRIES, 2),
            upload_timeout_sec=TELEGRAM_UPLOAD_TIMEOUT_SEC,
            archive_builder=build_original_archives,
            original_files=True,
            upload_attempt_guard_sec=45,
        )
        report = await service.deliver(message, results, site, is_owner=False)
        delivered = bool(report.sent_parts) and not report.failed_parts and not report.build_failed
        if delivered:
            for path in paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Không thể dọn file gốc đã gửi: %s", path)
            try:
                raw_root.rmdir()
            except OSError:
                pass
        return delivered

    async def handle_url(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.effective_message:
            return
        url = (update.effective_message.text or "").strip()
        site = source_name_from_url(url)
        logger.info("Bắt đầu xử lý liên kết từ %s", site)
        status_message = await update.effective_message.reply_text(
            format_link_progress("searching"),
            parse_mode="HTML",
        )
        try:
            urls = await self.crawler.sniff_urls(url)
            if not urls:
                raise NoAudioFoundError(f"No public audio assets were discovered on {site}")
            logger.info("Đã tìm thấy %d đường dẫn âm thanh từ %s", len(urls), site)
            raw_root = DOWNLOAD_DIR / site
            raw_root.mkdir(parents=True, exist_ok=True)
            raw_dir = Path(tempfile.mkdtemp(prefix="job-", dir=raw_root))
            await status_message.edit_text(
                format_link_progress("downloading", discovered=len(urls)),
                parse_mode="HTML",
            )
            loop = asyncio.get_running_loop()
            download_semaphore = asyncio.Semaphore(4)
            downloaded_total = 0
            processed_total = 0
            failures = 0
            delivered_batches = 0
            total_batches = (len(urls) + JOB_BATCH_FILES - 1) // JOB_BATCH_FILES

            async def download_one(audio_url: str) -> Path | None:
                async with download_semaphore:
                    return await loop.run_in_executor(
                        None,
                        partial(
                            self.crawler.download,
                            audio_url,
                            raw_dir,
                            self.crawler.discovered_titles.get(audio_url),
                        ),
                    )

            for batch_number, start in enumerate(
                range(0, len(urls), JOB_BATCH_FILES), start=1
            ):
                batch_urls = urls[start : start + JOB_BATCH_FILES]
                batch_downloaded: list[Path] = []
                download_tasks = [
                    asyncio.create_task(download_one(audio_url))
                    for audio_url in batch_urls
                ]
                for completed, download_task in enumerate(
                    asyncio.as_completed(download_tasks), start=1
                ):
                    try:
                        downloaded = await download_task
                        if downloaded is not None:
                            batch_downloaded.append(downloaded)
                            downloaded_total += 1
                        else:
                            failures += 1
                    except Exception as exc:
                        failures += 1
                        logger.warning("Không tải được một đường dẫn âm thanh: %s", exc)
                    if completed == 1 or completed % 10 == 0 or completed == len(batch_urls):
                        try:
                            await status_message.edit_text(
                                format_link_progress(
                                    "downloading",
                                    discovered=len(urls),
                                    downloaded=downloaded_total,
                                    failed_downloads=failures,
                                ),
                                parse_mode="HTML",
                            )
                        except TelegramError:
                            pass

                if not batch_downloaded:
                    continue

                processed_total += len(batch_downloaded)
                try:
                    await status_message.edit_text(
                        format_link_progress(
                            "packaging",
                            discovered=len(urls),
                            downloaded=downloaded_total,
                            processed=processed_total,
                            failed_downloads=failures,
                        ),
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass
                delivered = await self._send_downloaded_files(
                    update,
                    batch_downloaded,
                    site,
                    raw_dir,
                )
                if not delivered:
                    await status_message.edit_text(
                        format_link_failure(
                            f"Không gửi được lô {batch_number}/{total_batches}. "
                            "Bot đã dừng trước khi tải lô tiếp theo."
                        ),
                        parse_mode="HTML",
                    )
                    return
                delivered_batches += 1
                try:
                    await status_message.edit_text(
                        format_link_progress(
                            "packaging",
                            discovered=len(urls),
                            downloaded=downloaded_total,
                            processed=processed_total,
                            failed_downloads=failures,
                        )
                        + f"\n✅ Đã gửi xong lô {batch_number}/{total_batches}.",
                        parse_mode="HTML",
                    )
                except TelegramError:
                    pass

            if downloaded_total == 0:
                await status_message.edit_text(
                    format_link_failure(
                        f"⚠️ Không tải được tệp âm thanh nào từ {len(urls)} đường dẫn đã tìm thấy. "
                        "Trang có thể đã đổi cách cung cấp bản nghe thử hoặc tạm thời từ chối tải."
                    ),
                    parse_mode="HTML",
                )
                return

            await status_message.edit_text(
                format_link_progress(
                    "complete",
                    discovered=len(urls),
                    downloaded=downloaded_total,
                    processed=processed_total,
                    failed_downloads=failures,
                ),
                parse_mode="HTML",
            )
            if delivered_batches == 0:
                await status_message.edit_text(
                    format_link_failure("Không có file gốc nào được gửi."),
                    parse_mode="HTML",
                )
        except CrawlerError as exc:
            logger.warning("Không thể quét liên kết từ %s: %s", site, exc)
            error_text = format_crawler_error(exc)
            try:
                await status_message.edit_text(
                    format_link_failure(error_text),
                    parse_mode="HTML",
                )
            except TelegramError:
                logger.warning("Không thể cập nhật tin nhắn tiến trình; gửi thông báo mới thay thế")
                await update.effective_message.reply_text(
                    format_link_failure(error_text),
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Lỗi ngoài dự kiến khi xử lý liên kết")
            error_text = (
                "⚠️ Dạ, hệ thống vừa gặp lỗi ngoài dự kiến và đã dừng an toàn. "
                "Mong anh/chị vui lòng thử lại giúp em."
            )
            try:
                await status_message.edit_text(format_link_failure(error_text), parse_mode="HTML")
            except TelegramError:
                await send_chunked(update, error_text)

    async def handle_authorized_url(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Check approval and serialize access to shared URL-pipeline state."""
        if not self._has_access(update):
            message = update.effective_message
            user = update.effective_user
            message_text = getattr(message, "text", None)
            if message and user and isinstance(message_text, str) and message_text:
                text = message_text.strip()
                # Tự động nhận diện nếu tin nhắn gửi lên là mã mời
                try:
                    outcome = self.access_control.submit_request(
                        telegram_user_id=user.id,
                        username=getattr(user, "username", None),
                        full_name=getattr(user, "full_name", None),
                        invite_code=text,
                    )
                    if outcome is RequestOutcome.CREATED:
                        await message.reply_text(
                            "✅ <b>ĐÃ GỬI YÊU CẦU THÀNH CÔNG</b>\n\n"
                            "Quản trị viên sẽ xem xét. Anh/chị chỉ có thể sử dụng sau khi được duyệt.",
                            parse_mode="HTML",
                        )
                        username = html.escape(
                            f"@{user.username}" if getattr(user, "username", None) else "Không có"
                        )
                        full_name = html.escape(str(getattr(user, "full_name", None) or "Không có"))
                        await context.bot.send_message(
                            chat_id=ADMIN_USER_ID,
                            text=(
                                "🔐 <b>YÊU CẦU QUYỀN MỚI</b>\n\n"
                                f"• Telegram ID: <code>{user.id}</code>\n"
                                f"• Tên: {full_name}\n"
                                f"• Username: {username}"
                            ),
                            parse_mode="HTML",
                            reply_markup=admin_access_keyboard(user.id, AccessStatus.PENDING),
                        )
                        await self.backup_database_to_telegram(context)
                        return
                except Exception:
                    pass

            await self._reply_access_gate(update)
            return
        if not hasattr(self, "url_job_lock"):
            self.url_job_lock = asyncio.Lock()
        if self.url_job_lock.locked():
            if update.effective_message:
                await update.effective_message.reply_text(
                    "⏳ Bot đang xử lý một job khác. Anh/chị vui lòng gửi lại liên kết sau."
                )
            return
        async with self.url_job_lock:
            if not self._has_access(update):
                await self._reply_access_gate(update)
                return
            await self.handle_url(update, context)

    async def backup_database_to_telegram(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            self.access_control.checkpoint()
            message = await context.bot.send_document(
                chat_id=ADMIN_USER_ID,
                document=open(ACCESS_DB_PATH, "rb"),
                filename="access-control.db",
                caption="📦 Bản sao lưu dữ liệu phân quyền tự động.",
            )
            await context.bot.pin_chat_message(
                chat_id=ADMIN_USER_ID,
                message_id=message.message_id,
                disable_notification=True,
            )
            logger.info("Đã tự động sao lưu và ghim database lên Telegram.")
        except Exception as e:
            logger.error("Lỗi khi tự động sao lưu database lên Telegram: %s", e)

    async def configure_profile(self, application: Application) -> None:
        # Khôi phục database từ tin nhắn ghim nếu có
        try:
            chat = await application.bot.get_chat(chat_id=ADMIN_USER_ID)
            if chat.pinned_message and chat.pinned_message.document:
                doc = chat.pinned_message.document
                if doc.file_name == "access-control.db":
                    file_id = doc.file_id
                    file = await application.bot.get_file(file_id)
                    ACCESS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
                    await file.download_to_drive(custom_path=ACCESS_DB_PATH)
                    logger.info("Đã khôi phục thành công access-control.db từ tin nhắn ghim Telegram.")
        except Exception as e:
            logger.warning("Không thể tự động khôi phục database từ Telegram: %s", e)

        name = "MH - Downsample Pro"
        short_description = "Tải file âm thanh gốc, giữ tên nguồn và gửi ZIP theo từng lô."
        description = (
            "🎧 Trợ lý tải mẫu âm thanh dành cho người làm nhạc Việt Nam.\n\n"
            "Gửi một đường dẫn có âm thanh để hệ thống tải file gốc, giữ tên nguồn, "
            "chia ZIP an toàn và gửi từng lô ngay trên Telegram."
        )
        public_commands = [
            BotCommand("batdau", "Mở hướng dẫn sử dụng"),
            BotCommand("yeucau", "Gửi yêu cầu bằng mã mời"),
            BotCommand("quyen", "Xem trạng thái quyền sử dụng"),
            BotCommand("thongke", "Xem thống kê thư viện âm thanh"),
            BotCommand("taigoi", "Đóng gói lại thư viện hiện có"),
        ]
        admin_commands = [
            *public_commands,
            BotCommand("taoma", "Tạo mã mời dùng một lần"),
            BotCommand("duyet", "Xem người dùng đang chờ duyệt"),
            BotCommand("thumuc", "Xem nơi lưu âm thanh trên máy chủ"),
            BotCommand("datthumuc", "Chọn nơi lưu sample trên máy chủ"),
            BotCommand("sapxep", "Xử lý một thư mục trên máy chủ"),
            BotCommand("ganjob", "Gán job cũ cho đúng khách"),
        ]
        try:
            if (await application.bot.get_my_name()).name != name:
                await application.bot.set_my_name(name)
            if (await application.bot.get_my_short_description()).short_description != short_description:
                await application.bot.set_my_short_description(short_description)
            if (await application.bot.get_my_description()).description != description:
                await application.bot.set_my_description(description)
            current_public_commands = await application.bot.get_my_commands()
            current_public_values = [(item.command, item.description) for item in current_public_commands]
            wanted_public_values = [(item.command, item.description) for item in public_commands]
            if current_public_values != wanted_public_values:
                await application.bot.set_my_commands(public_commands)

            admin_scope = BotCommandScopeChat(chat_id=ADMIN_USER_ID)
            current_admin_commands = await application.bot.get_my_commands(scope=admin_scope)
            current_admin_values = [(item.command, item.description) for item in current_admin_commands]
            wanted_admin_values = [(item.command, item.description) for item in admin_commands]
            if current_admin_values != wanted_admin_values:
                await application.bot.set_my_commands(admin_commands, scope=admin_scope)
        except RetryAfter as exc:
            retry_after = exc.retry_after
            delay = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
            logger.warning("Telegram tam gioi han cap nhat ho so; se thu lai sau %.0f giay", delay)
            if not self.profile_retry_task or self.profile_retry_task.done():
                self.profile_retry_task = asyncio.create_task(self._retry_profile(application, delay + 1))
        except TelegramError as exc:
            logger.warning("Chưa thể đồng bộ hồ sơ Telegram; trợ lý vẫn tiếp tục chạy: %s", exc)

        # Khởi chạy Web Server để Render ping giúp Bot online 24/24
        import os

        from aiohttp import web

        async def health_check(request):
            return web.Response(text="OK")

        async def start_web_server():
            try:
                app = web.Application()
                app.router.add_get("/", health_check)
                app.router.add_get("/health", health_check)
                runner = web.AppRunner(app)
                await runner.setup()
                port = int(os.getenv("PORT", "8080"))
                site = web.TCPSite(runner, "0.0.0.0", port)  # nosec B104
                await site.start()
                logger.info("Web server started successfully on port %d", port)
            except Exception as e:
                logger.error("Failed to start health check web server: %s", e)

        asyncio.create_task(start_web_server())

    async def _retry_profile(self, application: Application, delay: float) -> None:
        await asyncio.sleep(delay)
        self.profile_retry_task = None
        await self.configure_profile(application)

    async def shutdown(self, _application: Application) -> None:
        if self.profile_retry_task and not self.profile_retry_task.done():
            self.profile_retry_task.cancel()
            await asyncio.gather(self.profile_retry_task, return_exceptions=True)
        self.crawler.close()
        self.organizer.db.checkpoint()
        self.organizer.db.close_all()
        self.access_control.checkpoint()
        cleanup_run(self.run_dir)

    def run(self) -> None:
        application = (
            Application.builder()
            .token(TELEGRAM_TOKEN)
            .read_timeout(30)
            .write_timeout(30)
            .media_write_timeout(TELEGRAM_UPLOAD_TIMEOUT_SEC)
            .connect_timeout(15)
            .pool_timeout(30)
            .post_init(self.configure_profile)
            .post_shutdown(self.shutdown)
            .build()
        )
        application.add_handler(CommandHandler(["start", "batdau"], self.cmd_start))
        application.add_handler(CommandHandler("yeucau", self.cmd_request_access))
        application.add_handler(CommandHandler("quyen", self.cmd_access_status))
        application.add_handler(CommandHandler("taoma", self.cmd_create_invite))
        application.add_handler(CommandHandler("duyet", self.cmd_pending_access))
        application.add_handler(CommandHandler(["stats", "thongke"], self.cmd_authorized_stats))
        application.add_handler(CommandHandler(["path", "thumuc"], self.cmd_path))
        application.add_handler(CommandHandler("datthumuc", self.cmd_set_output))
        application.add_handler(CommandHandler(["organize", "sapxep"], self.cmd_organize))
        application.add_handler(CommandHandler("taigoi", self.cmd_retry_delivery))
        application.add_handler(CommandHandler("ganjob", self.cmd_assign_retry))
        application.add_handler(
            CallbackQueryHandler(self.handle_access_callback, pattern=r"^access:")
        )
        application.add_handler(
            CallbackQueryHandler(self.handle_authorized_menu, pattern=r"^menu:")
        )
        application.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self.handle_authorized_url,
                block=False,
            )
        )
        application.run_polling()


if __name__ == "__main__":
    configure_bot_logging()
    AudioBot().run()
