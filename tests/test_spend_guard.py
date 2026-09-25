"""The daily spend ceiling on paid providers.

`allow_paid: true` went on alone on 2026-08-24. Nothing in the system counted
money — the ledger counted requests and tokens, both of which looked healthy —
so the permission was an open tab: four days, roughly $60, through a fallback
costing four times what the intended provider did. See
docs/wiki/pages/concepts/paid-spend-guard.md.
"""
from pathlib import Path

import pytest

from scraper.db import get_provider_usage, init_db, record_provider_call
from scraper.providers import (ModelSpec, ProviderCatalogue, ProviderSpec,
                               RouterSettings, build_extractors)
from scraper.router import ModelRouter, QuotaLedger


@pytest.fixture(autouse=True)
def _keys(monkeypatch):
    """A provider with no key is skipped silently, which would make every
    assertion below vacuously true."""
    monkeypatch.setenv("PAID_KEY", "k")
    monkeypatch.setenv("FREE_KEY", "k")


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def _paid_spec(name="paid", price_in=0.10, price_out=0.20, quality=80,
               max_out=None):
    return ProviderSpec(
        name=name, base_url="https://api.test/v1", api_key_env="PAID_KEY",
        models=(ModelSpec(model=f"{name}-m", quality=quality,
                          usd_per_1m_in=price_in, usd_per_1m_out=price_out,
                          max_output_tokens=max_out),),
        rpm=60, rpd=100000, paid=True,
    )


def _free_spec(name="free", quality=60):
    return ProviderSpec(
        name=name, base_url="https://free.test/v1", api_key_env="FREE_KEY",
        models=(ModelSpec(model=f"{name}-m", quality=quality),),
        rpm=30, rpd=1000,
    )


def _catalogue(*specs, allow_paid=True, budget=1.00):
    return ProviderCatalogue(
        router=RouterSettings(enabled=True, allow_paid=allow_paid,
                              daily_budget_usd=budget),
        providers=tuple(specs),
    )


def _router(tmp_path, *specs, allow_paid=True, budget=1.00):
    cat = _catalogue(*specs, allow_paid=allow_paid, budget=budget)
    ledger = QuotaLedger(_db(tmp_path))
    extractors = build_extractors(cat, allow_paid=True)
    return ModelRouter(cat, ledger, extractors)


# ── the money ceiling ────────────────────────────────────────────────────────

def test_the_days_paid_spend_is_counted_in_dollars(tmp_path):
    """A request count cannot express a budget denominated in money.

    The same 10,000 calls cost $0.40 on one paid model and $20 on another, and
    on 2026-08-24 the fleet spent four days' money through the expensive one
    while every counter in the system read "within budget".
    """
    db = _db(tmp_path)
    record_provider_call(db, "2026-08-27", "paid", tokens=3000, cost_usd=0.004)
    record_provider_call(db, "2026-08-27", "paid", tokens=3000, cost_usd=0.004)
    row = get_provider_usage(db, "2026-08-27")["paid"]
    assert row["calls"] == 2
    assert row["cost_usd"] == pytest.approx(0.008)


def test_a_paid_provider_stops_when_the_days_budget_is_spent(tmp_path):
    router = _router(tmp_path, _paid_spec(), _free_spec(), budget=1.00)
    paid = [e for e in router._all if e.provider == "paid"][0]
    free = [e for e in router._all if e.provider == "free"][0]

    assert router.can_use(paid) is True
    router.note(paid, ok=True, cost_usd=0.99)
    # Just under the line is still allowed. Asked of the budget rather than of
    # `can_use`, which is also false for the one-second rpm cooldown the call
    # above just started — a wait, not a ceiling.
    assert router.paid_allowed() is True
    assert router.done_for_today(paid) is False

    router.note(paid, ok=True, cost_usd=0.02)
    assert router.paid_spend_today() == pytest.approx(1.01)
    assert router.can_use(paid) is False
    # Free capacity is untouched: the day ends on free providers or not at all,
    # which is the ordinary end of a window rather than an outage.
    assert router.can_use(free) is True
    assert router.order() == [free]


