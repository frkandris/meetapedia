"""Our own GPU may be switched off, asleep, or throttled by its owner's governor.

On 2026-10-23 the machine was switched off at 09:34 UTC. Every enrichment call
from then on ended in Cloudflare's HTTP 530, counted as a failure, and booked a
"used" request in the ledger — 6,623 of them by the afternoon — while the daily
guides deferred on the same error every fifteen minutes.
"""
import asyncio
import json

import httpx
import pytest

from scraper import extract as extract_mod
from scraper.extract import (
    ExtractorAbsentError,
    ExtractorUnavailableError,
    FallbackExtractor,
    _ApiExtractor,
)
from scraper.providers import OpenAICompatExtractor


@pytest.fixture(autouse=True)
def _clean():
    extract_mod._ABSENT_UNTIL.clear()
    _ApiExtractor._PROVIDER_SLOTS.clear()
    yield
    extract_mod._ABSENT_UNTIL.clear()
    _ApiExtractor._PROVIDER_SLOTS.clear()


def _client(monkeypatch, handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("scraper.extract._shared_client", lambda timeout: client)


def _gpu(**kw):
    return OpenAICompatExtractor(provider="localgpu", base_url="https://gpu.test/v1",
                                 api_key="k", model="qwen", quality=79,
                                 self_hosted=True, **kw)


@pytest.mark.asyncio
async def test_a_530_from_our_own_machine_is_absence_not_failure(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(530, text="<html>error 1033</html>")

    _client(monkeypatch, handler)
    ex = _gpu()
    with pytest.raises(ExtractorAbsentError):
        await ex._post({}, "p1")
    assert extract_mod.absent_for("localgpu") > 0
    # The next page does not walk the dead address again.
    with pytest.raises(ExtractorAbsentError):
        await ex._post({}, "p2")
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_hosted_provider_530_stays_an_ordinary_failure(monkeypatch):
    _client(monkeypatch, lambda request: httpx.Response(530, text="down"))
    ex = OpenAICompatExtractor(provider="groq", base_url="https://api.test/v1",
                               api_key="k", model="m", quality=60)
    with pytest.raises(ExtractorUnavailableError) as info:
        await ex._post({}, "p")
    assert not isinstance(info.value, ExtractorAbsentError)
    assert extract_mod.absent_for("groq") == 0


@pytest.mark.asyncio
async def test_the_chain_skips_an_absent_gpu_without_counting_a_failure():
    class _Stub:
        def __init__(self, provider, outcome):
            self.provider, self.model, self.quality = provider, "m", 70
            self.self_hosted = provider == "localgpu"
            self.outcome = outcome
            self.calls = 0

        async def complete(self, *a, **kw):
            self.calls += 1
            if isinstance(self.outcome, Exception):
                if self.self_hosted:
                    extract_mod.mark_absent(self.provider)
                raise self.outcome
            return self.outcome

    gpu = _Stub("localgpu", ExtractorAbsentError("localgpu HTTP 530"))
    hosted = _Stub("mistral", "ok")
    fe = FallbackExtractor(primaries=[gpu, hosted])
    assert await fe._call("complete", "page") == "ok"
    assert fe._provider_failures == [0, 0]
    assert fe._consecutive_failures == 0
    # Known absent: not asked again on the next page.
    assert await fe._call("complete", "page2") == "ok"
    assert gpu.calls == 1


@pytest.mark.asyncio
async def test_the_gate_follows_the_slots_the_server_reports(monkeypatch):
    """The governor drops llama-server to one slot while its owner works. A
    second concurrent call would queue inside the server with the connection
    open — Cloudflare's 524 — so the gate must shrink with it."""
    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, json={"total_slots": 1})
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _client(monkeypatch, handler)
    ex = _gpu(max_concurrency=2)
    live, peak = 0, 0
    real = ex._post_now

    async def _counting(payload, label):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        try:
            return await real(payload, label)
        finally:
            live -= 1

    ex._post_now = _counting
    await asyncio.gather(*[ex._post({}, f"p{i}") for i in range(4)])
    assert peak == 1
    assert _ApiExtractor._PROVIDER_SLOTS["localgpu"].limit == 1


@pytest.mark.asyncio
async def test_the_gate_never_exceeds_the_configured_maximum(monkeypatch):
    def handler(request):
        if request.url.path == "/props":
            return httpx.Response(200, text=json.dumps({"total_slots": 8}))
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    _client(monkeypatch, handler)
    ex = _gpu(max_concurrency=2)
    await ex._post({}, "p")
    assert _ApiExtractor._PROVIDER_SLOTS["localgpu"].limit == 2


def test_an_absent_call_gives_its_ledger_slot_back(tmp_path):
    from scraper.db import init_db
    from scraper.router import QuotaLedger

    db = tmp_path / "t.db"
    init_db(db)
    ledger = QuotaLedger(db, day="2026-10-23")
    ledger.reserve_call("localgpu")
    ledger.unreserve_call("localgpu")
    assert ledger.used("localgpu") == 0


def test_the_catalogue_marks_only_env_addressed_providers_self_hosted(monkeypatch):
    from scraper.providers import build_extractors, load_catalogue

    monkeypatch.setenv("LOCAL_GPU_URL", "https://gpu.test/v1")
    monkeypatch.setenv("LOCAL_GPU_KEY", "k")
    fleet = build_extractors(load_catalogue(), fingerprint_model="deepseek-chat")
    hosted = {e.provider for e in fleet if e.self_hosted}
    assert hosted == {"localgpu"}
