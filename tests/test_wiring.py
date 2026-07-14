from pathlib import Path

from organizer import Organizer
from utils.db import DatabasePool


def test_organizer_uses_database_pool(tmp_path: Path) -> None:
    organizer = Organizer(tmp_path / "out", tmp_path / "db.sqlite", pool_size=1)
    try:
        assert isinstance(organizer.db, DatabasePool)
    finally:
        organizer.db.close_all()
