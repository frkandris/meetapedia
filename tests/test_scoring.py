

def test_the_golden_set_can_be_restricted_to_one_market(tmp_path):
    """A ranking measured on the wrong workload describes the wrong thing.

    The corpus is roughly 30% Hungarian and 70% international, so an unfiltered
    sample is mostly English pages — while Hungarian is the primary market. A
    sibling project found this the expensive way: the model that scored better
    on its English sample dropped into English mid-answer on the real Hungarian
    task and lost half the required fields.
    """
    import json

    from scraper.db import init_db
    from scraper.scoring import golden_set

    db = tmp_path / "s.db"
    init_db(db)

    def _page(url, locale, name):
        import hashlib
        import sqlite3
        blob = json.dumps({
            "raw_text": "elég hosszú oldalszöveg " * 20,
            "records": [{"name": name, "locale": locale}],
        })
        with sqlite3.connect(db) as conn:
            conn.execute(
                "INSERT INTO cache_pages (url_hash, url, city, topic, domain,"
                " scraped_at, extracted_at, data) VALUES (?,?,?,?,?,?,?,?)",
                (hashlib.sha256(url.encode()).hexdigest()[:16], url, "X", "running",
                 "t", "2026-01-01", "2026-01-01", blob))

    _page("https://a.test", "hu", "Szentendrei Futóklub")
    _page("https://b.test", "en", "Brighton Runners")
    _page("https://c.test", "hu", "Pécsi Sakk Kör")

    assert len(golden_set(db, limit=10)) == 3               # every market
    hu = golden_set(db, limit=10, locale="hu")
    # A set: the order comes from url_hash, which is how the sample is kept
    # stable, not something a caller should depend on.
    assert {p["url"] for p in hu} == {"https://a.test", "https://c.test"}
    assert len(golden_set(db, limit=10, locale="en")) == 1
    assert golden_set(db, limit=10, locale="sv") == []


# ── the ledger sees what scoring spends ──────────────────────────────────────

def test_scoring_charges_the_ledger_for_every_call(tmp_path):
    """Scoring spends the provider's real allowance; the router must know.

    `score_model` drives an `_ApiExtractor` directly rather than through
    `FallbackExtractor`, so until 2026-09-19 nothing recorded its calls. A
    20-page run over three models is 60 real requests the router never learned
    about, and it then planned the rest of the day against an allowance that
    much too large — the same under-counting the 2026-09-12 enrichment fix
    removed, in the one place still doing it.

    Failures count too, for the reason they count everywhere else: a refused
    request still consumed a slot.
    """
    import asyncio

    from scraper.scoring import score_model

    class _Ex:
        provider, model, quality = "groq", "groq-m0", 60
        last_tokens = 11

        def __init__(self): self.n = 0

        async def extract(self, **kw):
            self.n += 1
            if self.n == 2:
                raise RuntimeError("upstream exploded")
            return []

    class _Router:
        def __init__(self): self.reserved, self.noted = 0, []

        def reserve(self, ex): self.reserved += 1

        def note(self, ex, **kw): self.noted.append(kw)

        def spec_for(self, ex): return None

    pages = [{"url": f"https://x/{i}", "city": "Budapest", "topic": "music",
              "text": "szöveg", "expected": frozenset()} for i in range(3)]
    router = _Router()
    asyncio.run(score_model(_Ex(), pages, router=router))

    assert router.reserved == 3, "one slot claimed per page, before the call"
    assert len(router.noted) == 3, "every call settled, successes and failures"
    assert [n["ok"] for n in router.noted] == [True, False, True]
    assert all(n["reserved"] for n in router.noted), "the reservation is settled, not doubled"
    assert router.noted[0]["tokens"] == 11


def test_a_broken_ledger_does_not_break_a_scoring_run(tmp_path):
    """A measurement must survive a bookkeeping problem.

    The same rule `FallbackExtractor._note_router` follows: losing a ledger
    write costs one number, losing the run costs the minutes of LLM calls that
    produced it.
    """
    import asyncio

    from scraper.scoring import score_model

    class _Ex:
        provider, model, quality = "groq", "groq-m0", 60
        last_tokens = 0

        async def extract(self, **kw): return []

    class _AngryRouter:
        def reserve(self, ex): raise RuntimeError("ledger down")

        def note(self, ex, **kw): raise RuntimeError("ledger down")

    pages = [{"url": "https://x/1", "city": "Budapest", "topic": "music",
              "text": "szöveg", "expected": frozenset()}]
    out = asyncio.run(score_model(_Ex(), pages, router=_AngryRouter()))
    assert out["answered"] == 1, "the run completed despite the ledger failing"
