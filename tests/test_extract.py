

def test_pooled_client_is_keyed_by_the_loop_object_not_its_id():
    """The pooled client must not outlive its event loop.

    The twin of the `search.py` regression fixed on 2026-09-17: keyed by
    `id(loop)`, two loops can collide, because CPython reuses an address once
    the object is collected — so the second `asyncio.run` could be handed the
    first loop's client, whose connections belong to a loop that no longer
    exists. `extract.py` kept the `id()` key when `search.py` lost it.

    Production has one loop and never fires this. The suite has one per test,
    and a client built while some earlier test monkeypatched
    `httpx.AsyncClient` is that test's fake, served to whoever comes next.
    """
    import asyncio
    import gc

    from scraper import extract as extract_mod

    grabbed = []

    async def _grab():
        grabbed.append(extract_mod._shared_client(60.0))

    asyncio.run(_grab())
    asyncio.run(_grab())

    assert grabbed[0] is not grabbed[1], "a new loop must get its own client"

    gc.collect()
    assert len(extract_mod._http_clients) == 0, (
        "entries must be released with their loop, not pinned for the process")


def test_one_loop_still_pools_per_timeout():
    """Splitting the key in two must not lose what it was for.

    The pool exists so a run does not open a connection pool per call, and it
    distinguishes timeouts because a paid reasoning endpoint and a free one do
    not share a deadline. Same loop, same timeout: one client. Same loop,
    different timeout: two.
    """
    import asyncio

    from scraper import extract as extract_mod

    seen = {}

    async def _grab():
        seen["a"] = extract_mod._shared_client(60.0)
        seen["b"] = extract_mod._shared_client(60.0)
        seen["c"] = extract_mod._shared_client(600.0)

    asyncio.run(_grab())
    assert seen["a"] is seen["b"], "one client per (loop, timeout)"
    assert seen["a"] is not seen["c"], "a different timeout is a different client"


# ── Answers that are nearly right ────────────────────────────────────────────
# Both of these were thrown away in production on 2026-09-20/21, and both cost
# a call we had already paid for. A discarded answer is not neutral: the page
# is retried by every run until the quarantine holds it, and it counts against
# the provider's day either way.

def test_a_stray_brace_before_the_answer_is_recovered():
    """Observed from a free OpenRouter model: `{\\n{"communities": [...]}`.

    The answer named a real association — Városlődi Sport és Szabadidős
    Egyesület — and the strict parser rejected the lot over one character.
    """
    from scraper.extract import _parse_communities

    raw = ('{\n{"communities": [{"name": "Városlődi Sport és Szabadidős Egyesület", '
           '"confidence": 0.9, "joinable": true}]}')
    records = _parse_communities(raw, "Városlőd", "sport", "hu", "https://varoslod.hu/")
    assert [r.name for r in records] == ["Városlődi Sport és Szabadidős Egyesület"]


def test_commentary_after_the_object_does_not_break_it():
    from scraper.extract import _parse_communities

    raw = ('{"communities": [{"name": "Futó Kör", "confidence": 0.8, "joinable": true}]}\n'
           "Remélem, ez segít!")
    assert [r.name for r in _parse_communities(
        raw, "Budapest", "running", "hu", "https://a.test")] == ["Futó Kör"]


def test_a_genuinely_broken_answer_still_raises():
    """The recovery must not become a way of inventing results.

    A failed extraction is never cached, so raising is what gets the page
    retried; returning [] would record "no communities" permanently.
    """
    import pytest

    from scraper.extract import ExtractorContentError, _parse_communities

    for raw in ("", "nincs itt semmi", "[1, 2, 3]"):
        with pytest.raises(ExtractorContentError):
            _parse_communities(raw, "Budapest", "running", "hu", "https://a.test")


def test_an_empty_content_falls_back_to_the_reasoning_field():
    """A reasoning model out of budget mid-thought returns an empty `content`
    with the answer in `reasoning` — most of one provider's 331 failed calls
    on 2026-09-20 were that empty string."""
    from scraper.extract import _message_text

    assert _message_text({"choices": [{"message": {
        "content": "", "reasoning": '{"communities": []}'}}]}) == '{"communities": []}'
    assert _message_text({"choices": [{"message": {
        "content": "", "reasoning_content": "válasz"}}]}) == "válasz"
    # Real content always wins.
    assert _message_text({"choices": [{"message": {
        "content": "igazi", "reasoning": "gondolat"}}]}) == "igazi"
    assert _message_text({"choices": [{"message": {}}]}) == ""
