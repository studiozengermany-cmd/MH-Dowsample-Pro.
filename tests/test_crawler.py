import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import crawler
import utils.network
from crawler import (
    AudioCrawler,
    extract_audio_assets_from_payload,
    extract_audio_urls_from_payload,
    extract_splice_listed_sample_urls,
    extract_splice_listed_samples,
    extract_splice_page_assets,
)
from exceptions import CrawlLimitError, CrawlTimeoutError, FileTooLargeError, NetworkError, NoAudioFoundError


def test_blocks_private_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        utils.network.socket, "getaddrinfo", lambda *args: [(None, None, None, None, ("127.0.0.1", 80))]
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


def test_splice_extractor_tolerates_changed_catalogue_wrapper() -> None:
    payload = {
        "data": {
            "packPresets": {
                "edges": [
                    {
                        "node": {
                            "name": "Airy Preset",
                            "files": [
                                {
                                    "asset_file_type_slug": "preview_mp3",
                                    "url": "https://cdn.test/airy.mp3",
                                }
                            ],
                        }
                    }
                ]
            }
        }
    }

    assert extract_splice_listed_samples(payload) == [
        ("https://cdn.test/airy.mp3", "Airy Preset")
    ]


def _splice_document(name: str, url: str, page: int, total_pages: int) -> str:
    payload = {
        "data": {
            "assetsSearch": {
                "items": [
                    {
                        "name": name,
                        "files": [
                            {
                                "asset_file_type_slug": "preview_mp3",
                                "url": url,
                            }
                        ],
                    }
                ],
                "pagination_metadata": {"currentPage": page, "totalPages": total_pages},
            }
        }
    }
    envelope = {"status": 200, "body": json.dumps(payload)}
    return (
        '<script type="application/json" data-sveltekit-fetched '
        f'data-url="https://surfaces-graphql.splice.com/graphql">{json.dumps(envelope)}</script>'
    )


def test_extracts_public_splice_previews_from_server_rendered_html() -> None:
    document = _splice_document("Warm Pad", "https://cdn.test/warm-pad.mp3", 2, 5)

    assets, current_page, total_pages = extract_splice_page_assets(document)

    assert assets == [("https://cdn.test/warm-pad.mp3", "Warm Pad")]
    assert (current_page, total_pages) == (2, 5)


def test_splice_page_keeps_preset_and_regular_assets() -> None:
    preset = _splice_document(
        "Preset",
        "https://cdn.test/premium_presets/previews/preset.mp3",
        1,
        1,
    )
    regular = _splice_document("Sample", "https://cdn.test/samples/sample.mp3", 1, 1)

    assets, _current_page, _total_pages = extract_splice_page_assets(preset + regular)

    assert assets == [
        ("https://cdn.test/premium_presets/previews/preset.mp3", "Preset"),
        ("https://cdn.test/samples/sample.mp3", "Sample"),
    ]


def test_splice_html_discovery_rejects_catalogue_above_safe_page_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = AudioCrawler(tmp_path)
    page_url = "https://splice.com/sounds/packs/vendor/pack/presets"
    document = _splice_document("First", "https://cdn.test/first.mp3", 1, 51)
    monkeypatch.setattr(instance, "_fetch_splice_page", lambda _client, _url: document)

    with pytest.raises(CrawlLimitError, match="51 pages"):
        instance._discover_splice_pages(page_url)

    instance.close()


def test_splice_html_discovery_collects_every_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = AudioCrawler(tmp_path)
    page_url = "https://splice.com/sounds/packs/vendor/pack/presets"
    documents = {
        page_url: _splice_document("First", "https://cdn.test/first.mp3", 1, 2),
        f"{page_url}?page=2": _splice_document("Second", "https://cdn.test/second.mp3", 2, 2),
    }
    monkeypatch.setattr(instance, "_fetch_splice_page", lambda _client, url: documents[url])

    assets = instance._discover_splice_pages(page_url)

    assert assets == [
        ("https://cdn.test/first.mp3", "First"),
        ("https://cdn.test/second.mp3", "Second"),
    ]
    instance.close()


