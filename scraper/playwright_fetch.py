import asyncio
from urllib.parse import urlparse

import structlog

from .url_safety import UnsafeURLError, assert_safe_public_url

log = structlog.get_logger()

_LOGIN_MARKERS = (
    'id="loginbutton"',
    'id="login_button"',
    '"login_form"',
    "Log in to Facebook",
    "You must log in",
    "Log In to Instagram",
    "This page isn't available",
    # Reddit
    "Log in to Reddit",
    "reddit.com/login",
)


def _is_login_wall(html: str) -> bool:
    return any(marker in html for marker in _LOGIN_MARKERS)


class PlaywrightFetcher:
    """Headless Chromium fetcher for JS-heavy / bot-blocking domains."""

    def __init__(self, domains: list[str], timeout_seconds: int = 20):
        self.domains = [d.lower() for d in domains]
        self.timeout_seconds = timeout_seconds
        self._browser = None
        self._pw = None

    def matches(self, url: str) -> bool:
        try:
            from .fetch import host_matches_domain
            host = urlparse(url).netloc
            return any(host_matches_domain(host, d) for d in self.domains)
        except Exception:
            return False

    async def start(self) -> None:
        try:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
            log.info("playwright_browser_started")
        except ImportError:
            log.warning(
                "playwright_not_installed",
                hint="pip install '.[browser]' && playwright install chromium",
            )

    async def stop(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
            if self._pw:
                await self._pw.stop()
        except Exception as exc:
            log.debug("playwright_stop_error", error=str(exc))

    async def fetch(self, url: str, min_text_length: int = 100) -> str | None:
        if not self._browser:
            return None
        page = None
        try:
            context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )

            async def _guard_request(route) -> None:
                request_url = route.request.url
                if request_url.startswith(("http://", "https://")):
                    try:
                        await assert_safe_public_url(request_url)
                    except UnsafeURLError:
                        await route.abort()
                        return
                await route.continue_()

            await context.route("**/*", _guard_request)
            page = await context.new_page()
            await page.goto(
                url,
                timeout=self.timeout_seconds * 1000,
                wait_until="domcontentloaded",
            )
            # Reddit and other SPAs need extra time to hydrate
            host = urlparse(url).netloc.lower()
            wait = 3.0 if "reddit.com" in host else 1.5
            await asyncio.sleep(wait)
            html = await page.content()
        except Exception as exc:
            log.debug("playwright_fetch_failed", url=url, error=str(exc))
            return None
        finally:
            if page:
                try:
                    await page.close()
                    await context.close()
                except Exception:
                    pass

        if _is_login_wall(html):
            log.debug("playwright_login_wall", url=url)
            return None

        from .fetch import _extract_text
        text = _extract_text(html, min_text_length=min_text_length)
        if text:
            log.info("playwright_fetch_ok", url=url, chars=len(text))
        return text
