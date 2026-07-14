import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import crawler
from crawler import (
    AudioCrawler,
    extract_audio_assets_from_payload,
    extract_audio_urls_from_payload,
    extract_splice_listed_sample_urls,
    extract_splice_listed_samples,
)
from exceptions import CrawlTimeoutError, FileTooLargeError, NetworkError


def test_blocks_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        crawler.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("127.0.0.1", 80))]
    )
    with pytest.raises(NetworkError):
        crawler.validate_public_url("http://example.test/audio.wav")


def test_extracts_every_audio_url_from_nested_payload() -> None:
    payload = {
        "data": {
            "assetsSearch": {
                "items": [
                    {
                        "name": "Warm Vinyl Kick",
                        "files": [
                            {"asset_file_type_slug": "preview_mp3", "url": "https://cdn.test/one.mp3"},
                            {"asset_file_type_slug": "full_wav", "url": "https://cdn.test/one.wav"},
                            {"asset_file_type_slug": "waveform", "url": "https://cdn.test/one.json"},
                        ],
                    },
                    {"files": [{"asset_file_type_slug": "preview_mp3", "url": "https://cdn.test/two.mp3"}]},
                ]
            }
        }
    }
    assert extract_audio_urls_from_payload(payload) == [
        "https://cdn.test/one.mp3",
        "https://cdn.test/one.wav",
        "https://cdn.test/two.mp3",
    ]
    assert extract_audio_assets_from_payload(payload)[0] == (
        "https://cdn.test/one.mp3",
        "Warm Vinyl Kick",
    )


def test_extracts_extensionless_audio_from_generic_json() -> None:
    payload = {
        "tracks": [
            {
                "title": "Kick Without Extension",
                "audio_url": "https://media.example.com/play?id=42",
            },
            {
                "title": "Snare By Mime Type",
                "content_type": "audio/mpeg",
                "url": "https://media.example.com/asset/99?signature=x",
                "cover_url": "https://media.example.com/cover/99",
            },
        ]
    }

    assert extract_audio_assets_from_payload(payload) == [
        ("https://media.example.com/play?id=42", "Kick Without Extension"),
        ("https://media.example.com/asset/99?signature=x", "Snare By Mime Type"),
    ]


async def test_direct_audio_url_skips_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = AudioCrawler(tmp_path)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    urls = await instance.sniff_urls("https://cdn.example.com/audio/Warm_Kick.mp3?token=1")

    assert urls == ["https://cdn.example.com/audio/Warm_Kick.mp3?token=1"]
    assert instance.discovered_titles[urls[0]] == "Warm_Kick"
    instance.close()


def test_splice_extractor_returns_one_playable_file_per_listed_sample() -> None:
    payload = {
        "data": {
            "assetsSearch": {
                "items": [
                    {
                        "name": "Warm Vinyl Kick",
                        "files": [
                            {"asset_file_type_slug": "full_wav", "url": "https://cdn.test/one.wav"},
                            {"asset_file_type_slug": "preview_mp3", "url": "https://cdn.test/one.mp3"},
                        ],
                    },
                    {"files": [{"asset_file_type_slug": "full_wav", "url": "https://cdn.test/two.wav"}]},
                ]
            }
        },
        "unrelatedWidget": {"url": "https://cdn.test/demo_1.mp3"},
    }

    assert extract_splice_listed_sample_urls(payload) == [
        "https://cdn.test/one.mp3",
        "https://cdn.test/two.wav",
    ]
    assert extract_splice_listed_samples(payload)[0] == (
        "https://cdn.test/one.mp3",
        "Warm Vinyl Kick",
    )


async def test_browser_crawl_has_a_hard_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    instance = AudioCrawler(tmp_path)

    async def never_finishes(_url: str) -> list[str]:
        await asyncio.sleep(1)
        return []

    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    monkeypatch.setattr(crawler, "CRAWL_TIMEOUT_SEC", 0.01)
    monkeypatch.setattr(instance, "_sniff_urls", never_finishes)

    with pytest.raises(CrawlTimeoutError, match="timed out"):
        await instance.sniff_urls("https://example.com")
    instance.close()


def test_stream_limit_removes_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={},
        history=[],
        url="https://example.com/a.wav",
        iter_content=lambda _size: iter([b"x" * (1024 * 1024 + 1)]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    monkeypatch.setitem(crawler.QUALITY, "max_file_mb", 1)
    with pytest.raises(FileTooLargeError):
        instance.download("https://example.com/a.wav")
    assert not list(tmp_path.glob("*.part"))


def test_download_successful_saves_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={},
        history=[],
        url="https://example.com/success.wav",
        iter_content=lambda _size: iter([b"data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    result = instance.download("https://example.com/success.wav")
    assert result is not None
    assert result.exists()
    assert result.name == "success.wav"
    assert result.read_bytes() == b"data"


def test_download_redirect_chain_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))

    # Mock socket.getaddrinfo to simulate the redirect going to a private IP
    def mock_getaddrinfo(host, port, *args, **kwargs):
        if host == "private.internal":
            return [(None, None, None, None, ("192.168.1.1", 80))]
        return [(None, None, None, None, ("8.8.8.8", 80))]

    monkeypatch.setattr(crawler.socket, "getaddrinfo", mock_getaddrinfo)

    history_mock = [
        SimpleNamespace(url="http://example.com", headers={"Location": "http://private.internal/audio.wav"})
    ]
    response = SimpleNamespace(
        status_code=200,
        headers={},
        history=history_mock,
        url="http://private.internal/audio.wav",
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)

    with pytest.raises(NetworkError, match="Private or non-global address is blocked"):
        instance.download("http://example.com/audio.wav")


def test_retry_on_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    attempts = 0

    def mock_get(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise requests.Timeout("Connection timed out")
        return SimpleNamespace(
            status_code=200,
            headers={},
            history=[],
            url="https://example.com/retry.wav",
            iter_content=lambda _size: iter([b"success"]),
            close=lambda: None,
        )

    session = SimpleNamespace(get=mock_get, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    monkeypatch.setattr("utils.retry.time.sleep", lambda _seconds: None)

    result = instance.download("https://example.com/retry.wav")
    assert result is not None
    assert result.exists()
    assert attempts == 3


def test_filename_from_content_disposition(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Disposition": 'attachment; filename="custom_name.wav"'},
        history=[],
        url="https://example.com/download?id=123",
        iter_content=lambda _size: iter([b"data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download("https://example.com/download?id=123")
    assert result is not None
    assert result.name == "custom_name.wav"


def test_suggested_name_replaces_opaque_cdn_hash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={},
        history=[],
        url="https://cdn.example/1234567890abcdef1234567890abcdef.mp3",
        iter_content=lambda _size: iter([b"data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download(response.url, suggested_name="Warm Vinyl Kick")

    assert result is not None
    assert result.name == "Warm Vinyl Kick.mp3"


def test_extensionless_audio_uses_title_and_content_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Type": "audio/mpeg; charset=binary"},
        history=[],
        url="https://media.example.com/play?id=42",
        iter_content=lambda _size: iter([b"data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download(response.url, suggested_name="Warm Kick")

    assert result is not None
    assert result.name == "Warm Kick.mp3"


def test_download_returns_none_when_pre_check_fails(tmp_path: Path) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (False, "file type not supported"))
    session = SimpleNamespace(get=lambda *args, **kwargs: None, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)

    result = instance.download("https://example.com/bad.txt")
    assert result is None
