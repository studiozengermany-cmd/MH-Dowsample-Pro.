"""Playwright URL discovery and guarded streaming audio downloads."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
from playwright.async_api import BrowserContext, Page, Request, Response, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from config import (
    BROWSER_PROFILE_DIR,
    CRAWL_LAUNCH_TIMEOUT_MS,
    CRAWL_TIMEOUT_SEC,
    CRAWL_WAIT_SEC,
    LOGIN_TIMEOUT_SEC,
    QUALITY,
    configure_playwright_runtime,
    find_browser_executable,
)
from exceptions import (
    AuthenticationRequiredError,
    BrowserUnavailableError,
    CrawlTimeoutError,
    FileTooLargeError,
    HTTPError,
    NetworkError,
    PathTraversalError,
)
from quality_gate import QualityGate
from utils.paths import safe_child, sanitize_filename
from utils.retry import retry

_AUDIO_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".aiff"}
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


def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise PathTraversalError("Only absolute HTTP/HTTPS URLs are allowed")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443)}
    except socket.gaierror as exc:
        raise NetworkError(f"Cannot resolve {parsed.hostname}") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise NetworkError(f"Private or non-global address is blocked: {ip}")


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
    """Return playable URLs plus human titles when the catalogue exposes them."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    search = data.get("assetsSearch") if isinstance(data, dict) else None
    items = search.get("items") if isinstance(search, dict) else None
    if not isinstance(items, list):
        return []

    samples: list[tuple[str, str | None]] = []
    for item in items:
        files = item.get("files") if isinstance(item, dict) else None
        if not isinstance(files, list):
            continue
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
    return samples


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
        self.browser_executable = find_browser_executable()
        self._browser_lock = asyncio.Lock()
        self.discovered_titles: dict[str, str] = {}

    @property
    def interactive_login_supported(self) -> bool:
        return self.browser_executable is not None

    async def sniff_urls(self, page_url: str) -> list[str]:
        validate_public_url(page_url)
        if _is_audio_url(page_url):
            self.discovered_titles.clear()
            self.discovered_titles[page_url] = unquote(Path(urlparse(page_url).path).stem)
            return [page_url]
        try:
            return await asyncio.wait_for(self._sniff_urls(page_url), timeout=CRAWL_TIMEOUT_SEC)
        except TimeoutError as exc:
            raise CrawlTimeoutError(f"Browser crawl timed out after {CRAWL_TIMEOUT_SEC:g} seconds") from exc
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
                    else:
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
                await page.goto(page_url, wait_until="domcontentloaded", timeout=30_000)
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
                if self._is_splice_url(page_url) and await self._splice_requires_login(page):
                    raise AuthenticationRequiredError("Splice requires an authenticated browser session")
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

    async def login_site(self, site_url: str, timeout_sec: float = LOGIN_TIMEOUT_SEC) -> bool:
        validate_public_url(site_url)
        if not self.browser_executable:
            raise BrowserUnavailableError("No interactive Chromium-based browser is installed")
        async with self._browser_lock:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            process = await asyncio.create_subprocess_exec(
                str(self.browser_executable),
                f"--user-data-dir={self.profile_dir.resolve()}",
                "--no-first-run",
                "--no-default-browser-check",
                "--new-window",
                site_url,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_sec)
                if self._is_splice_url(site_url):
                    return await self._verify_splice_session()
                return await self._verify_site_session(site_url)
            except TimeoutError:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (ProcessLookupError, TimeoutError):
                    process.kill()
                    await process.wait()
                return False
            except asyncio.CancelledError:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=5)
                except (ProcessLookupError, TimeoutError):
                    process.kill()
                    await process.wait()
                raise

    async def login_splice(self, timeout_sec: float = LOGIN_TIMEOUT_SEC) -> bool:
        """Compatibility wrapper for existing callers."""
        return await self.login_site("https://splice.com/accounts/sign-in", timeout_sec)

    async def _verify_site_session(self, site_url: str) -> bool:
        """Verify that the persisted browser profile can reopen a generic site."""
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    self.profile_dir,
                    headless=True,
                    timeout=CRAWL_LAUNCH_TIMEOUT_MS,
                    user_agent=self._USER_AGENT,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    response = await page.goto(site_url, wait_until="commit", timeout=20_000)
                    await self._sync_browser_session(context, site_url)
                    return response is not None and response.status < 400
                finally:
                    await context.close()
        except PlaywrightError:
            return False

    async def _verify_splice_session(self) -> bool:
        """Require positive authenticated UI evidence; never infer success from a closed window."""
        try:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    self.profile_dir,
                    headless=True,
                    timeout=CRAWL_LAUNCH_TIMEOUT_MS,
                    user_agent=self._USER_AGENT,
                    args=["--no-first-run", "--no-default-browser-check"],
                )
                try:
                    page = context.pages[0] if context.pages else await context.new_page()
                    await page.goto(
                        "https://splice.com/sounds/genres/drum-and-bass/samples",
                        wait_until="commit",
                        timeout=20_000,
                    )
                    login_link = page.locator('a[href*="/accounts/sign-in"]')
                    play_buttons = page.locator('button[aria-label="play sample"]')
                    for _attempt in range(20):
                        if await login_link.count():
                            return False
                        buttons = await play_buttons.all()
                        for button in buttons[:5]:
                            if await button.is_visible():
                                return True
                        await page.wait_for_timeout(1_000)
                    return False
                finally:
                    await context.close()
        except PlaywrightError:
            return False

    @staticmethod
    def _is_splice_url(url: str) -> bool:
        hostname = (urlparse(url).hostname or "").lower()
        return hostname == "splice.com" or hostname.endswith(".splice.com")

    @staticmethod
    async def _splice_requires_login(page: Page) -> bool:
        if urlparse(page.url).path.startswith("/accounts/sign-in"):
            return True
        return await page.locator('a[href*="/accounts/sign-in"]').count() > 0

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
                response = client.get(url, stream=True, allow_redirects=True, timeout=(10, 60))
            except requests.RequestException as exc:
                raise NetworkError(str(exc)) from exc
            try:
                for item in response.history:
                    validate_public_url(item.headers.get("Location", item.url))
                validate_public_url(response.url)
                if response.status_code >= 400:
                    raise HTTPError(response.status_code)
                raw = self._response_filename(response, url)
                raw_suffix = Path(raw).suffix.lower()
                suffix = (
                    raw_suffix if raw_suffix in _AUDIO_SUFFIXES else _response_audio_suffix(response, url)
                )
                if suggested_name and (_opaque_download_name(raw) or raw_suffix not in _AUDIO_SUFFIXES):
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
                total = 0
                try:
                    with partial.open("xb") as handle:
                        for chunk in response.iter_content(64 * 1024):
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
            return unquote(value)
        return unquote(Path(urlparse(url).path).name) or "download.bin"

    @staticmethod
    def _available_path(target: Path) -> Path:
        candidate, counter = target, 1
        while candidate.exists() or candidate.with_suffix(candidate.suffix + ".part").exists():
            candidate = target.with_stem(f"{target.stem}_{counter}")
            counter += 1
        return candidate

    def close(self) -> None:
        self.session.close()
