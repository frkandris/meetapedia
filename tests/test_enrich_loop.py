"""The enrichment loop's round-to-round decisions, with the clock substituted.

`_enrich_body` was a closure inside `main()` until 2026-09-17, so none of this
could be asserted: the loop that spends the whole free-tier budget had no test
at all. What made that expensive was the `_pause` fix — a provider retired by
the circuit breaker used to stay retired for the life of one extractor, and
under `worker_enabled` that run never ends, so a machine that came back was
ignored until someone restarted the container. That rebuild is a `nonlocal`
rebind inside a 130-line loop, which is exactly the kind of state mutation that
needs a test rather than a careful reading.

Every test drives the loop with `unbounded=True` and ends it by cancelling from
the fake batch, because cancellation is how the real loop ends. Nothing here
reads a wall clock — the pauses go through the `scraper.main._sleep` seam, and
the only clock the loop consults (`_next_window_end`) belongs to the bounded
path — so the substitute records the requested waits instead of advancing a
fake clock. `extract.py`'s virtual clock exists because `pace_wait` subtracts
`monotonic()`; there is no such arithmetic here, and pretending otherwise would
be cargo cult.
"""
import asyncio
from types import SimpleNamespace

import pytest

from scraper import main as main_mod
from scraper.web.state import app_state

_IDLE = main_mod._ENRICH_IDLE_PAUSE_S
_RATE_LIMIT = main_mod._ENRICH_RATE_LIMIT_PAUSE_S


def _stats(**kw):
    """One `enrich_batch` return value. Defaults are a productive round."""
    base = {"enriched": 3, "failed": 0, "pool": 5, "skipped": 0, "no_source": 0,
            "stopped_at_deadline": False, "stopped_rate_limited": False,
            "stopped_no_provider": False}
    base.update(kw)
    return base


class _Extractor:
    """Stands in for a FallbackExtractor chain: only these two are consulted."""

    def __init__(self, exhausted=False):
        self.exhausted = exhausted
        self.preflights = 0

    async def preflight(self):
        self.preflights += 1


@pytest.fixture
def loop_env(monkeypatch, tmp_path):
    """Wire the loop's collaborators and return the recorded state.

    `cities` carries one Hungarian city because the scope comes from
    `_saver_city_groups` + the real `pipeline.country_priority`, and an empty
    first group would return before the first round.
    """
    waits: list[float] = []

    async def _record(seconds):
        waits.append(float(seconds))

    monkeypatch.setattr(main_mod, "_sleep", _record)
    monkeypatch.setattr(app_state, "db_path", tmp_path / "scraper.db", raising=False)
    monkeypatch.setattr(app_state, "pipeline_cfg",
                        SimpleNamespace(fetch_blocked_domains=[]), raising=False)
    monkeypatch.setattr(app_state, "cities",
                        [SimpleNamespace(name="Budapest", country="Hungary")],
                        raising=False)

    built: list[_Extractor] = []

    def build_extractor(_cfg, exhausted=False):
        built.append(_Extractor(exhausted=exhausted))
        return built[-1]

    return SimpleNamespace(waits=waits, built=built, build_extractor=build_extractor)


async def _run(env, script, *, free_quota=True, unbounded=True,
               build_extractor=None):
    """Drive the loop, answering each round from `script`.

    A `script` entry that is an exception is raised instead of returned, which
    is how a test ends an unbounded loop.
    """
    rounds = iter(script)
    calls: list[set] = []

    async def enrich_batch(_db, _extractor, scope, **kwargs):
        calls.append(set(scope))
        item = next(rounds, asyncio.CancelledError())
        if isinstance(item, BaseException):
            raise item
        return item

    await main_mod._enrich_body(
        {"enrich_batch_limit": 5}, unbounded, None, None, enrich_batch,
        build_extractor or env.build_extractor, lambda: free_quota)
    return calls


@pytest.mark.asyncio
async def test_rate_limited_pause_rebuilds_the_chain(loop_env):
    """The fix: a per-minute limit waits 75 s and then starts over on a fresh
    chain, so a provider the circuit breaker retired gets another turn."""
    with pytest.raises(asyncio.CancelledError):
        await _run(loop_env, [_stats(enriched=0, stopped_rate_limited=True)])

    assert loop_env.waits == [_RATE_LIMIT]
    assert len(loop_env.built) == 2, "the chain should be rebuilt after the pause"


@pytest.mark.asyncio
async def test_spent_daily_quota_waits_the_long_pause(loop_env):
    """A spent allowance also answers 429, so the loop must not retry every 75
    seconds until midnight — it waits out the reset and rebuilds for it."""
    with pytest.raises(asyncio.CancelledError):
        await _run(loop_env, [_stats(enriched=0, stopped_rate_limited=True)],
                   free_quota=False)

    assert loop_env.waits == [_IDLE]
    assert len(loop_env.built) == 2


@pytest.mark.asyncio
async def test_provider_down_pause_rebuilds_the_chain(loop_env):
    """A round that enriched nothing and failed something is the fleet going
    quiet: wait for the daily reset, then rebuild — the same reason as above."""
    with pytest.raises(asyncio.CancelledError):
        await _run(loop_env, [_stats(enriched=0, failed=1)])

    assert loop_env.waits == [_IDLE]
    assert len(loop_env.built) == 2


@pytest.mark.asyncio
async def test_productive_rounds_keep_their_chain(loop_env):
    """The other half of the fix: rebuilding is for pauses only. A chain that is
    working keeps its circuit-breaker state, which is what stops the loop
    walking a dead provider on every page."""
    with pytest.raises(asyncio.CancelledError):
        await _run(loop_env, [_stats(), _stats()])

    assert loop_env.waits == [1, 1], "only the between-rounds yield"
    assert len(loop_env.built) == 1


@pytest.mark.asyncio
async def test_empty_pool_ends_a_bounded_run_without_pausing(loop_env):
    """Caught up with no lower-priority market left: a windowed run ends rather
    than sleeping until its window closes."""
    calls = await _run(loop_env, [_stats(enriched=1, pool=0)], unbounded=False)

    assert len(calls) == 1
    assert loop_env.waits == []
    assert len(loop_env.built) == 1


@pytest.mark.asyncio
async def test_exhausted_chain_never_runs_a_round(loop_env):
    """`exhausted` means no provider is configured at all — the loop must not
    start a round, because every call in it would be a refusal."""
    calls = await _run(loop_env, [_stats()],
                       build_extractor=lambda cfg: loop_env.build_extractor(
                           cfg, exhausted=True))

    assert calls == []
    assert loop_env.waits == []
