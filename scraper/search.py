import asyncio
import json
import weakref

import structlog
import httpx

from .models import SearchResult

log = structlog.get_logger()

#: One HTTP client for the whole process, created lazily inside the event loop.
#:
#: Every request used to open its own — and `_search_standard` opened one *per
#: poll*, so a single search at normal priority could open 150. With eight
#: searches in flight the container ran out of file descriptors on 2026-08-18:
#: `providers.yaml` became unreadable and SQLite could not open the database,
#: from socket exhaustion three levels away. A pooled client also reuses
#: connections to the same host, which is what an API client should do anyway.
#: Keyed by event loop, not global: a client holds connections belonging to the
#: loop that created them, so reusing one across loops is wrong — and it is what
#: made the first version of this leak into other tests.
#:
#: Keyed by the loop *object* in a WeakKeyDictionary, not by `id(loop)`. An id is
#: only unique while its object is alive: CPython reuses the address once a loop
#: is collected, so a fresh loop can be handed the dead one's client. In
#: production there is one loop and that never fires; in the suite there is one
#: per test, and it fired — a client built under a monkeypatched
#: `httpx.AsyncClient` (i.e. some earlier test's fake) was served to a later
#: test, which then failed on `client.is_closed` or on a payload its own fake
#: never saw. Two tests in `test_search.py` were order-dependent for exactly
#: this reason. The weak key also means the entry disappears with the loop
#: instead of pinning it forever.
_shared_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = (
    weakref.WeakKeyDictionary())


def shared_client() -> "httpx.AsyncClient":
    key = asyncio.get_running_loop()
    client = _shared_clients.get(key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16),
        )
        _shared_clients[key] = client
    return client



class SearchQuotaError(Exception):
    """Raised when the search API returns a rate-limit or payment-required error."""


class SearchUnavailableError(Exception):
    """Raised when a search could not run (network error, 5xx, bad response,
    queue timeout). Callers MUST NOT record the pair as searched — caching an
    empty result here would permanently mark it done without ever searching."""


# DataForSEO location codes: https://api.dataforseo.com/v3/serp/google/locations
# (GET endpoint returns the full list; these are the most common ones)
LOCALE_TO_DATAFORSEO_LOCATION: dict[str, int] = {
    "hu": 2348,   # Hungary
    "de": 2276,   # Germany
    "fr": 2250,   # France
    "it": 2380,   # Italy
    "es": 2724,   # Spain
    "nl": 2528,   # Netherlands
    "pl": 2616,   # Poland
    "sv": 2752,   # Sweden
    "da": 2208,   # Denmark
    "fi": 2246,   # Finland
    "no": 2578,   # Norway
    "cs": 2203,   # Czech Republic
    "ro": 2642,   # Romania
    "tr": 2792,   # Turkey
    "ru": 2643,   # Russia
    "uk": 2804,   # Ukraine
    "pt": 2076,   # Brazil
    "en": 2840,   # United States (default for English)
    "sk": 2703,   # Slovakia
    "sr": 2688,   # Serbia
    "hr": 2191,   # Croatia
    "sl": 2705,   # Slovenia
    "bg": 2100,   # Bulgaria
    "lv": 2428,   # Latvia
    "et": 2233,   # Estonia
    "lt": 2440,   # Lithuania
    "el": 2300,   # Greece
    "ja": 2392,   # Japan
    "ko": 2410,   # South Korea
    "zh": 2156,   # China (Taipei rides along; keyword carries the city)
    "id": 2360,   # Indonesia
}

#: Posted DataForSEO tasks not yet collected: request -> (task id, posted at).
#: DataForSEO keeps a task's result for days, so a resumed poll costs nothing.
_PENDING_TASKS: dict[str, tuple[str, float]] = {}
_PENDING_TASK_MAX_AGE_S = 24 * 3600
#: task_get statuses that mean "still working": 0 is an empty or unreadable
#: answer, 40601 "Task Handed", 40602 "Task in Queue".
_TASK_PENDING_STATUSES = (0, 40601, 40602)


