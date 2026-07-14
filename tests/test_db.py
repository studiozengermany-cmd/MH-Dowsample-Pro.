from pathlib import Path

import pytest

from utils.db import DatabasePool


def test_database_pool_commit_rollback_and_checkpoint(tmp_path: Path) -> None:
    pool = DatabasePool(tmp_path / "test.db", pool_size=2)
    try:
        with pool.connection(transaction=True) as conn:
            conn.execute("INSERT INTO files(filepath,site,file_hash,processed_at) VALUES ('a','s','h','now')")
        with pytest.raises(RuntimeError), pool.connection(transaction=True) as conn:
            conn.execute("INSERT INTO files(filepath,site,file_hash,processed_at) VALUES ('b','s','i','now')")
            raise RuntimeError("rollback")
        with pool.connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 1
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        pool.checkpoint()
    finally:
        pool.close_all()
