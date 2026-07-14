"""Atomic filesystem publishing backed by SQLite metadata."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from exceptions import DatabaseError, DuplicateFileError, OrganizerError
from library_layout import friendly_filename, library_parts, source_name_from_legacy
from utils.db import DatabasePool
from utils.paths import safe_child, sanitize_component

LIBRARY_LAYOUT_VERSION = "3"


class Organizer:
    def __init__(self, output_dir: Path | str, db_path: Path | str, pool_size: int = 4) -> None:
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.db = DatabasePool(db_path, pool_size=pool_size)

    @staticmethod
    def hash_file(filepath: Path | str) -> str:
        with Path(filepath).open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()

    def is_duplicate(self, file_hash: str) -> bool:
        with self.db.connection() as conn:
            row = conn.execute("SELECT 1 FROM files WHERE file_hash=? LIMIT 1", (file_hash,)).fetchone()
            return row is not None

    @staticmethod
    def _available_path(target: Path) -> Path:
        candidate, counter = target, 2
        while candidate.exists() or candidate.with_suffix(candidate.suffix + ".part").exists():
            candidate = target.with_stem(f"{target.stem} ({counter})")
            counter += 1
        return candidate

    @staticmethod
    def _filename(source_name: str, analysis: dict[str, Any], source_hash: str = "") -> str:
        return friendly_filename(source_name, analysis, source_hash, extension=".wav")

    def organize(self, staged_file: Path, site: str, analysis: dict[str, Any], source_hash: str) -> Path:
        safe_site = sanitize_component(site)
        genre = sanitize_component(str(analysis.get("genre_hint") or "other"))
        folder = safe_child(self.output_dir, *library_parts(analysis))
        folder.mkdir(parents=True, exist_ok=True)
        source_name = re.sub(r"\.processed(?:-\d+)?(?=\.wav$)", "", staged_file.name)
        desired = safe_child(folder, self._filename(source_name, analysis, source_hash))
        target = desired
        partial = target.with_suffix(target.suffix + ".part")
        published = False
        try:
            # SQLite serializes this short publish transaction.
            with self.db.connection(transaction=True, immediate=True) as conn:
                if conn.execute("SELECT 1 FROM files WHERE file_hash=?", (source_hash,)).fetchone():
                    raise DuplicateFileError(source_hash)
                target = self._available_path(desired)
                partial = target.with_suffix(target.suffix + ".part")
                with staged_file.open("rb") as source, partial.open("xb") as destination:
                    shutil.copyfileobj(source, destination, length=1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                os.replace(partial, target)
                published = True
                conn.execute(
                    """INSERT INTO files
                    (filepath, site, genre, content_type, bpm, bpm_confidence, key, bitrate_kbps,
                     duration_sec, silence_ratio, file_hash, processed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(target),
                        safe_site,
                        genre,
                        analysis.get("content_type", "unknown"),
                        int(analysis.get("bpm") or 0),
                        analysis.get("bpm_confidence", "none"),
                        analysis.get("key", "Unknown"),
                        int(analysis.get("bitrate_kbps") or 0),
                        float(analysis.get("duration_sec") or 0),
                        float(analysis.get("silence_ratio") or 0),
                        source_hash,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            return target
        except DuplicateFileError:
            if published:
                target.unlink(missing_ok=True)
            raise
        except DatabaseError:
            if published:
                target.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            if published:
                target.unlink(missing_ok=True)
            partial.unlink(missing_ok=True)
            if isinstance(exc, sqlite3.Error):
                raise DatabaseError(str(exc)) from exc
            raise OrganizerError(str(exc)) from exc
        finally:
            partial.unlink(missing_ok=True)

    def metadata_for_hash(self, source_hash: str) -> dict[str, Any] | None:
        with self.db.connection() as conn:
            row = conn.execute("SELECT * FROM files WHERE file_hash=?", (source_hash,)).fetchone()
        return dict(row) if row else None

    def archive_raw(
        self,
        source: Path,
        download_root: Path,
        site: str,
        analysis: dict[str, Any] | None,
        source_hash: str,
    ) -> Path:
        """Move a retained source into the same clean browse layout as the WAV library."""
        meta = (
            analysis
            or self.metadata_for_hash(source_hash)
            or {
                "content_type": "unknown",
                "genre_hint": "other",
            }
        )
        source_folder = sanitize_component(str(site or "web").lower(), fallback="web")
        folder = safe_child(download_root, source_folder, *library_parts(meta))
        folder.mkdir(parents=True, exist_ok=True)
        if source.parent.resolve() == folder.resolve():
            return source
        naming_source = source.name
        if source.stem.lower().endswith(f" - {source_hash[:8].lower()}"):
            naming_source = f"{source_hash}{source.suffix}"
        name = friendly_filename(naming_source, meta, source_hash, extension=source.suffix or ".bin")
        desired = safe_child(folder, name)
        if source.resolve() == desired.resolve():
            return source
        target = self._available_path(desired)
        os.replace(source, target)
        return target

    def migrate_layout(self, download_root: Path | None = None) -> dict[str, int]:
        """Move legacy site/genre/hash paths into the current human-readable layout."""
        counts = {"organized": 0, "raw": 0, "missing": 0}
        with self.db.connection() as conn:
            rows = [dict(row) for row in conn.execute("SELECT * FROM files ORDER BY id")]

        for row in rows:
            source = Path(str(row["filepath"]))
            if not source.exists():
                counts["missing"] += 1
                continue
            folder = safe_child(self.output_dir, *library_parts(row))
            folder.mkdir(parents=True, exist_ok=True)
            if source.parent.resolve() == folder.resolve():
                continue
            original = source_name_from_legacy(source.name)
            if source.stem.lower().endswith(f" - {str(row['file_hash'])[:8].lower()}"):
                original = str(row["file_hash"])
            desired = safe_child(folder, friendly_filename(original, row, str(row["file_hash"])))
            if source.resolve() == desired.resolve():
                continue
            target = self._available_path(desired)
            os.replace(source, target)
            try:
                with self.db.connection(transaction=True, immediate=True) as conn:
                    conn.execute("UPDATE files SET filepath=? WHERE id=?", (str(target), int(row["id"])))
            except BaseException:
                os.replace(target, source)
                raise
            counts["organized"] += 1

        if download_root and download_root.exists():
            audio_extensions = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff", ".aif"}
            raw_files = [
                path
                for path in download_root.rglob("*")
                if path.is_file() and path.suffix.lower() in audio_extensions
            ]
            for source in raw_files:
                digest = self.hash_file(source)
                meta = self.metadata_for_hash(digest)
                try:
                    relative = source.relative_to(download_root)
                    default_site = relative.parts[0] if len(relative.parts) > 1 else "web"
                    site = str(meta.get("site")) if meta else default_site
                    target = self.archive_raw(source, download_root, site, meta, digest)
                    if target.resolve() != source.resolve():
                        counts["raw"] += 1
                except OSError as exc:
                    raise OrganizerError(f"Cannot migrate raw file {source}: {exc}") from exc

        for root in filter(None, (self.output_dir, download_root)):
            directories = sorted(
                (path for path in root.rglob("*") if path.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            )
            for directory in directories:
                try:
                    directory.rmdir()
                except OSError:
                    pass
        with self.db.connection(transaction=True, immediate=True) as conn:
            conn.execute(
                """INSERT INTO app_meta(key, value) VALUES ('library_layout_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (LIBRARY_LAYOUT_VERSION,),
            )
        return counts

    def ensure_layout(self, download_root: Path | None = None) -> dict[str, int]:
        """Apply a layout upgrade once, then let the normal pipeline maintain it per file."""
        with self.db.connection() as conn:
            row = conn.execute("SELECT value FROM app_meta WHERE key='library_layout_version'").fetchone()
        if row and str(row[0]) == LIBRARY_LAYOUT_VERSION:
            return {"organized": 0, "raw": 0, "missing": 0}
        return self.migrate_layout(download_root)

    def get_stats(self) -> dict[str, Any]:
        with self.db.connection() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            sites = [
                dict(row)
                for row in conn.execute(
                    """SELECT site, COUNT(*) total, ROUND(AVG(bitrate_kbps), 1) avg_bitrate,
                SUM(content_type='loop') loops, SUM(content_type='one-shot') oneshots,
                SUM(content_type='fx') fx FROM files GROUP BY site ORDER BY total DESC"""
                )
            ]
            genres = [
                dict(row)
                for row in conn.execute(
                    "SELECT genre, COUNT(*) total FROM files GROUP BY genre ORDER BY total DESC LIMIT 10"
                )
            ]
        return {"total": total, "sites": sites, "genres": genres}
