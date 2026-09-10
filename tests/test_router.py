"""Free-tier model router: catalogue, quota ledger, ordering, upgrade policy."""
from pathlib import Path

import pytest

from scraper.db import (get_provider_usage, get_upgradable_pages, init_db,
                        record_provider_call, update_cache_page)
from scraper.providers import (ModelSpec, ProviderCatalogue, ProviderSpec,
                               RouterSettings, build_extractors, load_catalogue)
from scraper.router import ModelRouter, QuotaLedger, build_router


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def _spec(name="groq", rpd=1000, paid=False, quality=(60, 40), env="X_KEY", tpd=0):
    return ProviderSpec(
        name=name, base_url="https://api.test/v1", api_key_env=env,
        models=tuple(ModelSpec(model=f"{name}-m{i}", quality=q)
                     for i, q in enumerate(quality)),
        rpm=30, rpd=rpd, tpd=tpd, paid=paid,
    )


def _catalogue(*specs, enabled=True, allow_paid=False, min_gain=8, max_per_run=500):
    return ProviderCatalogue(
        router=RouterSettings(enabled=enabled, allow_paid=allow_paid,
                              upgrade_min_gain=min_gain,
                              upgrade_max_per_run=max_per_run),
        providers=tuple(specs),
    )


# ── catalogue ────────────────────────────────────────────────────────────────

def test_shipped_catalogue_parses():
    cat = load_catalogue()
    assert cat.router.enabled is True
    names = [p.name for p in cat.providers]
    for expected in ("groq", "cerebras", "gemini", "mistral", "openrouter",
                     "github", "deepseek"):
        assert expected in names
    # The two paid providers stay in the catalogue so the decision about them
    # is visible, but nothing may reach them: OFF since 2026-09-05 at the
    # operator's instruction that no path spend money on a model.
    assert [p.name for p in cat.providers if p.paid] == ["deepseek", "openrouter_paid"]
    assert cat.router.allow_paid is False
    assert cat.router.daily_budget_usd == 0.0
    # Belt and braces, and each one is load-bearing on its own: `paid_allowed()`
    # needs the permission *and* the amount, and `configured` needs `enabled`.
    # The OpenRouter account holds real money that was bought to unlock the free
    # tier rather than to spend, and openrouter_paid shares its key — so it is
    # the one paid provider that could actually succeed in spending.
    assert all(not p.enabled for p in cat.providers if p.paid)


def test_models_are_sorted_best_first_on_load():
    # The catalogue reorders on parse, so a provider's own listing order in the
    # YAML never has to be trusted.
    from scraper.providers import _parse_models
    models = _parse_models([
        {"model": "c", "quality": 30},
        {"model": "a", "quality": 70},
        {"model": "b", "quality": 50},
    ])
    assert [m.model for m in models] == ["a", "b", "c"]


def test_malformed_model_entries_are_dropped_not_fatal():
    from scraper.providers import _parse_models
    models = _parse_models([
        {"quality": 90},                      # no model name
        "not-a-dict",
        {"model": "ok", "quality": "high"},   # unparseable score → 0
        {"model": "clamped", "quality": 999},
    ])
    assert [(m.model, m.quality) for m in models] == [("clamped", 100), ("ok", 0)]


def test_provider_without_key_is_not_configured(monkeypatch):
    spec = _spec(env="ROUTER_TEST_ABSENT")
    monkeypatch.delenv("ROUTER_TEST_ABSENT", raising=False)
    assert spec.configured is False
    monkeypatch.setenv("ROUTER_TEST_ABSENT", "sk-x")
    assert spec.configured is True


def test_paid_providers_excluded_unless_allowed(monkeypatch):
    monkeypatch.setenv("FREE_KEY", "k")
    monkeypatch.setenv("PAID_KEY", "k")
    cat = _catalogue(_spec("free", env="FREE_KEY"),
                     _spec("paid", paid=True, env="PAID_KEY"))
    assert [p.name for p in cat.usable()] == ["free"]
    assert [p.name for p in cat.usable(allow_paid=True)] == ["free", "paid"]


def test_missing_config_file_disables_router_instead_of_raising(tmp_path):
    cat = load_catalogue(tmp_path)  # no providers.yaml here
    assert cat.router.enabled is False
    assert cat.providers == ()


def test_every_extractor_shares_one_fingerprint_model(monkeypatch):
    # The invariant that protects ~74K cached extractions: routing must never
    # change the cache key.
    monkeypatch.setenv("FREE_KEY", "k")
    cat = _catalogue(_spec("free", env="FREE_KEY", quality=(60, 40)))
    fleet = build_extractors(cat, fingerprint_model="deepseek-chat")
    assert len(fleet) == 2
    assert {e.fingerprint_model for e in fleet} == {"deepseek-chat"}
    assert {e.model_fingerprint for e in fleet} == {fleet[0].model_fingerprint}


# ── quota ledger ─────────────────────────────────────────────────────────────

def test_ledger_counts_failures_against_budget(tmp_path):
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=100)
    ledger.note_call("groq", ok=True)
    ledger.note_call("groq", ok=False, error="boom")
    # A rejected request still consumed a slot — undercounting is how a router
    # walks into a hard block.
    assert ledger.used("groq") == 2
    assert ledger.remaining(spec) == int(100 * 0.95) - 2


def test_ledger_survives_restart(tmp_path):
    db = _db(tmp_path)
    QuotaLedger(db, day="2026-08-16").note_call("groq")
    fresh = QuotaLedger(db, day="2026-08-16")
    assert fresh.used("groq") == 1


def test_token_per_minute_429_does_not_collapse_the_daily_budget(tmp_path):
    # Groq and OpenRouter return multi-minute Retry-After for TOKEN-per-minute
    # limits. At a 2-minute threshold, one of those at call 200 would pin the
    # ceiling to 200 and end the provider's day.
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=10_000)
    for _ in range(200):
        ledger.note_call("groq")
    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=300, spec=spec)
    assert ledger.budget(spec) == int(10_000 * 0.95)   # untouched


def test_minute_limit_429_does_not_collapse_the_daily_budget(tmp_path):
    # Free tiers publish rpm (10-30) alongside rpd (150-14400) and 429 on the
    # per-minute limit constantly. Recording the day's call count on one of
    # those would take Gemini from 1500/day to 15 after a single minute-limit
    # hit — the provider would be "spent" before it had really started.
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=10_000)
    for _ in range(5):
        ledger.note_call("groq")
    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=20, spec=spec)
    assert ledger.budget(spec) == int(10_000 * 0.95)   # untouched
    assert ledger.blocked("groq") is True              # but backed off


def test_long_retry_after_is_read_as_a_daily_ceiling(tmp_path):
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=10_000)
    for _ in range(5):
        ledger.note_call("groq")
    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=3600, spec=spec)
    # Published 10,000; refused for an hour at call 6. The observation wins.
    assert ledger.budget(spec) == int(6 * 0.95)
    # A later, higher observation must not restore optimism.
    record_provider_call(db, "2026-08-16", "groq", rate_limited=True, observed_limit=900)
    assert get_provider_usage(db, "2026-08-16")["groq"]["observed_limit"] == 6


def test_429_near_the_daily_limit_lowers_the_ceiling_even_when_brief(tmp_path):
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=10)  # budget = 9
    for _ in range(8):
        ledger.note_call("groq")
    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=5, spec=spec)
    assert ledger.budget(spec) == int(9 * 0.95)


