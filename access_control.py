"""Separate SQLite-backed user approval and one-time invite control."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

ACCESS_SCHEMA = """
CREATE TABLE IF NOT EXISTS access_users (
    telegram_user_id INTEGER PRIMARY KEY,
    username TEXT,
    full_name TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'approved', 'rejected', 'blocked', 'revoked')
    ),
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_by INTEGER
);
CREATE INDEX IF NOT EXISTS idx_access_users_status ON access_users(status);

CREATE TABLE IF NOT EXISTS invite_codes (
    code_hash TEXT PRIMARY KEY,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_by INTEGER,
    used_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_invite_codes_expires_at ON invite_codes(expires_at);
"""


class AccessStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    BLOCKED = "blocked"
    REVOKED = "revoked"


class RequestOutcome(StrEnum):
    CREATED = "created"
    ALREADY_PENDING = "already_pending"
    ALREADY_APPROVED = "already_approved"
    BLOCKED = "blocked"
    INVALID_INVITE = "invalid_invite"


@dataclass(frozen=True)
class AccessUser:
    telegram_user_id: int
    username: str | None
    full_name: str | None
    status: AccessStatus
    requested_at: datetime
    updated_at: datetime
    decided_by: int | None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _as_utc(value).isoformat()


def _normalize_code(code: str) -> str:
    return "".join(character for character in code.upper() if character.isalnum())


def _hash_code(code: str) -> str:
    return hashlib.sha256(_normalize_code(code).encode("utf-8")).hexdigest()


class AccessControlStore:
    """Own only authorization data; never share the audio-library database."""

    def __init__(self, db_path: Path | str, timeout: float = 5.0) -> None:
        self.db_path = Path(db_path)
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            with connection:
                connection.executescript(ACCESS_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.timeout)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
        return connection

    def create_invite(
        self,
        *,
        created_by: int,
        ttl: timedelta = timedelta(hours=24),
        code: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if created_by <= 0:
            raise ValueError("created_by must be a positive Telegram user ID")
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        created_at = _as_utc(now or _utc_now())
        for _attempt in range(10):
            plain = code or secrets.token_hex(4).upper()
            normalized = _normalize_code(plain)
            if len(normalized) < 6:
                raise ValueError("invite code must contain at least 6 letters or digits")
            display_code = f"{normalized[:4]}-{normalized[4:]}"
            try:
                with closing(self._connect()) as connection:
                    with connection:
                        connection.execute(
                            """INSERT INTO invite_codes(
                                code_hash, created_by, created_at, expires_at
                            ) VALUES (?, ?, ?, ?)""",
                            (
                                _hash_code(normalized),
                                created_by,
                                _iso(created_at),
                                _iso(created_at + ttl),
                            ),
                        )
                return display_code
            except sqlite3.IntegrityError:
                if code is not None:
                    raise ValueError("invite code already exists") from None
        raise RuntimeError("could not allocate a unique invite code")

    def submit_request(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        full_name: str | None,
        invite_code: str,
        now: datetime | None = None,
    ) -> RequestOutcome:
        if telegram_user_id <= 0:
            raise ValueError("telegram_user_id must be positive")
        requested_at = _as_utc(now or _utc_now())
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT status FROM access_users WHERE telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
            if existing is not None:
                status = AccessStatus(str(existing["status"]))
                if status is AccessStatus.APPROVED:
                    connection.rollback()
                    return RequestOutcome.ALREADY_APPROVED
                if status is AccessStatus.PENDING:
                    connection.rollback()
                    return RequestOutcome.ALREADY_PENDING
                if status is AccessStatus.BLOCKED:
                    connection.rollback()
                    return RequestOutcome.BLOCKED

            invite = connection.execute(
                """SELECT expires_at, used_at FROM invite_codes
                   WHERE code_hash=?""",
                (_hash_code(invite_code),),
            ).fetchone()
            if (
                invite is None
                or invite["used_at"] is not None
                or datetime.fromisoformat(str(invite["expires_at"])) <= requested_at
            ):
                connection.rollback()
                return RequestOutcome.INVALID_INVITE

            consumed = connection.execute(
                """UPDATE invite_codes SET used_by=?, used_at=?
                   WHERE code_hash=? AND used_at IS NULL""",
                (telegram_user_id, _iso(requested_at), _hash_code(invite_code)),
            )
            if consumed.rowcount != 1:
                connection.rollback()
                return RequestOutcome.INVALID_INVITE

            connection.execute(
                """INSERT INTO access_users(
                    telegram_user_id, username, full_name, status,
                    requested_at, updated_at, decided_by
                ) VALUES (?, ?, ?, 'pending', ?, ?, NULL)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    status='pending',
                    requested_at=excluded.requested_at,
                    updated_at=excluded.updated_at,
                    decided_by=NULL""",
                (
                    telegram_user_id,
                    username,
                    full_name,
                    _iso(requested_at),
                    _iso(requested_at),
                ),
            )
            connection.commit()
            return RequestOutcome.CREATED
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_user(self, telegram_user_id: int) -> AccessUser | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM access_users WHERE telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        return AccessUser(
            telegram_user_id=int(row["telegram_user_id"]),
            username=str(row["username"]) if row["username"] is not None else None,
            full_name=str(row["full_name"]) if row["full_name"] is not None else None,
            status=AccessStatus(str(row["status"])),
            requested_at=datetime.fromisoformat(str(row["requested_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            decided_by=int(row["decided_by"]) if row["decided_by"] is not None else None,
        )

    def list_users(
        self,
        *,
        status: AccessStatus | None = None,
        limit: int = 20,
    ) -> list[AccessUser]:
        """Return recent access records for the administrator review screen."""
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        query = "SELECT * FROM access_users"
        parameters: tuple[object, ...] = ()
        if status is not None:
            query += " WHERE status=?"
            parameters = (status.value,)
        query += " ORDER BY requested_at ASC LIMIT ?"
        parameters = (*parameters, limit)
        with closing(self._connect()) as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [
            AccessUser(
                telegram_user_id=int(row["telegram_user_id"]),
                username=str(row["username"]) if row["username"] is not None else None,
                full_name=str(row["full_name"]) if row["full_name"] is not None else None,
                status=AccessStatus(str(row["status"])),
                requested_at=datetime.fromisoformat(str(row["requested_at"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                decided_by=(int(row["decided_by"]) if row["decided_by"] is not None else None),
            )
            for row in rows
        ]

    def status_for(self, telegram_user_id: int) -> AccessStatus | None:
        user = self.get_user(telegram_user_id)
        return user.status if user else None

    def set_status(
        self,
        telegram_user_id: int,
        status: AccessStatus,
        *,
        decided_by: int,
        expected_status: AccessStatus | None = None,
        now: datetime | None = None,
    ) -> bool:
        if status is AccessStatus.PENDING:
            raise ValueError("pending status is created only by submit_request")
        decided_at = _as_utc(now or _utc_now())
        parameters = (status.value, _iso(decided_at), decided_by, telegram_user_id)
        with closing(self._connect()) as connection:
            with connection:
                if expected_status is None:
                    changed = connection.execute(
                        """UPDATE access_users
                           SET status=?, updated_at=?, decided_by=?
                           WHERE telegram_user_id=?""",
                        parameters,
                    )
                else:
                    changed = connection.execute(
                        """UPDATE access_users
                           SET status=?, updated_at=?, decided_by=?
                           WHERE telegram_user_id=? AND status=?""",
                        (*parameters, expected_status.value),
                    )
        return changed.rowcount == 1

    def checkpoint(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
