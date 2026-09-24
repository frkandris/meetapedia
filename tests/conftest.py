"""Shared test fixtures."""
import pytest


@pytest.fixture(autouse=True)
def _reset_pacing_clock():
    """Clear the router's process-wide rpm clock between tests.

    `QuotaLedger._last_call` is deliberately class-level so every ledger in the
    process shares it — the gateway builds a fresh router per request, and
    per-instance state would mean no rpm pacing on that path at all. The cost is
    that it also leaks across tests, so it is reset here rather than each test
    remembering to.
    """
    from scraper.router import QuotaLedger

    from scraper.extract import _LAST_SERVED

    QuotaLedger._last_call.clear()
    _LAST_SERVED.clear()
    yield
    QuotaLedger._last_call.clear()
    _LAST_SERVED.clear()


@pytest.fixture(autouse=True)
def _reset_sitemap_cache():
    """Clear the rendered-sitemap cache between tests.

    The cache is keyed by site alone — correct in production, where one process
    serves one corpus, and wrong in a test run, where every test swaps
    `app_state.db_path` under it. The first test to fetch /sitemap.xml pinned
    its own corpus for the whole hour-long TTL, so a later assertion read
    someone else's document: an order-dependent failure that passed when its
    file ran alone.
    """
    from scraper.web.app import _SITEMAP_CACHE

    _SITEMAP_CACHE.clear()
    yield
    _SITEMAP_CACHE.clear()


@pytest.fixture(autouse=True)
def _reset_shared_search_client():
    """Drop the search module's pooled HTTP client between tests.

    The pool is keyed by the running event loop, and pytest-asyncio builds one
    per test, so in production this never holds anything stale. In the suite it
    did: a test that monkeypatches `scraper.search.httpx.AsyncClient` leaves its
    fake in the pool, and the next test to reach the same key was served
    someone else's fake — which is what made two tests in `test_search.py`
    order-dependent. The pool is now a WeakKeyDictionary (see
    `scraper.search._shared_clients`), so this is belt *and* braces: isolation
    should not depend on when the garbage collector runs.
    """
    from scraper.search import _shared_clients

    _shared_clients.clear()
    yield
    _shared_clients.clear()


@pytest.fixture(autouse=True)
def _reset_shared_fetch_client():
    """Keep the fetch connection pool from leaking fakes between test loops."""
    from scraper.fetch import _shared_clients

    _shared_clients.clear()
    yield
    _shared_clients.clear()


@pytest.fixture(autouse=True)
def _reset_shared_extract_client():
    """Drop the extractor's pooled HTTP clients between tests.

    The twin of `_reset_shared_search_client`, for the same pool one module
    over. `scraper.extract` kept keying by `id(loop)` after `search.py` stopped
    (2026-09-17), so it carried the same latent leak: a client built under a
    monkeypatched `httpx.AsyncClient` could be served to a later test that never
    patched anything. Belt and braces next to the WeakKeyDictionary — isolation
    should not depend on when the garbage collector runs.
    """
    from scraper.extract import _http_clients

    _http_clients.clear()
    yield
    _http_clients.clear()
