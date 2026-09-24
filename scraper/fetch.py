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
    # No "br": httpx decodes Brotli only when the `brotli` package is
    # installed, and it is not. Until 2026-09-24 servers that honoured the
    # offer sent Brotli, httpx passed the bytes through undecoded, html2text
    # accepted them as text, and ~28% of cached pages were binary noise the
    # model then "extracted" communities from.
    "Accept-Encoding": "gzip, deflate",
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


#: Share of U+FFFD (the replacement character for undecodable bytes) above
#: which a page is binary noise, not text. Real pages measure ~0; the
#: undecoded-Brotli pages found on 2026-09-24 measured well above 5%.
_MAX_REPLACEMENT_SHARE = 0.02


def looks_undecoded(text: str) -> bool:
    """True when `text` is mostly bytes that failed to decode."""
    return bool(text) and text.count("\ufffd") / len(text) > _MAX_REPLACEMENT_SHARE


def _extract_text(html: str, min_text_length: int = 100) -> str | None:
    if looks_undecoded(html):
        # html2text accepts anything with 100 characters in it, so this has to
        # be refused before either extractor sees it.
        log.warning("fetch_undecoded_body")
        return None
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
) -> str | None:
    try:
        await assert_safe_public_url(url)
    except UnsafeURLError as exc:
        log.warning("fetch_unsafe_url_blocked", url=url, reason=str(exc))
        return None

    if _is_blocked(url, blocked_domains):
        log.debug("fetch_blocked", url=url)
        return None

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