def test_a_refused_paid_call_still_costs_what_it_cost(tmp_path):
    """A truncated answer is charged in full, and truncation was most of what
    the 2026-08-24 experiment bought. Counting only successes would have made
    the runaway invisible to its own guard."""
    router = _router(tmp_path, _paid_spec(), budget=0.10)
    paid = router._all[0]
    router.note(paid, ok=False, error="LLM returned invalid JSON", cost_usd=0.11)
    assert router.can_use(paid) is False


def test_allow_paid_without_a_budget_spends_nothing(tmp_path):
    """`allow_paid` says paid providers *may* be used; a budget says how much.

    Switched on alone on 2026-08-24, `allow_paid` was an open tab. The two are
    one decision now, and the default half of it is zero.
    """
    router = _router(tmp_path, _paid_spec(), _free_spec(), budget=0.0)
    paid = [e for e in router._all if e.provider == "paid"][0]
    assert router.paid_allowed() is False
    assert router.can_use(paid) is False
    assert router.has_capacity() is True  # the free provider still has a day


def test_the_ceiling_announces_itself_once_a_day(tmp_path):
    """Once per refused call is thousands of identical lines an hour; once ever
    is silence on the second day, and the worker holds a router across
    midnight."""
    import structlog

    router = _router(tmp_path, _paid_spec(), budget=0.10)
    router.note(router._all[0], ok=True, cost_usd=0.50)

    with structlog.testing.capture_logs() as captured:
        assert router.paid_allowed() is False
        assert router.paid_allowed() is False
    assert [e["event"] for e in captured].count("paid_daily_budget_spent") == 1

    # Tomorrow the allowance resets and the ceiling is worth announcing again.
    # Forced rather than waited for: the ledger rolls the day off the wall
    # clock, and a test that moves the clock is testing the clock.
    router._budget_warned_day = "2026-01-01"
    with structlog.testing.capture_logs() as tomorrow:
        assert router.paid_allowed() is False
    assert [e["event"] for e in tomorrow].count("paid_daily_budget_spent") == 1


def test_a_zero_budget_leaves_the_free_fleet_alone(tmp_path):
    """The shipped state since 2026-08-27: paid permitted, nothing to spend.

    The ceiling exists to stop paid providers, and the failure that would make
    it useless is stopping the free ones with them — the free fleet is the only
    thing extracting anything at all. So this asserts the whole free path, not
    just that the paid one is shut: a free provider is callable, it is what the
    router orders, and the run still reads as having capacity.
    """
    router = _router(tmp_path, _paid_spec(quality=90), _free_spec(quality=60),
                     budget=0.0)
    paid = [e for e in router._all if e.provider == "paid"][0]
    free = [e for e in router._all if e.provider == "free"][0]

    assert router.can_use(paid) is False
    assert router.done_for_today(paid) is True

    assert router.can_use(free) is True
    assert router.done_for_today(free) is False
    # The paid model outscores the free one 90 to 60 and still does not appear.
    assert router.order() == [free]
    assert router.with_budget() == [free]
    assert router.has_capacity() is True
    assert router.best_available_quality() == 60

    # And spending the free allowance is what ends the day — not the ceiling.
    for _ in range(1000):
        router.ledger.note_call("free", ok=True)
    assert router.has_capacity() is False


def test_the_shipped_catalogue_spends_nothing_until_someone_says_otherwise():
    """`allow_paid` is the permission and `daily_budget_usd` is the amount.

    Asserted on the real config because the whole guard reduces to this pair,
    and a stray edit to either half is the exact shape of the 2026-08-24
    failure.
    """
    from scraper.providers import load_catalogue

    assert load_catalogue().router.daily_budget_usd == 0.0


def test_a_paid_model_with_no_price_is_never_built(tmp_path):
    """Fail closed: an unpriced paid model reports $0.00 against the ceiling,
    so the guard would wave through exactly the runaway it exists to stop."""
    unpriced = ProviderSpec(
        name="unpriced", base_url="https://api.test/v1", api_key_env="PAID_KEY",
        models=(ModelSpec(model="mystery", quality=90),), paid=True)
    cat = _catalogue(unpriced, _free_spec())
    built = build_extractors(cat, allow_paid=True)
    assert [e.provider for e in built] == ["free"]