def test_rpm_paces_calls_per_provider(tmp_path):
    # Pacing must be per provider, not per extractor: a provider with two
    # models gets two extractors, each otherwise free-running at 60/min.
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = ProviderSpec(name="slow", base_url="u", api_key_env="K",
                        models=(ModelSpec(model="m", quality=50),), rpm=1, rpd=1000)
    assert ledger.available(spec) is True
    ledger.note_call("slow")
    assert ledger.paced(spec) is False      # 60s between calls at rpm=1
    assert ledger.available(spec) is False


def test_ledger_rolls_onto_a_new_utc_day(tmp_path, monkeypatch):
    # The ai_only window runs 16:35 -> 00:20 UTC. Without the roll, the run
    # keeps spending yesterday's row after midnight.
    import scraper.router as router_mod
    db = _db(tmp_path)
    ledger = QuotaLedger(db)            # follows the clock (no fixed day)
    monkeypatch.setattr(router_mod, "utc_day", lambda *a, **k: ledger.day)
    ledger.note_call("groq")
    assert ledger.used("groq") == 1
    monkeypatch.setattr(router_mod, "utc_day", lambda *a, **k: "2099-01-01")
    ledger.note_call("groq")
    assert ledger.day == "2099-01-01"
    assert ledger.used("groq") == 1      # fresh day, fresh allowance


def test_ledger_picks_up_a_concurrent_job_spend(tmp_path):
    # The enrichment job runs alongside ai_only in the same window with its own
    # ledger; without a periodic re-read they together burn ~2x the budget
    # before either notices. The DB counters are atomic, so re-reading suffices.
    db = _db(tmp_path)
    mine = QuotaLedger(db, day="2026-08-16")
    for _ in range(200):
        record_provider_call(db, "2026-08-16", "groq")   # the other process
    # Own calls up to the reload interval; the last one triggers the refresh.
    for _ in range(QuotaLedger._RELOAD_EVERY + 1):
        mine.note_call("groq")
    assert mine.used("groq") > 200


def test_exhausted_provider_is_unavailable(tmp_path):
    db = _db(tmp_path)
    ledger = QuotaLedger(db, day="2026-08-16")
    spec = _spec(rpd=3)  # budget = int(3*0.95) = 2
    assert ledger.available(spec) is True
    ledger.note_call("groq")
    ledger.note_call("groq")
    assert ledger.remaining(spec) == 0
    assert ledger.available(spec) is False


def test_ledger_without_db_still_works():
    # Admin one-offs may run before app_state.db_path is set.
    ledger = QuotaLedger(None)
    ledger.note_call("groq")
    assert ledger.used("groq") == 1


# ── routing order ────────────────────────────────────────────────────────────

def _router(tmp_path, *specs, **kw):
    db = _db(tmp_path)
    cat = _catalogue(*specs, **kw)
    ledger = QuotaLedger(db, day="2026-08-16")
    fleet = build_extractors(cat, fingerprint_model="fp", allow_paid=True)
    return ModelRouter(cat, ledger, fleet), ledger


def test_order_is_best_quality_first(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, _ = _router(tmp_path,
                        _spec("a", env="A_KEY", quality=(50,)),
                        _spec("b", env="B_KEY", quality=(70,)))
    assert [e.quality for e in router.order()] == [70, 50]
    assert router.best_available_quality() == 70


def test_spent_provider_drops_out_of_the_order(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, ledger = _router(tmp_path,
                             _spec("a", env="A_KEY", quality=(70,), rpd=3),
                             _spec("b", env="B_KEY", quality=(50,), rpd=1000))
    ledger.note_call("a")
    ledger.note_call("a")
    assert [e.provider for e in router.order()] == ["b"]
    assert router.best_available_quality() == 50


def test_quality_ties_break_on_remaining_quota(tmp_path, monkeypatch):
    # Published scores carry more uncertainty than the gaps between them;
    # remaining quota is measured, not estimated.
    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, ledger = _router(tmp_path,
                             _spec("a", env="A_KEY", quality=(60,), rpd=100),
                             _spec("b", env="B_KEY", quality=(60,), rpd=5000))
    assert [e.provider for e in router.order()] == ["b", "a"]


def test_disabled_router_yields_empty_fleet(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "k")
    db = _db(tmp_path)
    cat = _catalogue(_spec("a", env="A_KEY"), enabled=False)
    router = build_router(db, catalogue=cat)
    assert router.enabled is False
    assert router.order() == []


# ── upgrade policy ───────────────────────────────────────────────────────────

def test_upgrade_threshold_leaves_close_calls_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("A_KEY", "k")
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)), min_gain=8)
    # Best available is 60, so anything at 52+ is not worth re-spending on.
    assert router.upgrade_threshold() == 52


def test_upgradable_pages_are_worst_first_and_fingerprint_scoped(tmp_path):
    db = _db(tmp_path)
    for h, q, fp in (("a", 30, "fp1"), ("b", 10, "fp1"),
                     ("c", 55, "fp1"), ("d", 5, "OTHER")):
        update_cache_page(db, h, {
            "url": f"https://x/{h}", "extracted_at": "2026-08-01T00:00:00+00:00",
            "extract_fingerprint": fp, "extract_quality": q,
        }, create={"url": f"https://x/{h}"})
    rows = get_upgradable_pages(db, min_quality=52, limit=10, fingerprint="fp1")
    # 'c' scores above the bar; 'd' sits at a different fingerprint and is
    # already scheduled for ordinary re-extraction.
    assert [r["url_hash"] for r in rows] == ["b", "a"]


def test_never_extracted_page_is_not_an_upgrade_candidate(tmp_path):
    db = _db(tmp_path)
    update_cache_page(db, "fresh", {
        "url": "https://x/fresh", "extract_fingerprint": "fp1",
    }, create={"url": "https://x/fresh"})
    assert get_upgradable_pages(db, 52, 10, "fp1") == []


def test_pre_router_pages_are_never_upgrade_candidates(tmp_path):
    # NULL extract_quality means "extracted by the paid incumbent", which scores
    # ABOVE every free model. Ranking those worst-first would overwrite good
    # DeepSeek output with weaker free output — a downgrade wearing an
    # upgrade's name. ~74K existing rows are in exactly this state.
    db = _db(tmp_path)
    update_cache_page(db, "old", {
        "url": "https://x/old", "extracted_at": "2026-01-01T00:00:00+00:00",
        "extract_fingerprint": "fp1",
    }, create={"url": "https://x/old"})
    assert get_upgradable_pages(db, 52, 10, "fp1") == []


