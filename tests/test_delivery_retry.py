from pathlib import Path

from delivery_retry import DeliveryRetryStore


def test_retry_store_isolates_users_and_rejects_paths_outside_library(
    tmp_path: Path,
) -> None:
    library = tmp_path / "library"
    first = library / "Loops" / "first.wav"
    second = library / "FX" / "second.wav"
    outside = tmp_path / "other-user.wav"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    outside.write_bytes(b"outside")
    store = DeliveryRetryStore(tmp_path / "data" / "delivery-retries.db")

    saved_first = store.save(
        101,
        "splice.com",
        [
            {"status": "passed", "output": str(first)},
            {"status": "passed", "output": str(outside)},
        ],
        library,
    )
    saved_second = store.save(
        202,
        "loopcloud.com",
        [{"status": "duplicate", "output": str(second)}],
        library,
    )

    first_record = store.load(101, library)
    second_record = store.load(202, library)
    assert saved_first == 1
    assert saved_second == 1
    assert first_record is not None
    assert second_record is not None
    assert first_record.site == "splice.com"
    assert first_record.results == ({"status": "passed", "output": str(first.resolve())},)
    assert second_record.results == (
        {"status": "duplicate", "output": str(second.resolve())},
    )


def test_retry_store_replaces_only_the_same_users_latest_job(tmp_path: Path) -> None:
    library = tmp_path / "library"
    old = library / "old.wav"
    new = library / "new.wav"
    library.mkdir()
    old.write_bytes(b"old")
    new.write_bytes(b"new")
    store = DeliveryRetryStore(tmp_path / "delivery-retries.db")

    store.save(101, "old.example", [{"status": "passed", "output": str(old)}], library)
    store.save(101, "new.example", [{"status": "passed", "output": str(new)}], library)

    record = store.load(101, library)
    assert record is not None
    assert record.site == "new.example"
    assert record.results == ({"status": "passed", "output": str(new.resolve())},)

    store.save(101, "empty.example", [{"status": "rejected"}], library)
    assert store.load(101, library) is None
