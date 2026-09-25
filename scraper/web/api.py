"""Public OpenAI-compatible gateway over the free-tier model router.

Lets other software call "one LLM endpoint, routed across free providers" with
whatever OpenAI SDK it already uses — only `base_url` and `api_key` change:

    from openai import OpenAI
    client = OpenAI(base_url="https://meetapedia.com/v1", api_key="<ROUTER_API_KEY>")
    client.chat.completions.create(model="auto", messages=[...])

Why OpenAI's shape and not our own: every language already has a maintained
client for it, so integration cost is a config line instead of a new SDK. The
response body is the upstream provider's, forwarded intact — `id`, `usage`,
`finish_reason` and all — with two additive fields (`x_router`) naming what
actually served the request. Additive fields are ignored by strict clients.

Deliberately general purpose: no prompt, schema or persona of this project is
injected. The caller's `messages` go upstream as-is, so this is a plain LLM
endpoint that happens to be free and routed — not a community-extraction API.

Full contract: docs/wiki/pages/integrations/router-gateway-api.md.
"""
from __future__ import annotations

import asyncio
import hmac
import os
import time
from typing import Any

import structlog
from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ..extract import (ExtractorQuotaError, ExtractorRateLimitError,
                       ExtractorUnavailableError)
from .state import app_state

log = structlog.get_logger()

router = APIRouter(prefix="/v1", tags=["gateway"])

#: Comma-separated list, so callers can be revoked individually. Unset → the
#: gateway is off entirely rather than open: an unauthenticated LLM proxy is a
#: free-credit faucet for anyone who finds it.
_API_KEYS_ENV = "ROUTER_API_KEY"

#: Hard ceiling on messages accepted in one request. Not a quality judgement —
#: an unbounded list is an easy way to burn a provider's token budget.
_MAX_MESSAGES = 60
_MAX_CHARS = 200_000


def _valid_keys() -> list[str]:
    return [k.strip() for k in (os.environ.get(_API_KEYS_ENV) or "").split(",") if k.strip()]


#: Separate key for the operator endpoints under /v1/control. These start and
#: stop pipeline runs, which is a different kind of authority from asking a
#: model a question, and the gateway key is handed to other software.
_CONTROL_KEYS_ENV = "CONTROL_API_KEY"


def _control_keys() -> list[str]:
    """Keys accepted for /v1/control. Falls back to the gateway keys.

    The fallback keeps the endpoints usable the moment they ship, but it does
    mean anything holding a gateway key can drive the pipeline — so it warns.
    Setting CONTROL_API_KEY separates the two authorities.
    """
    keys = [k.strip() for k in (os.environ.get(_CONTROL_KEYS_ENV) or "").split(",") if k.strip()]
    if keys:
        return keys
    fallback = _valid_keys()
    if fallback:
        log.warning("control_api_using_gateway_key",
                    hint=f"set {_CONTROL_KEYS_ENV} to separate operator access "
                         "from model access")
    return fallback


def _control_authorized(authorization: str | None) -> bool:
    keys = _control_keys()
    if not keys:
        return False
    token = authorization[7:].strip() if (
        authorization and authorization.lower().startswith("bearer ")) else ""
    token_b = token.encode("utf-8", "surrogatepass")
    return any(hmac.compare_digest(token_b, k.encode("utf-8")) for k in keys)


def _authorized(authorization: str | None) -> bool:
    keys = _valid_keys()
    if not keys:
        return False
    token = ""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    # Compare as bytes: hmac.compare_digest raises TypeError on a non-ASCII str,
    # so `Authorization: Bearer héllo` would 500 with a stack trace instead of
    # returning the 401 it deserves.
    token_b = token.encode("utf-8", "surrogatepass")
    # compare_digest against every key: constant-time per comparison, and the
    # loop length depends on configuration, not on the submitted token.
    return any(hmac.compare_digest(token_b, k.encode("utf-8")) for k in keys)