async def test_splice_uses_public_html_before_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = AudioCrawler(tmp_path)
    page_url = "https://splice.com/sounds/packs/vendor/pack/presets"
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    monkeypatch.setattr(
        instance,
        "_discover_splice_pages",
        lambda _url, _stop=None: [("https://cdn.test/public.mp3", "Public Preview")],
    )

    async def browser_must_not_run(_url: str) -> list[str]:
        raise AssertionError("browser fallback should not run")

    monkeypatch.setattr(instance, "_sniff_urls", browser_must_not_run)

    assert await instance.sniff_urls(page_url) == ["https://cdn.test/public.mp3"]
    assert instance.discovered_titles == {"https://cdn.test/public.mp3": "Public Preview"}
    instance.close()


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


async def test_empty_browser_discovery_is_a_typed_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance = AudioCrawler(tmp_path)

    async def returns_empty(_url: str) -> list[str]:
        return []

    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)
    monkeypatch.setattr(instance, "_sniff_urls", returns_empty)

    with pytest.raises(NoAudioFoundError, match="example.com"):
        await instance.sniff_urls("https://example.com/catalogue")

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


def test_relative_redirect_is_resolved_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    first = SimpleNamespace(
        status_code=302,
        headers={"Location": "/audio.wav"},
        url="https://example.com/start",
        close=Mock(),
    )
    final = SimpleNamespace(
        status_code=200,
        headers={},
        history=[],
        url="https://example.com/audio.wav",
        iter_content=lambda _size: iter([b"audio"]),
        close=Mock(),
    )
    requested: list[str] = []

    def get(url, **_kwargs):
        requested.append(url)
        return first if len(requested) == 1 else final

    session = SimpleNamespace(get=get, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download("https://example.com/start")

    assert result is not None
    assert requested == ["https://example.com/start", "https://example.com/audio.wav"]
    first.close.assert_called_once()


def test_download_redirect_chain_validated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))

    # Mock socket.getaddrinfo to simulate the redirect going to a private IP
    def mock_getaddrinfo(host, port, *args, **kwargs):
        if host == "private.internal":
            return [(None, None, None, None, ("192.168.1.1", 80))]
        return [(None, None, None, None, ("8.8.8.8", 80))]

    monkeypatch.setattr(utils.network.socket, "getaddrinfo", mock_getaddrinfo)

    response = SimpleNamespace(
        status_code=302,
        headers={"Location": "http://private.internal/audio.wav"},
        url="http://example.com/audio.wav",
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


def test_content_disposition_catalogue_path_uses_only_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    response = SimpleNamespace(
        status_code=200,
        headers={"Content-Disposition": 'attachment; filename="20918/Indie_Tech_House_Demo.mp3"'},
        history=[],
        url="https://cdn.example.com/20918/Indie_Tech_House_Demo.mp3",
        iter_content=lambda _size: iter([b"audio-data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download(response.url)

    assert result is not None
    assert result.name == "Indie_Tech_House_Demo.mp3"
    assert result.parent == tmp_path


def test_encoded_url_path_separator_uses_only_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = SimpleNamespace(pre_download_ok=lambda *_args: (True, "ok"))
    url = "https://cdn.example.com/20918%2FIndie_Tech_House_Demo.mp3"
    response = SimpleNamespace(
        status_code=200,
        headers={},
        history=[],
        url=url,
        iter_content=lambda _size: iter([b"audio-data"]),
        close=lambda: None,
    )
    session = SimpleNamespace(get=lambda *args, **kwargs: response, close=lambda: None, headers={})
    instance = AudioCrawler(tmp_path, gate=gate, session=session)
    monkeypatch.setattr(crawler, "validate_public_url", lambda _url: None)

    result = instance.download(url)

    assert result is not None
    assert result.name == "Indie_Tech_House_Demo.mp3"
    assert result.parent == tmp_path


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
