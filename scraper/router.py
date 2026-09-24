"""Quota-aware model router over the free-tier provider fleet.

Design follows the arXiv reading in
`docs/wiki/sources/2026-08-16-llm-routing-arxiv-research.md`:

* **Route before generating, do not cascade.** A cascade pays the cheap model's
  generation before every escalation decision; a pre-generation router beat the
  best cascade policy on 4 of 5 datasets (arXiv:2605.06350) precisely by not
  paying that. So: pick the best model that still has budget, call it once.
* **Budget at the window level, not per query.** Per-query routing cannot control
  batch-level spend under non-uniform load (arXiv:2603.26796). Free tiers are
  hard per-day capacities, so the ledger is the constraint the router optimizes
  against — not a nice-to-have counter.
* **Learn the real limit from 429s.** Most free providers stopped publishing
  their numbers; a config constant is a guess, an observed ceiling is a fact.
* **Break quality ties by remaining quota**, not by score decimals — published
  scores carry more uncertainty than the gaps between them (arXiv:2606.13221).
* **New work outranks re-work.** Re-extraction is only ever a filler for quota
  that would otherwise expire unused.

The router does not replace `FallbackExtractor`; it *orders and filters* the
list handed to it. That keeps one failure path (circuit breaker, typed errors,
retry semantics) for every provider.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from .db import clear_observed_limit, get_provider_usage, record_provider_call
from .providers import (ProviderCatalogue, ProviderSpec, OpenAICompatExtractor,
                        build_extractors, load_catalogue)

log = structlog.get_logger()

#: Stop using a provider at this fraction of its daily allowance, leaving room
#: for the enrichment job and admin-triggered work that share the same key.
_DAILY_HEADROOM = 0.95

#: A provider with less than about one call's worth of tokens left is done for
#: the day: starting a call we cannot finish just buys another refusal.
#:
#: Sized from measurement, not from the worst case. Groq spent 200,000 tokens
#: over ~390 calls on 2026-08-19 — about 510 tokens each, far below the 8,000
#: characters a prompt may carry, because most pages are short. Reserving a
#: full worst-case request would throw away several real calls' worth of
#: budget at the end of every day.
_MIN_TOKENS_TO_START = 2000


def utc_day(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d")


def _next_utc_midnight() -> float:
    """Epoch seconds of the next 00:00 UTC — when free daily budgets reset."""
    now = datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


class QuotaLedger:
    """Per-provider daily budget, persisted so restarts cannot forge capacity.

    Free allowances are calendar-day budgets. Holding them in memory would hand
    a provider a fresh 14,400 requests on every container restart, and the
    provider would answer with hard 429s the router could not have predicted.
    """

    #: Re-read the DB after this many locally counted calls. The enrichment job
    #: runs concurrently with ai_only in the same off-peak window and holds its
    #: own ledger; without a periodic refresh neither sees the other's spend and
    #: together they burn roughly 2× the budget before either notices. The DB
    #: counters are atomic, so re-reading is all that is needed.
    _RELOAD_EVERY = 25

    #: Per-provider monotonic timestamp of the last attempt, for rpm pacing.
    #: **Class-level, so every ledger in the process shares it.** The gateway
    #: builds a fresh router per request; with per-instance state each HTTP call
    #: would start with an empty clock and rpm pacing would not exist on that
    #: path at all, while the long-lived pipeline router honoured it. The
    #: provider sees one client per container, so the container is the right
    #: granularity.
    #:
    #: In memory rather than in the DB on purpose: a per-minute limit is
    #: meaningless across a restart, unlike the daily counters.
    _last_call: dict[str, float] = {}

    def __init__(self, db_path: Path | None, day: str | None = None):
        self.db_path = db_path
        #: None → follow the wall clock, so a run crossing midnight UTC rolls
        #: onto the new day's allowance. A fixed value pins the day (tests).
        self._fixed_day = day
        self.day = day or utc_day()
        self._usage: dict[str, dict] = {}
        #: Calls claimed but not yet reported, per provider. Per instance, not
        #: shared like `_last_call`: a reservation belongs to a call this
        #: ledger is waiting on. Must exist before `reload`, which re-applies
        #: them over the freshly-read database counts.
        self._reserved: dict[str, int] = {}
        self._since_reload = 0
        self.reload()

    def reload(self) -> None:
        self._since_reload = 0
        if not self.db_path:
            self._usage = {}
        else:
            try:
                self._usage = get_provider_usage(self.db_path, self.day)
            except Exception as exc:  # a broken ledger must not stop extraction
                log.warning("quota_ledger_unreadable", error=str(exc))
                self._usage = {}
        # Reservations for calls still in flight are not in the database yet —
        # `note_call` writes them when the call returns. Re-applying them keeps
        # a periodic reload from handing the same capacity out twice.
        for provider, count in self._reserved.items():
            if count > 0:
                self._row(provider)["calls"] = int(
                    self._row(provider).get("calls") or 0) + count

    def _sync(self) -> None:
        """Roll onto a new UTC day and pick up other processes' spend.

        The ai_only window runs 16:35 → 00:20 UTC. Without the roll, the run
        keeps reading yesterday's row after midnight: providers look exhausted
        for the first 20 minutes of a day whose allowance just reset, and the
        calls are attributed to the wrong date.
        """
        if not self._fixed_day:
            today = utc_day()
            if today != self.day:
                log.info("quota_ledger_day_rollover", from_day=self.day, to_day=today)
                self.day = today
                self._last_call.clear()
                self.reload()
                return
        if self._since_reload >= self._RELOAD_EVERY:
            self.reload()

    def _row(self, provider: str) -> dict:
        return self._usage.setdefault(
            provider,
            {"calls": 0, "failures": 0, "rate_limits": 0, "tokens": 0,
             "cost_usd": 0.0, "observed_limit": None, "blocked_until": 0.0,
             "last_error": None},
        )

    def budget(self, spec: ProviderSpec) -> int:
        """Effective daily allowance: the observed ceiling while it is still
        fresh, otherwise the configured (published, possibly stale) number.

        A learned ceiling expires on purpose. It is an inference from a refusal,
        the inference is sometimes wrong, and it used to be irreversible within
        a day — see `_OBSERVED_LIMIT_TTL_S`. Re-testing costs one refused call
        per cooldown when the ceiling was right, and recovers a provider's whole
        remaining day when it was wrong.

        Pure: reads the row and the clock, nothing else. `remaining()` feeds the
        admin page and the daily report as well as routing, so it must not have
        side effects — an earlier draft handed out "probe slots" from here and
        would have consumed one every time someone loaded /admin/providers.
        """
        row = self._row(spec.name)
        observed = row.get("observed_limit")
        if observed and self._pin_is_fresh(row):
            limit = observed
        else:
            limit = spec.rpd
        return max(0, int(limit * _DAILY_HEADROOM))

    def _pin_is_fresh(self, row: dict) -> bool:
        """Is this learned ceiling still inside its TTL?

        A row written before `observed_at` existed has 0 and reads as stale,
        which is the safe direction: the first refusal after the upgrade stamps
        it properly.
        """
        stamped = float(row.get("observed_at") or 0.0)
        return (time.time() - stamped) < self._OBSERVED_LIMIT_TTL_S

    def used(self, provider: str) -> int:
        return int(self._row(provider).get("calls") or 0)

    def cost_used(self, provider: str) -> float:
        return float(self._row(provider).get("cost_usd") or 0.0)

    def cost_total(self, providers) -> float:
        """What this set of providers has spent today, in USD.

        Takes the names rather than reading every row: the ledger holds free
        providers too, and they contribute 0.00 whether or not anyone asks. The
        budget question is only ever about the paid ones.
        """
        return sum(self.cost_used(name) for name in providers)

    def tokens_used(self, provider: str) -> int:
        return int(self._row(provider).get("tokens") or 0)

    def tokens_left(self, spec: ProviderSpec) -> int:
        """Tokens still available today, or a large number when unbounded.

        For several free tiers this is the ceiling that binds. Groq allows
        14,400 requests a day and 200,000 tokens; at our prompt sizes the tokens
        run out after a few hundred calls, and a request budget alone cannot see
        it coming — it simply spends the day being refused.
        """
        if not spec.tpd:
            return 1 << 30
        return max(0, int(spec.tpd * _DAILY_HEADROOM) - self.tokens_used(spec.name))

    def remaining(self, spec: ProviderSpec) -> int:
        return max(0, self.budget(spec) - self.used(spec.name))

    def _unlearn_if_served_past_ceiling(self, provider: str, row: dict) -> None:
        """A success above the learned ceiling proves the ceiling wrong.

        The TTL in `budget()` is what lets us get here: once the pin goes stale
        the router plans against the configured number, issues a call that the
        old ceiling said was impossible, and the provider answers it. That is
        not an optimistic guess talking a proven limit back up — it is the
        provider demonstrating the inference was wrong, which is the one thing
        allowed to raise a ceiling.

        Cleared rather than raised to the current count: the count only says
        "at least this many", and the configured number is the honest next
        hypothesis. If it is still too high, the next refusal pins it again.
        """
        observed = row.get("observed_limit")
        if not observed or int(row.get("calls") or 0) <= int(observed):
            return
        log.info("provider_daily_limit_unlearned", provider=provider,
                 was=int(observed), served_at_call=int(row.get("calls") or 0))
        row["observed_limit"] = None
        row["observed_at"] = 0.0
        if not self.db_path:
            return
        try:
            clear_observed_limit(self.db_path, self.day, provider)
        except Exception as exc:
            log.warning("quota_ledger_write_failed", provider=provider, error=str(exc))

    def blocked(self, provider: str) -> bool:
        """True while a 429's Retry-After window is still open.

        `blocked_until` is a wall-clock epoch, not `time.monotonic()`, because
        it has to survive a process restart — monotonic clocks reset.
        """
        return time.time() < float(self._row(provider).get("blocked_until") or 0)

    def blocked_for_day(self, provider: str) -> bool:
        """True when the provider is blocked until the next 00:00 UTC.

        A 402 or a provider refusing everything is blocked to midnight; that is
        a spent day, not a short back-off, and must read as "no capacity".
        """
        return float(self._row(provider).get("blocked_until") or 0) >= _next_utc_midnight() - 1

    def paced(self, spec: ProviderSpec) -> bool:
        """False while `rpm` says the next call is too soon."""
        return self.pace_wait(spec) <= 0

    def pace_wait(self, spec: ProviderSpec) -> float:
        """Seconds until this provider's rpm window reopens (0 = ready now).

        Pacing is a *wait*, never a veto. Treating it as unavailability makes
        `_call` find nothing to try, which the circuit breaker counts as a
        failure — at the shipped `rpm: 30` that is 24 consecutive "failures" per
        minute and an aborted run within seconds.
        """
        last = self._last_call.get(spec.name)
        if last is None:
            return 0.0
        return max(0.0, spec.min_interval_s - (time.monotonic() - last))

    def available(self, spec: ProviderSpec) -> bool:
        self._sync()
        return (self.remaining(spec) > 0
                and self.tokens_left(spec) > _MIN_TOKENS_TO_START
                and not self.blocked(spec.name)
                and self.paced(spec))

    #: A 429 must ask us to wait at least this long before it is read as a spent
    #: daily allowance. Deliberately far above a minute: Groq and OpenRouter
    #: return multi-minute Retry-After for *token*-per-minute limits, and at a
    #: 2-minute threshold a TPM 429 at call 200 would pin observed_limit to 200
    #: and end the provider's day — persisted, and only ever lowered. A genuine
    #: daily cap points at the next UTC midnight, which is hours away.
    _DAILY_429_RETRY_AFTER = 1800.0

    #: How long a learned daily ceiling is believed before it must be proven
    #: again. The ceiling is inferred from a refusal, and the inference is
    #: sometimes wrong in the expensive direction: a per-minute *token* limit
    #: answers 429 like a spent day does, and on 2026-09-09 Groq's day ended at
    #: 187 calls against a real allowance of 1,000. Lowering had no way back —
    #: the pin was only ever lowered, so one bad reading cost the rest of the
    #: day.
    #:
    #: Past this, the router plans against the configured number again and finds
    #: out. Being wrong the other way is cheap and self-correcting: one refused
    #: call, and the pin returns for another cooldown. Being wrong the old way
    #: cost a provider's entire remaining allowance.
    _OBSERVED_LIMIT_TTL_S = 1800.0

    #: A provider that has answered 429 this many times today, while almost
    #: never succeeding, is not congested — it is shut. Measured 2026-09-20:
    #: Mistral took **470 calls and refused 469 of them**, one a minute for
    #: most of the day, because each 429 carried a 60-second Retry-After and
    #: sixty seconds later it came round again. A minute-long Retry-After is
    #: indistinguishable from ordinary congestion on its own; the four
    #: hundredth one is not.
    #:
    #: Deliberately not a streak counter. The ledger already persists `calls`,
    #: `failures` and `rate_limits` per provider per day, and a ratio over
    #: those survives a restart, where an in-memory streak would not — and
    #: restarts are exactly when this provider got another few hundred tries.
    _DAILY_429_GIVE_UP_COUNT = 25
    #: …and "almost never succeeding" means this share of the day's calls.
    #: Mistral's success rate was 0.2%. OpenRouter, which really is congested,
    #: ran at 59% with 78 rate limits — it must not be caught by this.
    _DAILY_429_GIVE_UP_SUCCESS_RATE = 0.10

    def reserve_call(self, provider: str) -> None:
        """Take a request slot before issuing the call, not after it returns.

        `note_call` records the outcome, which is too late to keep concurrent
        callers apart: while a call is in flight the provider still looks idle
        and under budget, so every waiting task picks the same one, blows its
        rpm together and collects a fleet's worth of 429s. Claiming the slot at
        selection time is what makes the second task look at the next provider.

        Serial callers are unaffected: the same slot is claimed either way, only
        earlier, and rpm is a limit on requests *started* per minute anyway.
        """
        self._sync()
        row = self._row(provider)
        row["calls"] = int(row.get("calls") or 0) + 1
        self._reserved[provider] = self._reserved.get(provider, 0) + 1
        self._last_call[provider] = time.monotonic()

    def note_call(
        self, provider: str, *, ok: bool = True, rate_limited: bool = False,
        retry_after: float | None = None, error: str | None = None,
        spec: ProviderSpec | None = None, reserved: bool = False,
        tokens: int = 0, cost_usd: float = 0.0, billing_blocked: bool = False,
    ) -> None:
        """Record one attempt. Counts even when it failed — a rejected request
        still consumed a slot at most providers, and undercounting is exactly
        how a router walks into a hard block."""
        self._sync()
        row = self._row(provider)
        if ok:
            self._unlearn_if_served_past_ceiling(provider, row)
        if reserved:
            # The slot was claimed by reserve_call, which also stamped the
            # pacing clock at call *start*. Counting again would double the
            # day's usage, and re-stamping would push the next call to rpm
            # seconds after this one finished rather than after it began. The
            # reservation is settled here: the call is about to be persisted.
            if self._reserved.get(provider):
                self._reserved[provider] -= 1
        else:
            row["calls"] = int(row.get("calls") or 0) + 1
            self._last_call[provider] = time.monotonic()
        self._since_reload += 1
        if not ok:
            row["failures"] = int(row.get("failures") or 0) + 1
        blocked_until = None
        observed_limit = None
        observed_at = None
        if billing_blocked:
            # HTTP 402 is not a wait-and-retry condition — the provider is
            # saying there is no credit. It was recorded as one more failed
            # call and nothing else, so every new run rebuilt the extractor and
            # tried again: on 2026-08-22 Cerebras answered 402 to **all 283**
            # calls it received, and it sits first in the routing order at
            # quality 80, so each run's best pick was spent on a refusal.
            # Blocked to the end of the UTC day, which is when free allowances
            # and trial credit both reset, and persisted so the next run does
            # not start the day's argument over.
            blocked_until = _next_utc_midnight()
            row["blocked_until"] = blocked_until
            log.warning("provider_billing_blocked", provider=provider,
                        until="next UTC midnight", error=(error or "")[:120])
        if rate_limited:
            row["rate_limits"] = int(row.get("rate_limits") or 0) + 1
            wait = float(retry_after or 60)
            blocked_until = time.time() + wait
            row["blocked_until"] = blocked_until
            # Give up on a provider that is refusing everything. A short
            # Retry-After asks us back in a minute, and honouring that
            # literally is how one provider spent a whole day being asked and
            # saying no — 469 refusals out of 470 calls, each one a real
            # request that consumed its own daily allowance. Past the
            # thresholds above, the refusals stop being evidence about *this*
            # minute and start being evidence about the day.
            _calls = int(row.get("calls") or 0)
            _successes = _calls - int(row.get("failures") or 0)
            if (row["rate_limits"] >= self._DAILY_429_GIVE_UP_COUNT
                    and _calls > 0
                    and _successes <= _calls * self._DAILY_429_GIVE_UP_SUCCESS_RATE):
                blocked_until = _next_utc_midnight()
                row["blocked_until"] = blocked_until
                log.warning("provider_refusing_everything", provider=provider,
                            calls=_calls, successes=_successes,
                            rate_limits=row["rate_limits"],
                            until="next UTC midnight")
            # Only a *daily* refusal tells us anything about the daily ceiling.
            # Free tiers publish both rpm (10-30) and rpd (150-14400) and 429 on
            # the per-minute limit constantly; recording the day's call count on
            # one of those would collapse Gemini's 1500/day to 15 after a single
            # minute-limit hit. A long Retry-After, or already being near the
            # configured rpd, is what distinguishes the two.
            # Near the configured allowance, a 429 of any length is much more
            # likely to be the daily cap than a burst limit.
            # Measured against the *configured* rpd, never against the learned
            # budget. Comparing with the learned one is a ratchet: the moment an
            # observed limit is recorded the budget drops, which makes
            # `near_daily` true at a lower call count, which lets the next
            # per-minute 429 lower it again. On 2026-08-18 that walked Groq's
            # 13,680/day down to 336 — the fleet lost 85% of its free capacity
            # to a heuristic eating its own output, and the worker started
            # buying searches because extraction "had no quota".
            configured = int(spec.rpd) if spec is not None else 0
            near_daily = configured > 0 and row["calls"] >= 0.8 * configured
            # The provider often says which limit it enforced. "tokens per day"
            # is a daily refusal however short the Retry-After — Groq's was
            # 1,149 seconds, under the 1,800 threshold, so without this the
            # ceiling was never learned and the router kept planning for 14,400
            # requests against a budget that was gone.
            said_daily = "per day" in (error or "").lower()
            near_daily = near_daily or said_daily
            if wait >= self._DAILY_429_RETRY_AFTER or near_daily:
                observed_limit = row["calls"]
                prev = row.get("observed_limit")
                row["observed_limit"] = min(prev, observed_limit) if prev else observed_limit
                # Stamped on every confirmation, not only when the number moves.
                # The freshness of a ceiling is what the router now trusts, so a
                # refusal that merely re-confirms an existing pin has to renew
                # it — otherwise a correctly-learned ceiling would expire while
                # the provider kept saying no.
                observed_at = time.time()
                row["observed_at"] = observed_at
                log.warning("provider_daily_limit_learned", provider=provider,
                            observed=row["observed_limit"], configured=configured,
                            wait_s=round(wait, 1))
            else:
                log.info("provider_minute_limit", provider=provider, wait_s=round(wait, 1))
        # Measured, not estimated: the response says exactly what it cost, and
        # a token ceiling is only useful if the number counted against it is the
        # provider's own.
        if tokens:
            row["tokens"] = int(row.get("tokens") or 0) + int(tokens)
        if cost_usd:
            # A refused call is usually free, but a truncated one is charged in
            # full — and that was most of what the 2026-08-24 experiment bought.
            # What the provider reported as used is what gets billed, so that is
            # what the budget counts, success or not.
            row["cost_usd"] = float(row.get("cost_usd") or 0.0) + float(cost_usd)
        if error:
            row["last_error"] = error[:200]
        if not self.db_path:
            return
        try:
            record_provider_call(
                self.db_path, self.day, provider, ok=ok, rate_limited=rate_limited,
                blocked_until=blocked_until, error=error, observed_limit=observed_limit,
                observed_at=observed_at,
                tokens=int(tokens or 0), cost_usd=float(cost_usd or 0.0),
            )
        except Exception as exc:
            log.warning("quota_ledger_write_failed", provider=provider, error=str(exc))

    def snapshot(self, catalogue: ProviderCatalogue) -> list[dict]:
        """Per-provider state for the admin page and the daily email."""
        out = []
        for spec in catalogue.providers:
            row = self._row(spec.name)
            out.append({
                "name": spec.name,
                "paid": spec.paid,
                "enabled": spec.enabled,
                "configured": spec.configured,
                "key_env": spec.api_key_env,
                "best_quality": max((m.quality for m in spec.models), default=0),
                "models": [{"model": m.model, "quality": m.quality} for m in spec.models],
                "budget": self.budget(spec),
                "used": self.used(spec.name),
                "remaining": self.remaining(spec),
                "tokens": int(row.get("tokens") or 0),
                "cost_usd": round(float(row.get("cost_usd") or 0.0), 4),
                "tpd": int(spec.tpd or 0),
                "tokens_left": self.tokens_left(spec) if spec.tpd else None,
                "rate_limits": int(row.get("rate_limits") or 0),
                "failures": int(row.get("failures") or 0),
                "observed_limit": row.get("observed_limit"),
                "blocked": self.blocked(spec.name),
                "last_error": row.get("last_error"),
            })
        return sorted(out, key=lambda p: (p["paid"], -p["best_quality"]))


class ModelRouter:
    """Orders the extractor fleet for one run and attributes calls to quota."""

    def __init__(
        self,
        catalogue: ProviderCatalogue,
        ledger: QuotaLedger,
        extractors: list[OpenAICompatExtractor],
    ):
        self.catalogue = catalogue
        self.ledger = ledger
        self._all = extractors
        self._specs = {p.name: p for p in catalogue.providers}
        self._paid = frozenset(p.name for p in catalogue.providers if p.paid)
        #: The UTC day whose exhaustion has already been logged. Once per day
        #: rather than once per refused call — the guard is consulted before
        #: every routing decision, which is thousands of identical lines an
        #: hour — and per *day* rather than once ever, because the worker holds
        #: a router across midnight and the second day's ceiling is as worth
        #: knowing about as the first's.
        self._budget_warned_day: str | None = None

    # ── The money guard ──────────────────────────────────────────────────────
    #
    # Free capacity is bounded by the provider: run out of requests or tokens
    # and the answer is a 429. Paid capacity has no such edge — it bills until
    # someone notices, and on 2026-08-24..27 nobody did for four days. So the
    # edge is drawn here, in the same place and the same way as the free one:
    # a daily budget the router checks before it picks a model.

    def paid_spend_today(self) -> float:
        """USD spent by every paid provider today."""
        self.ledger._sync()
        return self.ledger.cost_total(self._paid)

    @property
    def paid_budget_usd(self) -> float:
        return max(0.0, float(self.catalogue.router.daily_budget_usd or 0.0))

    def paid_allowed(self) -> bool:
        """False when today's paid spend has reached the ceiling.

        Also false when no ceiling is configured. `allow_paid` says paid
        providers *may* be used; a budget says how much. Without the second the
        first is an open tab, which is what this whole guard exists to prevent —
        so the two are one decision and both are required.
        """
        if not self.catalogue.router.allow_paid or self.paid_budget_usd <= 0:
            return False
        if self.paid_spend_today() < self.paid_budget_usd:
            return True
        if self._budget_warned_day != self.ledger.day:
            self._budget_warned_day = self.ledger.day
            log.warning("paid_daily_budget_spent", day=self.ledger.day,
                        spent_usd=round(self.paid_spend_today(), 4),
                        budget_usd=self.paid_budget_usd,
                        note="paid providers are off until 00:00 UTC")
        return False

    def _spendable(self, spec) -> bool:
        """Is this provider allowed to be called *at all* right now?

        Free providers: always. Paid: only inside the day's money budget.
        """
        return not spec.paid or self.paid_allowed()

    def done_for_today(self, extractor) -> bool:
        """True when nothing this run does can make this provider callable again.

        Money, requests and tokens — the three things that only reset at
        midnight. Deliberately distinct from `can_use`, which is also false for
        a two-second rpm cooldown: a caller that skips on *that* skips a healthy
        provider, and a caller that probes a provider which is out for the day
        pays for the answer it already had.
        """
        spec = self.spec_for(extractor)
        return spec is not None and not self._has_daily_budget(spec)

    @property
    def enabled(self) -> bool:
        return self.catalogue.router.enabled and bool(self._all)

    def order(self) -> list[OpenAICompatExtractor]:
        """Extractors that still have budget, best quality first.

        Ties are broken by remaining quota rather than by score decimals: the
        gap between two published scores is usually smaller than the uncertainty
        in either of them, while remaining quota is measured, not estimated.
        """
        usable = [e for e in self._all if self.can_use(e) and e.provider in self._specs]
        usable.sort(key=lambda e: (-e.quality,
                                   -self.ledger.remaining(self._specs[e.provider])))
        return usable

    def all_extractors(self) -> list[OpenAICompatExtractor]:
        """The whole fleet, best-quality first, regardless of current quota.

        For building a long-lived chain: `order()` is a point-in-time view, and
        a run spanning the off-peak window must be able to pick a provider back
        up after the ledger rolls onto a new day.
        """
        return sorted(self._all, key=lambda e: -e.quality)

    def _has_daily_budget(self, spec) -> bool:
        """Money, requests *and* tokens. Any of them running out ends the day.

        Checking requests alone let the worker choose extraction while Groq's
        200,000 tokens were gone but 13,000 of its 14,400 requests were not —
        so the pass started, hit the first pair with real work, and stopped on
        "all providers rate limited". Every run, all day, on 2026-08-19.
        """
        # A provider blocked until midnight has no budget either. Without this
        # it still counted as capacity: preflight probed it every pass (two
        # guaranteed 429s per run from Mistral), and the worker stayed in
        # extraction on providers that could not answer until tomorrow.
        return (self._spendable(spec)
                and not self.ledger.blocked_for_day(spec.name)
                and self.ledger.remaining(spec) > 0
                and self.ledger.tokens_left(spec) > _MIN_TOKENS_TO_START)

    def has_capacity(self, scope: list | None = None) -> bool:
        """True when at least one provider still has *daily* budget left.

        Distinct from `order()` being non-empty, which is also false while every
        provider is merely paced or inside a short back-off. Callers use this to
        tell "we are done for today" from "wait a moment".

        `scope` narrows the question to a specific set of extractors — the
        gateway serves one explicitly requested model, and unrelated providers'
        spare capacity must not make that request look servable.
        """
        self.ledger._sync()
        pool = scope if scope is not None else self._all
        return any(self._has_daily_budget(spec)
                   for spec in {self._specs[e.provider] for e in pool
                                if e.provider in self._specs})

    def with_budget(self) -> list[OpenAICompatExtractor]:
        """Extractors whose provider still has *daily budget*, best first.

        Pacing-blind on purpose. `order()` answers "what can I call this
        instant", which is the right question for picking a provider but the
        wrong one for planning: an rpm cooldown is a two-second wait, not an
        exhausted allowance. Conflating them made the upgrade sweep read "no
        capacity" right after preflight stamped every provider's clock, and made
        the gateway answer 429 "no quota left today" to any caller faster than
        one request every two seconds.
        """
        usable = [e for e in self._all
                  if e.provider in self._specs
                  and self._has_daily_budget(self._specs[e.provider])]
        return sorted(usable, key=lambda e: -e.quality)

    def best_available_quality(self) -> int:
        """Best quality reachable today — ignoring momentary rpm cooldowns."""
        with_budget = self.with_budget()
        return with_budget[0].quality if with_budget else 0

    def reserve(self, extractor) -> bool:
        """Claim a request slot for this extractor's provider. True if claimed."""
        provider = getattr(extractor, "provider", None)
        if not provider:
            return False
        self.ledger.reserve_call(provider)
        return True

    def note(self, extractor, **kwargs) -> None:
        """Attribute one call — and what it cost — to the provider's bucket."""
        provider = getattr(extractor, "provider", None)
        if provider:
            # The spec lets the ledger tell a per-minute 429 from a spent daily
            # allowance — see QuotaLedger.note_call.
            self.ledger.note_call(provider, spec=self._specs.get(provider), **kwargs)

    def spec_for(self, extractor) -> ProviderSpec | None:
        """Public accessor so FallbackExtractor need not reach into _specs."""
        return self._specs.get(getattr(extractor, "provider", ""))

    def can_use(self, extractor) -> bool:
        """Is this extractor's provider within budget, pacing and back-off?"""
        spec = self.spec_for(extractor)
        if spec is None:
            return True
        return self._spendable(spec) and self.ledger.available(spec)

    def pace_wait(self, extractor) -> float:
        """Seconds until this extractor's provider is rpm-ready (0 = now).

        Returns 0 for anything that is unavailable for a *different* reason —
        spent budget or a 429 back-off — so callers do not mistake those for
        something a short sleep can fix.
        """
        spec = self.spec_for(extractor)
        if spec is None:
            return 0.0
        if self.ledger.remaining(spec) <= 0 or self.ledger.blocked(spec.name):
            return 0.0
        return self.ledger.pace_wait(spec)

    def shortest_pace_wait(self) -> float:
        """Shortest rpm wait across the fleet, or 0 when none is merely paced."""
        waits = [w for w in (self.pace_wait(e) for e in self._all) if w > 0]
        return min(waits) if waits else 0.0