def _error(status: int, message: str, err_type: str, code: str | None = None) -> JSONResponse:
    """OpenAI's error envelope — clients parse `error.type` / `error.code`."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": err_type,
                           "param": None, "code": code}},
    )


def _build_router():
    """A router bound to the live app config, or None when unavailable."""
    from ..router import build_router as _bld
    cfg = app_state.pipeline_cfg
    if cfg is None:
        return None
    return _bld(
        app_state.db_path,
        temperature=cfg.deepseek_temperature,
        timeout_seconds=cfg.deepseek_timeout,
        max_text_chars=cfg.deepseek_max_text_chars,
        rate_limit_seconds=cfg.deepseek_rate_limit_seconds,
        fingerprint_model=cfg.deepseek_fingerprint_model or cfg.deepseek_model,
    )


def _select(fleet: list, requested: str) -> list:
    """Order the fleet for one request according to the `model` field.

    Accepted values:
      "auto" / "" / "default"  → best available (the point of the gateway)
      "<provider>:<model>"     → that exact model, no substitution
      "<provider>"             → best model on that provider
      "<model>"                → that model wherever it lives

    An explicit choice returns a one-element list on purpose: a caller who named
    a model wants that model, and silently answering from another one would make
    their evaluation meaningless.
    """
    want = (requested or "auto").strip()
    if want.lower() in ("auto", "", "default", "router"):
        return fleet
    # Exact model id first: OpenRouter ids end in ":free", so splitting on the
    # colon before trying a whole-id match turns "qwen/qwen3-235b-a22b:free"
    # into provider "qwen/qwen3-235b-a22b" and a bogus 404.
    exact = [e for e in fleet if e.model == want]
    if exact:
        return exact[:1]
    by_provider = [e for e in fleet if e.provider == want]
    if by_provider:
        return by_provider[:1]
    if ":" in want:
        provider, _, model = want.partition(":")
        return [e for e in fleet if e.provider == provider and e.model == model]
    return []


@router.get("/models")
async def list_models(authorization: str | None = Header(default=None)):
    """OpenAI `/v1/models`: what this gateway can route to today.

    Lists models with **daily budget** left, not models callable this
    millisecond. `order()` also excludes anything inside its rpm cooldown, which
    at rpm 30 is a two-second window — a client polling this endpoint would see
    most of the fleet blink in and out for no reason it could act on. Same
    distinction the completions route makes.

    `owned_by` carries the provider; the non-standard `quality` field exposes
    the routing score. `paced` flags a model that is momentarily on cooldown but
    otherwise available.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    mr = _build_router()
    if mr is None or not mr.enabled:
        return _error(503, "Model router is not configured.", "server_error",
                      "router_disabled")
    now = int(time.time())
    callable_now = {id(e) for e in mr.order()}
    data = [{
        "id": f"{e.provider}:{e.model}",
        "object": "model",
        "created": now,
        "owned_by": e.provider,
        "quality": e.quality,
        "paced": id(e) not in callable_now,
    } for e in mr.with_budget()]
    # "auto" is the model most callers should ask for, so advertise it first.
    data.insert(0, {"id": "auto", "object": "model", "created": now,
                    "owned_by": "router",
                    "quality": mr.best_available_quality()})
    return {"object": "list", "data": data}


