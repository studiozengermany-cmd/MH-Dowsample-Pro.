from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from exceptions import NetworkError
from utils.network import request_with_safe_redirects


def test_safe_redirects_resolve_relative_location_before_next_request() -> None:
    first = SimpleNamespace(
        status_code=302,
        headers={"Location": "/audio.wav"},
        url="https://example.com/start",
        close=Mock(),
    )
    final = SimpleNamespace(
        status_code=200,
        headers={},
        url="https://example.com/audio.wav",
        close=Mock(),
    )
    requested: list[str] = []
    validated: list[str] = []

    def get(url: str, **_kwargs):
        requested.append(url)
        return first if len(requested) == 1 else final

    result = request_with_safe_redirects(
        SimpleNamespace(get=get),
        "GET",
        "https://example.com/start",
        validator=validated.append,
    )

    assert result is final
    assert requested == ["https://example.com/start", "https://example.com/audio.wav"]
    assert validated == requested
    first.close.assert_called_once()


def test_safe_redirects_block_target_before_second_request() -> None:
    redirect = SimpleNamespace(
        status_code=302,
        headers={"Location": "http://private.internal/audio.wav"},
        url="https://example.com/start",
        close=Mock(),
    )
    get = Mock(return_value=redirect)

    def validate(url: str) -> None:
        if "private.internal" in url:
            raise NetworkError("private target")

    with pytest.raises(NetworkError, match="private target"):
        request_with_safe_redirects(
            SimpleNamespace(get=get),
            "GET",
            "https://example.com/start",
            validator=validate,
        )

    get.assert_called_once()
    redirect.close.assert_called_once()