class DataForSEOClient:
    """DataForSEO Google Organic SERP.

    Two modes:
    - "live" (default): live/regular endpoint, $2/1K queries, instant.
    - "standard": task_post + task_get polling. Normal priority costs $0.6/1K
      but can outlive the five-minute client timeout; production uses high
      priority at $1.2/1K (normally <=1 minute). Keep "live" for enrichment.

    Works reliably from datacenter IPs; raises SearchQuotaError on depleted credits.
    Auth: Basic HTTP with login:password (base64-encoded).
    """

    _BASE = "https://api.dataforseo.com/v3/serp/google/organic/live/regular"
    _TASK_POST = "https://api.dataforseo.com/v3/serp/google/organic/task_post"
    _TASK_GET = "https://api.dataforseo.com/v3/serp/google/organic/task_get/regular"
    _STANDARD_POLL_SECONDS = 10.0
    #: How long to wait for a queued task, by priority. DataForSEO publishes
    #: ~1 minute for the priority queue and ~5 minutes for the normal one, with
    #: a stated *target* of 45 minutes for the latter — so the old flat 300s
    #: made normal priority unusable and locked us into paying $1.2/1K instead
    #: of $0.6. Waiting longer only pays off when the waits overlap, which is
    #: what `pipeline.search_concurrency` is for.
    _STANDARD_TIMEOUT_SECONDS = 300.0
    _NORMAL_PRIORITY_TIMEOUT_SECONDS = 1500.0

    def __init__(self, login: str, password: str, rate_limit_seconds: float = 1.0,
                 mode: str = "live", standard_priority: int = 1):
        import base64
        self.rate_limit_seconds = rate_limit_seconds
        self.mode = mode if mode in ("live", "standard") else "live"
        self.standard_priority = 2 if standard_priority == 2 else 1
        self._last_request_time: float = 0.0
        raw = f"{login}:{password}".encode()
        self._auth_header = f"Basic {base64.b64encode(raw).decode()}"

    async def search(
        self,
        query: str,
        locale: str = "en",
        num_results: int = 10,
    ) -> list[SearchResult]:
        await self._rate_limit()
        locale = str(locale)  # guard against PyYAML parsing "no" as bool False
        # DataForSEO uses bare ISO 639-1 language codes
        lang = locale.split("-")[0] if "-" in locale else locale
        task: dict = {"keyword": query, "language_code": lang, "depth": min(num_results, 100)}
        location_code = LOCALE_TO_DATAFORSEO_LOCATION.get(lang)
        if location_code:
            task["location_code"] = location_code
        elif self.mode == "standard":
            # task_post REQUIRES a location (rejects with 40501 "Invalid Field:
            # 'location_name'" without one — the 2026-07 outage); live tolerates
            # the omission. An unmapped locale is likely an invalid language_code
            # too, which task_post would equally reject — default both to US/en
            # so the pair degrades instead of being permanently poisoned.
            log.warning("dataforseo_locale_unmapped", locale=lang, query=query)
            task["location_code"] = 2840
            task["language_code"] = "en"
        if self.mode == "standard":
            return await self._search_standard(task, query, num_results)
        payload = [task]
        headers = {"Authorization": self._auth_header, "Content-Type": "application/json"}
        try:
            client = shared_client()
            resp = await client.post(self._BASE, json=payload, headers=headers)
        except Exception as exc:
            log.warning("dataforseo_request_failed", query=query, error=str(exc))
            raise SearchUnavailableError(f"DataForSEO request failed: {exc}") from exc

        if resp.status_code == 402:
            raise SearchQuotaError("DataForSEO: insufficient credits (HTTP 402)")
        if resp.status_code == 429:
            raise SearchQuotaError("DataForSEO: rate limited (HTTP 429)")
        if resp.status_code >= 400:
            log.warning("dataforseo_http_error", query=query, status=resp.status_code)
            raise SearchUnavailableError(f"DataForSEO HTTP {resp.status_code}")

        try:
            data = resp.json()
        except Exception as exc:
            log.warning("dataforseo_bad_json", query=query)
            raise SearchUnavailableError("DataForSEO returned invalid JSON") from exc

        top = data.get("status_code", 0)
        if top == 40201:
            raise SearchQuotaError("DataForSEO: insufficient credits (40201)")
        if top not in (20000, 20100):
            log.warning("dataforseo_api_error", query=query, status_code=top,
                        message=data.get("status_message", ""))
            raise SearchUnavailableError(f"DataForSEO API status {top}")

        results = self._parse_tasks(data)
        log.info("dataforseo_results", query=query, found=len(results))
        return results[:num_results]

    @staticmethod
    def _parse_tasks(data: dict) -> list[SearchResult]:
        results: list[SearchResult] = []
        for task in data.get("tasks", []):
            task_status = task.get("status_code", 0)
            if task_status == 40201:
                raise SearchQuotaError("DataForSEO: task quota exhausted (40201)")
            if task_status not in (20000, 20100):
                # A rejected task inside an otherwise-20000 response is a
                # provider failure, not an empty SERP — returning [] here would
                # cache it as a successful empty search forever.
                raise SearchUnavailableError(
                    f"DataForSEO task status {task_status} "
                    f"{task.get('status_message', '')}".strip())
            for result in task.get("result") or []:
                for item in result.get("items") or []:
                    if item.get("type") != "organic":
                        continue
                    url = item.get("url", "")
                    if not url:
                        continue
                    results.append(SearchResult(
                        url=url,
                        title=item.get("title") or "",
                        snippet=item.get("description") or "",
                    ))
        return results

    async def _search_standard(self, task: dict, query: str,
                               num_results: int) -> list[SearchResult]:
        """Queue-mode search: task_post ($0.6/1K) then poll task_get until done.

        DataForSEO charges at task_post. A task still queued when the poll
        window closes used to be forgotten, so the next pass posted — and paid
        for — the same search again. It is remembered per process and resumed.
        """
        import time
        headers = {"Authorization": self._auth_header, "Content-Type": "application/json"}
        key = json.dumps(task, sort_keys=True)
        pending = _PENDING_TASKS.get(key)
        if pending and time.time() - pending[1] < _PENDING_TASK_MAX_AGE_S:
            task_id = pending[0]
            log.info("dataforseo_task_resumed", query=query, task_id=task_id)
        else:
            task_id = await self._post_task(task, query, headers)
            _PENDING_TASKS[key] = (task_id, time.time())

        budget = (self._STANDARD_TIMEOUT_SECONDS if self.standard_priority >= 2
                  else self._NORMAL_PRIORITY_TIMEOUT_SECONDS)
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            await asyncio.sleep(self._STANDARD_POLL_SECONDS)
            try:
                client = shared_client()
                poll = await client.get(f"{self._TASK_GET}/{task_id}", headers=headers)
                pdata = poll.json()
            except Exception as exc:
                log.warning("dataforseo_task_get_failed", query=query, error=str(exc))
                continue
            ptasks = pdata.get("tasks") or []
            status = ptasks[0].get("status_code", 0) if ptasks else 0
            if status in _TASK_PENDING_STATUSES:
                continue
            _PENDING_TASKS.pop(key, None)
            if status == 20000:
                results = self._parse_tasks(pdata)
                log.info("dataforseo_results", query=query, found=len(results), mode="standard")
                return results[:num_results]
            if status == 40102:
                # "No Search Results": a finished, empty SERP — a result, and
                # cacheable. It used to keep polling for the whole window, then
                # raise, so the empty search was bought again every run.
                log.info("dataforseo_results", query=query, found=0, mode="standard")
                return []
            if status == 40201:
                raise SearchQuotaError("DataForSEO: task quota exhausted (40201)")
            message = ptasks[0].get("status_message", "") if ptasks else ""
            raise SearchUnavailableError(f"DataForSEO task status {status} {message}".strip())
        log.warning("dataforseo_task_timeout", query=query, task_id=task_id,
                    waited_s=round(budget), priority=self.standard_priority,
                    note="kept; the next attempt resumes it instead of paying again")
        raise SearchUnavailableError("DataForSEO standard task timed out")

    async def _post_task(self, task: dict, query: str, headers: dict) -> str:
        try:
            client = shared_client()
            resp = await client.post(
                self._TASK_POST,
                json=[{**task, "priority": self.standard_priority}],
                headers=headers,
            )
        except Exception as exc:
            log.warning("dataforseo_task_post_failed", query=query, error=str(exc))
            raise SearchUnavailableError(f"DataForSEO task_post failed: {exc}") from exc
        if resp.status_code in (402, 429):
            raise SearchQuotaError(f"DataForSEO: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except Exception as exc:
            raise SearchUnavailableError("DataForSEO task_post returned invalid JSON") from exc
        if data.get("status_code") == 40201:
            raise SearchQuotaError("DataForSEO: insufficient credits (40201)")
        tasks = data.get("tasks") or []
        task_id = tasks[0].get("id") if tasks else None
        if tasks and tasks[0].get("status_code") == 40201:
            raise SearchQuotaError("DataForSEO: task quota exhausted (40201)")
        if tasks and tasks[0].get("status_code") not in (20000, 20100):
            # Rejected tasks still carry an id but never enter the queue —
            # polling one wastes the full 5-minute window (28.8K junk task_get
            # calls during the 2026-07 outage). Fail fast with the API's reason.
            status = tasks[0].get("status_code")
            message = tasks[0].get("status_message", "")
            log.warning("dataforseo_task_post_rejected", query=query,
                        status=status, message=message)
            raise SearchUnavailableError(
                f"DataForSEO task_post rejected: {status} {message}".strip())
        if not task_id:
            log.warning("dataforseo_task_post_no_id", query=query,
                        status=data.get("status_code"))
            raise SearchUnavailableError("DataForSEO task_post returned no task id")
        return task_id

    async def search_all(
        self,
        queries: list[str],
        locale: str = "en",
        num_results: int = 10,
    ) -> list[SearchResult]:
        seen_urls: set[str] = set()
        combined: list[SearchResult] = []
        for query in queries:
            for r in await self.search(query, locale=locale, num_results=num_results):
                if r.url not in seen_urls:
                    seen_urls.add(r.url)
                    combined.append(r)
        return combined

    async def _rate_limit(self) -> None:
        import time
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < self.rate_limit_seconds:
            await asyncio.sleep(self.rate_limit_seconds - elapsed)
        self._last_request_time = time.monotonic()


class FallbackSearchClient:
    """Tries primaries left-to-right. Providers are marked permanently exhausted
    on quota error — retrying won't help if credits are gone. (Currently the only
    provider is DataForSEO; the wrapper stays for the per-run exhaustion state and
    so a second provider can be added back with one line.)
    """

    def __init__(self, primaries: list):
        self.primaries = primaries
        self._blocked_until: list[float] = [0.0] * len(primaries)  # inf = exhausted
        self._blocked_reason: list[str | None] = [None] * len(primaries)
        self._unavailable_count: list[int] = [0] * len(primaries)
        # Original provider error that caused exhaustion — surfaced in the daily
        # report so "provider down" is diagnosable from the email alone.
        self.failure_reason: str | None = (
            None if primaries
            else "no search provider configured (DATAFORSEO_LOGIN/PASSWORD missing)")

    def _is_provider_blocked(self, i: int) -> bool:
        import time
        return time.monotonic() < self._blocked_until[i]

    @property
    def exhausted(self) -> bool:
        """True when no provider is configured or every provider hit a quota
        error this run. Callers must skip searching (and must NOT record an
        empty search_cache entry — the pair was not actually searched)."""
        return not self.primaries or all(b == float("inf") for b in self._blocked_until)

    def _block_provider(self, i: int, primary, reason: str) -> None:
        self._blocked_until[i] = float("inf")
        self._blocked_reason[i] = "quota"
        self.failure_reason = reason
        log.warning("search_quota_exhausted", provider=type(primary).__name__)

    def _record_unavailable(self, i: int, primary, reason: str) -> None:
        """Disable a provider after three faults; tolerate up to two in a row."""
        self._unavailable_count[i] += 1
        if self._unavailable_count[i] >= 3:
            self._blocked_until[i] = float("inf")
            self._blocked_reason[i] = "unavailable"
            self.failure_reason = reason
            log.warning("search_provider_disabled_for_run",
                        provider=type(primary).__name__, reason="repeated_unavailability")

    def _raise_exhausted(self) -> None:
        detail = f" ({self.failure_reason})" if self.failure_reason else ""
        if any(reason == "unavailable" for reason in self._blocked_reason):
            raise SearchUnavailableError(f"search providers unavailable for this run{detail}")
        raise SearchQuotaError(f"no search provider available{detail}")

    async def search(self, query: str, locale: str = "en",
                     num_results: int = 10) -> list[SearchResult]:
        if self.exhausted:
            self._raise_exhausted()
        hit_transient = False
        last_error = ""
        for i, primary in enumerate(self.primaries):
            if self._is_provider_blocked(i):
                continue
            try:
                result = await primary.search(query, locale=locale, num_results=num_results)
                self._unavailable_count[i] = 0
                return result
            except SearchQuotaError as exc:
                log.warning("search_quota_error", provider=type(primary).__name__, reason=str(exc))
                self._block_provider(i, primary, str(exc))
                last_error = str(exc)
            except SearchUnavailableError as exc:
                log.warning("search_unavailable", provider=type(primary).__name__, reason=str(exc))
                self._record_unavailable(i, primary, str(exc))
                hit_transient = True
                last_error = str(exc)
            except Exception as exc:  # see search_all: untyped bugs are transient, never empty
                log.exception("search_unexpected_error", provider=type(primary).__name__,
                              error=str(exc))
                self._record_unavailable(i, primary, f"{type(exc).__name__}: {exc}")
                hit_transient = True
                last_error = f"{type(exc).__name__}: {exc}"
        if hit_transient:
            raise SearchUnavailableError(f"search failed before any result ({last_error})")
        return []

    async def search_all(self, queries: list[str], locale: str = "en",
                         num_results: int = 10,
                         stop_after: int | None = None,
                         usable=None) -> list[SearchResult]:
        """Run queries left-to-right across providers, deduplicating by URL.

        stop_after: stop issuing further (paid) queries once this many unique
        results have been collected. The pipeline caps fetched pages anyway, so
        extra queries past that point only cost money. None = run all queries.
        usable: optional `url -> bool`; only URLs it accepts count toward
        `stop_after`. A blocked social URL will never be fetched, so counting
        it bought a second query for 40% of pairs whose extra results were
        never used.

        Failover semantics: quota error blocks the provider and the *remaining*
        queries move to the next one (already-collected results are kept). If a
        provider ran all remaining queries and the combined total is still empty,
        the next provider retries the full set.
        """
        if self.exhausted:
            self._raise_exhausted()

        combined: list[SearchResult] = []
        seen_urls: set[str] = set()
        remaining = list(queries)
        hit_quota = False
        hit_transient = False
        last_error = ""

        for i, primary in enumerate(self.primaries):
            if self._is_provider_blocked(i) or not remaining:
                continue
            provider_done: list[str] = []
            try:
                for query in remaining:
                    if stop_after is not None and sum(
                            1 for r in combined
                            if usable is None or usable(r.url)) >= stop_after:
                        log.info("search_stop_after_reached", collected=len(combined),
                                 skipped_queries=len(remaining) - len(provider_done))
                        return combined
                    for r in await primary.search(query, locale=locale, num_results=num_results):
                        if r.url not in seen_urls:
                            seen_urls.add(r.url)
                            combined.append(r)
                    self._unavailable_count[i] = 0
                    provider_done.append(query)
            except SearchQuotaError as exc:
                log.warning("search_quota_error", provider=type(primary).__name__, reason=str(exc))
                self._block_provider(i, primary, str(exc))
                hit_quota = True
                last_error = str(exc)
                remaining = [q for q in remaining if q not in provider_done]
                continue
            except SearchUnavailableError as exc:
                log.warning("search_unavailable", provider=type(primary).__name__, reason=str(exc))
                # One task/network failure can be transient. Repeated failures
                # disable the provider so a stuck queue cannot consume the whole
                # collector window one pair at a time.
                self._record_unavailable(i, primary, str(exc))
                hit_transient = True
                last_error = str(exc)
                remaining = [q for q in remaining if q not in provider_done]
                continue
            except Exception as exc:
                # The search-side twin of the extractor's net: a response shape the
                # parser never anticipated (a bare array, a null task list) raises
                # an untyped AttributeError/TypeError/ValidationError that would
                # otherwise unwind run_pipeline and kill the collector window from
                # one bad pair. See docs/wiki 2026-07-llm-bare-array-run-abort.
                # Treated as transient: not cached, provider blocked if it repeats.
                log.exception("search_unexpected_error", provider=type(primary).__name__,
                              error=str(exc))
                self._record_unavailable(i, primary, f"{type(exc).__name__}: {exc}")
                hit_transient = True
                last_error = f"{type(exc).__name__}: {exc}"
                remaining = [q for q in remaining if q not in provider_done]
                continue
            if combined:
                return combined
            log.info("search_empty_try_next", provider=type(primary).__name__, queries=queries)
            # total empty → let the next provider retry the full set
            remaining = list(queries)
        if not combined and hit_quota:
            # Nothing was successfully searched — this is a provider failure,
            # not a legitimate empty result. Callers must not cache it.
            raise SearchQuotaError(f"search providers exhausted before any result ({last_error})")
        if not combined and hit_transient:
            raise SearchUnavailableError(f"search failed before any result ({last_error})")
        return combined


def build_queries(
    city_name: str,
    search_variants: list[str],
    topic_terms: list[str],
) -> list[str]:
    if not city_name or not topic_terms:
        return []

    queries = []
    variants = search_variants or [city_name]
    primary_variant = variants[0]
    for term in topic_terms[:2]:
        queries.append(f"{term} {primary_variant}")
    if len(variants) > 1:
        queries.append(f"{topic_terms[0]} {variants[1]}")
    return queries