@router.get("/models/upstream")
async def models_upstream(authorization: str | None = Header(default=None)):
    """What each provider *actually serves* right now, next to what we configured.

    Distinct from `/v1/models`, which lists what the router can route to today —
    that view hides a provider whose daily budget is spent or one parked behind
    `allow_paid`, and a checker reading it concludes the models were retired.
    This asks the providers themselves, which is the only way to answer "is
    there a new free model" at all.

    Keys live only on the server, so this route is the only place the question
    can be asked from.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    from ..providers import fetch_upstream_models, load_catalogue

    catalogue = load_catalogue()
    out = []
    for spec in catalogue.providers:
        upstream, err = await asyncio.to_thread(fetch_upstream_models, spec)
        configured = [m.model for m in spec.models]
        out.append({
            "provider": spec.name,
            "enabled": spec.enabled,
            "paid": spec.paid,
            "error": err,
            "configured": configured,
            "upstream": upstream,
            # Only meaningful when we actually got a list back.
            "gone": [m for m in configured if m not in upstream] if upstream else [],
            "new": [m for m in upstream if m not in configured],
        })
    return {"object": "list", "data": out}


@router.get("/quota")
async def quota(authorization: str | None = Header(default=None)):
    """Non-standard: today's per-provider budget, for callers that want to back
    off before being rate limited rather than after."""
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    mr = _build_router()
    if mr is None:
        return _error(503, "Model router is not configured.", "server_error",
                      "router_disabled")
    return {
        "object": "list",
        "day": mr.ledger.day,
        "data": [{k: p[k] for k in
                  ("name", "configured", "best_quality", "budget", "used",
                   "remaining", "blocked", "paid")}
                 for p in mr.ledger.snapshot(mr.catalogue)],
    }


# ── Engine-room board ─────────────────────────────────────────────────────────
# Where the Claude sessions that operate this deployment leave each other
# messages without the operator relaying them: the server-side session and the
# one on the local GPU machine. Accepts the operator key or LOCAL_GPU_KEY — the
# GPU machine already holds the latter, so no new secret has to be handed out.


def _board_authorized(authorization: str | None) -> bool:
    if _control_authorized(authorization):
        return True
    gpu_key = (os.environ.get("LOCAL_GPU_KEY") or "").strip()
    token = authorization[7:].strip() if (
        authorization and authorization.lower().startswith("bearer ")) else ""
    return bool(gpu_key) and hmac.compare_digest(
        token.encode("utf-8", "surrogatepass"), gpu_key.encode("utf-8"))


@router.get("/board")
async def board_read(since: int = 0, authorization: str | None = Header(default=None)):
    """State plus messages after `since` (a message id), oldest first."""
    if not _board_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    from ..db import get_board_messages, get_board_state
    db = app_state.db_path
    messages = await asyncio.to_thread(get_board_messages, db, since) if db else []
    state = await asyncio.to_thread(get_board_state, db) if db else None
    return {"object": "board", "state": state, "messages": messages}


@router.post("/board/messages")
async def board_post(request: Request, authorization: str | None = Header(default=None)):
    """Body: {"author": "szerver"|"gpu", "text": "..."}"""
    if not _board_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    from ..db import add_board_message
    try:
        body = await request.json()
        author = str(body.get("author", ""))
        if author == "andras":
            raise ValueError("the operator posts from /admin/board")
        message = await asyncio.to_thread(
            add_board_message, app_state.db_path, author, str(body.get("text", "")))
    except (ValueError, AttributeError, TypeError) as exc:
        return _error(400, str(exc), "invalid_request_error")
    return {"object": "board.message", **message}


@router.put("/board/state")
async def board_state(request: Request, authorization: str | None = Header(default=None)):
    """Body: {"author": "szerver"|"gpu", "text": "..."} — replaces the one state line."""
    if not _board_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    from ..db import set_board_state
    try:
        body = await request.json()
        state = await asyncio.to_thread(
            set_board_state, app_state.db_path, str(body.get("author", "")),
            str(body.get("text", "")))
    except (ValueError, AttributeError, TypeError) as exc:
        return _error(400, str(exc), "invalid_request_error")
    return {"object": "board.state", **state}


# ── Operator control ──────────────────────────────────────────────────────────
# Deliberately NOT part of the OpenAI-compatible surface. /v1/chat/completions
# and /v1/models are a published interface: other software depends on their
# shape and it cannot be changed cheaply. These are for whoever operates this
# deployment, and they live under their own prefix and their own key so the two
# never have to evolve together.
# See martinfowler.com/bliki/PublishedInterface.html


@router.get("/control/status")
async def control_status(authorization: str | None = Header(default=None)):
    """What is running, and what it has been doing."""
    if not _control_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    return {
        "object": "control.status",
        "running": bool(app_state.is_running),
        "run_mode": app_state.current_run_mode,
        "phase": app_state.current_phase,
        "city": app_state.current_city,
        "topic": app_state.current_topic,
        "url": app_state.current_url,
        "enriching": bool(getattr(app_state, "_enrich_running", False)),
        "worker_paused": bool(getattr(app_state, "worker_paused", False)),
        "last_run_at": (app_state.last_run_at.isoformat()
                        if app_state.last_run_at else None),
    }


@router.post("/control/run")
async def control_run(request: Request,
                      authorization: str | None = Header(default=None)):
    """Start a pipeline run now. Body: {"mode": "ai_only"|"search_only"|"full", …}"""
    if not _control_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        return _error(400, "Body must be a JSON object.", "invalid_request_error")
    mode = str(body.get("mode") or "ai_only")
    if mode not in ("ai_only", "search_only", "full"):
        return _error(400, f"Unknown mode {mode!r}.", "invalid_request_error")

    from .app import launch_pipeline_run
    # Asking for a run means the operator wants work happening again.
    app_state.worker_paused = False
    started, reason = launch_pipeline_run(
        mode,
        skip_scraped=bool(body.get("skip_scraped", True)),
        skip_extracted=bool(body.get("skip_extracted", True)),
        run_communities=bool(body.get("run_communities", True)),
        run_venues=bool(body.get("run_venues", True)),
        run_persons=bool(body.get("run_persons", True)),
        filter_country=str(body.get("country") or ""),
        filter_city=str(body.get("city") or ""),
    )
    if not started:
        return _error(409, reason, "invalid_request_error", "run_in_progress")
    log.info("control_run_started", mode=mode)
    return {"object": "control.run", "started": True, "mode": mode}


@router.post("/control/stop")
async def control_stop(authorization: str | None = Header(default=None)):
    """Stop the running pipeline and any enrichment job."""
    if not _control_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    # Pause first: cancelling alone only ended the current run, and the
    # continuous worker started another within the minute.
    app_state.worker_paused = True
    stopped = bool(app_state.run_coordinator.cancel())
    # Enrichment runs outside the coordinator (it coexists with extraction), so
    # it is cancelled separately or it would keep going alone.
    task = getattr(app_state, "_enrich_task", None)
    enrich_stopped = False
    if task is not None and not task.done():
        task.cancel()
        enrich_stopped = True
    log.info("control_stop", run=stopped, enrich=enrich_stopped)
    return {"object": "control.stop", "run_stopped": stopped,
            "enrich_stopped": enrich_stopped, "worker_paused": True}


@router.post("/control/resume")
async def control_resume(authorization: str | None = Header(default=None)):
    """Let the worker pick work again after a stop."""
    if not _control_authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    app_state.worker_paused = False
    log.info("control_resume")
    return {"object": "control.resume", "worker_paused": False}


@router.get("/backlog")
async def backlog(authorization: str | None = Header(default=None)):
    """Non-standard: how much work is queued, so nobody has to infer it.

    "Why is there so little for the extractor to do?" was asked three times in
    two days and answered each time by reading logs that only hold the last few
    minutes. `pages_pending` is the direct answer: pages fetched and cached
    whose extraction at the current fingerprint is missing. If it is near zero,
    the extraction window has nothing to do and the constraint is collection.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    if not app_state.db_path or not app_state.pipeline_cfg:
        return _error(503, "Pipeline is not configured.", "server_error", "no_config")
    from ..db import get_backlog_counts, get_fully_processed_pairs
    from ..pipeline import build_extractor
    cfg = app_state.pipeline_cfg
    fp = build_extractor(cfg).canonical_fingerprint

    def _work() -> dict:
        counts = get_backlog_counts(app_state.db_path, fp)
        # `pages_pending` counts every cached page whose extraction is not
        # current. That is not the same as work the run will do: the done-pair
        # filter only looks at a pair's first `max_pages` urls, so pages beyond
        # that are pending forever and invisible to it. `pairs_pending` is the
        # number the run actually acts on — report both, and their disagreement.
        done = get_fully_processed_pairs(
            app_state.db_path, fp, max_pages=cfg.search_max_pages,
            quarantine_threshold=cfg.extract_max_page_failures)
        total = len(app_state.cities or []) * len(app_state.topics or [])
        counts["pairs_total"] = total
        counts["pairs_done"] = len(done)
        counts["pairs_pending"] = max(0, total - len(done))
        return counts

    # Off the event loop: several COUNT(*) over the biggest tables plus the
    # done-pair scan, and /healthz already taught us what a blocking query on a
    # busy database costs.
    counts = await asyncio.to_thread(_work)
    return {
        "object": "backlog",
        "fingerprint": fp,
        # The settings that decide throughput and cost, as the *running process*
        # sees them. config/settings.yaml is a mounted volume edited through
        # /admin/config, so the repo's copy proves nothing about production and
        # "did my change take effect?" had no answer short of reading behaviour
        # hours later.
        "config": {
            "extract_concurrency": cfg.extract_concurrency,
            "search_concurrency": cfg.search_concurrency,
            "dataforseo_mode": cfg.dataforseo_mode,
            "standard_priority": cfg.dataforseo_priority,
            "search_max_pages": cfg.search_max_pages,
        },
        **counts,
    }