@pytest.mark.asyncio
async def test_upgrade_pass_respects_the_topic_tier_freeze(tmp_path, monkeypatch):
    # core-tier cities run only core_topics; re-extracting a frozen pair spends
    # quota on work the pipeline deliberately does not do.
    from scraper.pipeline import CityConfig, TopicConfig, _run_quality_upgrade

    monkeypatch.setenv("A_KEY", "k")
    db = _db(tmp_path)
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)))
    for h, topic in (("core", "running"), ("frozen", "chess")):
        update_cache_page(db, h, {
            "url": f"https://x/{h}", "city": "Kistelepules", "topic": topic,
            "extracted_at": "2026-08-01T00:00:00+00:00",
            "extract_fingerprint": "fp1", "extract_quality": 10,
            "raw_text": "A helyi futóklub minden kedden edz.",
        }, create={"url": f"https://x/{h}"})

    calls = []

    class _Extractor:
        router = None
        last_model = "a-m0"
        last_quality = 60
        canonical_fingerprint = "fp1"

        async def extract(self, **kw):
            calls.append(kw["topic"])
            return []

    ex = _Extractor()
    ex.router = router

    class _Cache:
        def save_extracted(self, *a, **k): pass

    class _Cfg:
        db_path = db
        core_topics = ["running"]

    cities = [CityConfig(name="Kistelepules", country="Hungary", locale="hu",
                         search_variants=[], topic_tier="core")]
    topics = [TopicConfig(name="running", search_terms={}),
              TopicConfig(name="chess", search_terms={})]
    await _run_quality_upgrade(cities, topics, _Cfg(), ex, _Cache(), "fp1")
    assert calls == ["running"]  # 'chess' is tiered out and must be skipped


@pytest.mark.asyncio
async def test_upgrade_pass_uses_the_city_locale_not_a_hardcoded_one(tmp_path, monkeypatch):
    # cache_pages rows carry no locale; the old fallback stamped "hu" onto every
    # record, relabelling German communities as Hungarian in the database.
    from scraper.pipeline import CityConfig, TopicConfig, _run_quality_upgrade

    monkeypatch.setenv("A_KEY", "k")
    db = _db(tmp_path)
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)))
    update_cache_page(db, "de", {
        "url": "https://x/de", "city": "Berlin", "topic": "running",
        "extracted_at": "2026-08-01T00:00:00+00:00",
        "extract_fingerprint": "fp1", "extract_quality": 10,
        "raw_text": "Der Laufclub trifft sich jeden Dienstag.",
    }, create={"url": "https://x/de"})

    seen = {}

    class _Extractor:
        router = None
        last_model = "a-m0"
        last_quality = 60
        canonical_fingerprint = "fp1"

        async def extract(self, **kw):
            seen.update(kw)
            return []

    ex = _Extractor()
    ex.router = router

    class _Cache:
        def save_extracted(self, *a, **k): pass

    class _Cfg:
        db_path = db
        core_topics = []

    await _run_quality_upgrade(
        [CityConfig(name="Berlin", country="Germany", locale="de", search_variants=[])],
        [TopicConfig(name="running", search_terms={})],
        _Cfg(), ex, _Cache(), "fp1")
    assert seen["locale"] == "de"


# ── admin page ───────────────────────────────────────────────────────────────

def test_providers_admin_page_lists_the_fleet(tmp_path):
    import base64
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from scraper.web import app as web_app
    from scraper.web.state import app_state

    db = _db(tmp_path)
    old = app_state.db_path
    app_state.db_path = db
    # _ADMIN_PASSWORD is read at import time, so patch the module attribute
    # rather than the env var.
    with patch("scraper.web.app._ADMIN_PASSWORD", "t"):
        auth = {"Authorization": "Basic " + base64.b64encode(b"admin:t").decode()}
        resp = TestClient(web_app.app).get("/admin/providers", headers=auth)
        assert resp.status_code == 200
        for provider in ("groq", "cerebras", "gemini", "mistral", "openrouter",
                         "github", "deepseek"):
            assert provider in resp.text
        # A provider with no key must be shown, not hidden — the env var name is
        # the actionable part of the page.
        assert "GROQ_API_KEY" in resp.text
        # "Free only" again since 2026-09-05: paid providers are switched off,
        # so the page must say so. This assertion is the inverse of the one it
        # replaced, and it is here for the same reason — the page has to report
        # the policy that is actually in force, in whichever direction.
        assert "Free only" in resp.text
    app_state.db_path = old


# ── regressions from review round 2 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_rpm_pacing_waits_instead_of_failing(tmp_path, monkeypatch):
    """Pacing must never look like unavailability.

    `_call` only sleeps on `_blocked_until`, which pacing does not set. When the
    single configured provider was merely paced, the loop tried nothing, raised,
    and `_note_failure` counted it — 24 "failures" a minute at the shipped
    rpm: 30, so the breaker opened and aborted the run within seconds. This is
    the expected state during rollout, when only one or two keys are set.
    """
    from scraper.extract import FallbackExtractor

    monkeypatch.setenv("A_KEY", "k")
    # rpm 600 → a 0.1s cooldown, so the test exercises the real sleep path
    # without being slow.
    spec = ProviderSpec(name="a", base_url="u", api_key_env="A_KEY",
                        models=(ModelSpec(model="a-m0", quality=60),),
                        rpm=600, rpd=1000)
    router, _ = _router(tmp_path, spec)
    calls = []

    class _Model:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            calls.append(1)
            return []

    chain = FallbackExtractor(primaries=[_Model()], router=router)
    for i in range(3):
        await chain.extract(text="t", city="c", topic="running", locale="hu",
                            source_url=f"https://x/{i}")
    assert len(calls) == 3                      # all served, none dropped
    assert chain._consecutive_failures == 0     # nothing counted as a failure
    assert chain.providers_down is False        # breaker never opened


@pytest.mark.asyncio
async def test_spent_quota_raises_the_type_callers_catch(tmp_path, monkeypatch):
    """A spent daily budget must surface as ExtractorUnavailableError.

    ExtractorQuotaError is not a subclass of it, so raising that sailed past
    every `except ExtractorUnavailableError` in the pipeline and failed the run
    — the exact outcome the clean-stop path exists to prevent.
    """
    from scraper.extract import ExtractorUnavailableError, FallbackExtractor

    monkeypatch.setenv("A_KEY", "k")
    router, ledger = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,), rpd=2))
    for _ in range(2):
        ledger.note_call("a")
    assert router.has_capacity() is False

    class _Model:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            raise AssertionError("must not reach the provider")

    chain = FallbackExtractor(primaries=[_Model()], router=router)
    with pytest.raises(ExtractorUnavailableError):
        await chain.extract(text="t", city="c", topic="running", locale="hu",
                            source_url="https://x/1")
    # …and it is flagged as quota, not as an outage, so the caller stops cleanly.
    assert chain.quota_exhausted is True
    assert chain.providers_down is False


def test_chain_is_built_from_the_whole_fleet_not_todays_survivors(tmp_path, monkeypatch):
    """A provider spent at 16:35 must be able to return after the UTC rollover.

    The chain is built once for a run that spans the off-peak window and crosses
    midnight; filtering by live quota at build time would exclude a provider for
    the whole run, and the ledger's day-rollover could never readmit it.
    """
    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, ledger = _router(tmp_path,
                             _spec("a", env="A_KEY", quality=(70,), rpd=2),
                             _spec("b", env="B_KEY", quality=(50,), rpd=1000))
    for _ in range(2):
        ledger.note_call("a")
    assert [e.provider for e in router.order()] == ["b"]          # spent now
    assert [e.provider for e in router.all_extractors()] == ["a", "b"]


# ── regressions from review round 3 ──────────────────────────────────────────