def build_router(
    db_path: Path | None,
    *,
    temperature: float = 0.1,
    timeout_seconds: int = 60,
    max_text_chars: int = 8000,
    max_output_tokens: int = 1500,
    rate_limit_seconds: float = 1.0,
    fingerprint_model: str | None = None,
    catalogue: ProviderCatalogue | None = None,
) -> ModelRouter:
    """Assemble catalogue + ledger + extractor fleet.

    Returns a disabled router (empty fleet) when `router.enabled` is off or no
    provider has a key, so callers can fall back to the single-provider path
    without special-casing.
    """
    cat = catalogue or load_catalogue()
    ledger = QuotaLedger(db_path)
    if not cat.router.enabled:
        return ModelRouter(cat, ledger, [])
    extractors = build_extractors(
        cat,
        temperature=temperature,
        timeout_seconds=timeout_seconds,
        max_text_chars=max_text_chars,
        max_output_tokens=max_output_tokens,
        rate_limit_seconds=rate_limit_seconds,
        fingerprint_model=fingerprint_model,
    )
    if extractors:
        # Debug: the worker's quota check builds one of these between pairs, and
        # at info level this line was ~870 of a day's log entries.
        log.debug("model_router_ready",
                 fleet=[f"{e.provider}:{e.model}" for e in extractors[:6]],
                 total=len(extractors))
    router = ModelRouter(cat, ledger, extractors)
    if cat.router.allow_paid and router.paid_budget_usd <= 0 and router._paid:
        # Visible at startup rather than at the first refused call. `allow_paid`
        # with no `daily_budget_usd` is the state that reads as "paid providers
        # are on" and behaves as "paid providers are off" — worth one loud line
        # either way round.
        log.warning("paid_providers_have_no_budget",
                    providers=sorted(router._paid),
                    hint="set router.daily_budget_usd in providers.yaml")
    return router