@router.get("/funnel")
async def funnel(authorization: str | None = Header(default=None), days: int = 30):
    """The acquisition funnel: visitors → outclicks → subscriptions → claims.

    Separate from /backlog, which measures the crawler. This measures whether
    any of the crawling reaches a person. Every number in it was already being
    written to the database and none of it was readable without the admin
    password, so the honest answer to "is anything converting?" was a guess.

    Cheap on purpose — indexed counts over small tables, no corpus scan — so it
    can be polled without the cost that makes /backlog a once-a-day question.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    if not app_state.db_path:
        return _error(503, "No database configured.", "server_error", "no_config")
    from ..db import get_funnel_counts
    days = max(1, min(int(days or 30), 365))
    counts = await asyncio.to_thread(get_funnel_counts, app_state.db_path, days)
    return {"object": "funnel", **counts}


@router.get("/logs")
async def logs(
    authorization: str | None = Header(default=None),
    lines: int = 200,
    grep: str = "",
    level: str = "",
):
    """Application log lines, newest last, from the rotating file on disk.

    Reads history, not a live buffer. The in-memory ring the admin page streams
    holds 500 lines — a few minutes under the continuous worker — and every
    "what happened last night?" this week hit a buffer that had forgotten.

    Exists so a debugging session can read production logs the way it reads
    /healthz, without a Coolify login. It is a *complement* to the platform's
    logs, not a replacement: this endpoint needs a running app, so it cannot
    explain a container that fails to start — for that the Coolify deployment
    log is still the only source.

    `grep` is a plain substring match (not a regex: an operator-supplied regex
    is an easy accidental catastrophic backtrack). `level` filters exactly.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    from .log_stream import broadcaster

    rows = broadcaster.history(limit=max(1, min(lines, 5000)), grep=grep, level=level)
    return {"object": "list", "count": len(rows), "data": rows}


