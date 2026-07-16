"""CLI entry point and shared Audio Organizer pipeline."""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
from collections import Counter
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from config import (
    AUDIO_EXTS,
    DB_PATH,
    DEFAULT_BATCH_SIZE,
    DEFAULT_WORKERS,
    DOWNLOAD_DIR,
    OUTPUT_DIR,
    REPORT_DIR,
    TEMP_ROOT,
    ensure_runtime_dirs,
    validate_audio_tools,
)
from exceptions import AudioOrganizerError, DuplicateFileError
from organizer import Organizer
from processor import AudioProcessor
from quality_gate import QualityGate
from utils.cleanup import cleanup_run, setup_cleanup

console = Console()


def configure_cli_logging() -> None:
    """Enable rich console logs only for the command-line entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def iter_audio_files(root: Path, output: Path) -> Iterator[Path]:
    output_resolved = output.resolve()
    for path in root.rglob("*"):
        if (
            path.is_file()
            and path.suffix.lower() in AUDIO_EXTS
            and not path.resolve().is_relative_to(output_resolved)
        ):
            yield path


def process_file(
    path: Path,
    site: str,
    gate: QualityGate,
    processor: AudioProcessor,
    organizer: Organizer,
    staging_dir: Path,
    *,
    dry_run: bool = False,
    delete_source: bool = True,
    ephemeral: bool = False,
) -> dict[str, Any]:
    try:
        source_hash = organizer.hash_file(path)
        if organizer.is_duplicate(source_hash):
            existing = organizer.metadata_for_hash(source_hash) or {}
            return {
                "status": "duplicate",
                "file": str(path),
                "output": existing.get("filepath"),
                "source_hash": source_hash,
            }
        analysis = gate.analyze(path)
        if not analysis["passed"]:
            if ephemeral and path.resolve().is_relative_to(staging_dir.resolve()):
                path.unlink(missing_ok=True)
            return {
                "status": "rejected",
                "file": str(path),
                "issues": analysis["issues"],
                "analysis": analysis,
                "source_hash": source_hash,
            }
        if dry_run:
            return {
                "status": "would_pass",
                "file": str(path),
                "analysis": analysis,
                "source_hash": source_hash,
            }
        staged = processor.process(path, analysis, staging_dir)
        try:
            output = organizer.organize(staged, site, analysis, source_hash)
        finally:
            staged.unlink(missing_ok=True)
        if delete_source:
            path.unlink()
        return {
            "status": "passed",
            "file": str(path),
            "output": str(output),
            "analysis": analysis,
            "source_hash": source_hash,
        }
    except DuplicateFileError:
        return {"status": "duplicate", "file": str(path)}
    except AudioOrganizerError as exc:
        return {"status": "error", "file": str(path), "error": str(exc)}
    except OSError as exc:
        return {"status": "error", "file": str(path), "error": str(exc)}


def _batches(items: Iterable[Path], size: int) -> Iterator[list[Path]]:
    iterator = iter(items)
    while batch := list(itertools.islice(iterator, size)):
        yield batch


async def run_pipeline(
    input_dir: Path,
    output_dir: Path,
    site: str = "local",
    *,
    dry_run: bool = False,
    workers: int = DEFAULT_WORKERS,
    batch_size: int = DEFAULT_BATCH_SIZE,
    delete_source: bool = True,
) -> Counter[str]:
    if workers < 1 or batch_size < 1:
        raise ValueError("workers and batch_size must be positive")
    input_resolved, output_resolved = input_dir.resolve(), output_dir.resolve()
    if input_resolved == output_resolved:
        raise ValueError("input and output must differ")
    ensure_runtime_dirs()
    validate_audio_tools()
    run_dir = setup_cleanup(TEMP_ROOT)
    organizer = Organizer(output_dir, DB_PATH, pool_size=workers)
    gate, processor = QualityGate(), AudioProcessor()
    counts: Counter[str] = Counter()
    report = REPORT_DIR / "latest.jsonl"
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        with report.open("w", encoding="utf-8") as manifest:
            for batch in _batches(iter_audio_files(input_dir, output_dir), batch_size):
                pending = [
                    loop.run_in_executor(
                        executor,
                        partial(
                            process_file,
                            path,
                            site,
                            gate,
                            processor,
                            organizer,
                            run_dir,
                            dry_run=dry_run,
                            delete_source=delete_source,
                        ),
                    )
                    for path in batch
                ]
                results = await asyncio.gather(*pending)
                for result in results:
                    counts[result["status"]] += 1
                    status = result["status"]
                    filepath = Path(result["file"]).name
                    if status == "error":
                        logging.error("Failed %s: %s", filepath, result.get("error", "Unknown error"))
                    elif status == "rejected":
                        logging.warning("Rejected %s: %s", filepath, ", ".join(result.get("issues", [])))
                    elif status == "duplicate":
                        logging.info("Skipped %s (Duplicate)", filepath)
                    else:
                        logging.info("Organized %s", filepath)

                    manifest.write(json.dumps(result, ensure_ascii=False, default=str) + "\n")
                organizer.db.checkpoint()
        return counts
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        organizer.db.close_all()
        cleanup_run(run_dir)


def _print_summary(counts: Counter[str]) -> None:
    table = Table(title="Audio Organizer v4.1")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for status in ("passed", "would_pass", "rejected", "duplicate", "error"):
        table.add_row(status, str(counts[status]))
    console.print(table)


def cmd_stats(output_dir: Path) -> None:
    ensure_runtime_dirs()
    organizer = Organizer(output_dir, DB_PATH, pool_size=1)
    try:
        console.print_json(data=organizer.get_stats())
    finally:
        organizer.db.close_all()


def cmd_rebuild_layout(output_dir: Path) -> None:
    ensure_runtime_dirs()
    organizer = Organizer(output_dir, DB_PATH, pool_size=1)
    try:
        console.print_json(data=organizer.migrate_layout(DOWNLOAD_DIR))
    finally:
        organizer.db.close_all()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Analyze, normalize and organize audio samples")
    parser.add_argument("--input", "-i", type=Path)
    parser.add_argument("--output", "-o", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--site", "-s", default="local")
    parser.add_argument("--dry-run", "-d", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument(
        "--rebuild-layout",
        action="store_true",
        help="Move the existing library into the current clean folder and filename layout",
    )
    parser.add_argument("--workers", "-w", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--copy", action="store_true", help="Keep source files after successful organization")
    return parser


def main() -> int:
    configure_cli_logging()
    args = build_parser().parse_args()
    if args.stats:
        cmd_stats(args.output)
        return 0
    if args.rebuild_layout:
        cmd_rebuild_layout(args.output)
        return 0
    if args.input is None or not args.input.is_dir():
        console.print("[red]--input must be an existing directory[/red]")
        return 2
    try:
        counts = asyncio.run(
            run_pipeline(
                args.input,
                args.output,
                args.site,
                dry_run=args.dry_run,
                workers=args.workers,
                batch_size=args.batch_size,
                delete_source=not args.copy,
            )
        )
        _print_summary(counts)
        return 1 if counts["error"] else 0
    except (AudioOrganizerError, ValueError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
