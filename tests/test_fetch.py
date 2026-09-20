import asyncio

from scraper import fetch


def test_extract_text_respects_configured_minimum(monkeypatch):
    monkeypatch.setattr(fetch.trafilatura, "extract", lambda *_args, **_kwargs: "short")

    assert fetch._extract_text("", min_text_length=5) == "short"
    assert fetch._extract_text("", min_text_length=6) is None


def test_shared_client_is_reused_within_loop(monkeypatch):
    made = []

    class FakeClient:
        is_closed = False

        def __init__(self, **kwargs):
            made.append(kwargs)

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeClient)
    fetch._shared_clients.clear()

    async def run():
        assert fetch.shared_client() is fetch.shared_client()

    asyncio.run(run())
    assert len(made) == 1


def test_shared_client_is_not_reused_across_loops(monkeypatch):
    class FakeClient:
        is_closed = False

        def __init__(self, **_kwargs):
            pass

    monkeypatch.setattr(fetch.httpx, "AsyncClient", FakeClient)
    fetch._shared_clients.clear()

    first = asyncio.run(_get_shared_client())
    second = asyncio.run(_get_shared_client())
    assert first is not second


async def _get_shared_client():
    return fetch.shared_client()