def test_capacity_checks_ignore_momentary_rpm_pacing(tmp_path, monkeypatch):
    """`order()` answers "callable this instant"; planning needs "callable today".

    Preflight stamps every provider's clock, so an order()-based capacity check
    read "no free capacity left today" for the next 60/rpm seconds — which made
    the upgrade sweep skip entirely and the gateway answer a spurious 429.
    """
    monkeypatch.setenv("A_KEY", "k")
    router, ledger = _router(tmp_path, _spec("a", env="A_KEY", quality=(62,)))
    assert router.best_available_quality() == 62
    ledger.note_call("a")                       # now inside the rpm window
    assert router.order() == []                 # nothing callable this instant
    assert router.best_available_quality() == 62  # …but the day is not spent
    assert router.has_capacity() is True
    assert [e.quality for e in router.with_budget()] == [62]


def test_capacity_can_be_scoped_to_one_requested_model(tmp_path, monkeypatch):
    # The gateway serves a single pinned model; unrelated providers' spare
    # capacity must not make an exhausted request look servable.
    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, ledger = _router(tmp_path,
                             _spec("a", env="A_KEY", quality=(70,), rpd=2),
                             _spec("b", env="B_KEY", quality=(50,), rpd=1000))
    for _ in range(2):
        ledger.note_call("a")
    pinned = [e for e in router.all_extractors() if e.provider == "a"]
    assert router.has_capacity() is True            # 'b' still has budget
    assert router.has_capacity(pinned) is False     # but 'a' does not


def test_upgrade_candidates_are_city_scoped_in_sql(tmp_path):
    """The city filter must run before LIMIT.

    The caller sweeps one country group at a time. Filtering after a global
    LIMIT can return nothing while thousands of eligible pages sit lower in the
    ordering.
    """
    db = _db(tmp_path)
    for h, city, q in (("de1", "Berlin", 1), ("de2", "Berlin", 2),
                       ("hu1", "Szentendre", 30)):
        update_cache_page(db, h, {
            "url": f"https://x/{h}", "city": city, "topic": "running",
            "extracted_at": "2026-08-01T00:00:00+00:00",
            "extract_fingerprint": "fp1", "extract_quality": q,
        }, create={"url": f"https://x/{h}"})
    # Worst-first globally would return the two Berlin rows and drop Szentendre.
    rows = get_upgradable_pages(db, 52, 2, "fp1", cities=["Szentendre"])
    assert [r["url_hash"] for r in rows] == ["hu1"]
    assert get_upgradable_pages(db, 52, 2, "fp1", cities=[]) == []


def test_upgrade_pair_log_renders_in_run_detail(tmp_path):
    """The sweep's pair log must carry every key run_detail.html touches.

    _new_pair_log's own docstring says so: the template compares
    `p.records_extracted > 0` under strict Jinja Undefined, and a hand-rolled
    partial dict raises UndefinedError — 500ing the whole admin page for any run
    that included a sweep.
    """
    from scraper.pipeline import _new_pair_log

    canonical = set(_new_pair_log("c", "t", []))
    entry = _new_pair_log("—", "quality_upgrade", [])
    entry.update({"urls_found": 3, "records_extracted": 2,
                  "extract_failed": 1, "cache_hits_extract": 2})
    assert canonical <= set(entry)
    for key in ("records_extracted", "queries", "cache_hits_scrape",
                "fetched_urls", "search_failed", "aborted"):
        assert key in entry


@pytest.mark.asyncio
async def test_retired_model_is_dropped_for_the_run_not_retried(monkeypatch, tmp_path):
    """HTTP 404/410 means the model is gone — retrying costs one request a page.

    Live rollout on 2026-08-16: Groq's qwen3-32b, Gemini 2.5-flash, three
    OpenRouter ":free" slugs and all of GitHub Models (410 retirement brownout)
    404'd on *every* enrichment record, because the generic >=400 branch raised
    the transient error type.
    """
    from scraper.extract import (ExtractorModelError, FallbackExtractor)

    monkeypatch.setenv("A_KEY", "k")
    # rpm 600 -> a 0.1s cooldown: both models sit on one provider clock, so the
    # chain must be able to wait it out rather than give up.
    spec = ProviderSpec(name="a", base_url="u", api_key_env="A_KEY",
                        models=(ModelSpec(model="a-m0", quality=60),
                                ModelSpec(model="a-m1", quality=40)),
                        rpm=600, rpd=1000)
    router, _ = _router(tmp_path, spec)
    calls = []

    class _Dead:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            calls.append("dead")
            raise ExtractorModelError("a:a-m0 HTTP 404")

    class _Live:
        provider, model, quality = "a", "a-m1", 40

        async def extract(self, *a, **kw):
            calls.append("live")
            return []

    chain = FallbackExtractor(primaries=[_Dead(), _Live()], router=router)
    for i in range(3):
        await chain.extract(text="t", city="c", topic="running", locale="hu",
                            source_url=f"https://x/{i}")
    # Probed once, then never again; the live model served the rest.
    assert calls.count("dead") == 1
    assert calls.count("live") == 3
    # A stale catalogue entry is a config problem, not a provider outage.
    assert chain._consecutive_failures == 0
    assert chain.providers_down is False


# ── scoring safety ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unmeasured_model_scores_null_not_zero():
    """A rate limit is not a quality signal.

    The first live run had two of three Groq models return 0 — one was rate
    limited for 1197s, the other 400'd. Written into providers.yaml those zeros
    would have buried working models at the bottom of the routing order.
    """
    from scraper.extract import ExtractorRateLimitError
    from scraper.scoring import score_model

    pages = [{"url": "u", "city": "c", "topic": "running",
              "text": "t", "expected": ["Szentendrei Futóklub"]}]

    class _Limited:
        provider, model, quality = "groq", "m", 62

        async def extract(self, **kw):
            raise ExtractorRateLimitError(1197)

    r = await score_model(_Limited(), pages)
    assert r["score"] is None
    assert r["measured"] is False
    assert r["failed"] == 1


