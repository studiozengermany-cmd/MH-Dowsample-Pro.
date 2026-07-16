"""Delivery-only packaging and Telegram transport for completed audio results."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any, Literal, cast

from telegram.error import NetworkError as TelegramNetworkError
from telegram.error import RetryAfter, TelegramError

logger = logging.getLogger(__name__)

DeliverableStatus = Literal["passed", "duplicate"]
ArchiveBuilder = Callable[[Sequence[Path], Path, Path, str, int], list[Path]]


@dataclass(frozen=True)
class DeliveryItem:
    """One unique library file eligible for delivery."""

    status: DeliverableStatus
    path: Path


@dataclass(frozen=True)
class DeliveryManifest:
    """Runtime summary of one core result list at the delivery boundary."""

    items: tuple[DeliveryItem, ...]
    passed_count: int
    duplicate_count: int
    rejected_count: int
    error_count: int
    unavailable_count: int
    other_count: int

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(item.path for item in self.items)

    @property
    def ready_count(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class DeliveryReport:
    """Observable outcome used by callers and tests without exposing temporary files."""

    manifest: DeliveryManifest
    local_notified: bool = False
    archive_count: int = 0
    sent_parts: tuple[int, ...] = ()
    failed_parts: tuple[int, ...] = ()
    build_failed: bool = False


def build_delivery_manifest(results: Sequence[Mapping[str, Any]]) -> DeliveryManifest:
    """Classify core results and retain each existing deliverable path only once."""
    counts = {"passed": 0, "duplicate": 0, "rejected": 0, "error": 0}
    unavailable_count = 0
    other_count = 0
    items: list[DeliveryItem] = []
    seen: set[Path] = set()

    for result in results:
        status = str(result.get("status", "error"))
        if status in counts:
            counts[status] += 1
        else:
            other_count += 1

        if status not in {"passed", "duplicate"}:
            continue
        output = result.get("output")
        if not output:
            unavailable_count += 1
            continue
        path = Path(str(output))
        if not path.is_file():
            unavailable_count += 1
            continue
        if path in seen:
            continue
        items.append(DeliveryItem(status=cast(DeliverableStatus, status), path=path))
        seen.add(path)

    return DeliveryManifest(
        items=tuple(items),
        passed_count=counts["passed"],
        duplicate_count=counts["duplicate"],
        rejected_count=counts["rejected"],
        error_count=counts["error"],
        unavailable_count=unavailable_count,
        other_count=other_count,
    )


def deliverable_paths(results: Sequence[Mapping[str, Any]]) -> list[Path]:
    """Compatibility helper returning unique files eligible for delivery."""
    return list(build_delivery_manifest(results).paths)


def build_result_archive(paths: Sequence[Path], output_root: Path, archive_path: Path) -> Path:
    """Bundle result files while preserving organized library folders."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                try:
                    relative = path.resolve().relative_to(output_root.resolve())
                except ValueError:
                    relative = Path(path.name)
                member = relative.as_posix()
                if member in used_names:
                    stem, suffix, counter = relative.stem, relative.suffix, 2
                    while member in used_names:
                        member = relative.with_name(f"{stem} ({counter}){suffix}").as_posix()
                        counter += 1
                archive.write(path, member)
                used_names.add(member)
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def cleanup_archives(paths: Sequence[Path]) -> None:
    """Best-effort removal limited to temporary archive paths created by delivery."""
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Không thể dọn gói delivery tạm %s", path, exc_info=True)