@router.post("/score")
async def score(
    authorization: str | None = Header(default=None),
    pages: int = 8,
    provider: str = "",
    locale: str = "",
):
    """Measure the routed fleet on our own extraction task.

    Read-only: it reports scores and never writes `config/providers.yaml`. The
    catalogue is a mounted volume, so applying a result is a deliberate edit,
    not a side effect of measuring — a bad golden set silently rewriting the
    routing order is exactly the failure worth keeping manual.

    Costs one LLM call per model per page: at the default 8 pages and a
    12-model fleet that is ~96 calls, which comes out of the same daily budget
    the crawler uses. Check `GET /v1/quota` first.

    `locale` restricts the sample to one market. Measuring without it ranks the
    fleet on whatever the corpus holds most of — 70% international here — which
    is not the question when the primary market is Hungarian.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    mr = _build_router()
    if mr is None or not mr.enabled:
        return _error(503, "Model router is not configured.", "server_error",
                      "router_disabled")
    if not app_state.db_path:
        return _error(503, "No database configured.", "server_error", "no_database")

    fleet = mr.all_extractors()
    if provider:
        fleet = [e for e in fleet if e.provider == provider]
    if not fleet:
        return _error(404, f"No models for provider '{provider}'.",
                      "invalid_request_error", "model_not_found")

    from ..scoring import golden_set, score_fleet

    _pages, _locale = max(1, min(pages, 40)), (locale.strip() or None)

    # Cheap and up front: one query decides whether there is anything to
    # measure, so an empty sample is a 422 the caller can act on rather than a
    # background job that quietly does nothing.
    try:
        _golden = await asyncio.to_thread(golden_set, app_state.db_path, _pages, _locale)
    except FileNotFoundError as exc:
        return _error(503, str(exc), "server_error", "no_database")
    if not _golden:
        return _error(422, "no usable golden pages (need cached pages with records)"
                           + (f" for locale {_locale!r}" if _locale else ""),
                      "invalid_request_error", "no_golden_pages")

    async def _run_scoring() -> None:
        try:
            # `mr` so the calls are charged: scoring spends the provider's
            # real daily allowance, and a router that does not know has a
            # budget number that is simply wrong for the rest of the day.
            out = await score_fleet(app_state.db_path, fleet,
                                    locale=_locale, pages=_pages, golden=_golden,
                                    router=mr)
        except Exception as exc:
            log.error("fleet_score_failed", error=str(exc), locale=_locale)
            return
        if out.get("error"):
            log.warning("fleet_score_no_pages", error=out["error"], locale=_locale)
            return
        # Logged, not returned: this is the only durable copy. See below.
        log.info("fleet_scored", locale=out.get("locale"), pages=out.get("pages"),
                 sample=out.get("sample"),
                 scores={f"{r['provider']}:{r['model']}": r.get("score")
                         for r in out.get("results", [])},
                 unmeasured=out.get("unmeasured"))

    # In the background, and the answer goes to the log rather than the
    # response. A fleet-wide measurement is minutes of LLM calls and the CDN
    # cuts a request off at about 100 seconds: on 2026-08-20 the work completed
    # server-side, the caller got a 502, and the result was simply lost.
    asyncio.create_task(_run_scoring())
    return {
        "object": "score.started",
        "locale": _locale or "mixed",
        "pages": len(_golden),
        "models": len(fleet),
        "note": ("Running in the background — a fleet measurement outlives the "
                 "CDN's request timeout. Read the result with "
                 "GET /v1/logs?grep=fleet_scored."),
    }


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """OpenAI `/v1/chat/completions`, routed across the free-tier fleet.

    Differences from the upstream contract, all deliberate:

    * `model` selects a *routing policy*, not only a model — see `_select`.
    * `stream: true` is rejected rather than silently ignored. Answering a
      streaming request with a non-streaming body hangs clients that are waiting
      for SSE frames; an explicit 400 is the honest failure.
    * The response carries an extra `x_router` object naming the provider and
      model that served it. Unknown top-level fields are ignored by every
      OpenAI client, so this stays compatible.
    """
    if not _authorized(authorization):
        return _error(401, "Invalid or missing API key.", "invalid_request_error",
                      "invalid_api_key")
    try:
        body: Any = await request.json()
    except Exception:
        return _error(400, "Request body must be valid JSON.", "invalid_request_error")
    if not isinstance(body, dict):
        return _error(400, "Request body must be a JSON object.", "invalid_request_error")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _error(400, "'messages' must be a non-empty array.",
                      "invalid_request_error", "messages")
    if len(messages) > _MAX_MESSAGES:
        return _error(400, f"'messages' exceeds the {_MAX_MESSAGES}-message limit.",
                      "invalid_request_error", "messages")
    for m in messages:
        if not isinstance(m, dict) or "role" not in m:
            return _error(400, "Each message needs a 'role' and 'content'.",
                          "invalid_request_error", "messages")
    if sum(len(str(m.get("content") or "")) for m in messages) > _MAX_CHARS:
        return _error(400, f"Total content exceeds {_MAX_CHARS} characters.",
                      "invalid_request_error", "messages")
    if body.get("stream"):
        return _error(400, "Streaming is not supported by this gateway; "
                           "omit 'stream' or set it to false.",
                      "invalid_request_error", "stream")

    mr = _build_router()
    if mr is None or not mr.enabled:
        return _error(503, "Model router is not configured.", "server_error",
                      "router_disabled")

    requested = str(body.get("model") or "auto")
    # with_budget(), not order(): order() also excludes providers inside their
    # rpm cooldown, which at rpm: 30 is a two-second wait — telling a caller
    # "no quota left today" for that would be a lie, and FallbackExtractor's
    # _await_pacing waits it out anyway.
    fleet = _select(mr.with_budget(), requested)
    if not fleet:
        # Distinguish "you named something that does not exist" from "everything
        # that could serve you is out of quota" — different fixes.
        if _select(mr._all, requested):
            return _error(429, f"No quota left today for '{requested}'.",
                          "rate_limit_error", "quota_exhausted")
        return _error(404, f"Unknown model '{requested}'. See GET /v1/models.",
                      "invalid_request_error", "model_not_found")

    from ..extract import FallbackExtractor
    # scope_to=fleet so "out of quota" is judged against the models that could
    # serve THIS request. A caller pinning one exhausted model would otherwise
    # see 502 from unrelated providers' spare capacity instead of a 429.
    chain = FallbackExtractor(primaries=fleet, router=mr, scope_to=fleet)
    # Filter here, not only inside _ApiExtractor.completion: these kwargs travel
    # through FallbackExtractor._call(self, method, label, *args, **kwargs), so a
    # body field named "method" or "label" would raise TypeError and surface as a
    # 500 on a public endpoint.
    from ..extract import _ApiExtractor
    params = {k: v for k, v in body.items() if k in _ApiExtractor._PASSTHROUGH_FIELDS}
    try:
        data = await chain.completion(messages, **params)
    except ExtractorRateLimitError:
        return _error(429, "All routed providers are rate limited; retry shortly.",
                      "rate_limit_error", "rate_limited")
    except ExtractorQuotaError:
        return _error(429, "Provider quota exhausted for today.",
                      "rate_limit_error", "quota_exhausted")
    except ExtractorUnavailableError as exc:
        log.warning("gateway_upstream_unavailable", model=requested, error=str(exc))
        return _error(502, f"No provider could serve the request: {exc}",
                      "server_error", "upstream_unavailable")
    except Exception as exc:
        log.exception("gateway_unexpected_error", model=requested)
        return _error(500, f"Gateway error: {type(exc).__name__}", "server_error")

    if not isinstance(data, dict):
        return _error(502, "Upstream returned an unexpected response shape.",
                      "server_error", "bad_upstream_response")
    data["x_router"] = {
        # From the chain, not matched by model id: the catalogue already lists
        # llama-3.3-70b under three providers, and after failover the head of
        # the fleet is the wrong answer anyway.
        "provider": chain.last_provider or fleet[0].provider,
        "model": chain.last_model,
        "quality": chain.last_quality,
        "requested": requested,
    }
    return data