@pytest.mark.asyncio
async def test_score_is_averaged_over_answered_pages_only():
    # Otherwise reliability folds into quality, and at free-tier rate limits
    # that mostly measures how recently the fleet ran.
    from scraper.extract import ExtractorUnavailableError
    from scraper.models import CommunityRecord
    from scraper.scoring import score_model

    # Raw names, not identity keys — score_page's contract since the MINEA
    # matcher landed; the key form has no spaces and cannot be tokenised.
    name = "Szentendrei Futóklub"
    pages = [{"url": f"u{i}", "city": "c", "topic": "running", "text": "t",
              "expected": [name]} for i in range(3)]
    calls = {"n": 0}

    class _Flaky:
        provider, model, quality = "groq", "m", 50

        async def extract(self, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise ExtractorUnavailableError("HTTP 400")
            return [CommunityRecord(name=name, topic="running", city="c",
                                    locale="hu", source_url="https://a.test",
                                    extracted_at="2026-01-01T00:00:00+00:00")]

    r = await score_model(_Flaky(), pages)
    assert r["score"] == 100          # perfect on the one page it answered
    assert r["answered"] == 1 and r["failed"] == 2
    assert r["coverage"] == 0.33      # …but coverage says how thin that is


# ── scoring: matching tolerance (MINEA, arXiv:2404.04068) ────────────────────

def test_matching_tolerates_phrasing_but_not_different_clubs():
    """Exact-key matching alone measures phrasing, not extraction.

    MINEA scored the same extractions at 59.4% on exact name match and 88.4%
    once containment was allowed. Our identity key also strips spaces
    ("Szentendrei Futóklub" -> "szentendreifutoklub"), so token overlap is
    impossible on it — the scorer normalises separately.
    """
    from scraper.scoring import _matches

    # Same club, different phrasing → must match.
    assert _matches("Szentendrei Futóklub", ["Szentendrei Futóklub"])
    assert _matches("Szentendrei Futóklub", ["Szentendrei Futóklub Egyesület"])
    assert _matches("Szentendrei Futóklub Egyesület", ["Szentendrei Futóklub"])
    assert _matches("Futóklub Szentendre", ["Szentendre Futóklub"])
    assert _matches("Szentendrei Futoklub", ["Szentendrei Futóklub"])

    # Different clubs → must NOT match, or the score is meaningless.
    assert not _matches("Szentendrei Futóklub", ["Budapesti Sakk Kör"])
    assert not _matches("Pécsi Sakk Kör", ["Győri Sakk Kör"])
    # Filler words alone identify nothing.
    assert not _matches("Szentendrei Futóklub", ["Egyesület"])
    assert not _matches("Szentendrei Futóklub", ["Sport Klub"])


def test_score_page_is_symmetric_in_tolerance():
    """Precision must forgive what recall forgives.

    Otherwise a model is rewarded for finding the club and penalised for
    naming it slightly differently — the same difference counted twice.
    """
    from scraper.scoring import score_page

    expected = ["Alfa Klub", "Béta Kör"]
    assert score_page(expected, expected) == 100
    assert score_page(expected, ["Alfa Klub Egyesület", "Béta Kör"]) == 100
    assert score_page(expected, ["Alfa Klub"]) == 75          # half the recall
    assert score_page(expected, expected + ["Zaj Kft"]) == 90  # invented one
    assert score_page(expected, []) == 20                      # answered, found none


def test_golden_set_is_stable_across_runs(tmp_path):
    """A moving sample makes scores incomparable, and silently so.

    The set was ordered by extracted_at, which the pipeline rewrites
    continuously — so two measurements an hour apart ran on different pages and
    the difference read as a change in model quality (2026-08-16:
    mistral-small appeared to drop 80 -> 55 for this reason alone).
    """
    from scraper.db import init_db, update_cache_page
    from scraper.scoring import golden_set

    db = tmp_path / "s.db"
    init_db(db)
    for i in range(6):
        update_cache_page(db, f"h{i}", {
            "url": f"https://x/{i}", "city": "Budapest", "topic": "running",
            "extracted_at": f"2026-08-0{i + 1}T00:00:00+00:00",
            "raw_text": "A helyi futóklub keddenként edz.",
            "records": [{"name": f"Klub {i}"}],
        }, create={"url": f"https://x/{i}"})

    first = [p["url"] for p in golden_set(db, limit=3)]

    # Simulate the pipeline re-extracting a page: extracted_at moves, the
    # corpus does not.
    update_cache_page(db, "h0", {"extracted_at": "2026-12-31T23:59:59+00:00"})

    assert [p["url"] for p in golden_set(db, limit=3)] == first


def _scoring_context():
    """Corpus-derived generic tokens and place stems, as production builds them."""
    from scraper.scoring import _generic_tokens

    towns = ["Szentendrei", "Pécsi", "Győri", "Szegedi", "Debreceni",
             "Miskolci", "Egri", "Váci", "Bajai", "Tatai"]
    types = ["Futóklub", "Sakk Kör", "Kajak Klub", "Kórus", "Tánccsoport"]
    corpus = [f"{t} {c}" for t in towns for c in types] + ["MTK Budapest", "Vasas SC"] * 4
    places = frozenset({"szentendre", "pecs", "gyor", "szeged", "debrecen",
                        "miskolc", "eger", "vac", "baja", "tata", "budapest",
                        "musterstadt", "norrkoping", "beispielstadt"})
    return _generic_tokens(corpus), places


def test_matcher_accepts_phrasing_differences():
    """Strict matching understates every model — MINEA: 59.4% vs 88.4%."""
    from scraper.scoring import _matches

    g, p = _scoring_context()
    assert _matches("Szentendrei Futóklub", ["Szentendrei Futóklub Egyesület"], g, p)
    assert _matches("Futóklub Szentendre", ["Szentendre Futóklub"], g, p)
    assert _matches("Szentendrei Futoklub", ["Szentendrei Futóklub"], g, p)
    # Abbreviation spelled out — the dominant German and Swedish shape.
    assert _matches("SV Musterstadt", ["Sportverein Musterstadt"], g, p)
    assert _matches("IF Norrköping", ["Idrottsförening Norrköping"], g, p)
    assert _matches("MTK", ["MTK Budapest"], g, p)


def test_matcher_refuses_different_clubs():
    """Every case here scored as a match at some point, and each one lets a
    fabricated answer earn points that --apply writes into the routing order."""
    from scraper.scoring import _matches

    g, p = _scoring_context()
    # Two clubs in one town. Golden pages are single city×topic pages, so
    # every expected name shares a town — collapsing names to their town made
    # any plausible "<Town> <word>" answer score near 100.
    assert not _matches("Szentendrei Futóklub", ["Szentendrei Kajak Klub"], g, p)
    assert not _matches("Pécsi Sakk Kör", ["Pécsi Tánccsoport"], g, p)
    # A bare town, or a bare club type, identifies nothing.
    assert not _matches("Szentendrei Futóklub", ["Szentendrei"], g, p)
    assert not _matches("Szentendrei Futóklub", ["Futóklub"], g, p)
    assert not _matches("Schachverein Musterstadt", ["Schachverein"], g, p)
    # Same name, different town — the commonest German shape.
    assert not _matches("SV Grün-Weiß Musterstadt", ["SV Grün-Weiß Beispielstadt"], g, p)
    assert not _matches("Pécsi Sakk Kör", ["Győri Sakk Kör"], g, p)


def test_degenerate_answers_score_the_floor():
    """20 is the "answered at all" score: no credit for a plausible guess."""
    from scraper.scoring import score_page

    g, p = _scoring_context()
    assert score_page(["Szentendrei Futóklub"], ["Szentendrei Kajak Klub"], g, p) == 20
    assert score_page(["Szentendrei Futóklub", "Szentendrei Kórus"],
                      ["Szentendrei"], g, p) == 20
    assert score_page(["Szentendrei Futóklub", "Pécsi Futóklub"],
                      ["Futóklub"], g, p) == 20
    assert score_page(["Szentendrei Futóklub", "Pécsi Sakk Kör"],
                      ["Szentendrei Futóklub", "Pécsi Sakk Kör"], g, p) == 100


def test_scoring_is_one_to_one_and_order_independent():
    """Greedy pairing consumed a candidate a later item needed, so the same
    answer set scored differently depending on the order the model listed
    clubs — reintroducing the variance the deterministic sample removed."""
    from scraper.scoring import score_page

    g, p = _scoring_context()
    expected = ["Alfa Béta Klub", "Alfa Béta Gamma Klub"]
    got = ["Alfa Béta Gamma Klub", "Alfa Béta Delta Klub"]
    assert score_page(expected, got, g, p) == score_page(expected, list(reversed(got)), g, p)

    # Duplicates must not cap recall on either side: a correct distinct answer
    # scored below a model that repeated itself, and two spellings of one club
    # inflated the model's own precision denominator.
    dupes = ["Szentendrei Futóklub", "Szentendrei Futóklub", "Pécsi Sakk Kör"]
    assert score_page(dupes, ["Szentendrei Futóklub", "Pécsi Sakk Kör"], g, p) == 100
    assert score_page(["Szentendrei Futóklub"],
                      ["Szentendrei Futóklub", "Szentendrei Futoklub"], g, p) == 100


@pytest.mark.asyncio
async def test_rate_limits_do_not_open_the_circuit_breaker(tmp_path, monkeypatch):
    """A 429 is the API asking us to slow down, not a provider outage.

    The 2026-08-17 overnight run aborted after 45 minutes with
    "no extraction provider configured (20 consecutive failures)" while 13,523
    Groq calls were still available: every provider happened to be inside a
    back-off window, the chain could not find one to call, and counted each
    miss as a failure until the breaker opened.
    """
    from scraper.extract import (ExtractorRateLimitError,
                                 ExtractorUnavailableError, FallbackExtractor)

    monkeypatch.setenv("A_KEY", "k")
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)))

    class _Limited:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            # Longer than the chain is willing to wait, as Groq's 1197s was.
            raise ExtractorRateLimitError(1200)

    chain = FallbackExtractor(primaries=[_Limited()], router=router)
    for i in range(25):
        with pytest.raises(ExtractorUnavailableError):
            await chain.extract(text="t", city="c", topic="running", locale="hu",
                                source_url=f"https://x/{i}")

    assert chain.rate_limited_out is True
    # Well past the 20-failure threshold, and still not treated as an outage.
    assert chain.providers_down is False
    assert chain.failure_reason is None