def build_result_archives(
    paths: Sequence[Path],
    output_root: Path,
    archive_dir: Path,
    archive_stem: str,
    max_part_bytes: int,
) -> list[Path]:
    """Build ZIP parts whose final on-disk sizes do not exceed the upload limit."""
    if max_part_bytes <= 0:
        raise ValueError("max_part_bytes must be positive")
    if not paths:
        return []
    archive_dir.mkdir(parents=True, exist_ok=True)
    initial_batches: list[list[Path]] = []
    current: list[Path] = []
    current_bytes = 0
    for path in paths:
        size = path.stat().st_size
        if current and current_bytes + size > max_part_bytes:
            initial_batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += size
    if current:
        initial_batches.append(current)

    with tempfile.TemporaryDirectory(prefix="delivery-build-", dir=archive_dir) as probe_dir_text:
        probe_dir = Path(probe_dir_text)
        pending = initial_batches
        staged: list[Path] = []
        probe_index = 0
        while pending:
            batch = pending.pop(0)
            probe_index += 1
            probe = probe_dir / f"part-{probe_index}.zip"
            build_result_archive(batch, output_root, probe)
            if probe.stat().st_size <= max_part_bytes:
                staged.append(probe)
                continue
            probe.unlink(missing_ok=True)
            if len(batch) == 1:
                raise ValueError(f"{batch[0].name} cannot fit in a {max_part_bytes}-byte ZIP part")
            middle = len(batch) // 2
            pending[0:0] = [batch[:middle], batch[middle:]]

        archives: list[Path] = []
        total = len(staged)
        try:
            for index, staged_path in enumerate(staged, start=1):
                suffix = "" if total == 1 else f"-{index:02d}-of-{total:02d}"
                archive_path = archive_dir / f"{archive_stem}{suffix}.zip"
                os.replace(staged_path, archive_path)
                archives.append(archive_path)
            return archives
        except Exception:
            cleanup_archives(archives)
            raise


