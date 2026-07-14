"""Bounded SQLite connection pool and schema owner."""

from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from exceptions import DatabaseError

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filepath TEXT UNIQUE NOT NULL,
    site TEXT NOT NULL,
    genre TEXT NOT NULL DEFAULT 'other',
    content_type TEXT NOT NULL DEFAULT 'unknown',
    bpm INTEGER NOT NULL DEFAULT 0,
    bpm_confidence TEXT NOT NULL DEFAULT 'none',
    key TEXT NOT NULL DEFAULT 'Unknown',
    bitrate_kbps INTEGER NOT NULL DEFAULT 0,
    duration_sec REAL NOT NULL DEFAULT 0,
    silence_ratio REAL NOT NULL DEFAULT 1,
    file_hash TEXT UNIQUE NOT NULL,
    processed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_files_hash ON files(file_hash);
CREATE INDEX IF NOT EXISTS idx_files_site ON files(site);
CREATE INDEX IF NOT EXISTS idx_files_genre ON files(genre);
CREATE INDEX IF NOT EXISTS idx_files_bpm ON files(bpm);
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class DatabasePool:
    def __init__(self, db_path: Path | str, pool_size: int = 4, timeout: float = 5.0) -> None:
        if pool_size < 1:
            raise ValueError("pool_size must be >= 1")
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.pool_size = pool_size
        self.timeout = timeout
        self._pool: queue.LifoQueue[sqlite3.Connection] = queue.LifoQueue(pool_size)
        self._all: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self._closed = False
        for _ in range(pool_size):
            connection = self._connect()
            self._pool.put(connection)
            self._all.append(connection)
        try:
            with self.connection() as conn:
                conn.executescript(SCHEMA)
        except (sqlite3.Error, DatabaseError) as exc:
            self.close_all()
            raise DatabaseError(f"Cannot initialize database: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def connection(
        self, *, transaction: bool = False, immediate: bool = False
    ) -> Iterator[sqlite3.Connection]:
        if self._closed:
            raise DatabaseError("Database pool is closed")
        try:
            conn = self._pool.get(timeout=self.timeout)
        except queue.Empty as exc:
            raise DatabaseError("Timed out waiting for database connection") from exc
        try:
            if transaction:
                conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            if transaction:
                conn.commit()
        except sqlite3.Error as exc:
            if transaction:
                conn.rollback()
            raise DatabaseError(str(exc)) from exc
        except BaseException:
            if transaction:
                conn.rollback()
            raise
        finally:
            if not self._closed:
                self._pool.put(conn)

    # Compatibility with the v4.1 snippets.
    get_conn = connection

    def checkpoint(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    def close_all(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for conn in self._all:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
            self._all.clear()
