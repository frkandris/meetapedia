import asyncio
import weakref
from urllib.parse import urljoin, urlparse

import html2text
import httpx
import structlog
import trafilatura

from .url_safety import UnsafeURLError, assert_safe_public_url

log = structlog.get_logger()

# One connection pool per event loop.  Search and extraction already use this
# shape; fetch used to construct a client (and therefore a fresh DNS/TLS/socket
# pool) for every URL.  A loop object is the key rather than id(loop): CPython
# may reuse an id after a test loop is collected.
_shared_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary()
)


def shared_client() -> "httpx.AsyncClient":
    loop = asyncio.get_running_loop()
    client = _shared_clients.get(loop)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            follow_redirects=False,
            headers=_HEADERS,
            trust_env=False,
        )
        _shared_clients[loop] = client
    return client

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT": "1",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def host_matches_domain(host: str, domain: str) -> bool:
    """True when host IS the domain or a subdomain of it. Plain substring
    matching was a false-positive trap: 'x.com' matched linux.com, maxx.com…"""
    host = host.lower().split(":")[0].lstrip(".")
    domain = domain.lower().lstrip(".")
    return host == domain or host.endswith("." + domain)


def _is_blocked(url: str, blocked_domains: list[str]) -> bool:
    try:
        host = urlparse(url).netloc
        return any(host_matches_domain(host, d) for d in blocked_domains)
    except Exception:
        return False


def _extract_text(html: str, min_text_length: int = 100) -> str | None:
    text = trafilatura.extract(html, include_comments=False, include_tables=False)
    if text and len(text) >= min_text_length:
        return text
    converter = html2text.HTML2Text()
    converter.ignore_links = True
    converter.ignore_images = True
    fallback = converter.handle(html).strip()
    return fallback if len(fallback) >= min_text_length else None


async def fetch_and_clean(
    url: str,
    blocked_domains: list[str],
    timeout_seconds: int = 15,
    min_text_length: int = 100,
    semaphore: asyncio.Semaphore | None = None,
    playwright_fetcher=None,
) -> str | None:
    try:
        await assert_safe_public_url(url)
    except UnsafeURLError as exc:
        log.warning("fetch_unsafe_url_blocked", url=url, reason=str(exc))
        return None

    if _is_blocked(url, blocked_domains):
        log.debug("fetch_blocked", url=url)
        return None

    if playwright_fetcher and playwright_fetcher.matches(url):
        async def _playwright_fetch() -> str | None:
            return await playwright_fetcher.fetch(url, min_text_length=min_text_length)
        if semaphore:
            async with semaphore:
                return await _playwright_fetch()
        return await _playwright_fetch()

    async def _fetch() -> str | None:
        try:
            client = shared_client()
            current_url = url
            resp = None
            for _redirect_count in range(6):
                await assert_safe_public_url(current_url)
                if _is_blocked(current_url, blocked_domains):
                    log.debug("fetch_redirect_blocked", url=current_url)
                    return None
                resp = await client.get(current_url, timeout=timeout_seconds)
                if resp.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = resp.headers.get("location")
                if not location:
                    return None
                current_url = urljoin(str(resp.url), location)
            else:
                log.warning("fetch_too_many_redirects", url=url)
                return None

            if resp is None:
                return None
            if resp.status_code >= 400:
                log.warning("fetch_failed", url=current_url, status=resp.status_code)
                return None
            if "text/html" not in resp.headers.get("content-type", ""):
                return None
            return _extract_text(resp.text, min_text_length=min_text_length)
        except UnsafeURLError as exc:
            log.warning("fetch_unsafe_redirect_blocked", url=url, reason=str(exc))
            return None
        except Exception as exc:
            log.debug("fetch_failed", url=url, error=str(exc))
            return None

    if semaphore:
        async with semaphore:
            return await _fetch()
    return await _fetch()


async def fetch_many(
    urls: list[str],
    blocked_domains: list[str],
    max_pages: int = 5,
    timeout_seconds: int = 15,
    min_text_length: int = 100,
    max_concurrent: int = 3,
) -> list[tuple[str, str]]:
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [
        fetch_and_clean(url, blocked_domains, timeout_seconds, min_text_length, semaphore)
        for url in urls[:max_pages]
    ]
    results = await asyncio.gather(*tasks)
    return [(url, text) for url, text in zip(urls[:max_pages], results) if text]
