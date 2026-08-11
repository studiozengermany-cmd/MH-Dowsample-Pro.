"""Durable, per-user manifests for retrying the latest Telegram delivery."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeliveryRetryRecord:
    site: str
    results: tuple[dict[str, str], ...]
    created_at: str


class DeliveryRetryStore:
    """Keep one isolated retry manifest per Telegram user."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS delivery_retries (
                    telegram_user_id INTEGER PRIMARY KEY,
                    site TEXT NOT NULL,
                    results_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )"""
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _safe_relative(path: Path, output_root: Path) -> str | None:
        try:
            relative = path.resolve().relative_to(output_root.resolve())
        except (OSError, ValueError):
            return None
        return relative.as_posix()

    def save(
        self,
        telegram_user_id: int,
        site: str,
        results: Sequence[Mapping[str, Any]],
        output_root: Path,
    ) -> int:
        """Replace one user's retry record with deliverables from their latest job."""
        entries: list[dict[str, str]] = []
        seen: set[str] = set()
        for result in results:
            status = str(result.get("status", ""))
            if status not in {"passed", "duplicate"}:
                continue
            output = result.get("output")
            if not output:
                continue
            relative = self._safe_relative(Path(str(output)), output_root)
            if relative is None or relative in seen:
                continue
            entries.append({"status": status, "path": relative})
            seen.add(relative)

        created_at = datetime.now(UTC).isoformat()
        payload = json.dumps(entries, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO delivery_retries(
                    telegram_user_id, site, results_json, created_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    site=excluded.site,
                    results_json=excluded.results_json,
                    created_at=excluded.created_at""",
                (int(telegram_user_id), site, payload, created_at),
            )
        return len(entries)

    def load(
        self, telegram_user_id: int, output_root: Path
    ) -> DeliveryRetryRecord | None:
        """Load only the requesting user's paths, constrained to the output root."""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT site, results_json, created_at
                FROM delivery_retries WHERE telegram_user_id=?""",
                (int(telegram_user_id),),
            ).fetchone()
        if row is None:
            return None

        try:
            raw_entries = json.loads(str(row[1]))
        except (TypeError, ValueError):
            return None
        if not isinstance(raw_entries, list):
            return None

        root = output_root.resolve()
        results: list[dict[str, str]] = []
        for entry in raw_entries:
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status", ""))
            relative = Path(str(entry.get("path", "")))
            if status not in {"passed", "duplicate"}:
                continue
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                continue
            try:
                output = (root / relative).resolve()
                output.relative_to(root)
            except (OSError, ValueError):
                continue
            results.append({"status": status, "output": str(output)})

        if not results:
            return None
        return DeliveryRetryRecord(
            site=str(row[0]),
            results=tuple(results),
            created_at=str(row[2]),
        )
