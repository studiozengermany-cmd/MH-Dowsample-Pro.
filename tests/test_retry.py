import pytest

from exceptions import HTTPError
from utils.retry import retry


def test_retries_transient_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("utils.retry.time.sleep", lambda _seconds: None)
    calls = 0

    @retry(attempts=3, exceptions=(ConnectionError,))
    def operation() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise ConnectionError
        return "ok"

    assert operation() == "ok"
    assert calls == 3


def test_does_not_retry_404() -> None:
    calls = 0

    @retry(attempts=3)
    def operation() -> None:
        nonlocal calls
        calls += 1
        raise HTTPError(404)

    with pytest.raises(HTTPError):
        operation()
    assert calls == 1
