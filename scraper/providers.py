"""Free-tier LLM provider catalogue.

Loads `config/providers.yaml` into typed specs and turns each configured model
into an extractor the rest of the pipeline can call. Every provider here speaks
the OpenAI `/chat/completions` contract, so one generic subclass of
`_ApiExtractor` covers all of them — including Google Gemini, which is reached
through its OpenAI-compatibility endpoint rather than a second client.

Nothing here decides *which* model runs; that is `scraper/router.py`. This module
only answers "what is available, how good is it, and what are its limits".
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

from .extract import _ApiExtractor

log = structlog.get_logger()

PROVIDERS_FILE = "providers.yaml"
# Defined here rather than imported from .config: that module imports .pipeline,
# which imports .router, which imports this one — a cycle. The path is one line.
CONFIG_DIR = Path(__file__).parent.parent / "config"


@dataclass(frozen=True)
class ModelSpec:
    """One model on one provider."""
    model: str
    #: 0-100 prior for this model on our extraction task. A weak prior from
    #: public benchmarks until scripts/score_providers.py measures it on our own
    #: golden set — LLMStructBench found prompting strategy outweighs model size
    #: for JSON extraction, so leaderboard rank is a poor proxy here.
    quality: int
    json_mode: bool = True
    #: List price in USD per million tokens, input and output. Required on a
    #: paid model and meaningless on a free one. `build_extractors` refuses to
    #: build a paid model that has neither: the daily spend guard is only a
    #: guard if every paid call it is guarding can be priced.
    usd_per_1m_in: float = 0.0
    usd_per_1m_out: float = 0.0
    #: Generated-token cap for this model, overriding the provider's and the
    #: global setting. This is capacity, not headroom (see `max_output_tokens`
    #: in extract.py) — but it is capacity *per provider*: Groq reserves
    #: prompt + max_tokens against an 8,000-token minute window before
    #: generating, and a paid endpoint does not. One global number therefore
    #: either starves Groq or truncates every reasoning model, and on
    #: 2026-08-24 it did the second: 28 truncations in 14 pages, each charged
    #: in full.
    max_output_tokens: int | None = None


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key_env: str
    models: tuple[ModelSpec, ...]
    #: Env var holding this provider's base URL, overriding `base_url` when set.
    #:
    #: For an endpoint we host ourselves, the URL is deployment state, not code.
    #: A tunnel to a machine on someone's desk gets a new hostname every time it
    #: reconnects, and config/ is not a persisted volume — so a URL written in
    #: the YAML would make each reconnection a code deploy. This keeps the
    #: provider's *identity* in git and its *address* in the environment, which
    #: is the same split `api_key_env` already makes for its credential.
    base_url_env: str = ""
    rpm: int = 30
    rpd: int = 1000
    #: Tokens per day. 0 = unknown/unlimited. For several free tiers this is the
    #: ceiling that actually binds — Groq allows 14,400 requests a day but only
    #: 200,000 tokens, so a request budget alone plans for capacity that is not
    #: there and the run spends the day being refused.
    tpd: int = 0
    paid: bool = False
    enabled: bool = True
    #: Provider-wide default for its models' generated-token cap. None → the
    #: global `deepseek.max_output_tokens` from settings.yaml.
    max_output_tokens: int | None = None
    #: Most in-flight calls allowed to this provider at once. None = unlimited,
    #: which is correct for every hosted API. Set it to 1 for a model on
    #: hardware we own: there the GPU is the bottleneck, so concurrency buys
    #: nothing and multiplies every call's latency — see `max_concurrency` in
    #: extract.py for what that cost on 2026-09-06.
    max_concurrency: int | None = None
    #: Seconds to wait for this provider's answer, overriding the global
    #: `deepseek.timeout_seconds`. None → the global.
    #:
    #: The global 60 is sized for a hosted API answering in seconds. It is the
    #: wrong number for a model generating on our own GPU, where the same page
    #: is minutes of real work and a timeout is scored as a failure — which
    #: would retire a working provider through the circuit breaker rather than
    #: report that it is merely slow. Raising the *global* instead is the wrong
    #: fix, and an expensive one: it would leave a genuinely hung hosted call
    #: holding a slot for as long as the slowest local model is allowed.
    timeout_seconds: int | None = None

    @property
    def api_key(self) -> str:
        return (os.environ.get(self.api_key_env) or "").strip()

    @property
    def url(self) -> str:
        """The address to call: the env var when set, else the YAML value."""
        return ((os.environ.get(self.base_url_env) or "").strip()
                if self.base_url_env else "") or self.base_url

    @property
    def configured(self) -> bool:
        """A provider with no key is skipped silently — that is the normal
        state for one we have not signed up for, not an error.

        A missing *address* is skipped the same way, and for the same reason: a
        self-hosted provider whose tunnel is down has no URL in the environment,
        and building it would mean every call in the run failing at connect time
        until the circuit breaker retires it. Absent is the honest state."""
        return (self.enabled and bool(self.api_key) and bool(self.models)
                and bool(self.url))

    @property
    def min_interval_s(self) -> float:
        """Seconds to leave between calls to satisfy `rpm`.

        Enforced per *provider*, not per extractor: a provider with two models
        gets two extractors, and each `_ApiExtractor` paces itself with its own
        `_last_request_time`. Two extractors free-running at the shared
        `rate_limit_seconds` (60/min) against a published 15/min is continuous
        429 churn. The ledger owns this because it is the only per-provider
        object in the request path.
        """
        return 60.0 / self.rpm if self.rpm > 0 else 0.0


@dataclass
class RouterSettings:
    enabled: bool = False
    allow_paid: bool = False
    upgrade_min_gain: int = 8
    upgrade_max_per_run: int = 500
    #: Hard ceiling on what every paid provider together may spend in one UTC
    #: day. Reached → paid providers are unavailable until midnight and the
    #: fleet finishes the day on free capacity or stops, which is the ordinary
    #: end of a window rather than an outage.
    #:
    #: **0 means no paid calls at all**, and that is the default on purpose.
    #: `allow_paid: true` was switched on alone on 2026-08-24; the intended
    #: cheap provider had no credit, every call fell through to the expensive
    #: fallback, and nothing in the system had a number that could notice. Four
    #: days, ~$60, ~30 wasted calls per page extracted. A permission to spend
    #: without an amount is not a decision anyone made.
    daily_budget_usd: float = 0.0


@dataclass
class ProviderCatalogue:
    router: RouterSettings = field(default_factory=RouterSettings)
    providers: tuple[ProviderSpec, ...] = ()

    def usable(self, allow_paid: bool | None = None) -> list[ProviderSpec]:
        """Configured providers, paid ones included only when allowed."""
        paid_ok = self.router.allow_paid if allow_paid is None else allow_paid
        return [p for p in self.providers if p.configured and (paid_ok or not p.paid)]


class OpenAICompatExtractor(_ApiExtractor):
    """Generic extractor for any OpenAI-compatible chat endpoint.

    Carries its provider identity and quality score so the router can attribute
    a call to the right quota bucket and stamp the cache row with which model
    produced the result.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        model: str,
        quality: int,
        json_mode: bool = True,
        temperature: float = 0.1,
        timeout_seconds: int = 60,
        max_text_chars: int = 8000,
        max_output_tokens: int = 1500,
        rate_limit_seconds: float = 1.0,
        max_concurrency: int | None = None,
        fingerprint_model: str | None = None,
        usd_per_1m_in: float = 0.0,
        usd_per_1m_out: float = 0.0,
    ):
        super().__init__(
            api_key, model, temperature, timeout_seconds, max_text_chars,
            rate_limit_seconds, fingerprint_model=fingerprint_model,
        )
        self.max_output_tokens = max_output_tokens
        self.max_concurrency = max_concurrency
        self.provider = provider
        self.quality = quality
        self.json_mode = json_mode
        #: List price, read by `_ApiExtractor.last_cost_usd`. Zero on the free
        #: fleet, where the budget is denominated in requests and tokens.
        self.usd_per_1m_in = usd_per_1m_in
        self.usd_per_1m_out = usd_per_1m_out
        self._BASE_URL = base_url.rstrip("/")

    @property
    def priced(self) -> bool:
        return bool(self.usd_per_1m_in or self.usd_per_1m_out)

    def __repr__(self) -> str:  # shows up in run logs
        return f"<{self.provider}:{self.model} q={self.quality}>"


