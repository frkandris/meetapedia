

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
