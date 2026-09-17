import asyncio
from pathlib import Path

import pytest
import yaml

from scraper.search import (
    LOCALE_TO_DATAFORSEO_LOCATION,
    DataForSEOClient,
    SearchUnavailableError,
    build_queries,
)


def test_build_queries_uses_primary_and_secondary_city_variants():
    assert build_queries("Budapest", ["Budapest", "Budapest Hungary"], ["running", "club"]) == [
        "running Budapest",
        "club Budapest",
        "running Budapest Hungary",
    ]


def test_build_queries_handles_missing_terms_or_variants():
    assert build_queries("Budapest", ["Budapest"], []) == []
    assert build_queries("Budapest", [], ["running"]) == ["running Budapest"]


@pytest.mark.asyncio
async def test_standard_search_posts_configured_high_priority(monkeypatch):
    posted = {}

    class FakeClient:
        # `shared_client()` reuses a cached client unless it is closed, and
        # `_search_standard` asks for one per poll — a fake without this
        # attribute fails on the second call instead of standing in for a client.
        is_closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            posted["payload"] = json
            raise RuntimeError("stop after payload capture")

    monkeypatch.setattr("scraper.search.httpx.AsyncClient", lambda **kwargs: FakeClient())
    client = DataForSEOClient(
        "login", "password", mode="standard", standard_priority=2,
        rate_limit_seconds=0,
    )
    with pytest.raises(SearchUnavailableError):
        await client.search("running Stockholm", locale="sv")

    assert posted["payload"][0]["priority"] == 2


def test_every_city_locale_has_a_dataforseo_location():
    """task_post rejects location-less tasks (40501 "Invalid Field:
    'location_name'"), and the pipeline's fail-fast then kills the whole pass —
    the 2026-07-16..23 outage started at Bratislava (locale sk, unmapped)."""
    cities = yaml.safe_load(
        (Path(__file__).parent.parent / "config" / "cities.yaml").read_text(
            encoding="utf-8"))["cities"]
    locales = {str(c["locale"]).split("-")[0] for c in cities}
    unmapped = locales - set(LOCALE_TO_DATAFORSEO_LOCATION)
    assert not unmapped, f"locales without DataForSEO location_code: {sorted(unmapped)}"


@pytest.mark.asyncio
async def test_standard_search_falls_back_to_us_location_for_unknown_locale(monkeypatch):
    posted = {}

    class FakeClient:
        # `shared_client()` reuses a cached client unless it is closed, and
        # `_search_standard` asks for one per poll — a fake without this
        # attribute fails on the second call instead of standing in for a client.
        is_closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            posted["payload"] = json
            raise RuntimeError("stop after payload capture")

    monkeypatch.setattr("scraper.search.httpx.AsyncClient", lambda **kwargs: FakeClient())
    client = DataForSEOClient(
        "login", "password", mode="standard", rate_limit_seconds=0,
    )
    with pytest.raises(SearchUnavailableError):
        await client.search("running Atlantis", locale="xx")

    # An unmapped locale is likely an invalid language_code too — both must
    # fall back or task_post still rejects the task and poisons the pair.
    assert posted["payload"][0]["location_code"] == 2840
    assert posted["payload"][0]["language_code"] == "en"


@pytest.mark.asyncio
async def test_standard_task_post_rejection_fails_fast_without_polling(monkeypatch):
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"status_code": 20000, "tasks": [{
                "id": "07230425-1757-0066-0000-dead",
                "status_code": 40501,
                "status_message": "Invalid Field: 'location_name'.",
            }]}

    class FakeClient:
        # `shared_client()` reuses a cached client unless it is closed, and
        # `_search_standard` asks for one per poll — a fake without this
        # attribute fails on the second call instead of standing in for a client.
        is_closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            return FakeResponse()

        async def get(self, url, headers):
            raise AssertionError("a rejected task must not be polled")

    monkeypatch.setattr("scraper.search.httpx.AsyncClient", lambda **kwargs: FakeClient())
    client = DataForSEOClient(
        "login", "password", mode="standard", rate_limit_seconds=0,
    )
    with pytest.raises(SearchUnavailableError, match="40501.*location_name"):
        await client.search("choir Bratislava", locale="sk")


def test_normal_priority_gets_a_longer_polling_window():
    """Half the price, a slower queue — the wait has to fit.

    DataForSEO publishes ~1 min for the priority queue and ~5 min for the
    normal one, with a stated *target* of 45 minutes. A flat 300s made normal
    priority time out, which is why we paid $1.2/1K instead of $0.6.
    """
    from scraper.search import DataForSEOClient
    high = DataForSEOClient("u", "p", mode="standard", standard_priority=2)
    normal = DataForSEOClient("u", "p", mode="standard", standard_priority=1)
    assert high._STANDARD_TIMEOUT_SECONDS == 300.0
    assert normal._NORMAL_PRIORITY_TIMEOUT_SECONDS > high._STANDARD_TIMEOUT_SECONDS


def test_shared_client_is_keyed_by_the_loop_object_not_its_id():
    """The pooled client must not outlive its event loop.

    Keyed by `id(loop)`, this test's two loops could collide: CPython reuses an
    address once the object is collected, so the second `asyncio.run` could be
    handed the first loop's client — a client whose connections belong to a loop
    that no longer exists. In the suite that meant a monkeypatched fake leaking
    into a later test, which is a symptom rather than the defect.
    """
    import gc

    from scraper import search as search_mod

    grabbed = []

    async def _grab():
        grabbed.append(search_mod.shared_client())

    asyncio.run(_grab())
    asyncio.run(_grab())

    assert grabbed[0] is not grabbed[1], "a new loop must get its own client"

    gc.collect()
    assert len(search_mod._shared_clients) == 0, (
        "entries must be released with their loop, not pinned for the process")