def test_the_budget_ends_the_paid_providers_day_not_just_this_moment(tmp_path):
    """Preflight probes anything that is merely paced, and skips what is out
    for the day. Money belongs in the second group: until 2026-08-27 it was in
    neither, so every run probed `openrouter_paid` once — 41 refused calls a
    day, none of them necessary."""
    router = _router(tmp_path, _paid_spec(), budget=0.10)
    paid = router._all[0]
    assert router.done_for_today(paid) is False
    router.note(paid, ok=False, cost_usd=0.50)
    assert router.done_for_today(paid) is True


def test_cost_is_read_from_the_call_the_task_just_made(tmp_path):
    """Priced from the provider's own usage numbers, split the way it bills."""
    from scraper import extract as extract_mod

    ex = build_extractors(_catalogue(_paid_spec(price_in=1.0, price_out=2.0)),
                          allow_paid=True)[0]
    extract_mod._CALL_PROMPT_TOKENS.set(1_000_000)
    extract_mod._CALL_COMPLETION_TOKENS.set(500_000)
    assert ex.last_cost_usd == pytest.approx(2.0)

    # Providers that report only a total are priced at the input rate — low,
    # but never zero: a guard that reads a call as free is not a guard.
    extract_mod._CALL_PROMPT_TOKENS.set(0)
    extract_mod._CALL_COMPLETION_TOKENS.set(0)
    extract_mod._CALL_TOKENS.set(2_000_000)
    assert ex.last_cost_usd == pytest.approx(2.0)

    # A free model has no price and costs nothing, whatever it reports.
    free = build_extractors(_catalogue(_free_spec()))[0]
    assert free.last_cost_usd == 0.0


# ── per-model output cap ─────────────────────────────────────────────────────

def test_output_cap_is_per_model_then_per_provider_then_global():
    """One global cap either starves Groq or truncates every reasoning model.

    Groq's free tier reserves prompt + max_tokens against an 8,000-token minute
    window *before* generating; a paid endpoint does not. On 2026-08-24 the one
    number did the second: 28 truncations in 14 pages, each charged in full.
    """
    provider_default = ProviderSpec(
        name="p", base_url="https://api.test/v1", api_key_env="PAID_KEY",
        models=(ModelSpec(model="inherits", quality=50, usd_per_1m_in=1.0),
                ModelSpec(model="overrides", quality=40, usd_per_1m_in=1.0,
                          max_output_tokens=9000)),
        paid=True, max_output_tokens=4000)
    built = {e.model: e for e in build_extractors(
        _catalogue(provider_default, _free_spec()), allow_paid=True,
        max_output_tokens=1500)}
    assert built["inherits"].max_output_tokens == 4000     # provider's
    assert built["overrides"].max_output_tokens == 9000    # model's
    assert built["free-m"].max_output_tokens == 1500       # global


def test_timeout_is_per_provider_then_global():
    """A model on our own GPU is minutes of work; a hosted API is seconds.

    The global 60 is sized for the hosted case, and a timeout is scored as a
    *failure* — so a slow-but-working provider would be retired by the circuit
    breaker rather than reported as slow. Raising the global instead would let a
    genuinely hung hosted call hold a slot for as long as the slowest local
    model is allowed, which is the opposite of what the ceiling is for.
    """
    slow = ProviderSpec(
        name="local", base_url="http://127.0.0.1:8080/v1", api_key_env="FREE_KEY",
        models=(ModelSpec(model="on-our-gpu", quality=50),),
        timeout_seconds=900)
    built = {e.model: e for e in build_extractors(
        _catalogue(slow, _free_spec()), timeout_seconds=60)}
    assert built["on-our-gpu"].timeout_seconds == 900   # provider's
    assert built["free-m"].timeout_seconds == 60        # global