def _safe_archive_stem(site: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", site).strip("-._")
    return f"{cleaned or 'audio'}-samples"


def _status_lines(manifest: DeliveryManifest) -> list[str]:
    lines = [
        f"• File mới đã xử lý: <b>{manifest.passed_count}</b>",
        f"• File trùng đã có sẵn: <b>{manifest.duplicate_count}</b>",
        f"• File bị loại: <b>{manifest.rejected_count}</b>",
        f"• File lỗi: <b>{manifest.error_count}</b>",
    ]
    if manifest.unavailable_count:
        lines.append(f"• File kết quả không còn sẵn sàng: <b>{manifest.unavailable_count}</b>")
    if manifest.other_count:
        lines.append(f"• Trạng thái khác: <b>{manifest.other_count}</b>")
    return lines


class DeliveryService:
    """Deliver completed core results without mutating library or raw files."""

    def __init__(
        self,
        *,
        output_root: Path,
        temp_root: Path,
        owner_mode: str,
        archive_part_bytes: int,
        upload_retries: int,
        upload_timeout_sec: float,
        archive_builder: ArchiveBuilder = build_result_archives,
    ) -> None:
        if owner_mode not in {"local", "telegram", "both"}:
            raise ValueError("owner_mode must be local, telegram, or both")
        if archive_part_bytes <= 0:
            raise ValueError("archive_part_bytes must be positive")
        if upload_retries < 1:
            raise ValueError("upload_retries must be positive")
        if upload_timeout_sec < 1:
            raise ValueError("upload_timeout_sec must be positive")
        self.output_root = output_root
        self.temp_root = temp_root
        self.owner_mode = owner_mode
        self.archive_part_bytes = archive_part_bytes
        self.upload_retries = upload_retries
        self.upload_timeout_sec = upload_timeout_sec
        self.archive_builder = archive_builder

    async def send_archive_with_retry(
        self,
        message: Any,
        archive_path: Path,
        caption: str,
    ) -> bool:
        for attempt in range(1, self.upload_retries + 1):
            delay = 0.0
            try:
                with archive_path.open("rb") as bundle:
                    await message.reply_document(
                        document=bundle,
                        filename=archive_path.name,
                        caption=caption,
                        parse_mode="HTML",
                        read_timeout=self.upload_timeout_sec,
                        write_timeout=self.upload_timeout_sec,
                        connect_timeout=30,
                        pool_timeout=30,
                    )
                return True
            except RetryAfter as exc:
                retry_after = exc.retry_after
                delay = (
                    retry_after.total_seconds()
                    if isinstance(retry_after, timedelta)
                    else float(retry_after)
                )
                logger.warning(
                    "Telegram giới hạn gửi %s; thử lại lần %d sau %.0f giây",
                    archive_path.name,
                    attempt,
                    delay,
                )
            except TelegramNetworkError as exc:
                delay = min(2 ** (attempt - 1), 15)
                logger.warning(
                    "Kết nối Telegram lỗi khi gửi %s lần %d/%d: %s",
                    archive_path.name,
                    attempt,
                    self.upload_retries,
                    exc,
                )
            except TelegramError as exc:
                logger.error("Telegram từ chối gói %s: %s", archive_path.name, exc)
                return False
            if attempt < self.upload_retries:
                await asyncio.sleep(delay)
        return False

    async def deliver(
        self,
        message: Any,
        results: Sequence[Mapping[str, Any]],
        site: str,
        *,
        is_owner: bool,
    ) -> DeliveryReport:
        manifest = build_delivery_manifest(results)
        if not manifest.paths:
            await message.reply_text(
                "⚠️ <b>KHÔNG CÓ FILE ĐỦ ĐIỀU KIỆN GIAO</b>\n\n"
                + "\n".join(_status_lines(manifest)),
                parse_mode="HTML",
            )
            return DeliveryReport(manifest=manifest)

        local_notified = False
        if is_owner and self.owner_mode in {"local", "both"}:
            total_mb = sum(path.stat().st_size for path in manifest.paths) / (1024 * 1024)
            await message.reply_text(
                "📁 <b>ĐÃ LƯU KẾT QUẢ TRÊN MÁY</b>\n\n"
                + "\n".join(_status_lines(manifest))
                + f"\n• Số file sẵn sàng: <b>{manifest.ready_count}</b>"
                + f"\n• Dung lượng: <b>{total_mb:.1f} MB</b>"
                + f"\n• Thư mục: <code>{html.escape(str(self.output_root))}</code>\n\n"
                + "Dùng lệnh /thumuc để xem lại nơi lưu bất kỳ lúc nào.",
                parse_mode="HTML",
            )
            local_notified = True
            if self.owner_mode == "local":
                return DeliveryReport(manifest=manifest, local_notified=True)

        archive_paths: list[Path] = []
        sent_parts: list[int] = []
        failed_parts: list[int] = []
        try:
            archive_paths = await asyncio.get_running_loop().run_in_executor(
                None,
                partial(
                    self.archive_builder,
                    manifest.paths,
                    self.output_root,
                    self.temp_root,
                    _safe_archive_stem(site),
                    self.archive_part_bytes,
                ),
            )
            for index, archive_path in enumerate(archive_paths, start=1):
                part = "" if len(archive_paths) == 1 else f" — gói <b>{index}/{len(archive_paths)}</b>"
                caption = (
                    f"✅ <b>Đã gom gọn {manifest.ready_count} sample</b>{part}\n"
                    + "\n".join(_status_lines(manifest))
                    + "\nCác file trong gói giữ nguyên thư mục phân loại."
                )
                if await self.send_archive_with_retry(message, archive_path, caption):
                    sent_parts.append(index)
                else:
                    failed_parts.append(index)
            if failed_parts:
                failed_text = ", ".join(str(part_number) for part_number in failed_parts)
                await message.reply_text(
                    "⚠️ <b>Telegram chưa nhận được một số gói</b>\n\n"
                    f"Các gói chưa gửi được: <b>{failed_text}</b>. "
                    f"Bot đã thử lại {self.upload_retries} lần cho mỗi gói. "
                    "Kết quả xử lý vẫn được giữ an toàn trên máy chủ.",
                    parse_mode="HTML",
                )
        except Exception:
            logger.exception("Không thể tạo gói kết quả")
            await message.reply_text(
                "⚠️ <b>Chưa tạo được gói sample</b>\n\n"
                "Không thể đọc hoặc đóng gói một số file. "
                "Kết quả đã xử lý vẫn được giữ trên máy chủ.",
                parse_mode="HTML",
            )
            return DeliveryReport(
                manifest=manifest,
                local_notified=local_notified,
                build_failed=True,
            )
        finally:
            cleanup_archives(archive_paths)

        return DeliveryReport(
            manifest=manifest,
            local_notified=local_notified,
            archive_count=len(archive_paths),
            sent_parts=tuple(sent_parts),
            failed_parts=tuple(failed_parts),
        )