def test_throughput_reports_where_the_window_went():
    """The number that decides whether concurrency is worth it.

    A serial chain achieving far fewer calls/min than the fleet's combined rpm
    ceiling is idling on latency; wait_s approaching call_s means pacing binds
    instead. Overnight on 2026-08-17 there was no measurement to tell them
    apart.
    """
    from scraper.extract import FallbackExtractor

    ex = FallbackExtractor(primaries=[])
    assert ex.throughput() == {"calls": 0, "call_s": 0.0, "wait_s": 0.0,
                               "avg_call_s": 0.0, "calls_per_min": 0.0}

    ex.calls_made, ex.call_seconds, ex.wait_seconds = 10, 40.0, 20.0
    t = ex.throughput()
    assert t["avg_call_s"] == 4.0
    assert t["calls_per_min"] == 10.0  # 10 calls over 60s of busy time


@pytest.mark.asyncio
async def test_a_dead_fleet_still_opens_the_breaker(tmp_path, monkeypatch):
    """Rate limits are exempt from the breaker; genuine errors must not be.

    The exemption nearly swallowed the breaker whole: the quota ledger stamps a
    provider's rpm clock on *every* attempt, failures included, so a fleet
    answering 500s ends the call looking exactly like a fleet in cooldown. Only
    a real error seen during the call may open it — and it must still open.
    """
    from scraper.extract import (ExtractorUnavailableError, FallbackExtractor)

    monkeypatch.setenv("A_KEY", "k")
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)))

    class _Broken:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            raise ExtractorUnavailableError("500 upstream exploded")

    chain = FallbackExtractor(primaries=[_Broken()], router=router)
    for i in range(25):
        with pytest.raises(ExtractorUnavailableError):
            await chain.extract(text="t", city="c", topic="running", locale="hu",
                                source_url=f"https://x/{i}")

    assert chain.providers_down is True
    assert chain.rate_limited_out is False


@pytest.mark.asyncio
async def test_rate_limited_out_clears_on_the_next_call(tmp_path, monkeypatch):
    """The flag describes one attempt, not the rest of the run.

    Latched, it would both stop extraction for the whole window after a single
    unlucky moment and permanently mask a fleet that died afterwards, because
    callers stop before the chain can notice.
    """
    from scraper.extract import (ExtractorRateLimitError,
                                 ExtractorUnavailableError, FallbackExtractor)

    monkeypatch.setenv("A_KEY", "k")
    router, _ = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,)))

    class _Flaky:
        provider, model, quality = "a", "a-m0", 60
        limited = True

        async def extract(self, *a, **kw):
            if _Flaky.limited:
                raise ExtractorRateLimitError(1200)
            return []

    chain = FallbackExtractor(primaries=[_Flaky()], router=router)
    with pytest.raises(ExtractorUnavailableError):
        await chain.extract(text="t", city="c", topic="running", locale="hu",
                            source_url="https://x/1")
    assert chain.rate_limited_out is True

    # Let both back-off clocks expire — the chain's own, and the ledger's.
    _Flaky.limited = False
    chain._blocked_until = [0.0]
    router.ledger._row("a")["blocked_until"] = 0.0
    router.ledger._last_call.pop("a", None)
    assert await chain.extract(text="t", city="c", topic="running", locale="hu",
                               source_url="https://x/2") == []
    assert chain.rate_limited_out is False


@pytest.mark.asyncio
async def test_one_broken_provider_does_not_retire_a_healthy_one(tmp_path, monkeypatch):
    """The breaker is per provider, not per fleet.

    A single endpoint stuck on 500s used to drive one global counter to 20 and
    retire everything with it — including providers that were healthy and simply
    waiting out an rpm window.
    """
    from scraper.extract import ExtractorUnavailableError, FallbackExtractor

    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, _ = _router(tmp_path,
                        _spec("a", env="A_KEY", quality=(60,)),
                        _spec("b", env="B_KEY", quality=(50,)))

    class _Broken:
        provider, model, quality = "a", "a-m0", 60

        async def extract(self, *a, **kw):
            raise ExtractorUnavailableError("500 upstream exploded")

    class _Healthy:
        provider, model, quality = "b", "b-m0", 50
        calls = 0

        async def extract(self, *a, **kw):
            _Healthy.calls += 1
            return []

    chain = FallbackExtractor(primaries=[_Broken(), _Healthy()], router=router)
    for i in range(30):
        assert await chain.extract(text="t", city="c", topic="running", locale="hu",
                                   source_url=f"https://x/{i}") == []

    assert chain.providers_down is False
    assert chain._exhausted[0] is True    # the broken one retired itself
    assert chain._exhausted[1] is False   # the healthy one kept serving
    assert _Healthy.calls == 30


@pytest.mark.asyncio
async def test_provenance_survives_an_interleaved_call(tmp_path, monkeypatch):
    """Which model served a page must not depend on what ran next.

    `last_model` is a single mutable attribute. Reading it after the await was
    correct only while nothing else could call the chain in between — the
    ordering requirement that concurrency removes. `extract_traced` carries the
    answer out with the result instead.
    """
    from scraper.extract import FallbackExtractor

    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, _ = _router(tmp_path,
                        _spec("a", env="A_KEY", quality=(90,)),
                        _spec("b", env="B_KEY", quality=(40,)))

    class _P:
        def __init__(self, provider, model, quality):
            self.provider, self.model, self.quality = provider, model, quality

        async def extract(self, *a, **kw):
            return []

    good, weak = _P("a", "a-m0", 90), _P("b", "b-m0", 40)
    chain = FallbackExtractor(primaries=[good, weak], router=router)

    _records, model, quality = await chain.extract_traced(
        text="t", city="c", topic="running", locale="hu", source_url="https://x/1")
    # Another page goes through the chain before the caller uses the answer.
    chain._exhausted[0] = True
    await chain.extract_traced(text="t", city="c", topic="running", locale="hu",
                               source_url="https://x/2")

    assert (model, quality) == ("a-m0", 90)
    assert chain.last_model == "b-m0"   # the mutable attribute did move on