def test_catalogue_parses_the_provider_timeout():
    """The override is useless if the YAML key is not read."""
    import yaml

    from scraper.providers import load_catalogue

    raw = yaml.safe_load("""
router:
  enabled: true
providers:
  - name: withtimeout
    api_key_env: X
    base_url: http://127.0.0.1:8080/v1
    timeout_seconds: 900
    models:
      - model: m
        quality: 50
  - name: without
    api_key_env: Y
    base_url: https://api.test/v1
    models:
      - model: m
        quality: 50
""")
    import tempfile
    from pathlib import Path as _P
    with tempfile.TemporaryDirectory() as d:
        (_P(d) / "providers.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
        cat = load_catalogue(_P(d))
    by = {p.name: p for p in cat.providers}
    assert by["withtimeout"].timeout_seconds == 900
    assert by["without"].timeout_seconds is None


def test_base_url_comes_from_the_env_when_named(monkeypatch):
    """A self-hosted endpoint's address is deployment state, not code.

    A tunnel to a machine on a desk gets a new hostname whenever it reconnects,
    and config/ is not a persisted volume — a URL in the YAML would make each
    reconnection a code deploy.
    """
    spec = ProviderSpec(
        name="local", base_url="http://127.0.0.1:8080/v1",
        base_url_env="LOCAL_GPU_URL", api_key_env="FREE_KEY",
        models=(ModelSpec(model="m", quality=50),))
    monkeypatch.setenv("FREE_KEY", "k")

    monkeypatch.delenv("LOCAL_GPU_URL", raising=False)
    assert spec.url == "http://127.0.0.1:8080/v1"          # falls back to YAML

    monkeypatch.setenv("LOCAL_GPU_URL", "https://x.trycloudflare.com/v1 ")
    assert spec.url == "https://x.trycloudflare.com/v1"    # env wins, stripped
    # By model, not by position: build_extractors orders by quality.
    built = {e.model: e for e in build_extractors(_catalogue(spec, _free_spec()))}
    assert built["m"]._BASE_URL == "https://x.trycloudflare.com/v1"


def test_a_provider_with_no_address_is_skipped_not_built(monkeypatch):
    """Absent is the honest state for a self-hosted provider whose tunnel is down.

    Building it would mean every call in the run failing at connect time until
    the circuit breaker retires it — a run-long outage reported as a bad
    provider rather than a missing one.
    """
    spec = ProviderSpec(
        name="local", base_url="", base_url_env="LOCAL_GPU_URL",
        api_key_env="FREE_KEY", models=(ModelSpec(model="m", quality=99),))
    monkeypatch.setenv("FREE_KEY", "k")
    monkeypatch.delenv("LOCAL_GPU_URL", raising=False)
    assert spec.configured is False
    built = [e.model for e in build_extractors(_catalogue(spec, _free_spec()))]
    assert "m" not in built and "free-m" in built


@pytest.mark.asyncio
async def test_provider_concurrency_queues_before_the_request_is_sent():
    """The wait must happen on our side, not in the origin's queue.

    A proxy starts its timeout clock when the request arrives, so anything that
    waits *with a connection open* still times out. Cloudflare gives an origin
    100 s and answers 524 after that; on 2026-09-06 four concurrent pages on one
    GPU each took four times as long and every one of them blew through it.
    Holding the slot before `_post_now` means the model is already free when the
    request is sent.
    """
    import asyncio

    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(
        provider="localgpu", base_url="http://127.0.0.1:8080/v1", api_key="k",
        model="m", quality=67, max_concurrency=1)

    live, peak = 0, 0

    async def _fake_post_now(payload, label):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return {"choices": [{"message": {"content": "{}"}}]}

    ex._post_now = _fake_post_now
    await asyncio.gather(*[ex._post({}, f"p{i}") for i in range(6)])
    assert peak == 1, f"{peak} calls were in flight at once"


@pytest.mark.asyncio
async def test_the_limit_is_shared_by_every_extractor_for_that_provider():
    """One GPU is being rationed, not one Python object.

    `build_extractor()` builds a chain for the pipeline and `_enrich_run`
    builds its own, deliberately concurrent with it, so one provider has
    several live extractors in a single process. Per-instance semaphores let
    each politely limit itself to one call while three ran at once — measured
    2026-09-06 at 1.6-4.0 tok/s with single calls stretched to 76 s against
    Cloudflare's 100 s ceiling.
    """
    import asyncio

    from scraper.extract import _ApiExtractor
    from scraper.providers import OpenAICompatExtractor

    _ApiExtractor._PROVIDER_SLOTS.clear()

    def _mk():
        return OpenAICompatExtractor(
            provider="localgpu", base_url="http://127.0.0.1:8080/v1",
            api_key="k", model="m", quality=67, max_concurrency=1)

    live, peak = 0, 0

    async def _fake_post_now(payload, label):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return {}

    # Three separate extractors, as the pipeline and enrichment would build.
    fleet = [_mk() for _ in range(3)]
    for e in fleet:
        e._post_now = _fake_post_now
    await asyncio.gather(*[e._post({}, "p") for e in fleet for _ in range(2)])
    assert peak == 1, f"{peak} calls were in flight across {len(fleet)} extractors"
    _ApiExtractor._PROVIDER_SLOTS.clear()


@pytest.mark.asyncio
async def test_unlimited_by_default_so_hosted_apis_are_unaffected():
    """Concurrency is free where the wait is network latency, and every hosted
    provider depends on it — `pipeline.extract_concurrency` governs there."""
    import asyncio

    from scraper.providers import OpenAICompatExtractor

    ex = OpenAICompatExtractor(provider="groq", base_url="https://api.test/v1",
                               api_key="k", model="m", quality=60)
    assert ex.max_concurrency is None

    live, peak = 0, 0

    async def _fake_post_now(payload, label):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.02)
        live -= 1
        return {}

    ex._post_now = _fake_post_now
    await asyncio.gather(*[ex._post({}, f"p{i}") for i in range(4)])
    assert peak == 4


