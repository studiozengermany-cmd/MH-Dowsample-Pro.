"""Delivery-only packaging and Telegram transport for completed audio results."""

from __future__ import annotations

import asyncio
import errno
import html
import logging
import os
import re
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
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


def build_result_archive(
    paths: Sequence[Path],
    output_root: Path,
    archive_path: Path,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> Path:
    """Bundle result files while preserving organized library folders."""
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()
    try:
        with zipfile.ZipFile(archive_path, "w", compression=compression) as archive:
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


def _partition_readable_paths(
    paths: Sequence[Path], max_part_bytes: int
) -> tuple[list[list[Path]], list[Path]]:
    """Make small delivery batches and isolate paths that vanished before packaging."""
    batches: list[list[Path]] = []
    unavailable: list[Path] = []
    current: list[Path] = []
    current_bytes = 0

    for path in paths:
        try:
            size = path.stat().st_size
        except OSError:
            logger.warning(
                "File kết quả không còn đọc được trước khi đóng gói: %s", path
            )
            unavailable.append(path)
            continue

        if current and current_bytes + size > max_part_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += size

    if current:
        batches.append(current)
    return batches, unavailable


def _mark_paths_unavailable(
    manifest: DeliveryManifest, paths: Sequence[Path]
) -> DeliveryManifest:
    unavailable = set(paths)
    if not unavailable:
        return manifest
    remaining = tuple(item for item in manifest.items if item.path not in unavailable)
    removed = len(manifest.items) - len(remaining)
    return replace(
        manifest,
        items=remaining,
        unavailable_count=manifest.unavailable_count + removed,
    )


def build_result_archives(
    paths: Sequence[Path],
    output_root: Path,
    archive_dir: Path,
    archive_stem: str,
    max_part_bytes: int,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
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
            if compression == zipfile.ZIP_DEFLATED:
                build_result_archive(batch, output_root, probe)
            else:
                build_result_archive(
                    batch, output_root, probe, compression=compression
                )
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


def build_original_archives(
    paths: Sequence[Path],
    output_root: Path,
    archive_dir: Path,
    archive_stem: str,
    max_part_bytes: int,
) -> list[Path]:
    """Package already-compressed source audio without recompressing it."""
    return build_result_archives(
        paths,
        output_root,
        archive_dir,
        archive_stem,
        max_part_bytes,
        compression=zipfile.ZIP_STORED,
    )


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
        original_files: bool = False,
        upload_attempt_guard_sec: float = 45.0,
    ) -> None:
        if owner_mode not in {"local", "telegram", "both"}:
            raise ValueError("owner_mode must be local, telegram, or both")
        if archive_part_bytes <= 0:
            raise ValueError("archive_part_bytes must be positive")
        if upload_retries < 1:
            raise ValueError("upload_retries must be positive")
        if upload_timeout_sec < 1:
            raise ValueError("upload_timeout_sec must be positive")
        if upload_attempt_guard_sec <= 0:
            raise ValueError("upload_attempt_guard_sec must be positive")
        self.output_root = output_root
        self.temp_root = temp_root
        self.owner_mode = owner_mode
        self.archive_part_bytes = archive_part_bytes
        self.upload_retries = upload_retries
        self.upload_timeout_sec = upload_timeout_sec
        self.archive_builder = archive_builder
        self.original_files = original_files
        self.upload_attempt_guard_sec = upload_attempt_guard_sec

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
                    await asyncio.wait_for(
                        message.reply_document(
                            document=bundle,
                            filename=archive_path.name,
                            caption=caption,
                            parse_mode="HTML",
                            read_timeout=self.upload_timeout_sec,
                            write_timeout=self.upload_timeout_sec,
                            connect_timeout=30,
                            pool_timeout=30,
                        ),
                        timeout=self.upload_attempt_guard_sec,
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
            except TimeoutError:
                delay = min(2 ** (attempt - 1), 15)
                logger.warning(
                    "Dừng lần gửi %s bị treo sau %.0f giây (%d/%d)",
                    archive_path.name,
                    self.upload_attempt_guard_sec,
                    attempt,
                    self.upload_retries,
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
        batches, unavailable_paths = _partition_readable_paths(
            manifest.paths, self.archive_part_bytes
        )
        manifest = _mark_paths_unavailable(manifest, unavailable_paths)
        if not manifest.paths:
            await message.reply_text(
                "⚠️ <b>KHÔNG CÓ FILE ĐỦ ĐIỀU KIỆN GIAO</b>\n\n"
                + "\n".join(_status_lines(manifest)),
                parse_mode="HTML",
            )
            return DeliveryReport(manifest=manifest)

        local_notified = False
        if is_owner and self.owner_mode in {"local", "both"}:
            total_bytes = 0
            for path in manifest.paths:
                try:
                    total_bytes += path.stat().st_size
                except OSError:
                    logger.warning("Không thể tính dung lượng file kết quả %s", path)
            total_mb = total_bytes / (1024 * 1024)
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

        archive_count = 0
        sent_parts: list[int] = []
        failed_parts: list[int] = []
        failed_build_paths: list[Path] = []
        fatal_build_error: OSError | None = None
        build_attempt = 0
        archive_stem = _safe_archive_stem(site)

        async def build_and_send(batch: Sequence[Path]) -> bool:
            """Return false only for a fatal storage error that should stop this delivery."""
            nonlocal archive_count, build_attempt, fatal_build_error
            build_attempt += 1
            batch_stem = (
                archive_stem
                if len(batches) == 1 and build_attempt == 1
                else f"{archive_stem}-batch-{build_attempt:03d}"
            )
            archive_paths: list[Path] = []
            try:
                archive_paths = await asyncio.get_running_loop().run_in_executor(
                    None,
                    partial(
                        self.archive_builder,
                        batch,
                        self.output_root,
                        self.temp_root,
                        batch_stem,
                        self.archive_part_bytes,
                    ),
                )
                if not archive_paths:
                    raise RuntimeError("archive builder returned no files")
            except OSError as exc:
                if exc.errno == errno.ENOSPC:
                    fatal_build_error = exc
                    logger.exception("Ổ đĩa tạm hết chỗ khi tạo gói kết quả")
                    return False
                if len(batch) > 1:
                    middle = len(batch) // 2
                    return await build_and_send(batch[:middle]) and await build_and_send(
                        batch[middle:]
                    )
                logger.exception("Không thể đóng gói file kết quả %s", batch[0])
                failed_build_paths.append(batch[0])
                return True
            except Exception:
                if len(batch) > 1:
                    middle = len(batch) // 2
                    return await build_and_send(batch[:middle]) and await build_and_send(
                        batch[middle:]
                    )
                logger.exception("Không thể đóng gói file kết quả %s", batch[0])
                failed_build_paths.append(batch[0])
                return True

            try:
                for archive_path in archive_paths:
                    archive_count += 1
                    part_number = archive_count
                    if len(batch) == manifest.ready_count and len(archive_paths) == 1:
                        heading = f"✅ <b>Đã gom gọn {manifest.ready_count} sample</b>\n"
                    else:
                        heading = (
                            "✅ <b>Gói sample đã sẵn sàng</b> — "
                            f"gói <b>{part_number}</b>\n"
                        )
                    if self.original_files:
                        caption = (
                            heading
                            + f"• File gốc: <b>{manifest.ready_count}</b>"
                            + "\nTên file nguồn được giữ nguyên."
                        )
                    else:
                        caption = (
                            heading
                            + "\n".join(_status_lines(manifest))
                            + "\nCác file trong gói giữ nguyên thư mục phân loại."
                        )
                    try:
                        sent = await self.send_archive_with_retry(
                            message, archive_path, caption
                        )
                    except Exception:
                        logger.exception(
                            "Lỗi ngoài dự kiến khi gửi gói %s", archive_path
                        )
                        sent = False
                    if sent:
                        sent_parts.append(part_number)
                    else:
                        failed_parts.append(part_number)
                    # Free each ZIP before building the next batch. The organized
                    # library file is never touched by this cleanup.
                    cleanup_archives([archive_path])
                return True
            finally:
                cleanup_archives(archive_paths)

        for batch in batches:
            if not await build_and_send(batch):
                break

        manifest = _mark_paths_unavailable(manifest, failed_build_paths)
        build_failed = bool(failed_build_paths or fatal_build_error)
        if build_failed:
            if fatal_build_error:
                detail = (
                    "Ổ đĩa tạm không còn đủ chỗ để tạo gói tiếp theo. "
                    "Các ZIP tạm đã được dọn; file kết quả gốc vẫn được "
                    "giữ."
                )
            else:
                detail = (
                    f"Có <b>{len(failed_build_paths)}</b> file không đọc hoặc "
                    "đóng gói được. Bot đã bỏ qua đúng các file đó và vẫn "
                    "gửi những gói còn lại. "
                    "Kết quả đã xử lý vẫn được giữ trên máy chủ."
                )
            await message.reply_text(
                "⚠️ <b>Một phần kết quả chưa đóng gói được</b>\n\n" + detail,
                parse_mode="HTML",
            )

        if failed_parts:
            failed_text = ", ".join(str(part_number) for part_number in failed_parts)
            await message.reply_text(
                "⚠️ <b>Telegram chưa nhận được một số gói</b>\n\n"
                f"Các gói chưa gửi được: <b>{failed_text}</b>. "
                f"Bot đã thử lại {self.upload_retries} lần cho mỗi gói. "
                "Kết quả xử lý vẫn được giữ an toàn trên máy chủ.",
                parse_mode="HTML",
            )

        return DeliveryReport(
            manifest=manifest,
            local_notified=local_notified,
            archive_count=archive_count,
            sent_parts=tuple(sent_parts),
            failed_parts=tuple(failed_parts),
            build_failed=build_failed,
        )