@pytest.mark.asyncio
async def test_concurrent_pages_fan_out_across_providers(tmp_path, monkeypatch):
    """A call in flight must not leave its provider looking idle.

    The ledger used to record a call only when it returned, so every waiting
    task saw the best provider as available, picked it, and the fleet hit one
    provider's rpm together instead of using the other four.
    """
    import asyncio

    from scraper.extract import FallbackExtractor

    monkeypatch.setenv("A_KEY", "k")
    monkeypatch.setenv("B_KEY", "k")
    router, _ = _router(tmp_path,
                        _spec("a", env="A_KEY", quality=(90,)),
                        _spec("b", env="B_KEY", quality=(40,)))

    class _Slow:
        def __init__(self, provider, model, quality):
            self.provider, self.model, self.quality = provider, model, quality
            self.concurrent = 0
            self.peak = 0

        async def extract(self, *a, **kw):
            self.concurrent += 1
            self.peak = max(self.peak, self.concurrent)
            await asyncio.sleep(0.05)      # long enough to overlap
            self.concurrent -= 1
            return []

    a, b = _Slow("a", "a-m0", 90), _Slow("b", "b-m0", 40)
    chain = FallbackExtractor(primaries=[a, b], router=router)

    await asyncio.gather(*[
        chain.extract_traced(text="t", city="c", topic="running", locale="hu",
                             source_url=f"https://x/{i}")
        for i in range(2)
    ])

    # Both were used: the second page saw the first one's slot already claimed.
    assert a.peak == 1 and b.peak == 1


def test_a_minute_limit_cannot_ratchet_the_daily_ceiling_down(tmp_path):
    """The learned ceiling must not feed the rule that learns it.

    Comparing "are we near the daily limit?" against the *learned* budget makes
    a ratchet: recording an observed limit lowers the budget, which makes the
    next per-minute 429 look near-daily, which lowers it again. On 2026-08-18
    that walked Groq's 13,680/day down to 336 and the fleet lost 85% of its
    free capacity.
    """
    db = tmp_path / "s.db"
    init_db(db)
    ledger = QuotaLedger(db, day="2026-08-18")
    spec = _spec(rpd=13680)

    # Two hundred per-minute 429s, short Retry-After, nowhere near 13,680.
    for _ in range(200):
        ledger.note_call("groq", ok=False, rate_limited=True, retry_after=60, spec=spec)

    assert ledger._row("groq").get("observed_limit") in (None, 0)
    assert ledger.budget(spec) > 12_000, "the daily ceiling was eaten by minute limits"


def test_a_rate_limit_keeps_what_the_provider_said():
    """838 refusals in a day, and no record of which limit was hit.

    Requests-per-minute, tokens-per-minute and requests-per-day are three
    different problems with three different answers, and the router only models
    the first and the third. Discarding the reason made them indistinguishable.
    """
    from scraper.extract import ExtractorRateLimitError

    exc = ExtractorRateLimitError(60.0, "tokens per minute exceeded")
    assert exc.wait_seconds == 60.0
    assert "tokens per minute" in exc.reason
    assert "tokens per minute" in str(exc)

    # Still constructible without one — the preflight path has no body to quote.
    assert ExtractorRateLimitError(30.0).reason == ""


def test_every_request_carries_a_token_cap():
    """max_tokens is capacity on a free tier, not safety headroom.

    Groq reserves prompt + max_tokens against an 8,000-token minute window
    *before* generating, so sending no cap reserves the model's maximum on
    every call — one request a minute at best. Until 2026-08-19 we sent none:
    Groq stopped at 354 calls in a day and 838 of Gemini's 1,205 came back 429.
    """
    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="p", base_url="https://x.test",
                               api_key="k", model="m", quality=50)
    assert ex._budgeted() == {"max_tokens": 1500}

    # Tunable, because the lesson is that this is the first knob to turn.
    assert OpenAICompatExtractor(provider="p", base_url="https://x.test", api_key="k",
                                 model="m", quality=50,
                                 max_output_tokens=800)._budgeted() == {"max_tokens": 800}


def test_truncation_is_reported_as_truncation():
    """A cut-off answer is invalid JSON, which reads like a bad model."""
    import structlog

    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="p", base_url="https://x.test",
                               api_key="k", model="m", quality=50)
    with structlog.testing.capture_logs() as captured:
        assert ex._was_truncated({"choices": [{"finish_reason": "length"}]}, "url") is True
        assert ex._was_truncated({"choices": [{"finish_reason": "stop"}]}, "url") is False
        assert ex._was_truncated({}, "url") is False   # malformed: must not raise
    names = [e.get("event") for e in captured]
    assert names.count("llm_output_truncated") == 1


def test_a_truncated_answer_names_the_cap_that_cut_it():
    """"Invalid JSON" reads as a bad page; the cap is ours and is the fix."""
    from scraper.extract import ExtractorContentError
    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="p", base_url="https://x.test",
                               api_key="k", model="m", quality=50,
                               max_output_tokens=1500)

    def _parse():
        raise ExtractorContentError("LLM returned invalid communities JSON: x")

    with pytest.raises(ExtractorContentError) as cut:
        ex._parsed({"choices": [{"finish_reason": "length"}]}, "url", _parse)
    assert "max_output_tokens=1500" in str(cut.value)

    # A complete answer that will not parse is not blamed on the cap.
    with pytest.raises(ExtractorContentError) as whole:
        ex._parsed({"choices": [{"finish_reason": "stop"}]}, "url", _parse)
    assert "max_output_tokens" not in str(whole.value)

    # And a cap hit *after* the closing brace still yields the answer.
    assert ex._parsed({"choices": [{"finish_reason": "length"}]},
                      "url", lambda: ["kept"]) == ["kept"]


def test_a_token_ceiling_ends_the_day_even_with_requests_left(tmp_path):
    """Groq allows 14,400 requests a day and 200,000 tokens.

    On 2026-08-20 it refused with "TPD: Limit 200000, Used 199087" after about
    390 calls, while the catalogue planned for 13,680 — so extraction stopped
    at the first pair with real work, every run, all day, and the pages piled
    up behind it.
    """
    db = tmp_path / "s.db"
    init_db(db)
    ledger = QuotaLedger(db, day="2026-08-20")
    spec = _spec(rpd=14400, tpd=200_000)

    assert ledger.available(spec) is True
    # Two hundred calls, a thousand tokens each: requests barely touched.
    for _ in range(200):
        ledger.note_call("groq", spec=spec, tokens=1000)
    assert ledger.remaining(spec) > 13_000        # plenty of requests left
    assert ledger.available(spec) is False        # and no tokens to use them


def test_a_daily_token_refusal_is_learned_however_short_the_backoff(tmp_path):
    """Groq's Retry-After was 1,149s — under the 1,800s "this is daily" bar.

    Without reading what the provider said, the ceiling was never learned and
    the router kept planning against a budget that was already gone.
    """
    db = tmp_path / "s.db"
    init_db(db)
    ledger = QuotaLedger(db, day="2026-08-20")
    spec = _spec(rpd=14400)

    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=1149,
                     spec=spec, error="Rate limit reached ... on tokens per day (TPD)")
    assert ledger._row("groq").get("observed_limit")


