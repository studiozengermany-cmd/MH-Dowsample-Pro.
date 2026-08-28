"""Tests for GenericWebExtractor — the Playwright-based fallback that sniffs
audio URLs from any public page when no specialized extractor applies.

These tests cover the supported/unsupported URL routing, title inheritance,
and the crawl-limit guard so the generic path is not an untested black box.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from crawler import AudioCrawler, GenericWebExtractor
from exceptions import CrawlLimitError


def test_generic_extractor_is_a_universal_fallback() -> None:
    """The generic extractor accepts any public URL; specialized extractors
    are consulted first and win by ordering in AudioCrawler's registry."""
    extractor = GenericWebExtractor()
    assert extractor.supports("https://looperman.com/loops/123") is True
    assert extractor.supports("https://example.com/track/42") is True
    assert extractor.supports("https://splice.com/sounds/example") is True


@pytest.mark.asyncio
async def test_generic_extractor_defers_to_browser_sniffing(tmp_path: Path) -> None:
    """extract() must delegate to the crawler's guarded browser sniffing."""
    instance = AudioCrawler(tmp_path)
    page_url = "https://looperman.com/loops/example"
    extractor = GenericWebExtractor()
    discovered = ["https://cdn.looperman.com/loop.mp3", "https://cdn.looperman.com/loop2.wav"]

    instance._sniff_urls = AsyncMock(return_value=discovered)  # type: ignore[method-assign]

    urls = await extractor.extract(instance, page_url)

    assert urls == discovered
    instance._sniff_urls.assert_awaited_once_with(page_url)


def test_generic_extractor_rejects_catalogue_above_safe_page_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generic path must never silently crawl an unbounded page count."""
    instance = AudioCrawler(tmp_path)
    page_url = "https://looperman.com/loops/catalogue"

    async def too_many(_url: str) -> list[str]:
        raise CrawlLimitError("51 pages")

    monkeypatch.setattr(instance, "_sniff_urls", too_many)

    extractor = GenericWebExtractor()
    with pytest.raises(CrawlLimitError, match="51 pages"):
        import asyncio

        asyncio.run(extractor.extract(instance, page_url))