def test_the_shipped_catalogue_limits_our_own_gpu():
    """The one provider where concurrency is bounded by hardware: its cap is
    the llama-server's slot count (`-np 2`), never more, and it streams so a
    slower concurrent call outlasts Cloudflare's 100 s.
    """
    from scraper.providers import load_catalogue

    by = {p.name: p for p in load_catalogue().providers}
    assert by["localgpu"].max_concurrency == 2
    assert by["localgpu"].stream
    assert all(p.max_concurrency is None
               for n, p in by.items() if n != "localgpu")


def test_the_shipped_catalogue_prices_every_paid_model():
    """Every paid model the fleet can actually reach has a price, or the daily
    ceiling is measuring a subset of the spend."""
    from scraper.providers import load_catalogue

    for spec in load_catalogue().providers:
        if not (spec.paid and spec.enabled):
            continue
        for model in spec.models:
            assert model.usd_per_1m_in or model.usd_per_1m_out, (
                f"{spec.name}:{model.model} is paid and unpriced")


# ── what the morning report says ─────────────────────────────────────────────


def _summary(providers, **extra):
    zero = ("new_communities", "changed_communities", "change_rows", "new_venues",
            "new_persons", "pages_scraped", "pages_extracted", "searches")
    return {
        "hu": {k: 0 for k in zero},
        "intl": {k: 0 for k in zero},
        "totals": {"hu": 0, "intl": 0, "covered_pairs_hu": 0, "covered_pairs_intl": 0},
        "runs": [],
        "providers": providers,
        **extra,
    }


def test_the_report_states_the_days_paid_spend_against_its_ceiling():
    """Free capacity ends in a 429 nobody has to read; paid capacity ends when
    someone reads an invoice. So the invoice is in the report every morning,
    whether or not anything went wrong."""
    from scraper.report import build_report_html

    _, html = build_report_html("2026-08-27", _summary(
        [{"name": "openrouter_paid", "paid": True, "used": 400, "budget": 95000,
          "observed_limit": None, "rate_limits": 0, "failures": 3,
          "cost_usd": 0.42}],
        paid_budget_usd=2.00), {}, None)

    assert "$0.42" in html
    assert "$2.00 napi keret" in html


def test_a_paid_provider_that_answers_nothing_is_named():
    """`openrouter_paid` sat at 41 calls / 41 failures for four days while every
    page fell through to a fallback costing four times as much. A table of call
    counts is how that stayed unnoticed."""
    from scraper.report import build_report_html

    _, html = build_report_html("2026-08-27", _summary(
        [{"name": "openrouter_paid", "paid": True, "used": 41, "budget": 95000,
          "observed_limit": None, "rate_limits": 0, "failures": 41,
          "cost_usd": 0.0},
         {"name": "groq", "paid": False, "used": 100, "budget": 1000,
          "observed_limit": None, "rate_limits": 0, "failures": 100}],
        paid_budget_usd=2.00), {}, None)

    assert "egyetlen hívása sem sikerült" in html
    # Free providers refuse for free and do so routinely — not an alarm.
    assert html.count("egyetlen hívása sem sikerült") == 1