#: Cloudflare and some provider edges reject urllib's default agent.
_UA = "meetapedia-model-check/1.0"


def fetch_upstream_models(spec: ProviderSpec, timeout: int = 20) -> tuple[list[str], str | None]:
    """(model ids served by this provider right now, error).

    Errors are returned, never raised: one dead provider must not hide the
    others' answers. Lives here rather than in the script because the API keys
    only exist on the server, so the deployed app is the only place that can
    ask — see the /v1/models/upstream route.
    """
    import json as _json
    import urllib.error
    import urllib.request

    key = spec.api_key
    if not key:
        return [], "no API key"
    try:
        if spec.name.startswith("gemini"):
            # Google's OpenAI-compat surface has no /models; the native API does.
            # `startswith`, not `==`: Gemini is two catalogue entries since
            # 2026-09-10 (its limits are per model), and an exact match would
            # send the second one to the OpenAI-compat /models it does not have
            # — the same shape of bug the 2026-09-05 review caught for
            # Cloudflare, one provider later.
            url = f"https://generativelanguage.googleapis.com/v1beta/models?key={key}"
            req = urllib.request.Request(url, headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = _json.load(resp)
            return sorted(m["name"].removeprefix("models/")
                          for m in data.get("models", [])), None
        if spec.name == "cloudflare":
            # Cloudflare's OpenAI-compat layer answers 405 on /models; its
            # native API lists them. base_url ends in /ai/v1 and the catalogue
            # sits one level up.
            #
            # Filtered and paged on the *server* rather than trimmed here. A
            # single `per_page=200` looked sufficient — the account serves ~65
            # models — but "looked sufficient" is how a configured model
            # silently becomes UNLISTED, and this function's caller exits 1 on
            # that, which gates the scheduled check.
            root = spec.url.rstrip("/")
            root = root[: -len("/v1")] if root.endswith("/v1") else root
            names: list[str] = []
            page = 1
            while True:
                url = (f"{root}/models/search?task=Text+Generation"
                       f"&per_page=100&page={page}")
                req = urllib.request.Request(
                    url, headers={"User-Agent": _UA,
                                  "Authorization": f"Bearer {key}"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = _json.load(resp)
                batch = data.get("result") or []
                names.extend(m["name"] for m in batch)
                info = data.get("result_info") or {}
                total = info.get("total_count")
                # Stop on a short page as well as on the count: `result_info`
                # is not guaranteed, and an unbounded loop against a paid-ish
                # API is worse than a slightly short list.
                if not batch or total is None or len(names) >= int(total):
                    break
                page += 1
                if page > 20:
                    break
            return sorted(names), None
        req = urllib.request.Request(
            f"{spec.url.rstrip('/')}/models",
            headers={"User-Agent": _UA, "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = _json.load(resp)
        return sorted(m["id"] for m in data.get("data", [])), None
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:120]
        except Exception:
            pass
        return [], f"HTTP {exc.code} {body}"
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _float(value, default: float = 0.0) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


def _opt_int(value) -> int | None:
    """None stays None — "inherit", which is not the same as 0 ("no cap")."""
    if value is None:
        return None
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return None


def _parse_models(raw: list) -> tuple[ModelSpec, ...]:
    out = []
    for entry in raw or []:
        if not isinstance(entry, dict) or not entry.get("model"):
            continue
        try:
            quality = int(entry.get("quality", 0))
        except (TypeError, ValueError):
            quality = 0
        out.append(ModelSpec(
            model=str(entry["model"]),
            quality=max(0, min(100, quality)),
            json_mode=bool(entry.get("json_mode", True)),
            usd_per_1m_in=_float(entry.get("usd_per_1m_in")),
            usd_per_1m_out=_float(entry.get("usd_per_1m_out")),
            max_output_tokens=_opt_int(entry.get("max_output_tokens")),
        ))
    # Best first, so a provider's own preference order never has to be trusted.
    return tuple(sorted(out, key=lambda m: -m.quality))


def load_catalogue(config_dir: Path | None = None) -> ProviderCatalogue:
    """Read config/providers.yaml. A missing or malformed file yields a disabled
    router rather than an exception — the single-provider DeepSeek path must
    keep working if this config is absent."""
    path = (config_dir or CONFIG_DIR) / PROVIDERS_FILE
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return ProviderCatalogue()
    except Exception as exc:
        log.warning("providers_config_unreadable", path=str(path), error=str(exc))
        return ProviderCatalogue()

    router_raw = raw.get("router") or {}
    router = RouterSettings(
        enabled=bool(router_raw.get("enabled", False)),
        allow_paid=bool(router_raw.get("allow_paid", False)),
        upgrade_min_gain=int(router_raw.get("upgrade_min_gain", 8) or 0),
        upgrade_max_per_run=int(router_raw.get("upgrade_max_per_run", 500) or 0),
        daily_budget_usd=_float(router_raw.get("daily_budget_usd")),
    )

    providers = []
    for entry in raw.get("providers") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        models = _parse_models(entry.get("models"))
        if not models:
            continue
        providers.append(ProviderSpec(
            name=str(entry["name"]),
            base_url=str(entry.get("base_url") or ""),
            base_url_env=str(entry.get("base_url_env") or ""),
            api_key_env=str(entry.get("api_key_env") or ""),
            models=models,
            rpm=int(entry.get("rpm", 30) or 30),
            rpd=int(entry.get("rpd", 1000) or 1000),
            tpd=int(entry.get("tpd", 0) or 0),
            paid=bool(entry.get("paid", False)),
            enabled=bool(entry.get("enabled", True)),
            max_output_tokens=_opt_int(entry.get("max_output_tokens")),
            timeout_seconds=_opt_int(entry.get("timeout_seconds")),
            max_concurrency=_opt_int(entry.get("max_concurrency")),
        ))
    return ProviderCatalogue(router=router, providers=tuple(providers))


def build_extractors(
    catalogue: ProviderCatalogue,
    *,
    temperature: float = 0.1,
    timeout_seconds: int = 60,
    max_text_chars: int = 8000,
    max_output_tokens: int = 1500,
    rate_limit_seconds: float = 1.0,
    fingerprint_model: str | None = None,
    allow_paid: bool | None = None,
) -> list[OpenAICompatExtractor]:
    """One extractor per (provider, model), ordered best-quality first.

    `fingerprint_model` is passed to every one of them on purpose: the whole
    fleet must produce cache entries under a single fingerprint, or switching
    models would silently invalidate the entire extraction cache and re-pay for
    work already done. Which model actually ran is recorded in
    `cache_pages.extract_model`, outside any cache key.
    """
    out: list[OpenAICompatExtractor] = []
    for spec in catalogue.usable(allow_paid=allow_paid):
        for m in spec.models:
            if spec.paid and not (m.usd_per_1m_in or m.usd_per_1m_out):
                # Fail closed. An unpriced paid model spends real money and
                # reports 0.00 against the daily budget, so the guard would
                # wave through exactly the runaway it exists to stop. A missing
                # price is a config bug with a one-line fix; a silent one is
                # what 2026-08-24 cost.
                log.error("paid_model_has_no_price", provider=spec.name,
                          model=m.model,
                          hint="add usd_per_1m_in/usd_per_1m_out in providers.yaml")
                continue
            out.append(OpenAICompatExtractor(
                provider=spec.name,
                base_url=spec.url,
                api_key=spec.api_key,
                model=m.model,
                quality=m.quality,
                json_mode=m.json_mode,
                temperature=temperature,
                # Per provider, then the global setting. Same precedence as
                # max_output_tokens below, and for the same reason: one number
                # cannot serve a hosted API and a local GPU at once.
                timeout_seconds=spec.timeout_seconds or timeout_seconds,
                max_text_chars=max_text_chars,
                # Per model, then per provider, then the global setting.
                max_output_tokens=(m.max_output_tokens
                                   or spec.max_output_tokens
                                   or max_output_tokens),
                rate_limit_seconds=rate_limit_seconds,
                max_concurrency=spec.max_concurrency,
                fingerprint_model=fingerprint_model,
                usd_per_1m_in=m.usd_per_1m_in,
                usd_per_1m_out=m.usd_per_1m_out,
            ))
    out.sort(key=lambda e: -e.quality)
    return out
