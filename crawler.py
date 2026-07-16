"""Playwright URL discovery and guarded streaming audio downloads."""

from __future__ import annotations

import asyncio
import html as html_lib
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse

import requests
from playwright.async_api import BrowserContext, Request, Response, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import (
    BROWSER_PROFILE_DIR,
    CRAWL_LAUNCH_TIMEOUT_MS,
    CRAWL_TIMEOUT_SEC,
    CRAWL_WAIT_SEC,
    QUALITY,
    configure_playwright_runtime,
)
from exceptions import (
    BrowserUnavailableError,
    CrawlLimitError,
    CrawlTimeoutError,
    FileTooLargeError,
    HTTPError,
    NetworkError,
    NoAudioFoundError,
)
from quality_gate import QualityGate
from utils.network import request_with_safe_redirects, validate_public_url
from utils.paths import safe_child, sanitize_filename
from utils.retry import retry

_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff"}
_MAX_SPLICE_PAGES = 50
_SVELTEKIT_FETCHED_SCRIPT = re.compile(
    r"<script\b[^>]*\bdata-sveltekit-fetched\b[^>]*>(.*?)</script>",
    flags=re.IGNORECASE | re.DOTALL,
)
_CONTENT_TYPE_SUFFIXES = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/aiff": ".aiff",
    "audio/x-aiff": ".aiff",
}


def _is_audio_url(value: str) -> bool:
    if not value.startswith(("http://", "https://")):
        return False
    return Path(urlparse(value).path).suffix.lower() in _AUDIO_SUFFIXES


def extract_audio_urls_from_payload(payload: object) -> list[str]:
    """Recursively collect every HTTP audio URL exposed by a JSON response."""
    return [url for url, _title in extract_audio_assets_from_payload(payload)]


def extract_audio_assets_from_payload(payload: object) -> list[tuple[str, str | None]]:
    """Collect audio URLs and inherit a nearby catalogue title on any JSON shape."""
    assets: list[tuple[str, str | None]] = []

    def visit(value: object, inherited_title: str | None = None) -> None:
        if isinstance(value, dict):
            title = _catalogue_title(value) or inherited_title
            media_type = next(
                (
                    child
                    for key, child in value.items()
                    if str(key).lower() in {"content_type", "contenttype", "mime_type", "mimetype"}
                    and isinstance(child, str)
                ),
                "",
            )
            audio_record = media_type.lower().startswith("audio/")
            for key, child in value.items():
                key_text = str(key).lower()
                urlish = any(marker in key_text for marker in ("url", "src", "file", "download", "media"))
                keyed_audio = urlish and ("audio" in key_text or "sound" in key_text)
                non_audio_asset = any(
                    marker in key_text for marker in ("cover", "image", "artwork", "thumbnail", "waveform")
                )
                if (
                    isinstance(child, str)
                    and child.startswith(("http://", "https://"))
                    and (
                        _is_audio_url(child)
                        or keyed_audio
                        or (audio_record and urlish and not non_audio_asset)
                    )
                ):
                    if all(existing_url != child for existing_url, _existing_title in assets):
                        assets.append((child, title))
                    continue
                visit(child, title)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, inherited_title)
            return
        if not isinstance(value, str) or not _is_audio_url(value):
            return
        if all(existing_url != value for existing_url, _existing_title in assets):
            assets.append((value, inherited_title))

    visit(payload)
    return assets


def extract_splice_listed_sample_urls(payload: object) -> list[str]:
    """Return one playable audio URL for every sample row in a Splice search response."""
    return [url for url, _title in extract_splice_listed_samples(payload)]


def extract_splice_listed_samples(payload: object) -> list[tuple[str, str | None]]:
    """Return playable URLs plus titles from any Splice catalogue response shape."""
    samples: list[tuple[str, str | None]] = []

    def visit(value: object) -> None:
        if isinstance(value, list):
            for child in value:
                visit(child)
            return
        if not isinstance(value, dict):
            return

        files = value.get("files")
        if isinstance(files, list):
            item = value
            candidates = [
                file
                for file in files
                if isinstance(file, dict)
                and isinstance(file.get("url"), str)
                and str(file["url"]).startswith(("http://", "https://"))
                and Path(urlparse(str(file["url"])).path).suffix.lower() in _AUDIO_SUFFIXES
            ]
            preferred = next(
                (file for file in candidates if file.get("asset_file_type_slug") == "preview_mp3"),
                candidates[0] if candidates else None,
            )
            if preferred is not None:
                url = str(preferred["url"])
                title = _catalogue_title(item)
                if all(existing_url != url for existing_url, _existing_title in samples):
                    samples.append((url, title))

        for child in value.values():
            visit(child)

    visit(payload)
    return samples