@pytest.mark.asyncio
async def test_token_cost_is_not_shared_between_concurrent_pages():
    """One extractor instance serves several pages at once.

    An attribute holding "the last call's cost" hands a page its neighbour's
    number — the same shape `extract_traced` removed for provenance, and a
    token ceiling is only useful if the figure belongs to the call.
    """
    import asyncio

    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="p", base_url="https://x.test",
                               api_key="k", model="m", quality=50)
    seen: dict = {}

    async def one(name: str, cost: int, pause: float) -> None:
        ex._note_usage({"usage": {"total_tokens": cost}})
        await asyncio.sleep(pause)          # let the other task run in between
        seen[name] = ex.last_tokens

    await asyncio.gather(one("a", 100, 0.02), one("b", 900, 0.01))
    assert seen == {"a": 100, "b": 900}


def test_capacity_means_requests_and_tokens(tmp_path, monkeypatch):
    """The worker asked "is there quota?" and got a request count.

    Groq's day ends on 200,000 tokens while 13,000 of its 14,400 requests
    remain, so the worker chose extraction, the pass reached the first pair
    with real work, and stopped on "all providers rate limited". Ninety runs
    on 2026-08-19 did exactly that.
    """
    monkeypatch.setenv("A_KEY", "k")
    router, ledger = _router(tmp_path, _spec("a", env="A_KEY", quality=(60,), tpd=200_000))

    assert router.has_capacity() is True
    assert router.with_budget()
    spec = router.spec_for(router.with_budget()[0])

    for _ in range(200):
        ledger.note_call("a", spec=spec, tokens=1000)

    assert router.has_capacity() is False, "token exhaustion must end the day"
    assert router.with_budget() == []


# ── fenced JSON ──────────────────────────────────────────────────────────────

def test_a_markdown_fence_is_unwrapped_not_rejected():
    """A fenced answer is correct JSON in a wrapper, and rejecting it loses pages.

    `_json_items` raises ExtractorContentError, `_Quarantine` counts exactly
    that error, and three of them retire the page permanently under the current
    fingerprint. So a model that habitually fences would delete pages from the
    corpus over a formatting habit — measured on Qwen3-8B at roughly one answer
    in sixteen.
    """
    from scraper.extract import _json_items

    want = [{"name": "Szentendrei Futóklub"}]
    body = '{"communities": [{"name": "Szentendrei Futóklub"}]}'
    for raw in (body,
                f"```json\n{body}\n```",
                f"```JSON\n{body}\n```",
                f"```\n{body}\n```",
                f"  ```json\n{body}\n```  "):
        assert _json_items(raw, "communities", "communities", "u") == want


def test_unfencing_does_not_forgive_actually_broken_output():
    """The second chance is for the wrapper only.

    Truncation and prose still have to raise: caching them as an empty page
    would record "0 communities" permanently under the current fingerprint.
    """
    from scraper.extract import ExtractorContentError, _json_items

    for raw in ("I could not find any communities on this page.",
                '```json\n{"communities": [{"name": "Cut off mid',
                '{"communities": [{"name": "Cut off mid'):
        with pytest.raises(ExtractorContentError):
            _json_items(raw, "communities", "communities", "u")


# ── a learned ceiling is a hypothesis, not a verdict ─────────────────────────

def test_a_stale_learned_ceiling_stops_binding(tmp_path, monkeypatch):
    """A ceiling inferred from one refusal must not own the rest of the day.

    On 2026-09-09 Groq's day ended at 187 calls against a real allowance of
    1,000: a per-minute *token* limit answers 429 exactly like a spent day, and
    the pin was only ever lowered. Past the TTL the router plans against the
    configured number again and finds out.
    """
    db = _db(tmp_path)
    spec = _spec(rpd=1000)
    ledger = QuotaLedger(db, day="2026-09-09")
    ledger.reload()

    now = [1_000_000.0]
    monkeypatch.setattr("scraper.router.time.time", lambda: now[0])

    ledger._row("groq").update({"calls": 187, "observed_limit": 187,
                                "observed_at": now[0]})
    assert ledger.budget(spec) == int(187 * 0.95)   # fresh: believed
    assert ledger.remaining(spec) == 0

    now[0] += QuotaLedger._OBSERVED_LIMIT_TTL_S + 1
    assert ledger.budget(spec) == int(1000 * 0.95)  # stale: re-tested
    assert ledger.remaining(spec) > 0


def test_a_refusal_renews_a_ceiling_it_only_reconfirms(tmp_path, monkeypatch):
    """Freshness is what the router trusts, so re-confirming must renew it.

    Otherwise a *correctly* learned ceiling expires while the provider is still
    saying no, and the fleet re-tests a provider that has genuinely finished
    every half hour instead of once.
    """
    db = _db(tmp_path)
    spec = _spec(rpd=1000)
    ledger = QuotaLedger(db, day="2026-09-09")
    ledger.reload()

    now = [2_000_000.0]
    monkeypatch.setattr("scraper.router.time.time", lambda: now[0])

    ledger._row("groq").update({"calls": 800, "observed_limit": 800,
                                "observed_at": now[0]})
    now[0] += QuotaLedger._OBSERVED_LIMIT_TTL_S + 1     # would be stale now

    # The re-test happens and the provider refuses again, saying "per day".
    ledger.note_call("groq", ok=False, rate_limited=True, retry_after=60,
                     error="rate limit: requests per day", spec=spec)
    assert ledger._row("groq")["observed_at"] == now[0]
    assert ledger.budget(spec) == int(800 * 0.95)       # believed again


def test_a_success_above_the_ceiling_unlearns_it(tmp_path):
    """The provider itself is the only thing allowed to raise a ceiling.

    Lowering stays cautious — an unproven number must never talk down a proven
    one — but a call that *succeeds* past the ceiling is not a guess, and
    without this the day never recovers from a wrong inference.
    """
    db = _db(tmp_path)
    spec = _spec(rpd=1000)
    ledger = QuotaLedger(db, day="2026-09-09")
    ledger.reload()

    record_provider_call(db, "2026-09-09", "groq", ok=False, rate_limited=True,
                         observed_limit=187, observed_at=5.0)
    ledger.reload()
    assert ledger._row("groq")["observed_limit"] == 187

    ledger._row("groq")["calls"] = 188          # the re-test call
    ledger.note_call("groq", ok=True, spec=spec)

    assert ledger._row("groq").get("observed_limit") in (None, 0)
    assert get_provider_usage(db, "2026-09-09")["groq"]["observed_limit"] is None
    assert ledger.budget(spec) == int(1000 * 0.95)


def test_a_success_below_the_ceiling_leaves_it_alone(tmp_path):
    """Only a success *past* the ceiling is evidence about the ceiling.

    Ordinary successful calls happen all day underneath it and say nothing.
    """
    db = _db(tmp_path)
    spec = _spec(rpd=1000)
    ledger = QuotaLedger(db, day="2026-09-09")
    ledger.reload()
    record_provider_call(db, "2026-09-09", "groq", ok=False, rate_limited=True,
                         observed_limit=500, observed_at=5.0)
    ledger.reload()

    ledger._row("groq")["calls"] = 100
    ledger.note_call("groq", ok=True, spec=spec)
    assert ledger._row("groq")["observed_limit"] == 500