def extract_splice_page_assets(document: str) -> tuple[list[tuple[str, str | None]], int, int]:
    """Read public preset previews and pagination from Splice's server-rendered HTML."""
    assets: list[tuple[str, str | None]] = []
    current_page = 1
    total_pages = 1

    def add(items: list[tuple[str, str | None]]) -> None:
        for url, title in items:
            if all(existing_url != url for existing_url, _existing_title in assets):
                assets.append((url, title))

    def visit_metadata(value: object) -> None:
        nonlocal current_page, total_pages
        if isinstance(value, list):
            for child in value:
                visit_metadata(child)
            return
        if not isinstance(value, dict):
            return
        metadata = value.get("pagination_metadata")
        if isinstance(metadata, dict):
            try:
                current_page = max(1, int(metadata.get("currentPage") or current_page))
                total_pages = max(current_page, int(metadata.get("totalPages") or total_pages))
            except (TypeError, ValueError):
                pass
        for child in value.values():
            visit_metadata(child)

    for match in _SVELTEKIT_FETCHED_SCRIPT.finditer(document):
        try:
            envelope = json.loads(html_lib.unescape(match.group(1)).strip())
        except (json.JSONDecodeError, TypeError):
            continue
        body = envelope.get("body") if isinstance(envelope, dict) else None
        try:
            payload = json.loads(body) if isinstance(body, str) else body
        except json.JSONDecodeError:
            continue
        listed = extract_splice_listed_samples(payload)
        add(listed)
        visit_metadata(payload)

    if assets:
        return assets, current_page, total_pages

    # Retain a guarded fallback for future server-rendering changes where the
    # response body is no longer wrapped by data-sveltekit-fetched.
    normalized = html_lib.unescape(document).replace(r"\/", "/").replace(r"\u0026", "&")
    for raw_url in re.findall(r"https?://[^\s\"'<>\\]+", normalized):
        url = raw_url.rstrip(".,);]")
        if not _is_audio_url(url):
            continue
        title = unquote(Path(urlparse(url).path).stem) or None
        add([(url, title)])
    return assets, current_page, total_pages


def _catalogue_title(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    candidates = [item]
    candidates.extend(value for value in item.values() if isinstance(value, dict))
    for candidate in candidates:
        for field in ("name", "title", "display_name", "displayName"):
            value = candidate.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return None


def _opaque_download_name(filename: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{24,}", Path(filename).stem, flags=re.IGNORECASE))


def _response_audio_suffix(response: requests.Response, url: str) -> str:
    path_suffix = Path(urlparse(response.url or url).path).suffix.lower()
    if path_suffix in _AUDIO_SUFFIXES:
        return path_suffix
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    return _CONTENT_TYPE_SUFFIXES.get(content_type, ".bin")


class AudioCrawler:
    _USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self, download_dir: Path, gate: QualityGate | None = None, session: requests.Session | None = None
    ) -> None:
        configure_playwright_runtime()
        self.download_dir = download_dir
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.gate = gate or QualityGate()
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", self._USER_AGENT)
        self.profile_dir = BROWSER_PROFILE_DIR
        self._browser_lock = asyncio.Lock()
        self.discovered_titles: dict[str, str] = {}

    async def sniff_urls(self, page_url: str) -> list[str]:
        validate_public_url(page_url)
        if _is_audio_url(page_url):
            self.discovered_titles.clear()
            self.discovered_titles[page_url] = unquote(Path(urlparse(page_url).path).stem)
            return [page_url]
        self.discovered_titles.clear()
        try:
            urls = await self._discover_urls(page_url)
            if not urls:
                hostname = urlparse(page_url).hostname or "unknown"
                raise NoAudioFoundError(f"No public audio assets were discovered on {hostname}")
            return urls
        except TimeoutError as exc:
            raise CrawlTimeoutError(f"Crawl timed out after {CRAWL_TIMEOUT_SEC:g} seconds") from exc
        except PlaywrightTimeoutError as exc:
            raise CrawlTimeoutError("The page did not become ready in time") from exc
        except PlaywrightError as exc:
            message = str(exc)
            if "Executable doesn't exist" in message:
                raise BrowserUnavailableError(
                    "Playwright Chromium is not installed; run: "
                    "python -m playwright install chromium --only-shell"
                ) from exc
            raise BrowserUnavailableError(f"Playwright browser failed: {message}") from exc

    async def _discover_urls(self, page_url: str) -> list[str]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CRAWL_TIMEOUT_SEC
        if self._is_splice_url(page_url):
            stop = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(self._discover_splice_pages, page_url, stop))
            try:
                assets = await asyncio.wait_for(asyncio.shield(worker), timeout=CRAWL_TIMEOUT_SEC)
            except (TimeoutError, asyncio.CancelledError):
                stop.set()
                try:
                    await worker
                except Exception:
                    pass
                raise
            if assets:
                for url, title in assets:
                    if title:
                        self.discovered_titles.setdefault(url, title)
                return [url for url, _title in assets]
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(self._sniff_urls(page_url), timeout=remaining)

    def _discover_splice_pages(
        self,
        page_url: str,
        stop: threading.Event | None = None,
    ) -> list[tuple[str, str | None]]:
        """Collect every bounded public Splice preview page without driving its UI."""
        stop = stop or threading.Event()
        assets: list[tuple[str, str | None]] = []
        seen_urls: set[str] = set()
        with requests.Session() as client:
            client.headers.update({"User-Agent": self._USER_AGENT})
            first_document = self._fetch_splice_page(client, page_url)
            first_assets, current_page, total_pages = extract_splice_page_assets(first_document)

            def add(items: list[tuple[str, str | None]]) -> None:
                for url, title in items:
                    if url not in seen_urls:
                        assets.append((url, title))
                        seen_urls.add(url)

            add(first_assets)
            total_pages = max(total_pages, current_page)
            if total_pages > _MAX_SPLICE_PAGES:
                raise CrawlLimitError(
                    f"Splice catalogue has {total_pages} pages; safe limit is {_MAX_SPLICE_PAGES}"
                )
            for page_number in range(1, total_pages + 1):
                if stop.is_set():
                    return []
                if page_number == current_page:
                    continue
                document = self._fetch_splice_page(client, self._with_page(page_url, page_number))
                page_assets, _current, _total = extract_splice_page_assets(document)
                add(page_assets)
        return assets

    @retry(
        attempts=3,
        delay=2.0,
        exceptions=(requests.Timeout, requests.ConnectionError, NetworkError),
    )
    def _fetch_splice_page(self, client: requests.Session, page_url: str) -> str:
        try:
            response = request_with_safe_redirects(
                client,
                "GET",
                page_url,
                validator=validate_public_url,
                timeout=(10, 30),
            )
        except requests.RequestException as exc:
            raise NetworkError(str(exc)) from exc
        try:
            if response.status_code >= 400:
                raise HTTPError(response.status_code)
            return response.text
        finally:
            response.close()

    @staticmethod
    def _with_page(page_url: str, page_number: int) -> str:
        parsed = urlparse(page_url)
        query = [(key, value) for key, value in parse_qsl(parsed.query) if key != "page"]
        query.append(("page", str(page_number)))
        return parsed._replace(query=urlencode(query)).geturl()

    async def _sniff_urls(self, page_url: str) -> list[str]:
        found: set[str] = set()
        listed_samples: set[str] = set()
        self.discovered_titles.clear()
        is_splice_page = self._is_splice_url(page_url)
        response_tasks: list[asyncio.Task[None]] = []
        async with self._browser_lock, async_playwright() as playwright:
            context = await playwright.chromium.launch_persistent_context(
                self.profile_dir,
                headless=True,
                timeout=CRAWL_LAUNCH_TIMEOUT_MS,
                user_agent=self._USER_AGENT,
                viewport={"width": 1920, "height": 1080},
                locale="vi-VN",
                timezone_id="Asia/Ho_Chi_Minh",
                args=[
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            try:
                page = context.pages[0] if context.pages else await context.new_page()

                def inspect(response: Response) -> None:
                    url = response.url
                    content_type = response.headers.get("content-type", "")
                    if Path(urlparse(url).path).suffix.lower() in _AUDIO_SUFFIXES or content_type.startswith(
                        "audio/"
                    ):
                        found.add(url)

                def inspect_request(request: Request) -> None:
                    if Path(urlparse(request.url).path).suffix.lower() in _AUDIO_SUFFIXES:
                        found.add(request.url)

                async def inspect_json(response: Response) -> None:
                    try:
                        payload = await response.json()
                    except (PlaywrightError, ValueError):
                        return
                    if is_splice_page:
                        for url, title in extract_splice_listed_samples(payload):
                            listed_samples.add(url)
                            if title:
                                self.discovered_titles.setdefault(url, title)
                    # Keep a generic fallback even for Splice. Its catalogue API has
                    # changed wrappers before, and a valid preview must not disappear
                    # merely because it was not nested below assetsSearch.items.
                    for url, title in extract_audio_assets_from_payload(payload):
                        found.add(url)
                        if title:
                            self.discovered_titles.setdefault(url, title)

                async def inspect_dom() -> None:
                    selector = ", ".join(
                        (
                            "audio[src]",
                            "audio source[src]",
                            "source[src]",
                            "a[href]",
                            "link[as='audio'][href]",
                            "meta[property^='og:audio'][content]",
                            "meta[name='twitter:player:stream'][content]",
                            "[data-audio-url]",
                            "[data-audio-src]",
                            "[data-preview-url]",
                            "[data-url]",
                            "[data-src]",
                        )
                    )
                    try:
                        items = await page.locator(selector).evaluate_all(
                            """elements => elements.map(element => {
                                const raw = element.getAttribute('src')
                                    || element.getAttribute('href')
                                    || element.getAttribute('content')
                                    || element.getAttribute('data-audio-url')
                                    || element.getAttribute('data-audio-src')
                                    || element.getAttribute('data-preview-url')
                                    || element.getAttribute('data-url')
                                    || element.getAttribute('data-src');
                                let url = '';
                                try { url = raw ? new URL(raw, document.baseURI).href : ''; } catch (_) {}
                                const card = element.closest('article, li, [data-name], [class*="card" i]');
                                const heading = card?.querySelector('h1, h2, h3, h4, [data-name]');
                                const title = element.getAttribute('data-name')
                                    || element.getAttribute('aria-label')
                                    || element.getAttribute('title')
                                    || card?.getAttribute('data-name')
                                    || heading?.textContent
                                    || document.title
                                    || '';
                                return {url, title: title.trim()};
                            })"""
                        )
                        resources = await page.evaluate(
                            "performance.getEntriesByType('resource').map(entry => entry.name)"
                        )
                    except PlaywrightError:
                        return
                    for item in items if isinstance(items, list) else []:
                        if not isinstance(item, dict):
                            continue
                        url, title = item.get("url"), item.get("title")
                        if isinstance(url, str) and _is_audio_url(url):
                            found.add(url)
                            if isinstance(title, str) and title.strip():
                                self.discovered_titles.setdefault(url, title.strip())
                    for resource in resources if isinstance(resources, list) else []:
                        if isinstance(resource, str) and _is_audio_url(resource):
                            found.add(resource)

                def inspect_all(response: Response) -> None:
                    inspect(response)
                    if "json" in response.headers.get("content-type", "").lower():
                        response_tasks.append(asyncio.create_task(inspect_json(response)))

                async def drain_response_tasks() -> None:
                    while response_tasks:
                        pending = response_tasks[:]
                        response_tasks.clear()
                        await asyncio.gather(*pending, return_exceptions=True)

                page.on("request", inspect_request)
                page.on("response", inspect_all)
                navigation = await page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
                if navigation is not None and navigation.status >= 400:
                    raise HTTPError(navigation.status)
                ready = page.locator(
                    'a[href*="/accounts/sign-in"], button[aria-label*="play" i], '
                    "audio, source[src], [data-audio-url], [data-audio-src], "
                    "meta[property^='og:audio'], link[as='audio']"
                )
                for _attempt in range(15):
                    if await ready.count():
                        break
                    await page.wait_for_timeout(1_000)
                await drain_response_tasks()
                await inspect_dom()
                if listed_samples:
                    await self._sync_browser_session(context, page_url)
                    return sorted(listed_samples)
                selectors = (
                    'button[data-qa="playPausePlaybackButton"]',
                    "audio",
                    "button[aria-label*='play' i]",
                    "button[title*='play' i]",
                    "[class*='play-button' i]",
                    "[class*='PlayButton']",
                    "[data-action='play']",
                    ".waveform__wrapper",
                )
                stable_rounds = 0
                for _round in range(50):
                    clicked_this_round = 0
                    for selector in selectors:
                        elements = await page.locator(selector).all()
                        if not elements:
                            continue
                        for element in elements:
                            try:
                                if await element.get_attribute("data-audio-crawler-clicked") == "1":
                                    continue
                                await element.evaluate(
                                    "element => element.setAttribute('data-audio-crawler-clicked', '1')"
                                )
                                if not await element.is_visible():
                                    continue
                                await element.click(timeout=750)
                                clicked_this_round += 1
                                await page.wait_for_timeout(100)
                            except PlaywrightError:
                                pass
                        # The selectors are ordered from site-specific to broad fallbacks.
                        # Once one selector identifies the player set, scanning broader
                        # selectors would click duplicate containers and unrelated controls.
                        break
                    before_height = int(await page.evaluate("document.body.scrollHeight"))
                    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    await page.wait_for_timeout(750)
                    after_height = int(await page.evaluate("document.body.scrollHeight"))
                    await drain_response_tasks()
                    await inspect_dom()
                    stable_rounds = (
                        stable_rounds + 1 if clicked_this_round == 0 and after_height <= before_height else 0
                    )
                    if stable_rounds >= 3:
                        break
                await page.wait_for_timeout(int(CRAWL_WAIT_SEC * 1000))
                await drain_response_tasks()
                await inspect_dom()
                if listed_samples:
                    await self._sync_browser_session(context, page_url)
                    return sorted(listed_samples)
                await self._sync_browser_session(context, page_url)
            finally:
                await context.close()
        return sorted(found)

    async def _sync_browser_session(self, context: BrowserContext, page_url: str) -> None:
        """Carry browser authentication and origin context into HTTP downloads."""
        for cookie in await context.cookies():
            self.session.cookies.set(
                cookie["name"],
                cookie["value"],
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
        self.session.headers["Referer"] = page_url

    @staticmethod
    def _is_splice_url(url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname == "splice.com" or hostname.endswith(".splice.com")

    @retry(attempts=3, delay=0.5, exceptions=(requests.Timeout, requests.ConnectionError, NetworkError))
    def download(
        self,
        url: str,
        output_dir: Path | None = None,
        suggested_name: str | None = None,
    ) -> Path | None:
        validate_public_url(url)
        client, owns_client = self._download_session()
        try:
            ok, _reason = self.gate.pre_download_ok(url, client)
            if not ok:
                return None
            try:
                response = request_with_safe_redirects(
                    client,
                    "GET",
                    url,
                    validator=validate_public_url,
                    stream=True,
                    timeout=(5, 15),
                )
            except requests.RequestException as exc:
                raise NetworkError(str(exc)) from exc
            try:
                if response.status_code >= 400:
                    raise HTTPError(response.status_code)
                raw = self._response_filename(response, url)
                raw_suffix = Path(raw).suffix.lower()
                suffix = (
                    raw_suffix if raw_suffix in _AUDIO_SUFFIXES else _response_audio_suffix(response, url)
                )
                if suggested_name:
                    raw = f"{Path(suggested_name).stem}{suffix}"
                elif raw_suffix not in _AUDIO_SUFFIXES:
                    raw = f"{Path(raw).stem}{suffix}"
                filename = sanitize_filename(raw, fallback="download.bin")
                destination = output_dir or self.download_dir
                destination.mkdir(parents=True, exist_ok=True)
                target = safe_child(destination, filename)
                target = self._available_path(target)
                partial = target.with_suffix(target.suffix + ".part")
                maximum = int(QUALITY["max_file_mb"]) * 1024 * 1024
                import time
                start_time = time.time()
                total = 0
                try:
                    with partial.open("xb") as handle:
                        for chunk in response.iter_content(64 * 1024):
                            if time.time() - start_time > 15:
                                raise NetworkError("Download taking too long")
                            if not chunk:
                                continue
                            total += len(chunk)
                            if maximum and total > maximum:
                                raise FileTooLargeError(f"Download exceeds {QUALITY['max_file_mb']} MB")
                            handle.write(chunk)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(partial, target)
                    return target
                except BaseException:
                    partial.unlink(missing_ok=True)
                    raise
            finally:
                response.close()
        finally:
            if owns_client:
                client.close()

    def _download_session(self) -> tuple[requests.Session, bool]:
        if not isinstance(self.session, requests.Session):
            return self.session, False
        client = requests.Session()
        client.headers.update(self.session.headers)
        client.cookies.update(self.session.cookies)
        return client, True

    @staticmethod
    def _response_filename(response: requests.Response, url: str) -> str:
        disposition = response.headers.get("Content-Disposition", "")
        marker = "filename="
        if marker in disposition.lower():
            value = disposition.split("=", 1)[1].strip().strip("\"'")
            # Some CDNs include a catalogue folder in filename= (for example
            # "20918/demo.mp3").  A server-provided filename must never create
            # directories locally, so retain only its final path component.
            return unquote(value).replace("\\", "/").rsplit("/", 1)[-1] or "download.bin"
        decoded_path = unquote(urlparse(url).path).replace("\\", "/")
        return decoded_path.rsplit("/", 1)[-1] or "download.bin"

    @staticmethod
    def _available_path(target: Path) -> Path:
        candidate, counter = target, 1
        while candidate.exists() or candidate.with_suffix(candidate.suffix + ".part").exists():
            candidate = target.with_stem(f"{target.stem}_{counter}")
            counter += 1
        return candidate

    def close(self) -> None:
        self.session.close()
