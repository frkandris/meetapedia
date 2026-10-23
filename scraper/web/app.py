import asyncio
import base64
import html
import hmac
import importlib.metadata
import json
import os
import time
from functools import lru_cache
import re

from markupsafe import Markup, escape
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote as _url_quote, urlsplit

import structlog
import yaml
from fastapi import APIRouter, BackgroundTasks, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import load_config
from ..db import (
    count_extracted_pages,
    count_cache_pages,
    get_sitemap_communities,
    find_community_by_id,
    get_extraction_quality_mix,
    get_community_history,
    get_all_communities,
    get_city_topic_counts,
    get_topic_counts_for_city,
    get_community_lastmods,
    get_city_totals,
    get_communities,
    get_communities_by_ids,
    get_data_guide,
    get_data_guides,
    count_data_guides,
    get_data_guide_sitemap_rows,
    is_known_community_url,
    log_outclick,
    get_communities_for_city,
    search_communities_by_tag,
    save_not_community_report,
    get_not_community_reports,
    delete_not_community_report,
    set_community_hidden,
    _community_record_key,
    get_topic_counts,
    get_topic_counts_for_cities,
    get_total_community_count,
    get_all_venues,
    get_venue_counts,
    get_venues_by_city_topic,
    get_venues,
    get_communities_for_venue,
    get_venue_person_counts_by_url,
    get_venue_history,
    get_persons,
    get_person_counts,
    get_person_history,
    get_prompt_overrides,
    upsert_prompt_override,
    delete_prompt_override,
    save_city_request,
    init_db,
    get_duplicate_candidates,
    resolve_duplicate_candidate,
    delete_duplicate_candidate,
    get_wrong_city_candidates,
    resolve_wrong_city_candidate,
    merge_community_into,
    get_community_by_record_key,
    save_community_data,
    save_edit_request,
    get_edit_requests,
    resolve_edit_request,
    apply_community_edit,
    search_all,
    get_venue_for_community,
    get_persons_for_community,
    get_publishable_persons,
    save_community_submission,
    get_community_submissions,
    resolve_community_submission,
)
from ..false_positives import (add as fp_add, diff_html as fp_diff_html,
                               load as fp_load, load_history as fp_load_history,
                               remove as fp_remove, build_prompt_section)
from ..extract import (ENRICH_SCHEMA, ENRICH_SYSTEM_PROMPT, EXTRACTION_SCHEMA,
                       SYSTEM_PROMPT, USER_PROMPT_TEMPLATE,
                       VENUE_SCHEMA, PERSON_SCHEMA, PROMPT_KEYS, get_prompt, set_prompt_override)
from ..fetch import fetch_and_clean
from ..identity import public_slug
from ..models import CommunityRecord, is_placeholder_leader
from ..pipeline import (
    RUN_ABORTED,
    _enrich_record,
    _needs_enrichment,
    classify_run_outcome,
    reextract_community,
    run_pipeline,
    scrape_submitted_url,
)
from ..search import DataForSEOClient, FallbackSearchClient, SearchBudget
from ..store import save_results
from ..url_safety import (UnsafeURLError, assert_safe_public_url,
                          is_public_http_url)
from .i18n import display_languages, get_topic_labels, lang_context
from .log_stream import broadcaster
from .schema import (article_jsonld, breadcrumb_jsonld, person_jsonld, records_to_jsonld,
                     site_jsonld, venue_jsonld)
from .state import app_state

log = structlog.get_logger()

BASE_DIR = Path(__file__).parent.parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"

_ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
_ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

TOPIC_ICONS: dict[str, str] = {
    "running": "person-simple-run",
    "board_games": "puzzle-piece",
    "choir": "microphone-stage",
    "dance": "person-simple",
    "cycling": "bicycle",
    "hiking": "mountains",
    "yoga": "flower-lotus",
    "photography": "camera",
    "book_club": "books",
    "chess": "crown",
    "cooking": "cooking-pot",
    "theater": "ticket",
    "music": "music-notes",
    "martial_arts": "sword",
    "gaming": "game-controller",
    "volunteering": "hand-heart",
    "language_exchange": "translate",
    "art": "paint-brush",
    "meditation": "spiral",
    "swimming": "waves",
    "hagyomanyorzes": "scroll",
    "gardening": "plant",
    "film_club": "popcorn",
    "trivia": "lightbulb",
    "sustainability": "recycle",
    "crafts": "scissors",
    "fitness": "barbell",
    "religion": "hands-praying",
    "baby": "baby",
    "senior": "sun-horizon",
    "kisallat": "paw-print",
    "vallalkozas": "briefcase",
    "nok": "gender-female",
    "fogyatekossag": "wheelchair",
    "tech": "code",
    "other": "circles-four",
}

TOPIC_LABELS: dict[str, str] = {
    "running": "Running",
    "board_games": "Board Games",
    "choir": "Choir",
    "dance": "Dance",
    "cycling": "Cycling",
    "hiking": "Hiking",
    "yoga": "Yoga",
    "photography": "Photography",
    "book_club": "Book Club",
    "chess": "Chess",
    "cooking": "Cooking",
    "theater": "Theater",
    "music": "Music",
    "martial_arts": "Martial Arts",
    "gaming": "Gaming",
    "volunteering": "Volunteering",
    "language_exchange": "Language Exchange",
    "art": "Art",
    "meditation": "Meditation",
    "swimming": "Swimming",
    "hagyomanyorzes": "Hagyományőrzés",
    "gardening": "Gardening",
    "film_club": "Film Club",
    "trivia": "Trivia & Quizzes",
    "sustainability": "Sustainability",
    "crafts": "Crafts & Making",
    "fitness": "Fitness",
    "religion": "Religion & Faith",
    "baby": "Baba & Szülő",
    "senior": "Seniors",
    "kisallat": "Kisállat",
    "vallalkozas": "Entrepreneurship",
    "nok": "Women",
    "fogyatekossag": "Disability",
    "tech": "Tech",
    "other": "Other",
}


class _BasicAuth:
    """Pure ASGI auth middleware — protects /admin/* only, no SSE buffering."""

    def __init__(self, inner: ASGIApp) -> None:
        self._inner = inner

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self._inner(scope, receive, send)
            return

        path = scope.get("path", "")
        if not path.startswith("/admin"):
            await self._inner(scope, receive, send)
            return

        if not _ADMIN_PASSWORD:
            await self._send_plain(send, 503, b"ADMIN_PASSWORD is not configured")
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        auth = headers.get(b"authorization", b"").decode("latin-1")

        if auth.lower().startswith("basic "):
            # Only credential parsing is guarded — the inner app must be called
            # OUTSIDE the try, or a route exception would be caught here and a
            # second 401 response.start would race the one the app already sent
            # ('Received multiple "http.response.start" messages').
            authorized = False
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                user, _, pwd = decoded.partition(":")
                authorized = (
                    hmac.compare_digest(user, _ADMIN_USER)
                    and hmac.compare_digest(pwd, _ADMIN_PASSWORD)
                    and self._same_origin_admin_write(scope, headers)
                )
            except Exception:
                authorized = False
            if authorized:
                await self._inner(scope, receive, send)
                return

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"www-authenticate", b'Basic realm="Meetapedia Admin"'],
                [b"content-length", b"0"],
            ],
        })
        await send({"type": "http.response.body", "body": b""})

    @staticmethod
    async def _send_plain(send: Send, status: int, body: bytes) -> None:
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"text/plain; charset=utf-8"],
                [b"content-length", str(len(body)).encode("ascii")],
            ],
        })
        await send({"type": "http.response.body", "body": body})

    @staticmethod
    def _same_origin_admin_write(scope: Scope, headers: dict[bytes, bytes]) -> bool:
        method = scope.get("method", "GET").upper()
        if method in ("GET", "HEAD", "OPTIONS"):
            return True

        host = headers.get(b"host", b"").decode("latin-1")
        origin = headers.get(b"origin", b"").decode("latin-1")
        referer = headers.get(b"referer", b"").decode("latin-1")
        candidate = origin or referer
        if not candidate:
            return False
        try:
            return urlsplit(candidate).netloc == host
        except Exception:
            return False


_fastapi = FastAPI(title="Meetapedia")


_BOT_UA_MARKERS = ("bot", "spider", "crawl", "curl", "wget", "python-", "headless",
                   "facebookexternalhit", "preview", "monitor", "lighthouse")
_UNTRACKED_PREFIXES = ("/admin", "/static", "/api", "/healthz", "/robots",
                       "/sitemap", "/set-lang", "/unsubscribe", "/source")


@_fastapi.middleware("http")
async def _count_pageview(request: Request, call_next):
    """Lightweight server-side visitor counter for the daily report email.
    Counts public GET page hits per site per UTC day; uniques via a salted
    ip+ua day-hash. Bots (by UA marker) and utility paths are skipped."""
    response = await call_next(request)
    try:
        path = request.url.path
        if (request.method == "GET" and app_state.db_path
                and response.status_code < 400
                and not any(path.startswith(p) for p in _UNTRACKED_PREFIXES)
                and "text/html" in (response.headers.get("content-type") or "")):
            ua = (request.headers.get("user-agent") or "").lower()
            if ua and not any(m in ua for m in _BOT_UA_MARKERS):
                import asyncio as _asyncio
                import hashlib as _hashlib
                from datetime import datetime as _dt, timezone as _tz
                from .i18n import _detect_site as _ds
                from ..db import record_pageview
                day = _dt.now(_tz.utc).strftime("%Y-%m-%d")
                ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()                     or (request.client.host if request.client else "")
                vh = _hashlib.sha256(f"{day}|{ip}|{ua}".encode()).hexdigest()[:16]
                await _asyncio.to_thread(record_pageview, app_state.db_path, day,
                                         _ds(request), vh)
    except Exception:
        pass  # tracking must never break a page
    return response


@_fastapi.middleware("http")
async def _markdown_for_agents(request: Request, call_next):
    """Serve a public page as Markdown to a client that asks for it.

    See `agent_markdown`. `Vary: Accept` goes on the HTML too: the two bodies
    share one URL, and a cache that does not know they differ by Accept would
    hand a browser the Markdown, or an agent the HTML.
    """
    from . import agent_markdown
    response = await call_next(request)
    if (request.method != "GET" or request.url.path.startswith("/admin")
            or not agent_markdown.enabled()
            or "text/html" not in (response.headers.get("content-type") or "")):
        return response
    response.headers["Vary"] = ", ".join(
        v for v in (response.headers.get("vary"), "Accept") if v)
    if response.status_code != 200 or not agent_markdown.wants_markdown(
            request.headers.get("accept")):
        return response
    body = b"".join([chunk async for chunk in response.body_iterator])
    base = f"{lang_context(request)['site_url']}{request.url.path}"
    try:
        text = await asyncio.to_thread(
            agent_markdown.html_to_markdown, body.decode("utf-8"), base)
    except Exception:
        log.warning("agent_markdown_failed", path=request.url.path)
        return Response(body, status_code=200, headers=dict(response.headers))
    return Response(text, media_type="text/markdown; charset=utf-8",
                    headers={"Vary": "Accept", "X-Robots-Tag": "noindex"})


app = _BasicAuth(_fastapi)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["urlencode"] = lambda s: _url_quote(str(s), safe="")

def _sha256_16(url: str) -> str:
    import hashlib
    return hashlib.sha256(str(url).encode()).hexdigest()[:16]

templates.env.filters["sha256_16"] = _sha256_16


def _fmt_dur(s: float | None) -> str:
    # Tolerate Jinja Undefined / non-numeric values: legacy cache entries have
    # scraped_at but no duration field, and the detail page must not 500 on them.
    # Broad except is deliberate — float(jinja2.Undefined) raises UndefinedError,
    # which is neither TypeError nor ValueError.
    try:
        s = float(s)
    except Exception:
        return ""
    if s < 60:
        return f"{s:.1f}s"
    return f"{int(s / 60)}m {int(s % 60)}s"


templates.env.filters["fmt_dur"] = _fmt_dur


def _inbox_counts() -> dict:
    """Pending user-interaction counts for the admin nav badge. Registered as a
    Jinja GLOBAL CALLABLE so every admin page shows live counts without each
    route having to pass them."""
    from ..db import count_pending_interactions
    try:
        if app_state.db_path:
            return count_pending_interactions(app_state.db_path)
    except Exception:
        pass
    return {"edit_requests": 0, "reports": 0, "submissions": 0, "total": 0}


templates.env.globals["inbox_counts"] = _inbox_counts


_LINK_PLATFORMS = [
    (["facebook.com", "fb.com", "fb.me"],            "Facebook",  "ph-facebook-logo",  "text-blue-700 bg-blue-50 hover:bg-blue-100"),
    (["instagram.com"],                               "Instagram", "ph-instagram-logo", "text-pink-600 bg-pink-50 hover:bg-pink-100"),
    (["twitter.com", "x.com"],                        "X",         "ph-x-logo",         "text-gray-800 bg-gray-100 hover:bg-gray-200"),
    (["youtube.com", "youtu.be"],                     "YouTube",   "ph-youtube-logo",   "text-red-600 bg-red-50 hover:bg-red-100"),
    (["linkedin.com"],                                "LinkedIn",  "ph-linkedin-logo",  "text-blue-800 bg-blue-50 hover:bg-blue-100"),
    (["meetup.com"],                                  "Meetup",    "ph-users-three",    "text-red-500 bg-red-50 hover:bg-red-100"),
    (["t.me", "telegram.me", "telegram.org"],         "Telegram",  "ph-telegram-logo",  "text-sky-600 bg-sky-50 hover:bg-sky-100"),
    (["discord.gg", "discord.com"],                   "Discord",   "ph-discord-logo",   "text-indigo-600 bg-indigo-50 hover:bg-indigo-100"),
    (["whatsapp.com", "wa.me"],                       "WhatsApp",  "ph-whatsapp-logo",  "text-green-600 bg-green-50 hover:bg-green-100"),
    (["tiktok.com"],                                  "TikTok",    "ph-music-note",     "text-gray-900 bg-gray-100 hover:bg-gray-200"),
    (["github.com"],                                  "GitHub",    "ph-github-logo",    "text-gray-800 bg-gray-100 hover:bg-gray-200"),
    (["linktr.ee", "linktree.com"],                   "Linktree",  "ph-tree-structure", "text-green-700 bg-green-50 hover:bg-green-100"),
]


def _valid_url(url: str) -> bool:
    return is_public_http_url(url)


def _link_meta(url: str) -> dict:
    from urllib.parse import urlparse
    try:
        host = urlparse(url).netloc.lower().removeprefix("www.")
        for domains, label, icon, color in _LINK_PLATFORMS:
            if any(d in host for d in domains):
                return {"label": label, "icon": icon, "color": color}
        domain = host[:28] or url[:28]
    except Exception:
        domain = url[:28]
    return {"label": domain, "icon": "ph-globe", "color": "text-indigo-600 bg-indigo-50 hover:bg-indigo-100"}


templates.env.filters["valid_url"] = _valid_url
templates.env.filters["link_meta"] = _link_meta


@lru_cache(maxsize=8192)
def _slugify(text: str) -> str:
    return public_slug(text)


templates.env.filters["slugify"] = _slugify


def _link_communities(text: str, communities, city: str) -> "Markup":
    """Turn the group names a guide names into links to those groups.

    The whole point of the guide prose is that it names real groups — the
    validator refuses a draft that names fewer than three. Leaving those names
    as flat text asks the reader to scroll past two screens of cards and find
    the name again in a list, which is the work the article was supposed to do
    for them.

    Longest name first, so "Ambassador Club Mecsek (Regionális)" is not eaten by
    "Ambassador Club" sitting inside it. One pass over the text, so a name can
    never be matched inside a link already written. Whole words only: Hungarian
    inflects by suffix, so a group called "Futókör" would otherwise turn the
    first half of "futókörök" into a link. The prose is escaped either way: it
    is model output, and this is the one place it becomes markup.
    """
    raw = text or ""
    names = sorted({(c.get("name") or "").strip() for c in (communities or [])},
                   key=len, reverse=True)
    names = [n for n in names if len(n) > 3]
    if not raw or not names:
        return Markup(escape(raw))
    pattern = re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)")
    city_slug = public_slug(city or "")
    parts, cursor = [], 0
    for match in pattern.finditer(raw):
        parts.append(escape(raw[cursor:match.start()]))
        name = match.group(0)
        parts.append(Markup(
            '<a class="font-medium text-[#A8512F] underline decoration-[#E3D5C8] '
            'decoration-2 underline-offset-2 hover:decoration-[#A8512F]" '
            'href="/{}/{}">{}</a>').format(city_slug, public_slug(name), name))
        cursor = match.end()
    parts.append(escape(raw[cursor:]))
    return Markup("").join(parts)


templates.env.filters["link_communities"] = _link_communities


def _comparable_dimensions(dimensions, locale: str = "en") -> list:
    """The dimension cards that actually compare something.

    `common` holds the top value counts, `covered` how many groups reported the
    field. One value means every group said the same thing; a dominant value
    means near enough the same — the Pécs guide's "Nyelv" card had 42 groups
    saying "Hungarian" and two saying "Magyar", two values for one fact. The
    rule is `guides.is_comparable`, the same one the build uses, measured
    against `covered` because `common` is truncated to five values.

    Applied here as well as when a guide is built, so a guide stored before a
    rule existed follows it the moment this ships — without a rewrite or a
    fleet call. That includes the language normalisation: an older guide still
    stores "Hungarian" and "Magyar" as two values, so they are merged and
    written in the page's language here before the rule is asked.
    """
    from ..guides import is_comparable

    kept = []
    for dimension in dimensions or []:
        if dimension.get("field") == "language":
            dimension = _normalised_language_dimension(dimension, locale)
        common = dimension.get("common") or []
        counts = [int(entry.get("count") or 0) for entry in common]
        if not is_comparable(counts, int(dimension.get("covered") or 0)):
            continue
        kept.append(dimension)
    return kept


def _normalised_language_dimension(dimension: dict, locale: str) -> dict:
    """A stored language card with its values written one way per language."""
    merged: dict[str, int] = {}
    for entry in dimension.get("common") or []:
        value = display_languages(str(entry.get("value") or ""), locale)
        merged[value] = merged.get(value, 0) + int(entry.get("count") or 0)
    common = sorted(({"value": v, "count": c} for v, c in merged.items()),
                    key=lambda e: -e["count"])
    examples = [{**item, "value": display_languages(str(item.get("value") or ""), locale)}
                for item in dimension.get("examples") or []]
    return {**dimension, "common": common, "examples": examples,
            "distinct": len(common)}


templates.env.filters["comparable_dimensions"] = _comparable_dimensions
templates.env.filters["display_languages"] = display_languages
templates.env.filters["breadcrumb_jsonld"] = breadcrumb_jsonld


@_fastapi.post("/api/outclick")
async def record_outclick(request: Request):
    """Best-effort JS analytics while the anchor keeps its direct destination."""
    if not app_state.db_path:
        return JSONResponse({"ok": True}, status_code=202)
    try:
        if int(request.headers.get("content-length") or 0) > 4096:
            return JSONResponse({"ok": True}, status_code=202)
        payload = await request.json()
        community_id = str(payload.get("community_id") or "")
        url = str(payload.get("url") or "")
        link_type = str(payload.get("link_type") or "")
        if (not re.fullmatch(r"[0-9a-f]{12}", community_id)
                or len(url) > 2048
                or not url.startswith(("http://", "https://"))
                or link_type not in {"website", "source", "social"}):
            return JSONResponse({"ok": True}, status_code=202)
        if not await asyncio.to_thread(
                is_known_community_url, app_state.db_path, community_id, url):
            return JSONResponse({"ok": True}, status_code=202)
        await asyncio.to_thread(
            log_outclick, app_state.db_path, community_id, url, link_type)
    except Exception:  # analytics must never interfere with navigation
        pass
    return JSONResponse({"ok": True}, status_code=202)

_ROLE_HU = {
    "leader": "vezető",
    "instructor": "oktató",
    "speaker": "előadó",
    "organizer": "szervező",
    "founder": "alapító",
    "coach": "edző",
    "trainer": "tréner",
    "moderator": "moderátor",
    "admin": "adminisztrátor",
    "member": "tag",
    "volunteer": "önkéntes",
    "coordinator": "koordinátor",
}
templates.env.filters["role_hu"] = lambda r: _ROLE_HU.get((r or "").lower(), r)


def _safe_redirect_target(target: str, fallback: str) -> str:
    return target if target.startswith("/") and not target.startswith("//") else fallback


def _config_error_redirect(exc: Exception) -> RedirectResponse:
    return RedirectResponse(f"/admin/config?error={_url_quote(str(exc), safe='')}", status_code=302)


def _reload_runtime_config() -> None:
    if not app_state.db_path:
        return
    cities, topics, pipeline_cfg = load_config(app_state.db_path)
    app_state.cities = cities
    app_state.topics = topics
    app_state.pipeline_cfg = pipeline_cfg
    _load_prompt_overrides()


def _load_prompt_overrides() -> None:
    if not app_state.db_path:
        return
    overrides = get_prompt_overrides(app_state.db_path)
    for key in PROMPT_KEYS:
        set_prompt_override(key, overrides.get(key))

_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
_fastapi.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


admin = APIRouter(prefix="/admin")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _lib_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:
        return "?"


async def _build_software_info() -> dict:
    cfg = app_state.pipeline_cfg
    if cfg and cfg.dataforseo_login:
        search_info = {"label": "DataForSEO", "status": "ok", "backend": "dataforseo"}
    else:
        search_info = {"label": "Search", "status": "no key", "backend": "none"}
    return {
        "search": search_info,
        "python": {"label": "Python", "version": sys.version.split()[0]},
        "libs": {
            "httpx": _lib_version("httpx"),
            "trafilatura": _lib_version("trafilatura"),
            "pydantic": _lib_version("pydantic"),
            "fastapi": _lib_version("fastapi"),
        },
    }


CITY_COORDS: dict[str, tuple[float, float]] = {
    # World, ≥100000 — imported from Wikidata by scripts/import_world.py
    "Chongqing": (29.55, 106.5069),
    "Guangzhou": (23.13, 113.26),
    "Tianjin": (39.1467, 117.2056),
    "Xi'an": (34.2611, 108.9422),
    "Suzhou": (31.3, 120.6194),
    "Zhengzhou": (34.7492, 113.6605),
    "Wuhan": (30.595, 114.2975),
    "Hangzhou": (30.25, 120.1675),
    "Baoding": (38.8671, 115.4845),
    "Linyi": (35.1041, 118.3502),
    "Shijiazhuang": (38.0425, 114.51),
    "Dongguan": (23.0475, 113.7493),
    "Qingdao": (36.1167, 120.4),
    "Changsha": (28.1987, 112.9709),
    "Harbin": (45.75, 126.6333),
    "Nanyang": (32.9987, 112.5292),
    "Wenzhou": (27.9991, 120.6561),
    "Foshan": (23.0292, 113.1056),
    "Handan": (36.6116, 114.4894),
    "Ningbo": (29.875, 121.5492),
    "Weifang": (36.7167, 119.1),
    "Hefei": (31.8639, 117.2808),
    "Nanjing": (32.0608, 118.7789),
    "Jinan": (36.6667, 116.9833),
    "Xuzhou": (34.261, 117.1859),
    "Shenyang": (41.8025, 123.4281),
    "Changchun": (43.88, 125.3228),
    "Zhoukou": (33.625, 114.6418),
    "Ganzhou": (25.8292, 114.9336),
    "Heze": (35.2333, 115.4333),
    "Quanzhou": (24.9139, 118.5858),
    "Nanning": (22.8192, 108.315),
    "Kunming": (25.0433, 102.7061),
    "Jining": (35.4, 116.5667),
    "Fuzhou": (26.0769, 119.2917),
    "Fuyang": (32.8986, 115.8045),
    "Shangqiu": (34.4259, 115.6467),
    "Nantong": (31.9829, 120.8873),
    "Tangshan": (39.6294, 118.1739),
    "Wuxi": (31.5667, 120.2833),
    "Dalian": (38.9, 121.6),
    "Cangzhou": (38.3037, 116.8452),
    "Xingtai": (37.0659, 114.4753),
    "Yantai": (37.4644, 121.4478),
    "Luoyang": (34.6587, 112.4245),
    "Jinhua": (29.1046, 119.6494),
    "Zhumadian": (32.9773, 114.0253),
    "Zhanjiang": (21.1967, 110.4031),
    "Bijie": (27.3019, 105.2863),
    "Yancheng": (33.35, 120.1569),
    "Hengyang": (26.8968, 112.5857),
    "Taizhou": (28.6583, 121.4221),
    "Zunyi": (27.705, 106.9336),
    "Shaoyang": (27.2418, 111.4725),
    "Shangrao": (28.4419, 117.9633),
    "Huanggang": (30.45, 114.875),
    "Nanchang": (28.6842, 115.8872),
    "Xinxiang": (35.2992, 113.8851),
    "Xinyang": (32.1264, 114.0672),
    "Maoming": (21.6618, 110.9178),
    "Huizhou": (23.1115, 114.4152),
    "Guiyang": (26.5794, 106.7078),
    "Liaocheng": (36.45, 115.9833),
    "Yulin": (22.6293, 110.1507),
    "Changde": (29.0397, 111.6839),
    "Yueyang": (29.3647, 113.1309),
    "Qujing": (25.5102, 103.8029),
    "Pudong": (31.2231, 121.5397),
    "Xiangyang": (32.0654, 112.1531),
    "Dezhou": (37.4513, 116.3105),
    "Nanchong": (30.7991, 106.0784),
    "Jieyang": (23.5533, 116.3649),
    "Jingzhou": (30.3244, 112.2361),
    "Shantou": (23.3539, 116.6822),
    "Anyang": (36.0956, 114.3461),
    "Tai'an": (36.2001, 117.0809),
    "Dazhou": (31.2152, 107.4947),
    "Langfang": (39.5196, 116.7006),
    "Suihua": (46.6384, 126.9808),
    "Jiaxing": (30.7522, 120.75),
    "Qiqihar": (47.3398, 123.9512),
    "Taiyuan": (37.8733, 112.5425),
    "Yongzhou": (26.4515, 111.5953),
    "Changzhou": (31.8122, 119.9692),
    "Shaoxing": (30.0, 120.5833),
    "Xiamen": (24.4797, 118.0819),
    "Yuncheng": (35.0304, 110.998),
    "Zhaotong": (27.3328, 103.7144),
    "Guilin": (25.2819, 110.2864),
    "Zhangzhou": (24.5093, 117.6612),
    "Yichun": (27.8041, 114.383),
    "Bozhou": (33.8626, 115.7742),
    "Pingdingshan": (33.735, 113.2999),
    "Suqian": (33.9331, 118.2831),
    "Huaihua": (27.5494, 109.9592),
    "Xiaogan": (30.9273, 113.911),
    "Yibin": (28.7596, 104.64),
    "Mianyang": (31.4669, 104.7385),
    "Kaifeng": (34.795, 114.345),
    "Jiangmen": (22.5833, 113.0833),
    "Chenzhou": (25.7989, 113.0267),
    "Zibo": (36.7831, 118.0497),
    "Weinan": (34.4996, 109.4684),
    "Jiujiang": (29.7048, 116.0021),
    "Lianyungang": (34.5906, 119.1801),
    "Yangzhou": (32.3912, 119.4363),
    "Huai'an": (33.5886, 119.0192),
    "Hengshui": (37.7348, 115.686),
    "Ji'an": (27.1172, 114.9793),
    "Zhongshan": (22.5333, 113.35),
    "Yiyang": (28.5833, 112.3333),
    "Jilin": (43.8461, 126.5616),
    "Guigang": (23.1144, 109.5944),
    "Lu'an": (31.7542, 116.5078),
    "Xuchang": (34.0244, 113.8201),
    "Meizhou": (24.2998, 116.1191),
    "Lanzhou": (36.0617, 103.8318),
    "Zhangjiakou": (40.8108, 114.8811),
    "Chifeng": (42.255, 118.8825),
    "Luzhou": (28.8918, 105.4409),
    "Anqing": (30.5, 117.0333),
    "Liuzhou": (24.3264, 109.4281),
    "Guang'an": (30.4673, 106.6336),
    "Zhaoqing": (23.05, 112.4667),
    "Ürümqi": (43.8225, 87.6125),
    "Zhuzhou": (27.8407, 113.1469),
    "Yichang": (30.7083, 111.2803),
    "Chuzhou": (32.3062, 118.3115),
    "Linfen": (36.0812, 111.5087),
    "Qingyuan": (23.6842, 113.0507),
    "Xianyang": (34.3297, 108.7092),
    "Loudi": (27.7378, 111.9974),
    "Binzhou": (37.3806, 118.0125),
    "Deyang": (31.1289, 104.395),
    "Zaozhuang": (34.8667, 117.55),
    "Puyang": (35.7639, 115.03),
    "Lüliang": (37.5203, 111.1355),
    "Neijiang": (29.5872, 105.0635),
    "Baise": (23.901, 106.6194),
    "Wuhu": (31.334, 118.3622),
    "Jiaozuo": (35.229, 113.2304),
    "Hechi": (24.6928, 108.085),
    "Chengde": (40.9739, 117.9322),
    "Hohhot": (40.8151, 111.6629),
    "Shiyan": (32.6351, 110.7755),
    "Siping": (43.1715, 124.3644),
    "Jinzhong": (37.6933, 112.7416),
    "Huzhou": (30.8925, 120.0875),
    "Changzhi": (36.195, 113.1111),
    "Anshan": (41.1066, 122.9895),
    "Baoji": (34.3609, 107.1751),
    "Datong": (40.0936, 113.2914),
    "Qinzhou": (21.95, 108.6167),
    "Tongren": (27.7233, 109.1885),
    "Bengbu": (32.9354, 117.3531),
    "Bazhong": (31.8576, 106.7559),
    "Suining": (30.5098, 105.5737),
    "Leshan": (29.5854, 103.7575),
    "Hanzhong": (33.0794, 107.026),
    "Putian": (25.4394, 119.0103),
    "Zhenjiang": (32.2109, 119.4551),
    "Ningde": (26.6617, 119.5228),
    "Tongliao": (43.6172, 122.264),
    "Qinhuangdao": (39.9398, 119.5881),
    "Jinzhou": (41.1144, 121.1292),
    "Xinzhou": (38.4178, 112.7233),
    "Wuzhou": (23.4833, 111.3167),
    "Huainan": (32.4831, 117.0164),
    "Liupanshui": (26.5944, 104.8333),
    "Tianshui": (34.5809, 105.7311),
    "Rizhao": (35.4164, 119.4331),
    "Meishan": (30.0575, 103.8381),
    "Weihai": (37.5, 122.1),
    "Daqing": (46.5979, 125.008),
    "Jingmen": (31.0381, 112.2114),
    "Haikou": (20.02, 110.32),
    "Chaoyang": (41.5757, 120.4486),
    "Xiangtan": (27.8431, 112.9228),
    "Yinchuan": (38.485, 106.225),
    "Shaoguan": (24.8011, 113.5927),
    "Heyuan": (23.7503, 114.6923),
    "Mudanjiang": (44.5861, 129.5997),
    "Songyuan": (45.1388, 124.808),
    "Shanwei": (22.7664, 115.3331),
    "Longyan": (25.0881, 117.0244),
    "Tieling": (42.2841, 123.8365),
    "Baotou": (40.6562, 109.8345),
    "Dingxi": (35.5806, 104.6263),
    "Nanping": (26.6448, 118.1728),
    "Zigong": (29.3498, 104.7645),
    "Xianning": (29.8341, 114.3288),
    "Huludao": (40.7094, 120.8378),
    "Yangjiang": (21.8556, 111.9627),
    "Ziyang": (30.1256, 104.6397),
    "Chaozhou": (23.67, 116.63),
    "Jiamusi": (46.8081, 130.3653),
    "Hulunbuir": (49.2153, 119.751),
    "Lishui": (28.45, 119.9167),
    "Xuancheng": (30.9475, 118.7518),
    "Ankang": (32.6877, 109.0235),
    "Sanming": (26.2658, 117.6302),
    "Guangyuan": (32.4353, 105.8398),
    "Huangshi": (30.2018, 115.0326),
    "Anshun": (26.2456, 105.934),
    "Xining": (36.6239, 101.7787),
    "Dandong": (40.1167, 124.3833),
    "Zhuhai": (22.2742, 113.5719),
    "Baoshan": (25.1211, 99.169),
    "Yingkou": (40.6653, 122.2297),
    "Longnan": (33.535, 105.349),
    "Pu'er": (22.7803, 100.9698),
    "Yunfu": (22.9242, 112.0353),
    "Luohe": (33.583, 114.0109),
    "Yan'an": (36.5952, 109.4863),
    "Quzhou": (28.9545, 118.8763),
    "Lincang": (23.8864, 100.0871),
    "Yuxi": (24.3495, 102.5432),
    "Laibin": (23.7333, 109.2333),
    "Suizhou": (31.7175, 113.3658),
    "Qingyang": (35.7278, 107.64),
    "Jincheng": (35.4906, 112.8483),
    "Dongying": (37.45, 118.4667),
    "Ma'anshan": (31.6858, 118.5101),
    "Ordos": (39.815, 109.9975),
    "Chongzuo": (22.4167, 107.3667),
    "Hezhou": (24.4164, 111.5478),
    "Pingliang": (35.5412, 106.6819),
    "Shangluo": (33.868, 109.9244),
    "Sanmenxia": (34.7736, 111.195),
    "Baicheng": (45.6148, 122.832),
    "Puning": (23.2993, 116.1586),
    "Binhai New Area": (39.0033, 117.7108),
    "Huaibei": (33.9562, 116.789),
    "Jixi": (45.2937, 130.965),
    "Fushun": (41.8708, 123.8917),
    "Liaoyang": (41.2643, 123.1772),
    "Beihai": (21.4667, 109.1),
    "Fuxin": (42.0127, 121.6486),
    "Wuwei": (37.9278, 102.6329),
    "Pingxiang": (27.6333, 113.85),
    "Shuozhou": (39.3408, 112.4292),
    "Benxi": (41.292, 123.761),
    "Baiyin": (36.5448, 104.1766),
    "Ulanqab": (40.9919, 113.1244),
    "Heihe": (50.2458, 127.4886),
    "Bayannur": (40.7567, 107.4144),
    "Jingdezhen": (29.2942, 117.2036),
    "Hebi": (35.7497, 114.2887),
    "Zhangjiajie": (29.1255, 110.4844),
    "Rui'an": (27.778, 120.6526),
    "Guiping": (23.4, 110.0833),
    "Ya'an": (29.9888, 103.0007),
    "Shuangyashan": (46.6388, 131.1545),
    "Yueqing": (28.1188, 120.9621),
    "Guyuan": (36.008, 106.2782),
    "Wenling": (28.3797, 121.3718),
    "Haidong": (36.5014, 102.1031),
    "Panjin": (41.1198, 122.0634),
    "Wuzhong": (37.9874, 106.1919),
    "Chizhou": (30.6583, 117.4849),
    "Huangshan": (29.7132, 118.3151),
    "Gaozhou": (21.9135, 110.8481),
    "Yangquan": (37.8576, 113.5762),
    "Tongling": (30.9456, 117.8114),
    "Tonghua": (41.7302, 125.9471),
    "Baishan": (41.9377, 126.4179),
    "Yongcheng": (33.9317, 116.4459),
    "Yuyao": (30.0372, 121.1546),
    "Lijiang": (26.8808, 100.2208),
    "Lufeng": (22.9492, 115.6352),
    "Panzhihua": (26.5849, 101.7149),
    "Beiliu": (22.7, 110.35),
    "Xinyu": (27.795, 114.9242),
    "Zhangye": (38.9355, 100.4553),
    "Liaoyuan": (42.8976, 125.1381),
    "Zhoushan": (29.9887, 122.2049),
    "Yingtan": (28.2333, 117.0),
    "Leiyang": (26.4179, 112.8457),
    "Jiuquan": (39.737, 98.5049),
    "Gongzhuling": (43.5036, 124.8088),
    "Ezhou": (30.3972, 114.8842),
    "Zhongwei": (37.5139, 105.1884),
    "Hegang": (47.3139, 130.2775),
    "Fangchenggang": (21.6, 108.3),
    "Sanya": (18.2533, 109.5036),
    "Ruzhou": (34.1736, 112.839),
    "Danzhou": (19.5, 109.5833),
    "Yingde": (24.1878, 113.4042),
    "Luoding": (22.7645, 111.5745),
    "Qitaihe": (45.768, 131.0063),
    "Yangchun": (22.1717, 111.7846),
    "Lhasa": (29.6458, 91.1408),
    "Taoyuan County": (28.9036, 111.4769),
    "Tongchuan": (34.9057, 108.9422),
    "Shigatse": (29.25, 88.8833),
    "Laiyang": (36.9758, 120.7136),
    "Gongyi": (34.7513, 112.9854),
    "Kashgar": (39.45, 75.9833),
    "Xingning": (24.1681, 115.6669),
    "Chamdo": (31.1375, 97.1777),
    "Shizuishan": (39.0099, 106.3694),
    "Jiyuan": (35.0833, 112.5833),
    "Shihezi": (44.3, 86.0333),
    "Turpan": (42.95, 89.1822),
    "Macau": (22.19, 113.5381),
    "Hami": (42.8322, 93.5155),
    "Meihekou": (42.5279, 125.678),
    "Xinji": (37.9423, 115.2118),
    "Tianchang": (32.6853, 119.0041),
    "Wuhai": (39.6844, 106.8158),
    "Nagqu": (31.4766, 92.0569),
    "Karamay": (45.5928, 84.8711),
    "Jinchang": (38.5168, 102.1866),
    "Tieli": (46.9838, 128.04),
    "Yidu": (30.388, 111.4505),
    "Hancheng": (35.4768, 110.4419),
    "Lianzhou": (24.7868, 112.3735),
    "Ulanhot": (46.0726, 122.0719),
    "Lhoka": (28.5561, 92.5567),
    "Aral": (40.5418, 81.2657),
    "Jiayuguan": (39.8112, 98.2862),
    "Ruili": (24.0115, 97.858),
    "Tumxuk": (39.8675, 79.0722),
    "Manzhouli": (49.5881, 117.4525),
    "Hunchun": (42.8679, 130.3585),
    "Nyingchi": (29.6597, 94.3594),
    "Golmud": (36.4067, 94.9006),
    "Linjiang": (41.8471, 126.9301),
    "Tacheng": (46.8511, 83.1488),
    "Suifenhe": (44.3945, 131.1563),
    "Bengaluru": (12.9791, 77.5913),
    "Ahmedabad": (23.0219, 72.5797),
    "Prayagraj": (25.45, 81.85),
    "Surat": (21.205, 72.84),
    "Kanpur": (26.4725, 80.3311),
    "Kolkata": (22.5675, 88.37),
    "Lucknow": (26.847, 80.947),
    "Vadodara": (22.3, 73.2),
    "Jaipur": (26.915, 75.82),
    "Nagpur": (21.1497, 79.0806),
    "Ghaziabad": (28.6667, 77.4167),
    "Rajkot": (22.2969, 70.7984),
    "Visakhapatnam": (17.6939, 83.2922),
    "Indore": (22.7186, 75.855),
    "Thane": (19.18, 72.9633),
    "Bhopal": (23.2589, 77.4125),
    "Patna": (25.61, 85.1414),
    "Ludhiana": (30.9083, 75.8486),
    "Agra": (27.18, 78.02),
    "Meerut": (28.98, 77.7064),
    "Madurai": (9.9197, 78.1194),
    "Jamshedpur": (22.8, 86.1833),
    "Nashik": (20.0, 73.7833),
    "Faridabad": (28.4112, 77.3132),
    "Jabalpur": (23.1667, 79.9333),
    "Vasai-Virar": (19.47, 72.8),
    "Varanasi": (25.3189, 83.0128),
    "Srinagar": (34.0911, 74.8061),
    "Aurangabad": (19.88, 75.32),
    "Amritsar": (31.6269, 74.877),
    "Navi Mumbai": (19.03, 73.01),
    "Guwahati": (26.1806, 91.7502),
    "Ranchi": (23.3556, 85.3347),
    "Gwalior": (26.2125, 78.1775),
    "Chandigarh": (30.7333, 76.7797),
    "Vijayawada": (16.5167, 80.6167),
    "Jodhpur": (26.2944, 73.0278),
    "Raipur": (21.2379, 81.6337),
    "Kota": (25.18, 75.83),
    "Bareilly": (28.364, 79.415),
    "Coimbatore": (11.0008, 76.9633),
    "Solapur": (17.6703, 75.9011),
    "Mysore": (12.3086, 76.6531),
    "Tiruchirappalli": (10.8269, 78.6928),
    "Moradabad": (28.8389, 78.7769),
    "Jamnagar": (22.47, 70.07),
    "Gurgaon": (28.4667, 77.0333),
    "Aligarh": (27.88, 78.08),
    "Jalandhar": (31.3256, 75.5792),
    "Bhubaneswar": (20.295, 85.825),
    "Mira-Bhayandar": (19.3, 72.85),
    "Thiruvananthapuram": (8.4875, 76.9525),
    "Dispur": (26.15, 91.77),
    "Bhiwandi": (19.3, 73.0667),
    "Saharanpur": (29.964, 77.546),
    "Warangal": (17.9756, 79.6011),
    "Kochi": (9.9667, 76.2833),
    "Gorakhpur": (26.7611, 83.3667),
    "Guntur": (16.3, 80.45),
    "Noida": (28.57, 77.32),
    "Bidhannagar": (22.58, 88.42),
    "Bhilai": (21.21, 81.38),
    "Mangaluru": (12.8717, 74.8425),
    "Kozhikode": (11.2478, 75.7803),
    "Cuttack": (20.45, 85.8667),
    "Dehradun": (30.318, 78.029),
    "Malegaon": (20.55, 74.55),
    "Durgapur": (23.55, 87.32),
    "Asansol": (23.6833, 86.9667),
    "Rourkela": (22.2492, 84.8828),
    "Amravati": (20.9333, 77.75),
    "Nellore": (14.4333, 79.9667),
    "Ajmer": (26.468, 74.639),
    "Bikaner": (28.0181, 73.3169),
    "Agartala": (23.8333, 91.2667),
    "Ujjain": (23.1828, 75.7772),
    "Jhansi": (25.4486, 78.5696),
    "Ulhasnagar": (19.2167, 73.15),
    "Jammu": (32.7357, 74.8691),
    "Belgaum": (15.8569, 74.5044),
    "Vellore": (12.9333, 79.1333),
    "Gaya": (24.75, 85.0167),
    "Junagadh": (21.52, 70.47),
    "Jalgaon": (21.0167, 75.5667),
    "Kurnool": (15.8222, 78.035),
    "Udaipur": (24.5833, 73.6833),
    "Maheshtala": (22.506, 88.2502),
    "Sangli": (16.8667, 74.5667),
    "Akola": (20.7333, 77.0),
    "Rajpur Sonarpur": (22.4409, 88.4246),
    "Tirunelveli": (8.7289, 77.7081),
    "Ballari": (15.15, 76.9333),
    "Bhagalpur": (25.25, 87.0167),
    "Patiala": (30.5667, 76.6333),
    "South Dum Dum": (22.61, 88.4),
    "Kollam": (8.8853, 76.5864),
    "Muzaffarnagar": (29.4675, 77.705),
    "Bhatpara": (22.87, 88.41),
    "Kakinada": (16.9333, 82.2167),
    "Panihati": (22.69, 88.37),
    "Dhule": (20.9014, 74.7775),
    "Rohtak": (28.9, 76.5667),
    "Bilaspur": (22.08, 82.1556),
    "Bhilwara": (25.3469, 74.6408),
    "Ahilyanagar": (19.0833, 74.7333),
    "Kadapa": (14.4667, 78.8167),
    "Rajahmundry": (16.9833, 81.7833),
    "Alwar": (27.5667, 76.6167),
    "Muzaffarpur": (26.12, 85.3833),
    "Vijayapura": (16.8244, 75.7154),
    "Thrissur": (10.52, 76.21),
    "Bardhaman": (23.25, 87.85),
    "Nizamabad": (18.672, 78.094),
    "Purnia": (25.78, 87.47),
    "Hisar": (29.1489, 75.7367),
    "Siliguri": (26.72, 88.42),
    "Aizawl": (23.7333, 92.7167),
    "Bally": (22.65, 88.34),
    "Gandhinagar": (23.223, 72.65),
    "Farrukhabad": (27.3914, 79.5792),
    "Karimnagar": (18.4355, 79.1337),
    "Ichalkaranji": (16.7, 74.47),
    "Tirupati": (13.65, 79.42),
    "Bathinda": (30.2133, 74.9519),
    "Jalna": (19.8333, 75.8833),
    "Satna": (24.5984, 80.8281),
    "Barasat": (22.7191, 88.4826),
    "Ratlam": (23.3167, 75.0667),
    "Bandel": (22.9228, 88.3794),
    "Handwara": (34.4, 74.28),
    "Durg": (21.19, 81.28),
    "Imphal": (24.82, 93.95),
    "Ananthapuram": (14.6833, 77.6),
    "Latur": (18.4, 76.5833),
    "Arrah": (25.5631, 84.6711),
    "Narnaul": (28.0444, 76.1083),
    "Ongole": (15.5, 80.05),
    "Godavarikhani": (18.7519, 79.5133),
    "Bharatpur": (27.2172, 77.49),
    "Begusarai": (25.42, 86.13),
    "New Delhi": (28.6139, 77.2089),
    "Sri Ganganagar": (29.9167, 73.8833),
    "North Dum Dum": (22.6521, 88.4191),
    "Baranagar": (22.64, 88.37),
    "Uluberia": (22.47, 88.11),
    "Pali": (25.7725, 73.3233),
    "Vizianagaram": (18.1167, 83.4167),
    "Nadiad": (22.7, 72.87),
    "Thanjavur": (10.787, 79.1378),
    "Katni": (23.837, 80.3943),
    "Sambhal": (28.5852, 78.5709),
    "Munger": (25.381, 86.465),
    "Kharagpur": (22.3302, 87.3237),
    "Dindigul": (10.362, 77.9736),
    "Shimla": (31.1033, 77.1722),
    "Malda": (25.0044, 88.1458),
    "Eluru": (16.7117, 81.1031),
    "Deoghar": (24.48, 86.7),
    "Puri": (19.8, 85.8167),
    "Haldia": (22.0605, 88.1098),
    "Amroha": (28.9044, 78.4675),
    "Chakradharpur": (22.7, 85.63),
    "Bhiwani": (28.7931, 76.1397),
    "Madhyamgram": (22.7, 88.45),
    "Berhampore": (24.1, 88.25),
    "Ambala": (30.3786, 76.7725),
    "Mahabubnagar": (16.7333, 77.9833),
    "Bhuj": (23.2539, 69.6692),
    "Bahraich": (27.5742, 81.5958),
    "Mehsana": (23.6, 72.4),
    "Raiganj": (25.6167, 88.1167),
    "Batala": (31.8186, 75.2028),
    "Serampore": (22.75, 88.34),
    "Jaunpur": (25.7536, 82.6869),
    "Cuddalore": (11.75, 79.75),
    "Deo Town": (24.6561, 84.4356),
    "Bidar": (17.9, 77.55),
    "Machilipatnam": (16.1667, 81.1333),
    "Medinipur": (22.4333, 87.3333),
    "Chandannagar": (22.8671, 88.3674),
    "Udupi": (13.3347, 74.7462),
    "Faizabad": (26.7833, 82.1333),
    "Kanchipuram": (12.8308, 79.7078),
    "Moga": (30.8167, 75.17),
    "Godhra": (22.7772, 73.6203),
    "Chittoor": (13.2, 79.1167),
    "Bharuch": (21.6948, 72.9805),
    "Erode": (11.3428, 77.7274),
    "Saharsa": (25.88, 86.6),
    "Vidisha": (23.5239, 77.8061),
    "Pathankot": (32.2689, 75.6497),
    "Rudrapur": (28.9708, 79.3972),
    "Dibrugarh": (27.4833, 94.9),
    "Veraval": (20.9, 70.37),
    "Hazaribagh": (23.98, 85.35),
    "Krishnanagar": (23.4, 88.5),
    "Barrackpore": (22.7642, 88.3776),
    "Santipur": (23.25, 88.43),
    "Beawar": (26.1011, 74.3203),
    "Habra": (22.8419, 88.6561),
    "Nagaon": (26.3492, 92.6861),
    "Beed": (18.9904, 75.7542),
    "Mohali": (30.7194, 76.7181),
    "Etah": (27.63, 78.67),
    "Tiruvannamalai": (12.25, 79.0833),
    "Shillong": (25.5744, 91.8789),
    "Mandsaur": (24.03, 75.08),
    "Srikakulam": (18.3, 83.9),
    "Hassan": (13.005, 76.1028),
    "Gondia": (21.45, 80.2),
    "North Barrackpore": (22.7769, 88.3459),
    "Mandya": (12.5242, 76.8958),
    "Palakkad": (10.7792, 76.6547),
    "Anand": (22.556, 72.951),
    "Hanumangarh": (29.5833, 74.3167),
    "Kanchrapara": (22.9456, 88.4332),
    "Nabadwip": (23.4088, 88.3657),
    "Patan": (23.85, 72.1167),
    "Jagdalpur": (19.07, 82.03),
    "Basirhat": (22.6612, 88.866),
    "Rishra": (22.71, 88.35),
    "Baidyabati": (22.79, 88.32),
    "Ghazipur": (25.5833, 83.5667),
    "Satara": (17.6833, 73.9833),
    "Dabgram": (26.6961, 88.4571),
    "Darjeeling": (27.0375, 88.2631),
    "Sopore": (34.2864, 74.4628),
    "Narmadapuram": (22.75, 77.72),
    "Yavatmal": (20.4, 78.1333),
    "Chittorgarh": (24.8894, 74.6239),
    "Ambikapur": (23.12, 83.2),
    "Dharashiv": (18.1667, 76.05),
    "Champdani": (22.8, 88.37),
    "Mancherial": (18.8721, 79.4472),
    "Bangaon": (23.0435, 88.8318),
    "Khardah": (22.72, 88.38),
    "Greater Noida": (28.4962, 77.536),
    "Jalpaiguri": (26.5167, 88.7333),
    "Karaikudi": (10.0667, 78.7833),
    "Bansberia": (22.97, 88.4),
    "Bundi": (25.4383, 75.6372),
    "Amaravati": (16.5131, 80.5165),
    "Kapurthala": (31.38, 75.3811),
    "Bhadreswar": (22.82, 88.35),
    "Srivijayapuram": (11.6667, 92.75),
    "Kalyani": (22.975, 88.4344),
    "Tezpur": (26.62, 92.79),
    "Gangtok": (27.33, 88.62),
    "Houston": (29.7628, -95.3831),
    "Phoenix": (33.4483, -112.0739),
    "San Antonio": (29.425, -98.4939),
    "San Diego": (32.715, -117.1625),
    "Dallas": (32.7792, -96.8089),
    "San Jose": (37.3042, -121.8728),
    "Fort Worth": (32.7564, -97.3325),
    "Columbus": (39.9622, -83.0006),
    "Indianapolis": (39.7686, -86.1581),
    "Charlotte": (35.2269, -80.8433),
    "Washington, D.C.": (38.895, -77.0367),
    "Nashville": (36.1622, -86.7744),
    "Oklahoma": (35.4822, -97.5353),
    "El Paso": (31.7592, -106.4886),
    "Las Vegas": (36.1672, -115.1486),
    "Memphis": (35.1461, -90.0519),
    "Baltimore": (39.2864, -76.615),
    "Milwaukee": (43.05, -87.95),
    "Albuquerque": (35.0844, -106.6503),
    "Tucson": (32.2217, -110.9264),
    "Fresno": (36.7817, -119.7922),
    "Sacramento": (38.5753, -121.4861),
    "Kansas": (39.05, -94.5833),
    "Mesa": (33.415, -111.8314),
    "Omaha": (41.2586, -95.9375),
    "Colorado Springs": (38.8633, -104.7919),
    "Raleigh": (35.78, -78.64),
    "Long Beach": (33.7681, -118.1956),
    "Virginia Beach": (36.8506, -75.9779),
    "Oakland": (37.8, -122.25),
    "Tulsa": (36.1314, -95.9372),
    "Bakersfield": (35.3733, -119.0189),
    "Wichita": (37.6889, -97.3361),
    "Arlington": (32.705, -97.1228),
    "Aurora": (39.7108, -104.8125),
    "New Orleans": (29.9761, -90.0783),
    "Cleveland": (41.4992, -81.6947),
    "Honolulu": (21.3047, -157.8572),
    "Anaheim": (33.8361, -117.8897),
    "San Juan": (18.4653, -66.1167),
    "Orlando": (28.5336, -81.3867),
    "Lexington": (38.0497, -84.4586),
    "Stockton": (37.9575, -121.2925),
    "Corpus Christi": (27.7428, -97.4019),
    "Henderson": (36.0317, -114.9814),
    "Riverside": (33.9822, -117.3725),
    "Newark": (40.7356, -74.1722),
    "Saint Paul": (44.9442, -93.0936),
    "Santa Ana": (33.7408, -117.8814),
    "Cincinnati": (39.1, -84.5125),
    "Irvine": (33.6842, -117.7925),
    "Pittsburgh": (40.4417, -80.0),
    "St. Louis": (38.6264, -90.1994),
    "Greensboro": (36.0725, -79.7903),
    "Anchorage": (61.2167, -149.8936),
    "Lincoln": (40.8092, -96.6781),
    "Plano": (33.05, -96.75),
    "Durham": (35.9942, -78.8986),
    "Buffalo": (42.8864, -78.8781),
    "Chandler": (33.3033, -111.8414),
    "Chula Vista": (32.6278, -117.0481),
    "Toledo": (41.6528, -83.5378),
    "Madison": (43.0747, -89.3842),
    "Reno": (39.5261, -119.8125),
    "Fort Wayne": (41.0804, -85.1392),
    "North Las Vegas": (36.2006, -115.1216),
    "St. Petersburg": (27.7731, -82.64),
    "Lubbock": (33.585, -101.845),
    "Irving": (32.8117, -96.9508),
    "Laredo": (27.5061, -99.5072),
    "Winston-Salem": (36.1025, -80.2606),
    "Chesapeake": (36.7674, -76.2874),
    "Glendale": (33.5833, -112.2),
    "Louisville": (38.2561, -85.7514),
    "Garland": (32.9067, -96.635),
    "Scottsdale": (33.4931, -111.9261),
    "Norfolk": (36.8469, -76.2853),
    "Boise": (43.6136, -116.2378),
    "Fremont": (37.5486, -121.9883),
    "Spokane": (47.6572, -117.4235),
    "Santa Clarita": (34.3917, -118.5426),
    "Baton Rouge": (30.4475, -91.1786),
    "Richmond": (37.5408, -77.4367),
    "Hialeah": (25.8606, -80.2939),
    "San Bernardino": (34.1042, -117.2922),
    "Tacoma": (47.2525, -122.4475),
    "Modesto": (37.6614, -120.9944),
    "Babylon": (40.6944, -73.3294),
    "Huntsville": (34.7136, -86.5861),
    "Des Moines": (41.5908, -93.6208),
    "Rochester": (43.1656, -77.6114),
    "Moreno Valley": (33.9431, -117.2283),
    "Fayetteville": (35.0667, -78.9175),
    "Fontana": (34.1, -117.4667),
    "Worcester": (42.2714, -71.7989),
    "Port St. Lucie": (27.2758, -80.355),
    "Little Rock": (34.7444, -92.2881),
    "Oxnard": (34.1914, -119.1825),
    "Montgomery": (32.3675, -86.3),
    "Frisco": (33.1414, -96.8131),
    "Amarillo": (35.1992, -101.8453),
    "Grand Rapids": (42.9633, -85.6678),
    "Huntington Beach": (33.6928, -117.9997),
    "Overland Park": (38.9401, -94.6807),
    "Tallahassee": (30.4383, -84.2806),
    "Grand Prairie": (32.7153, -97.0169),
    "McKinney": (33.2, -96.6333),
    "Cape Coral": (26.6333, -81.9833),
    "Sioux Falls": (43.5461, -96.7314),
    "Peoria": (33.5825, -112.2386),
    "Providence": (41.824, -71.4128),
    "Knoxville": (35.9617, -83.9233),
    "Akron": (41.0731, -81.5178),
    "Shreveport": (32.5081, -93.7628),
    "Mobile": (30.7277, -88.0527),
    "Brownsville": (25.9303, -97.4844),
    "Newport News": (37.0708, -76.4844),
    "Chattanooga": (35.0456, -85.2672),
    "Tempe": (33.4256, -111.94),
    "Santa Rosa": (38.4486, -122.7047),
    "Elk Grove": (38.4383, -121.3819),
    "Ontario, California": (34.0528, -117.6278),
    "Rancho Cucamonga": (34.1233, -117.5794),
    "Oceanside": (33.2117, -117.3258),
    "Lancaster": (34.6869, -118.1542),
    "Garden Grove": (33.7789, -117.9603),
    "Pembroke Pines": (26.0125, -80.3136),
    "Fort Collins": (40.5667, -105.0833),
    "Palmdale": (34.5794, -118.1167),
    "Springfield": (37.195, -93.2861),
    "Clarksville": (36.5297, -87.3594),
    "Salinas": (36.6778, -121.6556),
    "Hayward": (37.6689, -122.0808),
    "Alexandria": (38.8047, -77.0472),
    "Macon": (32.8407, -83.6324),
    "Corona": (33.8667, -117.5667),
    "Lakewood": (39.7064, -105.1028),
    "Sunnyvale": (37.3689, -122.0369),
    "Jackson": (32.2989, -90.1847),
    "Killeen": (31.1056, -97.7267),
    "Hollywood": (26.0214, -80.175),
    "Murfreesboro": (35.8461, -86.3919),
    "Pasadena": (29.6761, -95.1739),
    "Bellevue": (47.6144, -122.1925),
    "Pomona": (34.0608, -117.7558),
    "Escondido": (33.1247, -117.0808),
    "Joliet": (41.5252, -88.0834),
    "Charleston": (32.7833, -79.9319),
    "Mesquite": (32.7828, -96.6097),
    "Bridgeport": (41.1833, -73.2),
    "Savannah": (32.0508, -81.1039),
    "Roseville": (38.7525, -121.2894),
    "Torrance": (33.8358, -118.3406),
    "Fullerton": (33.88, -117.9286),
    "Surprise": (33.6292, -112.3678),
    "McAllen": (26.2164, -98.2364),
    "Thornton": (39.9031, -104.954),
    "Visalia": (36.3303, -119.2925),
    "Olathe": (38.8808, -94.8031),
    "Gainesville": (29.6653, -82.3361),
    "West Valley": (40.6892, -111.9939),
    "Spotsylvania County": (38.18, -77.65),
    "Orange": (33.8031, -117.8325),
    "Denton": (33.2167, -97.1333),
    "Warren": (42.5125, -83.025),
    "Waco": (31.5492, -97.1472),
    "Cedar Rapids": (41.9833, -91.6686),
    "Dayton": (39.7667, -84.2),
    "Hampton": (37.0264, -76.3444),
    "Stamford": (41.0967, -73.5522),
    "Victorville": (34.5361, -117.2883),
    "Miramar": (25.9789, -80.2825),
    "Coral Springs": (26.2706, -80.2592),
    "Sterling Heights": (42.5803, -83.0303),
    "New Haven": (41.3083, -72.925),
    "Carrollton": (32.99, -96.8933),
    "Midland": (32.005, -102.0992),
    "Norman": (35.2208, -97.4436),
    "Santa Clara": (37.35, -121.9667),
    "Caguas": (18.2333, -66.0333),
    "Thousand Oaks": (34.1894, -118.875),
    "Topeka": (39.0483, -95.678),
    "Simi Valley": (34.2708, -118.7392),
    "Columbia": (38.9517, -92.3341),
    "Vallejo": (38.1041, -122.2566),
    "Fargo": (46.8772, -96.7894),
    "Pearland": (29.5544, -95.2958),
    "Concord": (37.9781, -122.0311),
    "Abilene": (32.4464, -99.7456),
    "Arvada": (39.8028, -105.0875),
    "Berkeley": (37.8703, -122.2681),
    "Ann Arbor": (42.2814, -83.7483),
    "Independence": (39.0925, -94.4139),
    "Lafayette": (30.2431, -92.0126),
    "Hartford": (41.7633, -72.685),
    "College Station": (30.6014, -96.3144),
    "Fairfield": (38.2578, -122.0542),
    "Palm Bay": (27.9979, -80.67),
    "Cambridge": (42.375, -71.1061),
    "Evansville": (37.9747, -87.5558),
    "Clearwater": (27.9656, -82.8),
    "Billings": (45.7867, -108.5372),
    "West Jordan": (40.6064, -111.9761),
    "Westminster": (39.8361, -105.0372),
    "Lowell": (42.6394, -71.3147),
    "Antioch": (38.005, -121.8058),
    "Beaumont": (30.08, -94.1267),
    "Provo": (40.2339, -111.6586),
    "Carlsbad": (33.1583, -117.351),
    "Waterbury": (41.5583, -73.0369),
    "Downey": (33.9381, -118.1308),
    "Gresham": (45.5036, -122.4394),
    "High Point": (35.9706, -79.9975),
    "Lansing": (42.7335, -84.5467),
    "Lakeland": (28.0411, -81.9589),
    "Pompano Beach": (26.2347, -80.1256),
    "Costa Mesa": (33.67, -117.91),
    "Pueblo": (38.2669, -104.6203),
    "Miami Gardens": (25.9419, -80.2456),
    "Las Cruces": (32.314, -106.7798),
    "Murrieta": (33.5694, -117.2025),
    "Ventura": (34.2678, -119.2542),
    "Everett": (47.9633, -122.2006),
    "Temecula": (33.5033, -117.1236),
    "West Covina": (34.0567, -117.9186),
    "El Monte": (34.0733, -118.0275),
    "Centennial": (39.5964, -104.8439),
    "Boulder": (40.0194, -105.2928),
    "Inglewood": (33.9575, -118.3461),
    "Burbank": (34.1803, -118.3283),
    "Daly": (37.6864, -122.4683),
    "Allen": (33.0997, -96.6631),
    "South Bend": (41.6764, -86.2503),
    "Norwalk": (33.9069, -118.0833),
    "Wichita Falls": (33.8969, -98.515),
    "Davenport": (41.5431, -90.5908),
    "Roanoke": (37.2706, -79.9425),
    "Karachi": (24.86, 67.01),
    "Lahore": (31.5497, 74.3436),
    "Faisalabad": (31.418, 73.079),
    "Rawalpindi": (33.6007, 73.0679),
    "Gujranwala": (32.15, 74.1833),
    "Peshawar": (34.0144, 71.5675),
    "Multan": (30.1978, 71.4711),
    "Islamabad": (33.6989, 73.0369),
    "Quetta": (30.1958, 67.0172),
    "Bahawalpur": (29.3956, 71.6722),
    "Parachinar": (33.8992, 70.1008),
    "Sargodha": (32.0836, 72.6711),
    "Shah Faisal Town": (24.8806, 67.1625),
    "Sialkot": (32.5, 74.5333),
    "Sahiwal": (30.6706, 73.1064),
    "Sukkur": (27.6995, 68.8673),
    "Larkana": (27.56, 68.2264),
    "Chiniot": (31.7167, 72.9833),
    "Sheikhupura": (31.7083, 74.0),
    "Rahim Yar Khan": (28.4206, 70.3033),
    "Jhang": (31.2681, 72.3181),
    "Wah Cantonment": (33.7892, 72.7253),
    "Dera Ghazi Khan": (30.05, 70.6333),
    "Gujrat": (32.5736, 74.0789),
    "Bhawana": (31.5664, 72.6483),
    "Malakwal": (32.5536, 73.2123),
    "Mardan": (34.1958, 72.0447),
    "Hafizabad": (32.0667, 73.6833),
    "Kasur": (31.1167, 74.45),
    "Jhelum": (32.9333, 73.7333),
    "Chakwal": (32.9329, 72.8629),
    "Khanewal": (30.3, 71.9333),
    "Mingora": (34.7717, 72.36),
    "Turbat": (26.0031, 63.0544),
    "Nawabshah": (26.2442, 68.41),
    "Gilgit": (35.9208, 74.3144),
    "Kotri": (25.366, 68.3122),
    "Mirpur Khas": (25.5269, 69.0111),
    "Okara": (30.81, 73.4597),
    "Mandi Bahauddin": (32.5861, 73.4917),
    "Pakpattan": (30.35, 73.4),
    "Thatta": (24.7461, 67.9243),
    "Jacobabad": (28.2769, 68.4514),
    "Burewala": (30.15, 72.6833),
    "Ahmedpur East": (29.1453, 71.2617),
    "Hub": (25.0289, 66.8819),
    "Chishtian": (29.7958, 72.8578),
    "Samundri": (31.0639, 72.9611),
    "Attock": (33.7667, 72.3667),
    "Kohat": (33.59, 71.4397),
    "Muzaffargarh": (30.0703, 71.1933),
    "Muridke": (31.802, 74.255),
    "Gojra": (31.15, 72.6833),
    "Ghoriwala": (32.9056, 70.7278),
    "Wazirabad": (32.4411, 74.1142),
    "Jaranwala": (31.3342, 73.4194),
    "Muzaffarabad": (34.37, 73.4711),
    "Abbottabad": (34.15, 73.2167),
    "Daska": (32.3167, 74.35),
    "Dadu": (26.7319, 67.775),
    "Khairpur": (27.5333, 68.7667),
    "Umerkot": (25.3614, 69.7361),
    "Lodhran": (29.5383, 71.6333),
    "Bahawalnagar": (29.9944, 73.2536),
    "Khuzdar": (27.8, 66.6167),
    "Tando Allahyar": (25.4667, 68.7167),
    "Mianwali": (32.5853, 71.5436),
    "Vehari": (30.0419, 72.3528),
    "Mirpur": (33.149, 73.7485),
    "Toba Tek Singh": (30.9667, 72.4833),
    "Kamalia": (30.7258, 72.6447),
    "Kot Addu": (30.47, 70.9644),
    "Ghotki": (28.0064, 69.315),
    "Nowshera": (34.0153, 71.9747),
    "Badin": (24.6558, 68.8383),
    "Swabi": (34.1167, 72.4667),
    "Tando Muhammad Khan": (25.1239, 68.5389),
    "Pattoki": (31.0214, 73.8528),
    "Shahdadpur": (25.9228, 68.6206),
    "Skardu": (35.3, 75.6167),
    "Khushab": (32.2917, 72.35),
    "Farooqabad": (31.7431, 73.8303),
    "Dera Ismail Khan": (31.8167, 70.9167),
    "Chaman": (30.921, 66.4597),
    "Charsadda": (34.1453, 71.7308),
    "Kandhkot": (28.4, 69.3),
    "Gujar Khan": (33.2508, 73.2966),
    "Kot Radha Kishan": (31.1667, 74.1),
    "Renala Khurd": (30.8783, 73.5958),
    "São Paulo (Brazil)": (-23.5504, -46.6339),
    "Brasília": (-15.7939, -47.8828),
    "Salvador": (-12.9831, -38.4928),
    "Belo Horizonte": (-19.9281, -43.9419),
    "Manaus": (-3.1189, -60.0217),
    "Curitiba": (-25.4297, -49.2719),
    "Recife": (-8.0539, -34.8808),
    "Goiânia": (-16.6806, -49.2564),
    "Porto Alegre": (-30.0328, -51.23),
    "Guarulhos": (-23.4667, -46.5333),
    "Belém": (-1.4558, -48.5039),
    "Nova Iguaçu": (-22.7589, -43.4508),
    "Campinas": (-22.9009, -47.0573),
    "São Luís": (-2.53, -44.3028),
    "Campo Grande": (-20.4686, -54.6222),
    "São Gonçalo": (-22.8269, -43.0539),
    "Duque de Caxias": (-22.7858, -43.3119),
    "Teresina": (-5.0903, -42.8167),
    "João Pessoa": (-7.12, -34.88),
    "São Bernardo do Campo": (-23.6939, -46.565),
    "Uberlândia": (-18.9189, -48.2769),
    "Natal": (-5.795, -35.2089),
    "Santo André": (-23.6533, -46.528),
    "São José dos Campos": (-23.1789, -45.8869),
    "Ribeirão Preto": (-21.1776, -47.8101),
    "Sorocaba": (-23.5019, -47.4578),
    "Jaboatão dos Guararapes": (-8.1128, -35.015),
    "Cuiabá": (-15.5958, -56.0969),
    "Contagem": (-19.9319, -44.0539),
    "Aracaju": (-10.9167, -37.05),
    "Joinville": (-26.3039, -48.8458),
    "Florianópolis": (-27.5933, -48.553),
    "Londrina": (-23.31, -51.1628),
    "Serra": (-20.1289, -40.3078),
    "Aparecida de Goiânia": (-16.8219, -49.245),
    "Juiz de Fora": (-21.75, -43.35),
    "Caxias do Sul": (-29.1678, -51.1789),
    "Campos dos Goytacazes": (-21.7539, -41.3239),
    "Porto Velho": (-8.7619, -63.9039),
    "São José do Rio Preto": (-20.82, -49.3789),
    "Niterói": (-22.8833, -43.1036),
    "Belford Roxo": (-22.7639, -43.3989),
    "Ananindeua": (-1.3658, -48.3719),
    "Vila Velha": (-20.3364, -40.2936),
    "Mogi das Cruzes": (-23.5228, -46.1931),
    "Betim": (-19.9678, -44.1978),
    "Macapá": (0.033, -51.0653),
    "São João de Meriti": (-22.8039, -43.3719),
    "Montes Claros": (-16.7322, -43.8636),
    "Piracicaba": (-22.7252, -47.6493),
    "Campina Grande": (-7.2306, -35.8811),
    "Santos": (-23.9336, -46.3286),
    "Petrolina": (-9.3928, -40.5078),
    "Mauá": (-23.6678, -46.4608),
    "Boa Vista": (2.82, -60.6719),
    "Maringá": (-23.425, -51.9389),
    "Caruaru": (-8.2828, -35.9758),
    "Carapicuíba": (-23.5228, -46.8358),
    "Jundiaí": (-23.1864, -46.8842),
    "Anápolis": (-16.3269, -48.9528),
    "Vitória da Conquista": (-14.8658, -40.8389),
    "Diadema": (-23.6858, -46.6228),
    "Bauru": (-22.3147, -49.0606),
    "Cariacica": (-20.2639, -40.42),
    "Ponta Grossa": (-25.095, -50.1619),
    "Itaquaquecetuba": (-23.4864, -46.3486),
    "Franca": (-20.5389, -47.4008),
    "Rio Branco": (-9.9781, -67.8117),
    "Blumenau": (-26.9189, -49.0658),
    "Santarém": (-2.4428, -54.7078),
    "Uberaba": (-19.7478, -47.9319),
    "Caucaia": (-3.7328, -38.6558),
    "Olinda": (-8.0089, -34.855),
    "Praia Grande": (-24.009, -46.4145),
    "São José dos Pinhais": (-25.535, -49.2058),
    "Cascavel": (-24.9558, -53.455),
    "Canoas": (-29.92, -51.18),
    "Pelotas": (-31.7719, -52.3428),
    "Paulista": (-7.9408, -34.8728),
    "Barueri": (-23.5111, -46.8764),
    "São Vicente": (-23.9664, -46.3861),
    "Ribeirão das Neves": (-19.7669, -44.0869),
    "Camaçari": (-12.6978, -38.3239),
    "Suzano": (-23.5428, -46.3108),
    "Várzea Grande": (-15.6469, -56.1328),
    "Taubaté": (-23.0333, -45.55),
    "Guarujá": (-23.9936, -46.2564),
    "Limeira": (-22.5647, -47.4017),
    "Parauapebas": (-6.0678, -49.9019),
    "Palmas": (-10.1844, -48.3336),
    "São José": (-27.615, -48.6278),
    "Marabá": (-5.3689, -49.1178),
    "Cotia": (-23.6039, -46.9189),
    "Juazeiro do Norte": (-7.2128, -39.315),
    "Gravataí": (-29.9433, -50.9939),
    "Foz do Iguaçu": (-25.54, -54.5875),
    "Santa Maria": (-29.6839, -53.8069),
    "Volta Redonda": (-22.5231, -44.1042),
    "Petrópolis": (-22.505, -43.1789),
    "Embu das Artes": (-23.6489, -46.8519),
    "Taboão da Serra": (-23.6019, -46.7528),
    "Imperatriz": (-5.5258, -47.4758),
    "Governador Valadares": (-18.8508, -41.9489),
    "Sumaré": (-22.8219, -47.2669),
    "Mossoró": (-5.1878, -37.3439),
    "Itajaí": (-26.9078, -48.6619),
    "Dourados": (-22.2208, -54.8058),
    "Rondonópolis": (-16.4708, -54.6358),
    "Viamão": (-30.0808, -51.0228),
    "Indaiatuba": (-23.0903, -47.2181),
    "São Carlos": (-22.0178, -47.8908),
    "Araraquara": (-21.7939, -48.1758),
    "Palhoça": (-27.6444, -48.6678),
    "Parnamirim": (-5.9158, -35.2628),
    "Colombo": (-25.2919, -49.2239),
    "Americana": (-22.7389, -47.3308),
    "Macaé": (-22.3708, -41.7869),
    "Águas Lindas de Goiás": (-15.7619, -48.2819),
    "São José de Ribamar": (-2.5619, -44.0539),
    "Sete Lagoas": (-19.4658, -44.2469),
    "Marília": (-22.2139, -49.9458),
    "São Leopoldo": (-29.76, -51.1469),
    "Jacareí": (-23.305, -45.9658),
    "Cabo Frio": (-22.8789, -42.0189),
    "Novo Hamburgo": (-29.6778, -51.1308),
    "Arapiraca": (-9.75, -36.65),
    "Maracanaú": (-3.8794, -38.625),
    "Santa Luzia": (-19.77, -43.8508),
    "Divinópolis": (-20.1389, -44.8839),
    "Magé": (-22.6528, -43.0408),
    "Ipatinga": (-19.4678, -42.5369),
    "Rio Verde": (-17.7978, -50.9278),
    "Presidente Prudente": (-22.1258, -51.3889),
    "Itaboraí": (-22.7475, -42.8558),
    "Chapecó": (-27.0958, -52.6178),
    "Sinop": (-11.8639, -55.5039),
    "Ilhéus": (-14.7889, -39.0489),
    "Itapevi": (-23.5489, -46.9342),
    "Luziânia": (-16.2528, -47.95),
    "Lauro de Freitas": (-12.8939, -38.3269),
    "Cabo de Santo Agostinho": (-8.2869, -35.035),
    "Hortolândia": (-22.8583, -47.22),
    "Passo Fundo": (-28.2628, -52.4069),
    "Maricá": (-22.9189, -42.8189),
    "Criciúma": (-28.6775, -49.3697),
    "Castanhal": (-1.2969, -47.9219),
    "Araçatuba": (-21.2089, -50.4328),
    "Nova Friburgo": (-22.2819, -42.5308),
    "Sobral": (-3.6856, -40.3442),
    "Rio Claro": (-22.4108, -47.5608),
    "Valparaíso de Goiás": (-16.0658, -47.9789),
    "Nossa Senhora do Socorro": (-10.855, -37.1258),
    "Rio Grande": (-32.035, -52.0989),
    "Mesquita": (-22.7819, -43.4289),
    "Alvorada": (-29.99, -51.0839),
    "Itabuna": (-14.7858, -39.28),
    "Teresópolis": (-22.4119, -42.9658),
    "Bragança Paulista": (-22.9189, -46.5425),
    "Barra Mansa": (-22.5439, -44.1708),
    "Ferraz de Vasconcelos": (-23.5411, -46.3689),
    "Ibirité": (-20.0219, -44.0589),
    "Linhares": (-19.3987, -40.0651),
    "Santa Bárbara d'Oeste": (-22.7539, -47.4139),
    "Jaraguá do Sul": (-26.4858, -49.0669),
    "Porto Seguro": (-16.45, -39.065),
    "Guarapuava": (-25.395, -51.4578),
    "Barreiras": (-12.1528, -44.99),
    "Araguaína": (-7.1908, -48.2069),
    "Jequié": (-13.8578, -40.0839),
    "Francisco Morato": (-23.2817, -46.7425),
    "Itapecerica da Serra": (-23.7172, -46.8494),
    "Atibaia": (-23.1172, -46.5506),
    "Itu": (-23.2642, -47.2992),
    "Caxias": (-4.8589, -43.3558),
    "São Caetano do Sul": (-23.6132, -46.5712),
    "Lages": (-27.8158, -50.3258),
    "Teixeira de Freitas": (-17.535, -39.7419),
    "Poços de Caldas": (-21.7878, -46.5608),
    "Parnaíba": (-2.905, -41.7769),
    "Alagoinhas": (-12.1356, -38.4189),
    "Pindamonhangaba": (-22.9239, -45.4617),
    "Mogi Guaçu": (-22.3719, -46.9419),
    "Patos de Minas": (-18.5869, -46.5147),
    "Abaetetuba": (-1.7178, -48.8828),
    "Paranaguá": (-25.5208, -48.5094),
    "Itapetininga": (-23.5917, -48.0531),
    "Rio das Ostras": (-22.5269, -41.945),
    "Camaragibe": (-8.0219, -34.9808),
    "Santana de Parnaíba": (-23.4439, -46.9178),
    "Pouso Alegre": (-22.2281, -45.9336),
    "Araucária": (-25.5928, -49.41),
    "Botucatu": (-22.8858, -48.445),
    "Santa Rita": (-7.1139, -34.9778),
    "Fazenda Rio Grande": (-25.6578, -49.3078),
    "Nilópolis": (-22.8078, -43.4139),
    "Franco da Rocha": (-23.3286, -46.7244),
    "Paço do Lumiar": (-2.5319, -44.1078),
    "Vitória de Santo Antão": (-8.1264, -35.3075),
    "São Lourenço da Mata": (-8.0019, -35.0178),
    "Varginha": (-21.5508, -45.43),
    "Garanhuns": (-8.8903, -36.4928),
    "Trindade": (-16.6489, -49.4889),
    "Caraguatatuba": (-23.62, -45.4128),
    "Brusque": (-27.0978, -48.9178),
    "Queimados": (-22.7158, -43.555),
    "Ji-Paraná": (-10.8853, -61.9517),
    "Balneário Camboriú": (-26.9908, -48.635),
    "Araruama": (-22.8728, -42.3428),
    "Sabará": (-19.8858, -43.8069),
    "Teófilo Otoni": (-17.8578, -41.505),
    "Simões Filho": (-12.7839, -38.4039),
    "Campo Largo": (-25.4589, -49.5278),
    "Guarapari": (-20.6578, -40.5108),
    "Cachoeirinha": (-29.9508, -51.0939),
    "Marituba": (-1.355, -48.3419),
    "Itaituba": (-4.2758, -55.9839),
    "São Mateus": (-18.7158, -39.8589),
    "Salto": (-23.2008, -47.2869),
    "Cametá": (-2.2439, -49.4958),
    "Jaú": (-22.2958, -48.5578),
    "Santa Cruz do Sul": (-29.7178, -52.4258),
    "Valinhos": (-22.9708, -46.9958),
    "Três Lagoas": (-20.7511, -51.6783),
    "Sapucaia do Sul": (-29.8389, -51.1439),
    "Cubatão": (-23.8953, -46.4256),
    "Conselheiro Lafaiete": (-20.66, -43.7858),
    "Itapipoca": (-3.4939, -39.5789),
    "Crato": (-7.2339, -39.4089),
    "Araras": (-22.3569, -47.3839),
    "Apucarana": (-23.5508, -51.4608),
    "Colatina": (-19.5389, -40.6308),
    "Vespasiano": (-19.6919, -43.9228),
    "Votorantim": (-23.5469, -47.4378),
    "Pinhais": (-25.445, -49.1928),
    "Barretos": (-20.5569, -48.5678),
    "Sertãozinho": (-21.1378, -47.99),
    "Barcarena": (-1.5058, -48.6258),
    "Jandira": (-23.5278, -46.9028),
    "Altamira": (-3.2028, -52.2058),
    "Barbacena": (-21.2258, -43.7739),
    "Resende": (-22.4689, -44.4469),
    "Codó": (-4.455, -43.8858),
    "Birigui": (-21.2889, -50.34),
    "Bento Gonçalves": (-29.1708, -51.5189),
    "Catanduva": (-21.1378, -48.9728),
    "Bragança": (-1.0628, -46.7728),
    "Itatiba": (-23.0058, -46.8389),
    "Eunápolis": (-16.3778, -39.58),
    "Almirante Tamandaré": (-25.325, -49.31),
    "Arapongas": (-23.4189, -51.4239),
    "Guaratinguetá": (-22.8167, -45.2278),
    "Piraquara": (-25.4419, -49.0628),
    "Bagé": (-31.3308, -54.1069),
    "Araguari": (-18.6489, -48.1869),
    "Uruguaiana": (-29.755, -57.0878),
    "Umuarama": (-23.7658, -53.325),
    "Itaguaí": (-22.8519, -43.775),
    "Tatuí": (-23.3556, -47.8569),
    "Formosa": (-15.5369, -47.3339),
    "Várzea Paulista": (-23.2108, -46.8278),
    "Ribeirão Pires": (-23.7108, -46.4128),
    "Igarassu": (-7.8339, -34.9058),
    "Catalão": (-18.17, -47.9419),
    "Itabira": (-19.6189, -43.2269),
    "Paulo Afonso": (-9.4078, -38.2219),
    "Passos": (-20.7189, -46.61),
    "Araxá": (-19.5928, -46.9408),
    "Ariquemes": (-9.9161, -63.0408),
    "Tubarão": (-28.4669, -49.0069),
    "Itumbiara": (-18.42, -49.2178),
    "Santana": (-0.035, -51.175),
    "Cambé": (-23.2758, -51.2778),
    "Breves": (-1.6804, -50.4791),
    "Açailândia": (-4.9469, -47.505),
    "Balsas": (-7.5328, -46.0358),
    "Erechim": (-27.6339, -52.2739),
    "Nova Serrana": (-19.8758, -44.9839),
    "Leme": (-22.1858, -47.39),
    "Maranguape": (-3.89, -38.6858),
    "Assis": (-22.6619, -50.4119),
    "Lavras": (-21.245, -45.0),
    "Coronel Fabriciano": (-19.5189, -42.6289),
    "Muriaé": (-21.1308, -42.3658),
    "São Pedro da Aldeia": (-22.8389, -42.1028),
    "Ourinhos": (-22.9789, -49.8708),
    "Bacabal": (-4.225, -44.78),
    "Mairiporã": (-23.3189, -46.5869),
    "Ubá": (-21.12, -42.9428),
    "Aracruz": (-19.82, -40.2728),
    "Patos": (-7.0244, -37.28),
    "Santo Antônio de Jesus": (-12.9692, -39.2611),
    "Ituiutaba": (-18.9689, -49.465),
    "Lagarto": (-10.9169, -37.65),
    "Itaperuna": (-21.205, -41.8878),
    "Kano": (12.0022, 8.592),
    "Port Harcourt": (4.7655, 7.0163),
    "Ibadan": (7.3775, 3.9058),
    "Benin": (6.3176, 5.6145),
    "Abuja": (9.0556, 7.4914),
    "Onitsha": (6.133, 6.7924),
    "Maiduguri": (11.8333, 13.15),
    "Ogbomosho": (8.1333, 4.25),
    "Enugu": (6.4403, 7.4942),
    "Owerri": (5.4833, 7.0333),
    "Abeokuta": (7.15, 3.35),
    "Ikeja": (6.6186, 3.3426),
    "Akure": (7.25, 5.195),
    "Ilorin": (8.5, 4.55),
    "Jos": (9.9333, 8.8833),
    "Zaria": (11.0667, 7.7),
    "Kaduna": (10.5231, 7.4403),
    "Calabar": (4.95, 8.325),
    "Osogbo": (7.7597, 4.5761),
    "Bauchi": (10.3158, 9.8442),
    "Lokoja": (8.205, 6.5631),
    "Jalingo": (8.9, 11.3667),
    "Makurdi": (7.7306, 8.5361),
    "Ile Ife": (7.4667, 4.5667),
    "Ado Ekiti": (7.6167, 5.2167),
    "Dutse": (11.7024, 9.334),
    "Awka": (6.222, 7.0821),
    "Uyo": (5.05, 7.9333),
    "Sokoto": (13.0622, 5.2339),
    "Lekki": (6.4934, 3.7207),
    "Yola": (9.2, 12.4833),
    "Oyo": (7.8333, 3.9333),
    "Gusau": (12.1667, 6.6667),
    "Iseyin": (7.9667, 3.6),
    "Umuahia": (5.5333, 7.4833),
    "Ondo": (7.0833, 4.8333),
    "Yenegoa": (4.925, 6.2639),
    "Owo": (7.1962, 5.5868),
    "Lafia": (8.6401, 8.6996),
    "Minna": (9.6139, 6.5569),
    "Okene": (7.5511, 6.2358),
    "Katsina": (12.9889, 7.6008),
    "Warri": (5.5167, 5.75),
    "Daura": (13.0315, 8.3225),
    "Apapa": (6.4489, 3.3589),
    "Ilesa": (7.6333, 4.75),
    "Burutu": (5.35, 5.5167),
    "Baure": (12.7889, 8.7892),
    "Mai'Adua": (13.1906, 8.2117),
    "Yewa North": (7.15, 2.9167),
    "Offa": (8.1491, 4.7207),
    "Zango": (12.9333, 8.5333),
    "Ipokia": (6.6181, 2.8106),
    "Kwale": (6.1978, 6.7285),
    "Bakassi": (4.6004, 8.5697),
    "Ikere": (7.5, 5.2333),
    "Hadejia": (12.45, 10.04),
    "Udu": (5.75, 5.7167),
    "Damaturu": (11.75, 11.9667),
    "Ikot Ekpene": (5.1819, 7.7147),
    "Idanre": (7.1045, 5.131),
    "Birnin Kebbi": (12.45, 4.2),
    "Dhaka": (23.7289, 90.3944),
    "Chittagong": (22.335, 91.8325),
    "Gazipur": (23.9889, 90.375),
    "Narayanganj": (23.6167, 90.5),
    "Khulna": (22.8167, 89.55),
    "Tongi": (23.8833, 90.4),
    "Sirajganj": (24.45, 89.7167),
    "Rajshahi": (24.3667, 88.6),
    "Bogura": (24.85, 89.3667),
    "Savar": (23.8482, 90.2577),
    "Barishal": (22.7, 90.3667),
    "Cumilla": (23.45, 91.2),
    "Rangpur": (25.749, 89.247),
    "Brahmanbaria": (23.9723, 91.1099),
    "Sreepur": (24.2011, 90.4843),
    "Mymensingh": (24.75, 90.4167),
    "Siddhirganj": (23.6833, 90.5167),
    "Kaliakair": (24.1, 90.28),
    "Faridpur": (23.5986, 89.8353),
    "Feni": (23.015, 91.3967),
    "Kushtia": (23.9088, 89.1221),
    "Tangail": (24.4, 90.0),
    "Dinajpur": (25.63, 88.65),
    "Jashore": (23.1667, 89.2167),
    "Tarabo": (23.723, 90.507),
    "Cox's Bazar": (21.5833, 92.0167),
    "Narsingdi": (23.9, 90.7167),
    "Naogaon": (24.9, 88.75),
    "Pabna": (24.0167, 89.3),
    "Jamalpur": (24.92, 89.96),
    "Jhenaidah": (23.5, 89.18),
    "Satkhira": (22.7167, 89.1),
    "Maijdee": (22.8333, 91.1),
    "Kishoreganj": (24.4333, 90.7833),
    "Chowmuhani": (22.9393, 91.1094),
    "Veracruz": (19.1903, -96.1533),
    "San Luis Potosí": (22.1497, -100.975),
    "Tijuana": (32.5364, -117.0372),
    "León de Los Aldama": (21.1219, -101.6833),
    "Ciudad Juárez": (31.6685, -106.42),
    "Zapopan": (20.7208, -103.3911),
    "Monterrey": (25.6844, -100.3181),
    "Aguascalientes": (21.8808, -102.2961),
    "Chihuahua": (28.6369, -106.0769),
    "Mérida": (20.97, -89.62),
    "Cancun": (21.1614, -86.8248),
    "Saltillo": (25.4231, -100.9919),
    "Hermosillo": (29.075, -110.9583),
    "Mexicali": (32.636, -115.4759),
    "Culiacán": (24.7903, -107.3878),
    "Santiago de Querétaro": (20.5881, -100.3881),
    "Guadalupe": (25.6716, -100.2145),
    "Reynosa": (26.0922, -98.2778),
    "Torreón": (25.5439, -103.419),
    "Acapulco de Juárez": (16.8629, -99.887),
    "Victoria de Durango": (24.0228, -104.6544),
    "Tlaquepaque": (20.6369, -103.3096),
    "Irapuato": (20.6742, -101.3475),
    "Tuxtla Gutiérrez": (16.7531, -93.1156),
    "Ensenada": (31.8578, -116.6058),
    "Heroica Matamoros": (25.85, -97.5042),
    "Mazatlán": (23.2167, -106.4083),
    "Xalapa": (19.5312, -96.9159),
    "Tonalá": (20.6233, -103.2456),
    "Tepic": (21.5, -104.9),
    "Nuevo Laredo": (27.4774, -99.5252),
    "Gómez Palacio": (25.5725, -103.4945),
    "Villahermosa": (17.9878, -92.9194),
    "Ciudad Obregón": (27.4864, -109.9408),
    "Heroica Nogales": (31.3258, -110.9458),
    "La Paz": (24.1486, -110.3067),
    "San Francisco de Campeche": (19.8448, -90.5396),
    "Chilpancingo de los Bravo": (17.5519, -99.5013),
    "Puerto Vallarta": (20.6413, -105.2233),
    "Toluca de Lerdo": (19.2922, -99.6539),
    "Tapachula de Córdova y Ordóñez": (14.9111, -92.2643),
    "Zamora de Hidalgo": (19.9833, -102.2833),
    "Córdoba": (18.8942, -96.9347),
    "Cabo San Lucas": (22.8947, -109.9153),
    "Guanajuato": (21.0178, -101.2567),
    "Ciudad del Carmen": (18.6378, -91.8364),
    "San Cristóbal de Las Casas": (16.737, -92.6376),
    "Cuauhtémoc": (28.405, -106.8667),
    "San Luis Río Colorado": (32.4375, -114.7708),
    "Piedras Negras": (28.7081, -100.5283),
    "Chetumal": (18.4939, -88.2979),
    "Ciudad Acuña": (29.3188, -100.9465),
    "Manzanillo": (19.0532, -104.3164),
    "Zacatecas": (22.7717, -102.5753),
    "Villa de Álvarez": (19.2648, -103.7368),
    "San José del Cabo": (23.0617, -109.7114),
    "Navojoa": (27.0817, -109.4456),
    "Tepexpan": (19.6192, -98.9353),
    "Comitán de Domínguez": (16.2511, -92.1342),
    "Rosarito": (32.3636, -117.0544),
    "Addis Ababa": (9.0358, 38.7525),
    "Wolaita Sodo": (6.86, 37.76),
    "Adama": (8.55, 39.2667),
    "Gondar": (12.6, 37.4667),
    "Mekele": (13.4969, 39.4769),
    "Hawaasa": (7.05, 38.4667),
    "Dire Dawa": (9.5833, 41.8667),
    "Bahir Dar": (11.585, 37.39),
    "Bishoftu": (8.75, 38.9833),
    "Dessie": (11.1333, 39.6333),
    "Jimma": (7.6667, 36.8333),
    "Harar": (9.3111, 42.1278),
    "Jijiga": (9.35, 42.8),
    "Assela": (7.95, 39.1333),
    "Yokohama": (35.4503, 139.6342),
    "Nagoya": (35.1815, 136.9064),
    "Sapporo": (43.0619, 141.3544),
    "Fukuoka": (33.5901, 130.4017),
    "Kawasaki": (35.5309, 139.703),
    "Kobe": (34.6902, 135.1954),
    "Saitama": (35.8614, 139.6456),
    "Hiroshima": (34.3852, 132.4553),
    "Sendai": (38.2682, 140.8694),
    "Chiba": (35.6073, 140.1064),
    "Kitakyūshū": (33.8834, 130.8752),
    "Sakai": (34.5733, 135.483),
    "Niigata": (37.9162, 139.0364),
    "Hamamatsu": (34.7109, 137.7262),
    "Kumamoto": (32.803, 130.7079),
    "Sagamihara": (35.5714, 139.3731),
    "Okayama": (34.6551, 133.9196),
    "Shizuoka": (34.9756, 138.3825),
    "Funabashi": (35.6946, 139.9826),
    "Kawaguchi": (35.8076, 139.7241),
    "Kagoshima": (31.5968, 130.5571),
    "Hachiōji": (35.6664, 139.316),
    "Himeji": (34.8152, 134.6854),
    "Utsunomiya": (36.5551, 139.8826),
    "Matsuyama": (33.8392, 132.7657),
    "Matsudo": (35.7876, 139.9032),
    "Ichikawa": (35.7219, 139.9311),
    "Higashiōsaka": (34.6794, 135.6009),
    "Nishinomiya": (34.7376, 135.3417),
    "Ōita": (33.2396, 131.6094),
    "Kurashiki": (34.5851, 133.7721),
    "Kanazawa": (36.5611, 136.6565),
    "Fukuyama": (34.5233, 133.3378),
    "Amagasaki": (34.7338, 135.4063),
    "Fujisawa": (35.3388, 139.4909),
    "Machida": (35.5467, 139.4386),
    "Kashiwa": (35.8676, 139.9758),
    "Aomori": (40.8228, 140.7469),
    "Toyota": (35.0824, 137.1563),
    "Takamatsu": (34.3428, 134.0466),
    "Toyama": (36.6959, 137.2137),
    "Gifu": (35.4263, 136.7594),
    "Toyonaka": (34.7815, 135.4696),
    "Miyazaki": (31.9077, 131.4202),
    "Hirakata": (34.8144, 135.6506),
    "Yokosuka": (35.2813, 139.6721),
    "Okazaki": (34.9547, 137.173),
    "Suita": (34.7595, 135.5168),
    "Ichinomiya": (35.3039, 136.8031),
    "Takasaki": (36.3231, 139.0024),
    "Toyohashi": (34.7692, 137.3915),
    "Nagano": (36.6487, 138.1947),
    "Kawagoe": (35.9251, 139.4858),
    "Wakayama": (34.2304, 135.1707),
    "Nara": (34.685, 135.8048),
    "Takatsuki": (34.8483, 135.6168),
    "Otsu": (35.0178, 135.8548),
    "Koshigaya": (35.8911, 139.7909),
    "Tokorozawa": (35.7994, 139.4689),
    "Iwaki": (37.0505, 140.8877),
    "Maebashi": (36.3895, 139.0634),
    "Asahikawa": (43.7706, 142.365),
    "Kōriyama": (37.4003, 140.3597),
    "Kōchi (Japan)": (33.5588, 133.5312),
    "Naha": (26.2122, 127.6792),
    "Yokkaichi": (34.965, 136.6244),
    "Kasugai": (35.2475, 136.9722),
    "Akita": (39.7199, 140.1026),
    "Kurume": (33.3192, 130.5083),
    "Akashi": (34.6431, 134.9972),
    "Morioka": (39.7455, 141.2691),
    "Fukushima": (37.7608, 140.4747),
    "Ibaraki": (34.8164, 135.5686),
    "Tsu": (34.7184, 136.5058),
    "Mito": (36.3658, 140.4712),
    "Ichihara": (35.4981, 140.1154),
    "Nagaoka": (37.4453, 138.8567),
    "Yao": (34.6269, 135.6008),
    "Fuchū": (35.6694, 139.4775),
    "Fukui": (36.0641, 136.2196),
    "Kakogawa": (34.7569, 134.8413),
    "Hiratsuka": (35.3355, 139.3494),
    "Tokushima": (34.0703, 134.5549),
    "Shimonoseki": (33.9579, 130.9413),
    "Hakodate": (41.7687, 140.7289),
    "Sōka": (35.8254, 139.8053),
    "Yamagata": (38.2554, 140.3396),
    "Tsukuba": (36.0835, 140.0764),
    "Fuji": (35.1613, 138.6763),
    "Sasebo": (33.18, 129.715),
    "Chigasaki": (35.3339, 139.4047),
    "Chōfu": (35.6506, 139.5407),
    "Yamato": (35.4875, 139.458),
    "Matsumoto": (36.238, 137.972),
    "Saga": (33.2635, 130.3008),
    "Kasukabe": (35.9753, 139.7522),
    "Neyagawa": (34.7661, 135.6281),
    "Ageo": (35.9774, 139.5932),
    "Takarazuka": (34.8, 135.3603),
    "Atsugi": (35.4431, 139.3625),
    "Ōta": (36.2911, 139.3754),
    "Hachinohe": (40.5123, 141.4884),
    "Kure": (34.2492, 132.5657),
    "Isesaki": (36.3114, 139.1968),
    "Nishitōkyō": (35.7256, 139.5383),
    "Matsue": (35.4681, 133.0485),
    "Yachiyo": (35.7224, 140.0999),
    "Nagareyama": (35.8563, 139.9029),
    "Itami": (34.7843, 135.4009),
    "Higashihiroshima": (34.4264, 132.7433),
    "Kodaira": (35.7285, 139.4774),
    "Suzuka": (34.882, 136.5842),
    "Mitaka": (35.6836, 139.5595),
    "Yamaguchi": (34.178, 131.4738),
    "Kumagaya": (36.1474, 139.3886),
    "Hino": (35.6713, 139.3951),
    "Odawara": (35.2646, 139.1522),
    "Anjō": (34.9587, 137.0803),
    "Kishiwada": (34.4605, 135.3709),
    "Numazu": (35.0956, 138.8634),
    "Tottori": (35.4944, 134.2216),
    "Jōetsu": (37.1479, 138.2361),
    "Kofu": (35.6621, 138.5682),
    "Izumi": (34.4832, 135.4232),
    "Toyokawa": (34.8268, 137.3759),
    "Tachikawa": (35.7139, 139.4079),
    "Uji": (34.8844, 135.7998),
    "Narashino": (35.6813, 140.0266),
    "Kamakura": (35.3192, 139.5472),
    "Hitachi": (36.5991, 140.6515),
    "Izumo": (35.4016, 132.6854),
    "Tomakomai": (42.6341, 141.6055),
    "Urayasu": (35.6531, 139.902),
    "Sakura": (35.7235, 140.2239),
    "Nishio": (34.8618, 137.0618),
    "Oyama": (36.3146, 139.8002),
    "Hirosaki": (40.6031, 140.4638),
    "Niiza": (35.7935, 139.5653),
    "Takaoka": (36.7541, 137.0257),
    "Kushiro": (42.9849, 144.3817),
    "Iwata": (34.7179, 137.8515),
    "Obihiro": (42.924, 143.1961),
    "Hadano": (35.3747, 139.2201),
    "Ube": (33.9516, 131.2467),
    "Inage-ku": (35.6364, 140.1072),
    "Miyakonojō": (31.7195, 131.0617),
    "Matsusaka": (34.5779, 136.5276),
    "Ōgaki": (35.3599, 136.6127),
    "Hitachinaka": (36.3967, 140.5347),
    "Tochigi": (36.3824, 139.7341),
    "Kariya": (34.9893, 137.0021),
    "Ueda": (36.4019, 138.2491),
    "Noda": (35.9551, 139.8748),
    "Kawanishi": (34.8794, 135.4092),
    "Higashimurayama": (35.7546, 139.4685),
    "Kuki": (36.0621, 139.6668),
    "Mihama-ku": (35.6403, 140.0631),
    "Imabari": (34.0662, 132.9978),
    "Sayama": (35.853, 139.4122),
    "Komaki": (35.2903, 136.911),
    "Tama": (35.6369, 139.4463),
    "Yonago": (35.4282, 133.3309),
    "Kakamigahara": (35.4028, 136.8783),
    "Kusatsu": (35.0132, 135.9599),
    "Ashikaga": (36.3594, 139.4531),
    "Toda": (35.8176, 139.6779),
    "Okinawa": (26.3343, 127.8057),
    "Misato": (35.8301, 139.8723),
    "Moriguchi": (34.7358, 135.5617),
    "Fujieda": (34.8674, 138.2577),
    "Fukaya": (36.1975, 139.2815),
    "Ishinomaki": (38.4342, 141.3027),
    "Kuwana": (35.0622, 136.6837),
    "Koga": (36.1782, 139.7554),
    "Shūnan": (34.0551, 131.8062),
    "Tsuchiura": (36.0783, 140.2045),
    "Minoh": (34.8269, 135.4704),
    "Kisarazu": (35.376, 139.9168),
    "Yaizu": (34.8669, 138.3233),
    "Inazawa": (35.2481, 136.7802),
    "Isahaya": (32.8434, 130.0531),
    "Ōme": (35.788, 139.2758),
    "Zama": (35.4886, 139.4076),
    "Narita": (35.7767, 140.3183),
    "Abiko": (35.8642, 140.0284),
    "Onomichi": (34.4091, 133.2054),
    "Iwakuni": (34.1666, 132.2188),
    "Seto": (35.224, 137.084),
    "Fujinomiya": (35.2221, 138.6216),
    "Koganei": (35.6995, 139.503),
    "Ōsaki": (38.5771, 140.9556),
    "Iizuka": (33.6466, 130.6911),
    "Kirishima": (31.741, 130.7632),
    "Ise": (34.4875, 136.7093),
    "Uruma": (26.3792, 127.8574),
    "Yatsushiro": (32.5076, 130.6017),
    "Kashihara": (34.5094, 135.7925),
    "Tsuruoka": (38.7272, 139.8267),
    "Ebetsu": (43.1037, 141.5362),
    "Daitō": (34.712, 135.6234),
    "Kadoma": (34.7322, 135.5994),
    "Aizuwakamatsu": (37.4948, 139.9298),
    "Nobeoka": (32.5823, 131.6649),
    "Handa": (34.8919, 136.9381),
    "Matsubara": (34.5779, 135.5519),
    "Kōnosu": (36.0659, 139.5222),
    "Ikoma": (34.6919, 135.7005),
    "Higashikurume": (35.758, 139.5299),
    "Karatsu": (33.4501, 129.9683),
    "Beppu": (33.2846, 131.4913),
    "Urasoe": (26.2457, 127.7219),
    "Nasushiobara": (36.9617, 140.0461),
    "Kitami": (43.8031, 143.8947),
    "Niihama": (33.9603, 133.2834),
    "Sano": (36.3144, 139.5783),
    "Hatsukaichi": (34.3482, 132.3316),
    "Kakegawa": (34.7687, 137.9984),
    "Nagahama": (35.3808, 136.2783),
    "Fujimino": (35.8795, 139.5198),
    "Hikone": (35.2744, 136.2597),
    "Hōfu": (34.0513, 131.5626),
    "Tōkai": (35.0231, 136.9022),
    "Ōshū": (39.1445, 141.1391),
    "Higashiōmi": (35.1126, 136.2075),
    "Akishima": (35.7057, 139.3535),
    "Fujimi": (35.8566, 139.5492),
    "Ichinoseki": (38.9347, 141.1266),
    "Kasuga": (33.5327, 130.4702),
    "Kazo": (36.1314, 139.6018),
    "Hakusan": (36.5144, 136.5656),
    "Ōmuta": (33.0303, 130.446),
    "Kamagaya": (35.7768, 140.0008),
    "Sanda": (34.89, 135.2254),
    "Marugame": (34.2894, 133.7976),
    "Tondabayashi": (34.4996, 135.5973),
    "Habikino": (34.5579, 135.6063),
    "Mishima": (35.1394, 138.9489),
    "Tajimi": (35.3339, 137.1125),
    "Komatsu": (36.4083, 136.4455),
    "Ikeda": (34.8217, 135.4285),
    "Toride": (35.9115, 140.0504),
    "Saijo": (33.9196, 133.1812),
    "Chikushino": (33.4874, 130.5258),
    "Inzai": (35.8323, 140.1457),
    "Otaru": (43.1907, 140.9944),
    "Isehara": (35.4029, 139.315),
    "Giza": (29.987, 31.2118),
    "New Cairo": (30.03, 31.47),
    "Shobra Elkhema": (30.1286, 31.2422),
    "Port Said": (31.2625, 32.3061),
    "Suez": (29.9667, 32.5333),
    "Sinnuris": (29.4072, 30.8667),
    "Dairut": (27.5667, 30.8167),
    "Mansoura": (31.05, 31.3833),
    "Helwan": (29.8493, 31.3327),
    "El Mahalla El Kubra": (30.9667, 31.1667),
    "Manfalut": (27.3128, 30.9703),
    "Shibin el-Qanater": (30.3133, 31.3214),
    "Asyut": (27.1869, 31.1714),
    "Faiyum": (29.3, 30.8333),
    "10th of Ramadan": (30.3022, 31.7456),
    "Esna": (25.2933, 32.5564),
    "Letopolis": (30.1222, 31.1333),
    "Tanta": (30.7833, 31.0),
    "Ismailia": (30.5833, 32.2667),
    "Samannud": (30.9622, 31.2425),
    "Kom Ombo": (24.4667, 32.95),
    "Aswan": (24.0889, 32.8997),
    "6th of October": (29.9361, 30.9269),
    "Shorouk city": (30.1448, 31.6296),
    "Damanhur": (31.05, 30.4667),
    "Basyoun": (30.9364, 30.8111),
    "Zagazig": (30.5667, 31.5),
    "Beni Suef": (29.0667, 31.0833),
    "Massarah": (29.9091, 31.2913),
    "Shibin Al Kawm": (30.55, 31.0),
    "Qena": (26.1667, 32.7167),
    "Al Mokattam": (30.0148, 31.2826),
    "Minya": (28.0833, 30.75),
    "Hurghada": (27.2578, 33.8117),
    "Mallawi": (27.7306, 30.8425),
    "Menouf": (30.4667, 30.9333),
    "Damietta": (31.4167, 31.8214),
    "Arish": (31.1333, 33.8),
    "Luxor": (25.6967, 32.6444),
    "Belbis": (30.4167, 31.5667),
    "Sohag": (26.5606, 31.6917),
    "New Borg El Arab": (30.8817, 29.5777),
    "Abu Kabir": (30.7167, 31.6667),
    "Matarieh": (31.1833, 32.0333),
    "Girga": (26.3383, 31.8917),
    "Benha": (30.4628, 31.1797),
    "Qift": (26.0, 32.8167),
    "Qalyub": (30.1997, 31.2053),
    "Kafr el-Sheikh": (31.1143, 30.9401),
    "Desouk": (31.1325, 30.6478),
    "Mit Ghamr": (30.7192, 31.2628),
    "Edfu": (24.9781, 32.8789),
    "Rosetta": (31.4014, 30.4194),
    "Marsa Matruh": (31.3333, 27.2333),
    "Atfih": (29.4083, 31.2528),
    "Musturud": (30.1388, 31.2924),
    "El Quseyya": (27.4403, 30.8183),
    "Idku": (31.3058, 30.2956),
    "New Beni Suef": (29.0436, 31.1198),
    "Kafr el-Dawwar": (31.1339, 30.1297),
    "Akhmim": (26.5642, 31.7461),
    "Ras Gharib": (28.3597, 33.0775),
    "Quezon": (14.65, 121.0475),
    "Davao": (7.0662, 125.6094),
    "Caloocan": (14.65, 120.9667),
    "Taguig": (14.5167, 121.05),
    "Zamboanga": (6.9042, 122.0761),
    "Cebu": (10.293, 123.902),
    "Pasig": (14.5605, 121.0765),
    "Dasmariñas": (14.3294, 120.9367),
    "Cagayan de Oro": (8.4833, 124.65),
    "General Santos": (6.1167, 125.1667),
    "Bacoor": (14.4624, 120.9645),
    "Biñan": (14.3333, 121.0833),
    "Muntinlupa": (14.3833, 121.05),
    "Lapu-Lapu": (10.3127, 123.9488),
    "General Trias": (14.3833, 120.8833),
    "Iloilo": (10.7, 122.5667),
    "Marikina": (14.65, 121.1),
    "Mandaluyong": (14.5833, 121.0333),
    "Pasay": (14.5439, 120.995),
    "Tarlac": (15.4869, 120.59),
    "Lipa": (13.9411, 121.1622),
    "Butuan": (8.948, 125.543),
    "Cotabato": (7.2167, 124.25),
    "San Fernando": (15.0333, 120.6833),
    "Iligan": (8.2333, 124.25),
    "Cabuyao": (14.275, 121.125),
    "Batangas": (13.75, 121.05),
    "Cabanatuan": (15.4908, 120.9678),
    "Puerto Princesa": (9.74, 118.744),
    "Mabalacat": (15.2167, 120.5833),
    "San Pablo": (14.07, 121.325),
    "Tagum": (7.4478, 125.8078),
    "Malolos": (14.8436, 120.8114),
    "Olongapo": (14.8333, 120.2833),
    "Talisay": (10.25, 123.8333),
    "Marawi": (8.0031, 124.285),
    "Tacloban": (11.2444, 125.0039),
    "Navotas": (14.6667, 120.9417),
    "Ormoc": (11.0106, 124.6075),
    "Meycauayan": (14.7333, 120.9667),
    "Trece Martires": (14.2833, 120.8667),
    "Panabo": (7.3014, 125.681),
    "Kabankalan": (9.9833, 122.8167),
    "Legazpi": (13.1333, 123.7333),
    "Tanauan": (14.0833, 121.15),
    "Pagadian": (7.8272, 123.4364),
    "Koronadal": (6.5, 124.85),
    "Malaybalay": (8.1564, 125.1333),
    "Bago": (10.5388, 122.8384),
    "Digos": (6.7454, 125.3559),
    "Calbayog": (12.0669, 124.5947),
    "Sorsogon": (12.9742, 124.0058),
    "Roxas": (11.5894, 122.7511),
    "Surigao": (9.7897, 125.4958),
    "Baliwag": (14.954, 120.901),
    "Tuguegarao": (17.6133, 121.7303),
    "Capas": (15.3372, 120.59),
    "Cadiz": (10.95, 123.3),
    "Danao": (10.5333, 123.9333),
    "Kidapawan": (7.0083, 125.0894),
    "Sagay": (10.9, 123.4167),
    "Isabela": (6.7, 121.9667),
    "Mati": (6.9535, 126.2163),
    "Calapan": (13.414, 121.18),
    "Porac": (15.0719, 120.5419),
    "Floridablanca": (14.974, 120.528),
    "Urdaneta": (15.9761, 120.5711),
    "Ozamiz": (8.15, 123.85),
    "Cauayan": (16.7667, 121.7833),
    "Tabaco": (13.35, 123.7333),
    "Carcar": (10.1167, 123.6333),
    "Gingoog": (8.8167, 125.1),
    "Silay": (10.8, 122.9667),
    "Dipolog": (8.5872, 123.3408),
    "San Carlos": (10.4929, 123.4095),
    "Gapan": (15.3122, 120.9489),
    "Bayawan": (9.3667, 122.8),
    "Tabuk": (17.4069, 121.4425),
    "Ligao": (13.2167, 123.5167),
    "Samal": (7.0783, 125.7117),
    "Tacurong": (6.6884, 124.6787),
    "Lamitan": (6.6582, 122.1457),
    "Tayabas": (14.0167, 121.5833),
    "Iriga": (13.4231, 123.4122),
    "Baybay": (10.6833, 124.8),
    "Carmona": (14.3167, 121.05),
    "Laoag": (18.1978, 120.5936),
    "Bayugan": (8.7143, 125.7481),
    "Balanga": (14.6833, 120.5333),
    "Catbalogan": (11.7833, 124.8833),
    "Guihulngan": (10.1167, 123.2667),
    "Tagbilaran": (9.65, 123.85),
    "Masbate": (12.3739, 123.6247),
    "Alaminos": (16.1553, 119.9808),
    "Kinshasa": (-4.3219, 15.3119),
    "Mbuji-Mayi": (-6.1261, 23.5997),
    "Kananga": (-5.8961, 22.4167),
    "Lubumbashi": (-11.6642, 27.4828),
    "Kisangani": (0.5153, 25.1911),
    "Tshikapa": (-6.4167, 20.8),
    "Goma": (-1.6641, 29.2229),
    "Bukavu": (-2.5, 28.8667),
    "Kabinda": (-6.13, 24.48),
    "Mbandaka": (0.0486, 18.2603),
    "Likasi": (-10.9833, 26.7333),
    "Kolwezi": (-10.7167, 25.4667),
    "Kikwit": (-5.0333, 18.8167),
    "Bunia": (1.5667, 30.25),
    "Matadi": (-5.8167, 13.4833),
    "Beni": (0.5, 29.4667),
    "Butembo": (0.1251, 29.299),
    "Kindu": (-2.95, 25.9167),
    "Kalemie": (-5.9475, 29.1947),
    "Bumba": (2.19, 22.46),
    "Đồng Nai": (11.1103, 107.1811),
    "Bắc Ninh": (21.1, 106.1),
    "Cần Thơ": (10.1547, 105.5005),
    "Da Nang": (16.0694, 108.2097),
    "Haiphong": (20.8, 106.6667),
    "Quảng Ninh": (21.25, 107.3333),
    "Huế": (16.4639, 107.5867),
    "Kadıköy": (40.9869, 29.0367),
    "Batman": (37.8833, 41.1333),
    "Elazığ": (38.6667, 39.2167),
    "Antakya": (36.2025, 36.1606),
    "İzmit": (40.7719, 29.9498),
    "Sivas": (39.75, 37.0167),
    "Sarıyer": (41.1733, 29.0467),
    "Tarsus": (36.9167, 34.9),
    "Merkezefendi": (37.7795, 29.083),
    "Ortahisar": (41.0, 39.7167),
    "Afyonkarahisar": (38.7581, 30.5386),
    "Çorlu": (41.1597, 27.8028),
    "Adapazarı": (40.7762, 30.4026),
    "İnegöl": (40.0806, 29.5097),
    "Kızıltepe": (37.1939, 40.5861),
    "Çorum": (40.5489, 34.9533),
    "Adıyaman": (37.7634, 38.2771),
    "Kütahya": (39.4242, 29.9833),
    "Siverek": (37.7542, 39.3178),
    "Aksaray": (38.3686, 34.0297),
    "Osmaniye": (37.075, 36.25),
    "İskenderun": (36.5872, 36.1733),
    "Beykoz": (41.125, 29.1056),
    "Isparta": (37.7667, 30.55),
    "Düzce": (40.8417, 31.1583),
    "Uşak": (38.6833, 29.4),
    "Niğde": (37.9667, 34.6792),
    "Süleymanpaşa": (40.9833, 27.5167),
    "Çerkezköy": (41.2889, 28.0042),
    "Viranşehir": (37.2306, 39.7653),
    "Karaköprü": (37.1847, 38.7908),
    "Artuklu": (37.3167, 40.7167),
    "Kırıkkale": (39.8417, 33.5139),
    "Menemen": (38.6036, 27.0781),
    "Bolu": (40.7333, 31.6),
    "Akhisar": (38.9239, 27.84),
    "Edirne": (41.673, 26.5736),
    "Karadeniz Ereğli": (41.2796, 31.423),
    "Beşiktaş": (41.0753, 29.0275),
    "Karaman": (37.1833, 33.2167),
    "Siirt": (37.8417, 41.9458),
    "Körfez": (40.7608, 29.7839),
    "Tokat": (40.3097, 36.5542),
    "Turgutlu": (38.5, 27.7084),
    "Gölcük": (40.7032, 29.8722),
    "Nazilli": (37.9125, 28.3206),
    "Bingöl": (38.8861, 40.5017),
    "Ceyhan": (37.0289, 35.8125),
    "Erzincan": (39.7464, 39.4914),
    "Edremit": (39.5961, 27.0244),
    "Bandırma": (40.3542, 27.9725),
    "Nevşehir": (38.625, 34.7122),
    "Kastamonu": (41.3833, 33.7833),
    "Kırşehir": (39.145, 34.1608),
    "Ağrı": (39.7225, 43.0544),
    "Lüleburgaz": (41.4056, 27.3569),
    "Rize": (41.0248, 40.5199),
    "Ereğli": (37.5142, 34.048),
    "Cizre": (37.3302, 42.1848),
    "Nizip": (37.0104, 37.7985),
    "Iğdır": (39.9237, 44.045),
    "Ergani": (38.2692, 39.7617),
    "Silopi": (37.2497, 42.4717),
    "Çanakkale": (40.15, 26.4),
    "Yalova": (40.6556, 29.275),
    "Ödemiş district": (38.2311, 27.9719),
    "Karabük": (41.2, 32.6333),
    "Kuşadası": (37.8632, 27.2669),
    "Kozan": (37.4507, 35.8123),
    "Çayırova": (40.8265, 29.3745),
    "Ünye": (41.1271, 37.2882),
    "Giresun": (40.9, 38.4167),
    "Polatlı": (39.5842, 32.1472),
    "Kadirli": (37.3739, 36.0961),
    "Patnos": (39.2336, 42.8636),
    "Doğubayazıt": (39.55, 44.0833),
    "Samandağ": (36.085, 35.9806),
    "Muş": (38.7333, 41.4911),
    "Yüksekova district": (37.5667, 44.2833),
    "Kartepe": (40.7454, 30.0113),
    "Amasya": (40.6787, 35.8278),
    "Midyat": (37.4167, 41.3531),
    "Akçakale": (36.7108, 38.9478),
    "Kilis": (36.7167, 37.1167),
    "Gemlik": (40.4308, 29.1597),
    "Soma": (39.1833, 27.6056),
    "Nusaybin": (37.0833, 41.2167),
    "Bergama": (39.1192, 27.1777),
    "Yozgat": (39.8208, 34.8083),
    "Zonguldak": (41.45, 31.7833),
    "Bordeaux Métropole": (44.84, -0.58),
    "Montpellier": (43.6109, 3.8772),
    "Lille": (50.6319, 3.0575),
    "Reims": (49.2653, 4.0286),
    "Saint-Étienne": (45.4339, 4.3897),
    "Le Havre": (49.4942, 0.1081),
    "Villeurbanne": (45.7661, 4.8794),
    "Dijon": (47.3231, 5.0419),
    "Angers": (47.4711, -0.5519),
    "Grenoble": (45.1869, 5.7264),
    "Saint-Denis": (-20.8789, 55.4481),
    "Nîmes": (43.8383, 4.3597),
    "Aix-en-Provence": (43.5278, 5.4456),
    "Clermont-Ferrand": (45.7797, 3.0869),
    "Le Mans": (48.0042, 0.1969),
    "Brest": (48.3906, -4.4861),
    "Tours": (47.3928, 0.6883),
    "Annecy": (45.8992, 6.1294),
    "Limoges": (45.8344, 1.2617),
    "Metz": (49.1197, 6.1769),
    "Perpignan": (42.6975, 2.8947),
    "Boulogne-Billancourt": (48.8353, 2.2414),
    "Besançon": (47.2422, 6.0214),
    "Rouen": (49.4431, 1.1025),
    "Orléans": (47.9022, 1.9042),
    "Caen": (49.1814, -0.3636),
    "Saint-Paul (France)": (-21.0097, 55.2697),
    "Mulhouse": (47.7486, 7.3392),
    "Nancy": (48.6928, 6.1836),
    "Glasgow": (55.8611, -4.25),
    "Leeds": (53.7975, -1.5436),
    "Sheffield": (53.3808, -1.4703),
    "Liverpool": (53.4072, -2.9917),
    "Cardiff": (51.4817, -3.1792),
    "Leicester": (52.6361, -1.1331),
    "Coventry": (52.4167, -1.5167),
    "Belfast": (54.5967, -5.93),
    "Newcastle upon Tyne": (54.9778, -1.6133),
    "Nottingham": (52.955, -1.1492),
    "Bolton": (53.5783, -2.43),
    "Sunderland": (54.9044, -1.3811),
    "Brighton and Hove": (50.8278, -0.1528),
    "Southampton": (50.9067, -1.4044),
    "Plymouth": (50.3714, -4.1422),
    "Milton Keynes": (52.0333, -0.7667),
    "Kingston upon Hull": (53.7444, -0.3325),
    "Stoke-on-Trent": (53.0, -2.1833),
    "Derby": (52.9217, -1.4767),
    "Wolverhampton": (52.5842, -2.1253),
    "Portsmouth": (50.8058, -1.0872),
    "Swansea": (51.6167, -3.95),
    "Swindon": (51.5583, -1.7811),
    "York": (53.95, -1.0833),
    "Solihull": (52.413, -1.772),
    "Aberdeen": (57.15, -2.1),
    "Norwich": (52.6286, 1.2928),
    "Peterborough": (52.5725, -0.2431),
    "St Helens": (53.4541, -2.7461),
    "Warrington": (53.3873, -2.6029),
    "Huddersfield": (53.645, -1.7797),
    "Southend-on-Sea": (51.5379, 0.7138),
    "Newport": (51.5886, -2.9978),
    "Dundee": (56.4606, -2.97),
    "Oxford": (51.7519, -1.2578),
    "West Bromwich": (52.5167, -1.9833),
    "Gloucester": (51.8653, -2.2458),
    "Telford": (52.6792, -2.4475),
    "Preston": (53.7594, -2.6981),
    "Middlesbrough": (54.5739, -1.2333),
    "Wrexham County Borough": (53.0507, -3.0094),
    "Exeter": (50.7256, -3.5269),
    "Colchester": (51.8886, 0.9036),
    "Slough": (51.5097, -0.5931),
    "Cheltenham": (51.8965, -2.0784),
    "Chelmsford": (51.7361, 0.4798),
    "Birkenhead": (53.3833, -3.0167),
    "Doncaster": (53.5228, -1.1325),
    "Rotherham": (53.43, -1.3572),
    "Basingstoke": (51.2628, -1.0862),
    "Salford": (53.4828, -2.2931),
    "Wigan": (53.5447, -2.6317),
    "Eastbourne": (50.7664, 0.2861),
    "Chiang Mai": (18.7889, 98.9833),
    "Phrae": (18.145, 100.1406),
    "Nonthaburi": (13.8667, 100.5167),
    "Pak Kret": (13.9125, 100.4978),
    "Hat Yai": (7.0167, 100.4667),
    "Nakhon Ratchasima": (14.9736, 102.0831),
    "Chaophraya Surasak": (13.1244, 100.9997),
    "Surat Thani": (9.1397, 99.3306),
    "Udon Thani": (17.4253, 102.7902),
    "Pattaya": (12.9496, 100.893),
    "Khon Kaen": (16.4333, 102.8333),
    "Nakhon Si Thammarat": (8.4363, 99.963),
    "Soweto": (-26.2678, 27.8586),
    "Pretoria": (-25.7464, 28.1881),
    "Durban": (-29.8583, 31.025),
    "East London": (-33.0175, 27.9047),
    "Randburg": (-26.0936, 28.0064),
    "Roodepoort": (-26.1625, 27.8725),
    "Gqeberha": (-33.9581, 25.6),
    "Bloemfontein": (-29.1, 26.2167),
    "Centurion": (-25.8603, 28.1894),
    "Benoni": (-26.1883, 28.3206),
    "George": (-33.9667, 22.45),
    "Potchefstroom": (-26.715, 27.1033),
    "Alberton": (-26.2672, 28.1219),
    "Upington": (-28.4572, 21.2425),
    "Paarl": (-33.7242, 18.9558),
    "Uitenhage": (-33.75, 25.4),
    "Palermo": (38.1157, 13.3613),
    "Genoa": (44.4072, 8.9339),
    "Catania": (37.5027, 15.0873),
    "Verona": (45.4386, 10.9928),
    "Venice": (45.4397, 12.3319),
    "Messina": (38.1936, 15.5542),
    "Padua": (45.4078, 11.8733),
    "Trieste": (45.6503, 13.7703),
    "Parma": (44.8015, 10.328),
    "Prato": (43.8808, 11.0966),
    "Taranto": (40.4711, 17.2431),
    "Modena": (44.6458, 10.9257),
    "Reggio Calabria": (38.1144, 15.65),
    "Reggio Emilia": (44.7, 10.6333),
    "Perugia": (43.1119, 12.3885),
    "Ravenna": (44.4161, 12.2017),
    "Livorno": (43.55, 10.3167),
    "Rimini": (44.0594, 12.5683),
    "Foggia": (41.4584, 15.5519),
    "Ferrara": (44.8392, 11.6175),
    "Latina": (41.4672, 12.9036),
    "Salerno": (40.6806, 14.7594),
    "Giugliano in Campania": (40.9283, 14.2011),
    "Monza": (45.5836, 9.2736),
    "Sassari": (40.7267, 8.5592),
    "Bergamo": (45.695, 9.67),
    "Pescara": (42.4643, 14.2142),
    "Forlì": (44.2228, 12.0414),
    "Syracuse": (37.0692, 15.2875),
    "Vicenza": (45.55, 11.55),
    "Terni": (42.5608, 12.6468),
    "Bolzano": (46.4981, 11.3548),
    "Piacenza": (45.05, 9.7),
    "Novara": (45.45, 8.62),
    "Dar es Salaam": (-6.8161, 39.2803),
    "Zanzibar": (-6.1667, 39.2),
    "Mwanza": (-2.5167, 32.9),
    "Mbeya": (-8.9, 33.45),
    "Morogoro": (-6.8242, 37.6633),
    "Arusha": (-3.3667, 36.6833),
    "Geita": (-2.8714, 32.2294),
    "Tabora": (-5.0167, 32.8),
    "Songea": (-10.6833, 35.65),
    "Bariadi": (-2.8, 33.9833),
    "Kahama": (-3.84, 32.6),
    "Kasulu": (-4.5767, 30.1025),
    "Kigoma": (-4.8833, 29.6333),
    "Tanga": (-5.0667, 39.1),
    "Dodoma": (-6.1835, 35.746),
    "Mpanda": (-6.3436, 31.0694),
    "Iringa": (-7.77, 35.69),
    "Singida": (-4.8186, 34.7506),
    "Musoma": (-1.5069, 33.8042),
    "Sumbawanga": (-7.9667, 31.6167),
    "Bukoba": (-1.3333, 31.8167),
    "Moshi": (-3.3349, 37.3404),
    "Mtwara": (-10.2736, 40.1828),
    "Shinyanga": (-3.6724, 33.4297),
    "Yangon": (16.8053, 96.1561),
    "Mandalay": (21.9831, 96.0844),
    "Naypyidaw": (19.7475, 96.115),
    "Hpa-An": (16.8906, 97.6333),
    "Mawlamyine": (16.4847, 97.6258),
    "Pathein": (16.7667, 94.7333),
    "Taunggyi": (20.7806, 97.0372),
    "Myitkyina": (25.3867, 97.3936),
    "Myeik": (12.4394, 98.6003),
    "Monywa": (22.1086, 95.1358),
    "Myawaddy": (16.6878, 98.5083),
    "Lashio": (22.9358, 97.7497),
    "Dawei": (14.0367, 98.1867),
    "Mogaung": (25.3014, 96.9417),
    "Sittwe": (20.1444, 92.8969),
    "Bogotá (Colombia)": (4.6097, -74.0817),
    "Medellín (Colombia)": (6.2502, -75.5676),
    "Cali": (3.44, -76.5197),
    "Barranquilla": (10.9833, -74.8019),
    "Cartagena": (10.4236, -75.5253),
    "Villavicencio": (4.1425, -73.6294),
    "Bucaramanga": (7.1186, -73.1161),
    "Pereira": (4.8144, -75.6947),
    "Ibagué": (4.4378, -75.2006),
    "Valledupar": (10.4603, -73.2597),
    "Santa Marta": (11.2361, -74.2017),
    "Manizales": (5.0675, -75.51),
    "Pasto": (1.21, -77.2747),
    "Montería": (8.76, -75.8856),
    "Neiva": (2.9275, -75.2875),
    "Armenia": (4.5389, -75.6725),
    "Popayán": (2.4542, -76.6092),
    "Sincelejo": (9.2994, -75.3958),
    "Riohacha": (11.5442, -72.9069),
    "Florencia": (1.6142, -75.6117),
    "Tunja": (5.5403, -73.3614),
    "Ipiales": (0.8289, -77.6406),
    "Quibdó": (5.6923, -76.6582),
    "Incheon": (37.4639, 126.6486),
    "Daegu": (35.8717, 128.6017),
    "Daejeon": (36.35, 127.385),
    "Suwon": (37.2858, 127.01),
    "Ulsan": (35.55, 129.3167),
    "Yongin": (37.2361, 127.2011),
    "Goyang": (37.6564, 126.835),
    "Changwon": (35.2281, 128.6811),
    "Hwaseong": (37.2, 126.7667),
    "Seongnam": (37.4386, 127.1378),
    "Cheongju": (36.6372, 127.4897),
    "Bucheon": (37.4989, 126.7831),
    "Namyangju": (37.6367, 127.2142),
    "Jeju": (33.5097, 126.5219),
    "Cheonan": (36.81, 127.1475),
    "Jeonju": (35.8239, 127.1475),
    "Ansan": (37.3236, 126.8219),
    "Pyeongtaek": (36.9947, 127.0889),
    "Gimhae": (35.2342, 128.8811),
    "Paju": (37.8328, 126.8169),
    "Pohang": (36.0322, 129.365),
    "Siheung": (37.3803, 126.803),
    "Uijeongbu": (37.7486, 127.0389),
    "Gumi": (36.21, 128.3544),
    "Gwangju": (37.4092, 127.2592),
    "Sejong": (36.487, 127.2822),
    "Gimpo": (37.6236, 126.7142),
    "Jinju": (35.1928, 128.0847),
    "Wonju": (37.3417, 127.9208),
    "Hanam": (37.5392, 127.2147),
    "Asan": (36.35, 126.9167),
    "Yangsan": (35.3386, 129.0386),
    "Yangju": (37.7853, 127.0458),
    "Chuncheon": (37.8747, 127.7342),
    "Gwangmyeong": (37.4772, 126.8664),
    "Gunsan": (35.9786, 126.7114),
    "Yeosu": (34.7607, 127.6622),
    "Iksan": (35.9439, 126.9544),
    "Gunpo": (37.3675, 126.9469),
    "Suncheon": (34.9506, 127.4875),
    "Gyeongsan": (35.8167, 128.7333),
    "Geoje": (34.8503, 128.5886),
    "Gyeongju": (35.85, 129.2167),
    "Osan": (37.145, 127.0694),
    "Mokpo": (34.7936, 126.3886),
    "Chungju": (36.9706, 127.9322),
    "Icheon": (37.2792, 127.4425),
    "Gangneung": (37.7556, 128.8961),
    "Anseong": (37.0078, 127.2797),
    "Seogwipo": (33.2497, 126.56),
    "Guri": (37.5947, 127.1428),
    "Dangjin": (36.8944, 126.6297),
    "Seosan": (36.7817, 126.4522),
    "Pocheon": (37.8944, 127.1992),
    "Uiwang": (37.3447, 126.9683),
    "Andong": (36.5656, 128.725),
    "Gwangyang": (34.9403, 127.7017),
    "Gimcheon": (36.1192, 128.1153),
    "Jecheon": (37.1361, 128.2119),
    "Tongyeong": (34.8458, 128.4236),
    "Nonsan": (36.2039, 127.0847),
    "Yeoju": (37.2939, 127.6383),
    "Sacheon": (35.2347, 128.3575),
    "Jeongeup": (35.5653, 126.8561),
    "Yeongju": (36.8217, 128.6308),
    "Gongju": (36.4556, 127.1247),
    "Sangju": (36.4153, 128.1606),
    "Boryeong": (36.3333, 126.6167),
    "Madrid city": (40.4084, -3.6876),
    "Seville city": (37.3828, -5.9731),
    "Málaga": (36.7167, -4.4167),
    "Palma": (39.5667, 2.6497),
    "Alicante": (38.3453, -0.4831),
    "Las Palmas de Gran Canaria": (28.0997, -15.4133),
    "Valladolid city": (41.6523, -4.7233),
    "L'Hospitalet de Llobregat": (41.3589, 2.0992),
    "Xixón": (43.545, -5.6621),
    "Terrassa": (41.5611, 2.0081),
    "Badalona": (41.4333, 2.2333),
    "Sabadell": (41.5483, 2.1075),
    "Móstoles": (40.3333, -3.8667),
    "Alcalá de Henares": (40.4821, -3.36),
    "Leganés": (40.3282, -3.7654),
    "Elche": (38.2654, -0.6989),
    "Oviedo": (43.3634, -5.8423),
    "Castellón de la Plana": (39.986, -0.0374),
    "Murcia": (37.987, -1.13),
    "Burgos city": (42.3411, -3.7042),
    "Almería city": (36.8389, -2.4641),
    "Albacete": (38.9959, -1.8557),
    "San Cristóbal de La Laguna": (28.4853, -16.3167),
    "Getafe": (40.304, -3.7294),
    "Badajoz": (38.8779, -6.9706),
    "Logroño": (42.4671, -2.4454),
    "Santa Cruz de Tenerife": (28.4629, -16.2472),
    "Salamanca": (40.9688, -5.6639),
    "Santander": (43.4647, -3.8044),
    "Alcobendas": (40.5333, -3.6333),
    "León": (42.5991, -5.5671),
    "Cádiz (Spain)": (36.5267, -6.2891),
    "Jaén": (37.7692, -3.7903),
    "Girona": (41.9831, 2.8249),
    "Barakaldo": (43.2972, -2.9917),
    "Mombasa": (-4.05, 39.6667),
    "Kisumu": (-0.1, 34.75),
    "Eldoret": (0.5167, 35.2833),
    "Lunga Lunga": (-4.55, 39.12),
    "Kitale": (1.0167, 35.0),
    "Rhamu": (3.9375, 41.2203),
    "Kilifi": (-3.6333, 39.85),
    "Machakos": (-1.5167, 37.2667),
    "Kisii": (-0.6698, 34.7675),
    "Rosario": (-32.9575, -60.6394),
    "San Miguel de Tucumán": (-26.8167, -65.2167),
    "Salta": (-24.7883, -65.4106),
    "Mar del Plata": (-38.0, -57.55),
    "Lanús": (-34.7, -58.4),
    "Corrientes": (-27.4833, -58.8167),
    "Santa Fe": (-31.6333, -60.7),
    "Avellaneda": (-34.6625, -58.3678),
    "San Salvador de Jujuy": (-24.1856, -65.2994),
    "Santiago del Estero": (-27.7844, -64.2669),
    "Resistencia": (-27.4514, -58.9867),
    "Bahía Blanca": (-38.7182, -62.2673),
    "Neuquén": (-38.9518, -68.0592),
    "Posadas": (-27.3667, -55.8969),
    "Paraná": (-31.7331, -60.5297),
    "Quilmes": (-34.7167, -58.2667),
    "Gregorio de Laferrere": (-34.75, -58.5833),
    "Merlo": (-34.6653, -58.7275),
    "González Catán": (-34.7708, -58.6464),
    "La Plata": (-34.9333, -57.95),
    "José C. Paz": (-34.5167, -58.7667),
    "San Rafael": (-34.6175, -68.3356),
    "La Rioja": (-29.4131, -66.8558),
    "San Luis": (-33.2994, -66.3392),
    "Comodoro Rivadavia": (-45.8667, -67.5),
    "Río Cuarto": (-33.123, -64.3478),
    "San Lorenzo": (-32.75, -60.7333),
    "Godoy Cruz": (-32.9167, -68.8333),
    "Isidro Casanova": (-34.7, -58.5833),
    "San Fernando del Valle de Catamarca": (-28.4686, -65.7792),
    "Berazategui": (-34.7679, -58.2133),
    "Concordia": (-31.3922, -58.0169),
    "Moreno": (-34.6576, -58.7872),
    "Ezeiza": (-34.8333, -58.5167),
    "San Nicolás de los Arroyos": (-33.3333, -60.2167),
    "San Miguel": (-34.5431, -58.7118),
    "Virrey del Pino": (-34.8447, -58.6517),
    "Tandil": (-37.3167, -59.1333),
    "Rafael Castillo": (-34.7167, -58.6167),
    "Florencio Varela": (-34.7975, -58.2759),
    "San Justo": (-34.6833, -58.55),
    "Bariloche": (-41.15, -71.3),
    "General Roca": (-39.0333, -67.5833),
    "Ciudad Madero": (-34.6833, -58.5),
    "Villa Mercedes": (-33.6667, -65.4667),
    "Ituzaingó": (-34.6582, -58.6672),
    "La Banda": (-27.7333, -64.25),
    "Mendoza": (-32.8897, -68.8444),
    "Temperley": (-34.7667, -58.3833),
    "Ramos Mejía": (-34.65, -58.5667),
    "Río Gallegos": (-51.6233, -69.2161),
    "Pergamino": (-33.8836, -60.5669),
    "Monte Grande": (-34.8167, -58.4667),
    "Gualeguaychú": (-33.0078, -58.5111),
    "Zárate": (-34.0833, -59.0333),
    "Trelew": (-43.2533, -65.3094),
    "Maipú": (-32.9667, -68.75),
    "Cipolletti": (-38.9333, -68.0167),
    "Puerto Madryn": (-42.773, -65.0366),
    "Presidencia Roque Sáenz Peña": (-26.7833, -60.45),
    "Libertad": (-34.692, -58.686),
    "Kampala": (0.3136, 32.5811),
    "Mbale": (1.0712, 34.175),
    "Arua": (3.03, 30.91),
    "Kira Town": (0.3972, 32.6389),
    "Masaka": (-0.3336, 31.7339),
    "Jinja": (0.4353, 33.2056),
    "Mbarara": (-0.6133, 30.6583),
    "Lira": (2.2596, 32.8903),
    "Gulu": (2.7667, 32.3056),
    "Hoima": (1.4331, 31.3522),
    "Fort Portal": (0.6564, 30.2797),
    "Soroti": (1.7291, 33.6288),
    "Algiers": (36.7764, 3.0586),
    "Oran": (35.6969, -0.6331),
    "Constantine": (36.365, 6.6147),
    "Batna": (35.55, 6.1667),
    "Sétif": (36.19, 5.41),
    "Djelfa": (34.6667, 3.25),
    "Annaba": (36.9, 7.7667),
    "Sidi Bel Abbès": (35.2, -0.6333),
    "Biskra": (34.85, 5.7333),
    "Tébessa": (35.4, 8.1167),
    "Blida": (36.4722, 2.8333),
    "Tiaret": (35.3667, 1.3167),
    "Béjaïa": (36.7511, 5.0642),
    "Tlemcen": (34.8828, -1.3167),
    "Bordj Bou Arréridj": (36.0667, 4.7667),
    "Béchar": (31.6164, -2.2183),
    "Skikda": (36.8667, 6.9),
    "Chlef": (36.1647, 1.3317),
    "Mostaganem": (35.9333, 0.0903),
    "Touggourt": (33.1, 6.0667),
    "Médéa": (36.2675, 2.75),
    "El Oued": (33.3683, 6.8675),
    "Ouargla": (31.95, 5.3167),
    "Ghardaia": (32.4833, 3.6667),
    "Aïn Beïda": (35.7964, 7.3928),
    "Mascara": (35.4, 0.1333),
    "Khenchela": (35.4167, 7.1333),
    "Kabul": (34.5328, 69.1658),
    "Kandahar": (31.6207, 65.7159),
    "Herat": (34.3419, 62.2031),
    "Mazar-i-Sharif": (36.7, 67.1167),
    "Kunduz": (36.7289, 68.8569),
    "Jalalabad": (34.4303, 70.4528),
    "Taloqan": (36.7358, 69.5347),
    "Lashkar Gah": (31.5938, 64.3716),
    "Sheberghan": (36.665, 65.752),
    "Khost": (33.3333, 69.9167),
    "Ghazni": (33.5492, 68.4233),
    "Pul-e Khomri": (35.95, 68.7),
    "Kharkiv": (49.9925, 36.2311),
    "Odesa": (46.4842, 30.7325),
    "Dnipro": (48.4675, 35.04),
    "Donetsk": (48.0158, 37.8028),
    "Lviv": (49.842, 24.0316),
    "Zaporizhzhia": (47.85, 35.1175),
    "Kryvyi Rih": (47.91, 33.39),
    "Sevastopol": (44.605, 33.5225),
    "Mykolaiv": (46.975, 31.995),
    "Luhansk": (48.5717, 39.2973),
    "Vinnytsia": (49.2372, 28.4672),
    "Makiivka": (48.0556, 37.9611),
    "Simferopol": (44.9484, 34.1),
    "Chernihiv": (51.4939, 31.2947),
    "Poltava": (49.5894, 34.5514),
    "Kherson": (46.6425, 32.625),
    "Khmelnytskyi": (49.42, 27.0),
    "Cherkasy": (49.4444, 32.0597),
    "Sumy": (50.9067, 34.7992),
    "Chernivtsi": (48.2908, 25.9344),
    "Zhytomyr": (50.2544, 28.6578),
    "Horlivka": (48.3336, 38.0925),
    "Mariupol": (47.1306, 37.5639),
    "Rivne": (50.6197, 26.2514),
    "Ivano-Frankivsk": (48.9228, 24.7106),
    "Kamyanske": (48.5167, 34.6133),
    "Ternopil": (49.5667, 25.6),
    "Kropyvnytskyi": (48.5103, 32.2667),
    "Lutsk": (50.7478, 25.3244),
    "Kremenchuk": (49.0631, 33.4039),
    "Bila Tserkva": (49.7956, 30.1167),
    "Kerch": (45.3386, 36.4681),
    "Melitopol": (46.8489, 35.3675),
    "Kramatorsk": (48.7392, 37.5839),
    "Khrustalnyi": (48.1214, 38.9453),
    "Uzhhorod": (48.6239, 22.295),
    "Brovary": (50.5111, 30.79),
    "Berdiansk": (46.7556, 36.7889),
    "Yevpatoriya": (45.2, 33.3583),
    "Alchevsk": (48.4672, 38.7983),
    "Nikopol": (47.5667, 34.4),
    "Sloviansk": (48.8533, 37.6059),
    "Pavlohrad": (48.52, 35.87),
    "Baghdad": (33.3153, 44.3661),
    "Mosul": (36.3417, 43.1292),
    "Basra": (30.515, 47.81),
    "Kirkuk": (35.4667, 44.4),
    "Sulaymaniyah": (35.55, 45.4333),
    "Ramadi": (33.4258, 43.2992),
    "Erbil": (36.1911, 44.0094),
    "Najaf": (32.029, 44.3396),
    "Karbala": (32.6167, 44.0333),
    "Nasiriyah": (31.0439, 46.2575),
    "Hillah": (32.4833, 44.4333),
    "Amarah": (31.8333, 47.15),
    "Bagdad": (33.7447, 44.6436),
    "Al Diwaniyah": (31.9892, 44.9247),
    "Kut": (32.5147, 45.8189),
    "Az Zubayr": (30.3892, 47.7081),
    "Duhok": (36.85, 42.9833),
    "Fallujah": (33.35, 43.7833),
    "Zakho": (37.1436, 42.6819),
    "Samawah": (31.332, 45.2944),
    "Kufa": (32.03, 44.4),
    "Samarra": (34.1959, 43.8857),
    "Umm Qasr": (30.0342, 47.9294),
    "Tikrit": (34.6, 43.6833),
    "Al-Faw": (29.98, 48.47),
    "Kraków (Poland)": (50.0614, 19.9372),
    "Wrocław": (51.11, 17.0325),
    "Łódź": (51.75, 19.4667),
    "Poznań (Poland)": (52.4083, 16.9336),
    "Gdańsk (Poland)": (54.3483, 18.654),
    "Szczecin": (53.4247, 14.5553),
    "Lublin": (51.25, 22.5667),
    "Bydgoszcz": (53.1167, 18.0),
    "Białystok": (53.1324, 23.1687),
    "Katowice": (50.25, 19.0),
    "Gdynia": (54.5175, 18.54),
    "Częstochowa": (50.8096, 19.1222),
    "Radom": (51.4167, 21.15),
    "Toruń": (53.0167, 18.6167),
    "Rzeszów": (50.0411, 21.9989),
    "Kielce": (50.8725, 20.6319),
    "Sosnowiec": (50.2711, 19.1946),
    "Gliwice": (50.2942, 18.6658),
    "Olsztyn": (53.78, 20.4942),
    "Bielsko-Biała": (49.8225, 19.0444),
    "Zabrze": (50.3, 18.7833),
    "Bytom": (50.347, 18.923),
    "Zielona Góra": (51.9397, 15.505),
    "Ruda Śląska": (50.2701, 18.8742),
    "Rybnik": (50.0833, 18.5),
    "Opole": (50.6667, 17.9242),
    "Tychy": (50.1236, 18.9867),
    "Gorzów Wielkopolski": (52.7333, 15.25),
    "Elbląg": (54.1522, 19.4089),
    "Płock": (52.55, 19.7),
    "Dąbrowa Górnicza": (50.3309, 19.2079),
    "Koszalin": (54.1903, 16.1817),
    "Włocławek": (52.65, 19.05),
    "Wałbrzych": (50.7667, 16.2833),
    "Chorzów": (50.3, 18.95),
    "Tarnów": (50.0125, 20.9883),
    "Edmonton": (53.5333, -113.5),
    "Winnipeg": (49.8956, -97.1386),
    "Mississauga": (43.6, -79.65),
    "Brampton": (43.6833, -79.7667),
    "Quebec": (46.8161, -71.2242),
    "Hamilton": (43.2567, -79.8692),
    "Surrey": (49.1833, -122.85),
    "Laval": (45.5833, -73.75),
    "Markham": (43.8833, -79.25),
    "Vaughan": (43.8333, -79.5),
    "Gatineau": (45.4833, -75.65),
    "Saskatoon": (52.1333, -106.6833),
    "Kitchener": (43.45, -80.4833),
    "Burnaby": (49.25, -122.9492),
    "Regina": (50.4547, -104.6067),
    "Richmond Hill": (43.8714, -79.4372),
    "Burlington": (43.325, -79.8),
    "Sherbrooke": (45.4, -71.9),
    "Oshawa": (43.9, -78.85),
    "Greater Sudbury": (46.49, -81.01),
    "Lévis": (46.8, -71.1833),
    "Abbotsford": (49.0517, -122.3289),
    "Saguenay": (48.4167, -71.0667),
    "Coquitlam": (49.2839, -122.7919),
    "Barrie": (44.39, -79.686),
    "Kelowna": (49.8801, -119.4436),
    "Trois-Rivières": (46.35, -72.55),
    "Guelph": (43.55, -80.25),
    "St. Catharines": (43.1833, -79.2333),
    "Kingston": (44.2306, -76.4811),
    "Terrebonne": (45.7, -73.6333),
    "Waterloo": (43.4667, -80.5167),
    "St. John's": (47.5614, -52.7119),
    "Thunder Bay": (48.3822, -89.2461),
    "Delta": (49.12, -122.99),
    "Brantford": (43.1408, -80.2631),
    "Chatham-Kent": (42.4, -82.1833),
    "Red Deer": (52.2681, -113.8111),
    "Tangier": (35.7695, -5.8045),
    "Fez": (34.0433, -5.0033),
    "Marrakech": (31.6295, -7.9811),
    "Salé": (34.05, -6.8167),
    "Meknes": (33.8833, -5.55),
    "Agadir": (30.4214, -9.5831),
    "Rabat": (34.0211, -6.8414),
    "Kenitra": (34.25, -6.5833),
    "Oujda": (34.6867, -1.9114),
    "Tétouan": (35.5697, -5.3758),
    "Temara": (33.9234, -6.9076),
    "Laayoune": (27.15, -13.2),
    "El Jadida": (33.2342, -8.5228),
    "Ait Melloul": (30.3342, -9.4972),
    "Bouskoura": (33.4489, -7.6486),
    "Mohammedia": (33.6833, -7.3833),
    "Settat": (33.0023, -7.6198),
    "Berrechid": (33.27, -7.5872),
    "Dakhla": (23.7167, -15.95),
    "Nador": (35.1739, -2.9292),
    "Taza": (34.2144, -4.0088),
    "Larache": (35.1951, -6.154),
    "Guelmim": (28.9833, -10.0667),
    "Khemisset": (33.81, -6.06),
    "Ksar el-Kebir": (34.9991, -5.8998),
    "Khenifra": (32.93, -5.66),
    "Skhirat": (33.85, -7.03),
    "Ben Guerir": (32.23, -7.95),
    "Lqliâa": (30.2942, -9.4544),
    "Tiflet": (33.9, -6.33),
    "Taourirt": (34.41, -2.89),
    "Oulad Teima": (30.4, -9.21),
    "Luanda": (-8.8383, 13.2344),
    "Quilamba Quiaxi": (-8.875, 13.2667),
    "Lubango": (-14.9167, 13.5),
    "Viana": (-8.9117, 13.371),
    "Huambo": (-12.7667, 15.7333),
    "Cazenga": (-8.8389, 13.2958),
    "Cabinda": (-5.5667, 12.2),
    "Malanje": (-9.5402, 16.341),
    "Benguela": (-12.55, 13.4167),
    "Lobito": (-12.35, 13.5464),
    "Belas": (-9.0583, 13.1539),
    "Luena": (-11.783, 19.913),
    "Bailundo": (-12.1958, 15.8556),
    "Sumbe": (-11.2052, 13.8417),
    "Moçâmedes": (-15.1953, 12.1508),
    "Dundo": (-7.3801, 20.8351),
    "N'dalatando": (-9.3, 14.9167),
    "M'banza-Kongo": (-6.2667, 14.25),
    "Porto Amboim": (-10.7326, 13.7602),
    "Ondjiva": (-17.0667, 15.7333),
    "Gabela": (-10.85, 14.3667),
    "Baía Farta": (-12.6128, 13.1987),
    "Tashkent": (41.3111, 69.2797),
    "Namangan": (40.9953, 71.6725),
    "Andijan": (40.7833, 72.3506),
    "Samarkand": (39.6547, 66.9758),
    "Nukus": (42.4647, 59.6022),
    "Fergana": (40.3864, 71.7864),
    "Bukhara": (39.7747, 64.4286),
    "Qarshi": (38.8667, 65.8),
    "Qoʻqon": (40.5286, 70.9425),
    "Margilan": (40.4667, 71.7167),
    "Jizzakh": (40.1167, 67.85),
    "Chirchiq": (41.4667, 69.5833),
    "Urgench": (41.5345, 60.6249),
    "Shahrisabz": (39.05, 66.8333),
    "Olmaliq": (40.85, 69.6),
    "Termez": (37.2242, 67.2783),
    "Navoiy": (40.0833, 65.3833),
    "Angren": (41.0111, 70.0735),
    "Khiva": (41.3814, 60.3611),
    "Klang": (3.0333, 101.45),
    "Ipoh": (4.6, 101.07),
    "Johor Bahru": (1.4556, 103.7611),
    "George Town": (5.4145, 100.3292),
    "Subang Jaya": (3.0644, 101.5936),
    "Petaling Jaya": (3.1073, 101.6067),
    "Kuantan": (3.8167, 103.3333),
    "Shah Alam": (3.0833, 101.5333),
    "Bharu": (6.1333, 102.25),
    "Malacca": (2.1889, 102.2511),
    "Kinabalu": (5.975, 116.0725),
    "Seremban": (2.7297, 101.9381),
    "Sandakan": (5.8388, 118.1173),
    "Muar": (2.05, 102.56),
    "Kuching": (1.5575, 110.3439),
    "Kuala Terengganu": (5.3303, 103.1408),
    "Sibu": (2.3, 111.8167),
    "Miri": (4.4147, 114.0089),
    "Alor Setar": (6.1167, 100.3667),
    "Lahad Datu": (5.03, 118.34),
    "Kulim": (5.365, 100.5617),
    "Tawau": (4.2981, 117.8831),
    "Putrajaya": (2.93, 101.69),
    "Arequipa": (-16.3989, -71.5369),
    "Trujillo": (-8.1119, -79.0289),
    "Vitarte": (-12.0249, -76.9223),
    "Callao": (-12.0522, -77.1392),
    "Chiclayo": (-6.7717, -79.8383),
    "Piura": (-5.2008, -80.6253),
    "Cusco": (-13.5169, -71.9786),
    "Chimbote": (-9.0745, -78.5936),
    "Huancayo": (-12.0667, -75.2167),
    "Iquitos": (-3.7333, -73.25),
    "Pucallpa": (-8.3833, -74.55),
    "Tacna": (-18.0556, -70.2483),
    "Ica": (-14.0667, -75.7333),
    "Juliaca": (-15.4908, -70.1269),
    "Tarapoto": (-6.4833, -76.3667),
    "Ayacucho": (-13.1631, -74.2244),
    "Cajamarca": (-7.1575, -78.5175),
    "Huacho": (-11.1067, -77.605),
    "Huánuco": (-9.9294, -76.2397),
    "Sullana": (-4.9039, -80.6853),
    "Puno": (-15.8433, -70.0236),
    "Huaraz": (-9.5333, -77.5333),
    "Pisco": (-13.71, -76.2032),
    "Riyadh": (24.6383, 46.715),
    "Jeddah": (21.5292, 39.1611),
    "Medina": (24.47, 39.61),
    "Dammam": (26.4333, 50.1),
    "Ta'if": (21.2667, 40.4167),
    "Burayda": (26.3333, 43.9667),
    "Khamis Mushait": (18.3, 42.7333),
    "Hafar Al-Batin": (28.4342, 45.9636),
    "Jubail": (27.0, 49.6667),
    "Ha'il": (27.5167, 41.6833),
    "Yanbu": (24.0894, 38.0619),
    "Najran": (17.4917, 44.1322),
    "Al-Kharj": (24.1483, 47.305),
    "Unaizah": (26.084, 43.994),
    "Abha": (18.2167, 42.5),
    "Khobar": (26.3, 50.2167),
    "Arar": (30.9833, 41.0167),
    "Sakakah": (29.9697, 40.2),
    "Al Hawiyah": (21.4411, 40.4975),
    "Dhahran": (26.2667, 50.15),
    "Jizan": (16.8892, 42.5611),
    "Qatif": (26.5752, 49.9969),
    "Qurayyat": (31.3318, 37.3428),
    "Sekondi-Takoradi": (4.9268, -1.7577),
    "Obuasi": (6.2023, -1.668),
    "Tema": (5.6698, -0.0166),
    "Ho": (6.6114, 0.4703),
    "Wa": (10.0667, -2.5),
    "Antananarivo": (-18.91, 47.525),
    "Antsirabe": (-19.8667, 47.0333),
    "Toamasina": (-18.155, 49.41),
    "Caracas": (10.5061, -66.9144),
    "Maracaibo": (10.6333, -71.6333),
    "Maracay": (10.2469, -67.5958),
    "Barquisimeto": (10.0678, -69.3467),
    "Barinas": (8.6333, -70.2167),
    "Maturín": (9.7423, -63.1889),
    "Ciudad Guayana": (8.3224, -62.6897),
    "Catia La Mar": (10.6038, -67.0303),
    "Ciudad Bolívar": (8.1029, -63.547),
    "Santa Teresa del Tuy": (10.2333, -66.65),
    "Cumaná": (10.45, -64.1667),
    "Cabimas": (10.4, -71.4333),
    "Los Teques": (10.3411, -67.0406),
    "Puerto La Cruz": (10.2167, -64.6167),
    "Punto Fijo": (11.7167, -70.1833),
    "San Cristóbal": (7.7682, -72.2322),
    "Coro": (11.395, -69.6816),
    "Turmero": (10.2283, -67.4753),
    "Valera": (9.3178, -70.6036),
    "San Felipe": (10.3353, -68.7458),
    "Cúa": (10.1667, -66.8833),
    "La Victoria": (10.2278, -67.3336),
    "El Tigre": (8.8858, -64.2611),
    "Puerto Cabello": (10.4667, -68.0167),
    "Guarenas": (10.4739, -66.5383),
    "La Guaira": (10.6, -66.9331),
    "Guacara": (10.2261, -67.877),
    "Tocuyito": (10.0889, -68.0922),
    "Acarigua": (9.5597, -69.2019),
    "San Fernando de Apure": (7.894, -67.473),
    "Guanare": (9.0436, -69.7489),
    "Calabozo": (8.9219, -67.4283),
    "Carúpano": (10.6722, -63.2403),
    "Anaco": (9.4333, -64.4667),
    "Palo Negro": (10.16, -67.5582),
    "Yaritagua": (10.08, -69.1261),
    "Los Guayos": (10.1833, -67.9333),
    "Táriba": (7.8167, -72.2167),
    "Guasdualito": (7.2467, -70.7292),
    "Machiques": (10.0592, -72.5531),
    "Cagua": (10.1875, -67.4611),
    "Ciudad Ojeda": (10.2, -71.3),
    "Porlamar": (10.9556, -63.8478),
    "Charallave": (10.2431, -66.8622),
    "La Asunción": (11.0333, -63.8628),
    "Valle de la Pascua": (9.2155, -66.0073),
    "Carora": (10.1692, -70.0783),
    "El Rosario de Soapire": (10.2606, -66.6639),
    "Quíbor": (9.9281, -69.5778),
    "Socopó": (8.2322, -70.8206),
    "Tinaquillo": (9.9167, -68.3),
    "Ejido": (8.5466, -71.2409),
    "El Limón": (10.3059, -67.6321),
    "Boconó": (9.2533, -70.25),
    "Punta de Mata": (9.6866, -63.611),
    "Abidjan": (5.3364, -4.0267),
    "Bouaké": (7.6833, -5.0331),
    "Korhogo": (9.4578, -5.6294),
    "San-Pédro": (4.7333, -6.6333),
    "Yamoussoukro": (6.8161, -5.2742),
    "Divo": (5.8372, -5.3572),
    "Gagnoa": (6.1333, -5.9333),
    "Soubré": (5.7836, -6.5939),
    "Tabou": (4.4231, -7.3528),
    "Man": (7.4125, -7.5536),
    "Duékoué": (6.7348, -7.3375),
    "Bouaflé": (6.9903, -5.7442),
    "Bingerville": (5.35, -3.9),
    "Guiglo": (6.5436, -7.4933),
    "Lakota": (5.8475, -5.6819),
    "Abengourou": (6.7297, -3.4964),
    "Ferkessédougou": (9.5928, -5.1944),
    "Adzopé": (6.1035, -3.8648),
    "Méagui": (5.4119, -6.5603),
    "Bondoukou": (8.04, -2.8),
    "Dabou": (5.3256, -4.3767),
    "Sinfra": (6.621, -5.9114),
    "Agboville": (5.9227, -4.2091),
    "Vavoua": (7.3833, -6.4833),
    "Danané": (7.2667, -8.1667),
    "Oumé": (6.3833, -5.4167),
    "Issia": (6.4922, -6.5856),
    "Grand-Bassam": (5.2, -3.7333),
    "Bloléquin": (6.5667, -8.0),
    "Bonoua": (5.2667, -3.6),
    "Bonon": (6.9333, -6.05),
    "Gabiadji": (5.0234, -6.5616),
    "Grand-Zattry": (6.1181, -6.5399),
    "Dania": (7.15, -6.85),
    "Séguéla": (7.9611, -6.6731),
    "Daoukro": (7.0578, -3.9661),
    "Aboisso": (5.4667, -3.2),
    "Maputo": (-25.9689, 32.5733),
    "Matola": (-25.9667, 32.4667),
    "Nampula": (-15.1167, 39.2667),
    "Beira": (-19.8333, 34.85),
    "Quelimane": (-17.8764, 36.8872),
    "Chimoio": (-19.1167, 33.45),
    "Nacala": (-14.55, 40.6833),
    "Maxixe": (-23.8597, 35.3472),
    "Tete": (-16.1579, 33.5898),
    "Lichinga": (-13.3, 35.2456),
    "Pemba": (-12.9667, 40.5167),
    "Xai-Xai": (-25.05, 33.65),
    "Kathmandu": (27.71, 85.32),
    "Lalitpur": (27.6833, 85.3333),
    "Pokhara": (28.2097, 83.9853),
    "Biratnagar": (26.4833, 87.2833),
    "Ghorahi": (28.0408, 82.4861),
    "Birgunj": (27.0, 84.8667),
    "Butwal": (27.7, 83.45),
    "Dharan": (26.8167, 87.2667),
    "Gokarneshwor": (27.7333, 85.4),
    "Bhimdatta": (28.9167, 80.3333),
    "Dhangadhi": (28.7056, 80.575),
    "Douala": (4.05, 9.7),
    "Yaoundé": (3.8578, 11.5181),
    "Garoua": (9.3, 13.4),
    "Bafoussam": (5.4667, 10.4167),
    "Bamenda": (5.9614, 10.1517),
    "Maroua": (10.5909, 14.3159),
    "Ngaoundéré": (7.3214, 13.5839),
    "Bertoua": (4.5833, 13.6833),
    "Kumba": (4.6333, 9.45),
    "Nkongsamba": (4.95, 9.9167),
    "Buea": (4.1667, 9.2333),
    "Kousséri": (12.0833, 15.0333),
    "Sanaa": (15.35, 44.2),
    "Aden": (12.8, 45.0333),
    "Al Hudaydah": (14.8022, 42.9511),
    "Ta'izz": (13.5789, 44.0219),
    "Ibb": (13.9667, 44.1667),
    "Dhamar": (14.55, 44.4017),
    "Mukalla": (14.5333, 49.1333),
    "Seiyun": (15.9667, 48.7833),
    "Tarim": (16.05, 49.0),
    "Sheikh Othman": (12.8866, 45.0279),
    "Gold Coast": (-28.0167, 153.4),
    "Canberra": (-35.2931, 149.1269),
    "Wollongong": (-34.4331, 150.8831),
    "Ipswich": (-27.6167, 152.7667),
    "Hobart": (-42.8833, 147.3167),
    "Geelong": (-38.15, 144.35),
    "Townsville": (-19.2622, 146.8158),
    "Newcastle": (-32.9167, 151.75),
    "Cairns": (-16.9256, 145.7753),
    "Darwin": (-12.4381, 130.8411),
    "Ballarat": (-37.5608, 143.8475),
    "Toowoomba": (-27.55, 151.95),
    "Bendigo": (-36.75, 144.2667),
    "Ouagadougou": (12.3686, -1.5275),
    "Bobo-Dioulasso": (11.1833, -4.2833),
    "Koudougou": (12.25, -2.3667),
    "Ouahigouya": (13.5833, -2.4167),
    "Kaya": (13.0833, -1.0833),
    "Banfora": (10.6308, -4.7589),
    "Bamako": (12.6458, -7.9922),
    "Sikasso": (11.3167, -5.6667),
    "Ségou": (13.4317, -6.2157),
    "Niamey": (13.515, 2.1175),
    "Maradi": (13.501, 7.1036),
    "Zinder": (13.8017, 8.9854),
    "Tahoua": (14.8903, 5.2642),
    "Gampaha": (7.0917, 79.9997),
    "Negombo": (7.2111, 79.8386),
    "Kandy": (7.2931, 80.635),
    "Sri Jayawardenepura Kotte": (6.9, 79.9164),
    "Almaty": (43.24, 76.915),
    "Shymkent": (42.3, 69.6),
    "Astana": (51.1333, 71.4333),
    "Karaganda": (49.8, 73.1167),
    "Taraz": (42.8833, 71.3667),
    "Aktobe": (50.2836, 57.2297),
    "Semey": (50.4111, 80.2275),
    "Pavlodar": (52.3156, 76.9564),
    "Oskemen": (49.95, 82.6167),
    "Atyrau": (47.1167, 51.8833),
    "Kyzylorda": (44.8479, 65.4999),
    "Kostanay": (53.2144, 63.6246),
    "Oral": (51.2225, 51.3725),
    "Turkistan": (43.3019, 68.2692),
    "Petropavl": (54.8622, 69.1408),
    "Temirtau": (50.0667, 72.9667),
    "Aktau": (43.6525, 51.1575),
    "Taldyqorğan": (45.0167, 78.3667),
    "Ekibastuz": (51.7298, 75.3266),
    "Kokshetau": (53.2824, 69.3969),
    "Rudny": (52.9667, 63.1167),
    "Lusaka": (-15.4167, 28.2833),
    "Kitwe": (-12.8208, 28.2119),
    "Ndola": (-12.9689, 28.6325),
    "Kabwe": (-14.4333, 28.45),
    "Chingola": (-12.5447, 27.8708),
    "Mufulira": (-12.5546, 28.2604),
    "Livingstone": (-17.85, 25.8667),
    "Kasama": (-10.2117, 31.1783),
    "Mongu": (-15.2483, 23.1275),
    "Luanshya": (-13.1333, 28.4),
    "Chililabombwe": (-12.3667, 27.8333),
    "Chipata (Fort Jameson)": (-13.6386, 32.6453),
    "Santo Domingo": (-33.6333, -71.65),
    "Puente Alto": (-33.6167, -70.5833),
    "Antofagasta": (-23.6464, -70.3981),
    "Viña del Mar": (-33.0245, -71.5518),
    "San Bernardo": (-33.6, -70.7167),
    "Valparaíso": (-33.0461, -71.6197),
    "Las Condes": (-33.4167, -70.5833),
    "Temuco": (-38.7399, -72.5901),
    "Talcahuano": (-36.7145, -73.1141),
    "Talca": (-35.4269, -71.6656),
    "Rancagua": (-34.1654, -70.7398),
    "Concepción": (-36.8269, -73.0503),
    "Coquimbo": (-29.9532, -71.338),
    "Arica": (-18.4784, -70.3212),
    "La Serena": (-29.9027, -71.252),
    "Iquique": (-20.2141, -70.1524),
    "Curicó": (-34.9854, -71.2394),
    "Puerto Montt": (-41.4718, -72.9396),
    "Chillán": (-36.6067, -72.1033),
    "Calama": (-22.4624, -68.9272),
    "Copiapó": (-27.3665, -70.3323),
    "Valdivia": (-39.8142, -73.2459),
    "Melipilla": (-33.6855, -71.2146),
    "Quilpué": (-33.0498, -71.4415),
    "Osorno": (-40.5667, -73.15),
    "Colina": (-33.2023, -70.6749),
    "Los Ángeles (Chile)": (-37.4707, -72.3517),
    "Villa Alemana": (-33.0428, -71.3744),
    "San Pedro de la Paz": (-36.8408, -73.1031),
    "Ovalle": (-30.6031, -71.203),
    "Punta Arenas": (-53.1625, -70.9081),
    "Coronel": (-37.0167, -73.1333),
    "Lampa": (-33.2833, -70.9),
    "N'Djamena": (12.11, 15.05),
    "Moundou": (8.5667, 16.0833),
    "Sarh": (9.15, 18.3833),
    "Iași": (47.1622, 27.5889),
    "Constanța": (44.1767, 28.6508),
    "Timișoara": (45.7597, 21.23),
    "Brașov": (45.6522, 25.6106),
    "Craiova": (44.3333, 23.8167),
    "Galați": (45.4336, 28.0553),
    "Oradea": (47.0722, 21.9211),
    "Ploiești": (44.9411, 26.0225),
    "Brăila": (45.2692, 27.9575),
    "Arad": (46.1667, 21.3167),
    "Pitești": (44.8667, 24.8833),
    "Bacău": (46.5833, 26.9167),
    "Sibiu": (45.8, 24.15),
    "Suceava": (47.6514, 26.2556),
    "Târgu Mureș": (46.5497, 24.5597),
    "Baia Mare": (47.6567, 23.5719),
    "Buzău": (45.1517, 26.8167),
    "Lilongwe": (-13.9864, 33.7681),
    "Blantyre": (-15.7861, 35.0058),
    "Mzuzu": (-11.45, 34.0333),
    "Conakry": (9.5092, -13.7122),
    "Siguiri": (11.4189, -9.1644),
    "Guéckédougou": (8.5667, -10.1333),
    "Kamsar": (10.65, -14.6167),
    "Labé": (11.3167, -12.2833),
    "Phnom Penh": (11.5696, 104.921),
    "Siem Reap": (13.3622, 103.8597),
    "Battambang": (13.1028, 103.1983),
    "Kampong Cham": (11.9933, 105.4636),
    "Guatemala": (14.6417, -90.5133),
    "Mixco": (14.6338, -90.6146),
    "Villa Nueva": (14.5314, -90.5964),
    "Quetzaltenango": (14.8333, -91.5167),
    "Jalapa": (14.6379, -89.9904),
    "Escuintla": (14.2978, -90.7869),
    "Villa Canales": (14.4816, -90.534),
    "Huehuetenango": (15.3147, -91.4761),
    "Totonicapán": (14.9108, -91.3606),
    "Puerto Barrios": (15.73, -88.6),
    "Retalhuleu": (14.5333, -91.6833),
    "Guayaquil": (-2.19, -79.8875),
    "Cuenca": (-2.8974, -79.0045),
    "Durán": (-2.1733, -79.8311),
    "Machala": (-3.2667, -79.9667),
    "Manta": (-0.95, -80.7162),
    "Portoviejo": (-1.0544, -80.4544),
    "Loja": (-3.9906, -79.205),
    "Quevedo": (-1.0333, -79.45),
    "Ambato": (-1.2422, -78.6289),
    "Riobamba": (-1.6731, -78.6483),
    "Daule": (-1.8667, -79.9833),
    "Milagro": (-2.1347, -79.5872),
    "Ibarra": (0.3628, -78.13),
    "Esmeraldas": (0.95, -79.6667),
    "La Libertad": (-2.2333, -80.9),
    "Dakar": (14.6726, -17.432),
    "Pikine": (14.744, -17.4021),
    "Touba": (14.8665, -15.8994),
    "Thiès": (14.7916, -16.9249),
    "Rufisque": (14.714, -17.2723),
    "M'Bour": (14.4104, -16.9649),
    "Saint-Louis": (16.0333, -16.5),
    "Keur Massar Nord": (14.7844, -17.3095),
    "Ziguinchor": (12.5861, -16.2707),
    "Yeumbeul Nord": (14.7763, -17.3525),
    "Kaolack": (14.152, -16.0726),
    "Sangalkam": (14.7768, -17.2256),
    "Tivaouane": (14.9543, -16.8132),
    "Diourbel": (14.655, -16.2314),
    "Harare": (-17.8292, 31.0522),
    "Masvingo": (-20.0744, 30.8328),
    "Bulawayo": (-20.1667, 28.5667),
    "Mutare": (-18.9707, 32.6709),
    "Gweru": (-19.4533, 29.8153),
    "Kigali": (-1.9525, 30.115),
    "Muhanga": (-2.0842, 29.7514),
    "Ruhengeri": (-1.4944, 29.6417),
    "Cotonou": (6.3667, 2.4167),
    "Porto-Novo": (6.4833, 2.6167),
    "Malanville": (11.8667, 3.3833),
    "Ouidah": (6.3667, 2.0833),
    "Dogbo-Tota": (6.8, 1.7833),
    "Bujumbura": (-3.3825, 29.3611),
    "Gitega": (-3.4283, 29.925),
    "Santa Cruz de la Sierra": (-17.7892, -63.1975),
    "El Alto": (-16.5047, -68.1633),
    "Cochabamba": (-17.3935, -66.157),
    "Sucre": (-19.0475, -65.26),
    "Oruro": (-17.9667, -67.1167),
    "Warnes": (-17.5103, -63.1647),
    "Potosí": (-19.5892, -65.7533),
    "Tarija": (-21.5339, -64.7342),
    "Sacaba": (-17.4042, -66.0408),
    "Quillacollo": (-17.3975, -66.2817),
    "Montero": (-17.3425, -63.2553),
    "Trinidad": (-14.8333, -64.9),
    "Ghent": (51.0536, 3.7253),
    "Liège": (50.6397, 5.5706),
    "Anderlecht": (50.8392, 4.3297),
    "Bruges": (51.2089, 3.2242),
    "Namur": (50.4667, 4.8667),
    "Leuven": (50.8775, 4.7044),
    "Tunis": (36.8008, 10.18),
    "Bizerte": (37.2744, 9.8739),
    "Monastir": (35.778, 10.8262),
    "North East State of Somalia": (9.1333, 48.4167),
    "Mogadishu": (2.0392, 45.3419),
    "Burao": (9.5279, 45.5345),
    "Berbera": (10.4395, 45.013),
    "Merca": (1.7156, 44.7703),
    "Kismayo": (-0.3603, 42.5489),
    "Ostrava": (49.8356, 18.2925),
    "Plzeň": (49.7414, 13.3825),
    "Liberec": (50.7667, 15.05),
    "Olomouc": (49.5939, 17.2508),
    "La Vega": (19.2242, -70.5283),
    "Higüey": (18.6181, -68.7111),
    "San Pedro de Macorís": (18.4572, -69.3061),
    "San Francisco de Macorís": (19.3, -70.25),
    "La Romana": (18.43, -68.97),
    "Patras": (38.2464, 21.7349),
    "Piraeus": (37.943, 23.6469),
    "Larissa": (39.6385, 22.4131),
    "Heraklion": (35.3403, 25.1344),
    "Peristeri": (38.0167, 23.6833),
    "Rhodes": (36.17, 27.92),
    "Amman": (31.95, 35.9333),
    "Irbid": (32.5556, 35.85),
    "Zarqa": (32.0833, 36.1),
    "Russeifa": (32.0178, 36.0464),
    "Ar Ramtha": (32.5592, 36.0069),
    "Ajloun": (32.3325, 35.7517),
    "Wadi Al-Seer": (31.9545, 35.8183),
    "Aqaba": (29.5322, 35.0061),
    "Braga": (41.5503, -8.42),
    "Baku": (40.3667, 49.8352),
    "Sumgait": (40.5961, 49.67),
    "Ganja": (40.6828, 46.3606),
    "Khyrdalan": (40.4486, 49.7564),
    "Abu Dhabi": (24.4511, 54.3969),
    "Sharjah": (25.3375, 55.4119),
    "Al Ain": (24.2075, 55.7447),
    "Ajman": (25.3994, 55.4797),
    "Ras al-Khaimah": (25.8986, 56.1283),
    "Jerusalem": (31.7767, 35.2342),
    "Haifa": (32.8192, 34.9992),
    "Rishon LeZion": (31.95, 34.8),
    "Petah Tikva": (32.0833, 34.8833),
    "Ashdod": (31.7978, 34.6503),
    "Netanya": (32.3286, 34.8567),
    "Beersheba": (31.2522, 34.7867),
    "Holon": (32.0167, 34.7667),
    "Bnei Brak": (32.0807, 34.8338),
    "Ashkelon": (31.6658, 34.5664),
    "Rehovot": (31.8914, 34.8078),
    "Beit Shemesh": (31.7514, 34.9886),
    "Kfar Saba": (32.1858, 34.9077),
    "Dushanbe": (38.5731, 68.7864),
    "Khujand": (40.2794, 69.6319),
    "Bokhtar": (37.8364, 68.7803),
    "Kulob": (37.9119, 69.7808),
    "Canton of Basel-Stadt": (47.5667, 7.6),
    "Lausanne": (46.5333, 6.6333),
    "Winterthur": (47.4992, 8.7267),
    "Linz": (48.3058, 14.2864),
    "Innsbruck": (47.2683, 11.3933),
    "Klagenfurt am Wörthersee": (46.6167, 14.3),
    "Port Moresby": (-9.4789, 147.1494),
    "Lae": (-6.7303, 147.0008),
    "Tegucigalpa": (14.1058, -87.2047),
    "San Pedro Sula": (15.5053, -88.0251),
    "Comayagua": (14.4528, -87.6379),
    "Lomé": (6.13, 1.2158),
    "Sokodé": (8.9833, 1.1333),
    "Freetown": (8.4872, -13.2356),
    "Bo": (7.9564, -11.74),
    "Kenema": (7.8833, -11.1833),
    "Makeni": (8.8817, -12.0442),
    "Tripoli": (32.8875, 13.1875),
    "Benghazi": (32.1167, 20.0667),
    "Misrata": (32.3778, 15.0901),
    "Al Bayda": (32.7628, 21.755),
    "Tobruk": (32.0833, 23.95),
    "Gharyan": (32.1669, 13.0167),
    "Sabratha": (32.8054, 12.4852),
    "Surman": (32.7567, 12.5717),
    "Bishkek": (42.8667, 74.5667),
    "Osh": (40.5333, 72.7833),
    "Vientiane": (17.98, 102.63),
    "Kaysone Phomvihane": (16.55, 104.75),
    "Pāksē": (15.1167, 105.7833),
    "Asunción": (-25.28, -57.6344),
    "Ciudad del Este": (-25.5167, -54.6167),
    "Pedro Juan Caballero": (-22.5667, -55.7167),
    "Encarnación": (-27.3333, -55.8667),
    "Caaguazú District": (-25.46, -56.02),
    "Novi Sad": (45.2644, 19.8317),
    "Niš": (43.3192, 21.8961),
    "Zemun": (44.85, 20.4),
    "Kragujevac": (44.0101, 20.9172),
    "Subotica": (46.1002, 19.6653),
    "Plovdiv": (42.1421, 24.7415),
    "Varna": (43.2114, 27.9111),
    "Burgas": (42.503, 27.4702),
    "Stara Zagora": (42.4257, 25.6346),
    "Ruse": (43.8445, 25.9539),
    "Brazzaville": (-4.2694, 15.2711),
    "Pointe-Noire": (-4.7975, 11.8503),
    "Dolisie": (-4.2006, 12.6792),
    "Nkayi": (-4.1842, 13.2883),
    "Ashgabat": (37.95, 58.3833),
    "Türkmenabat": (39.1, 63.5667),
    "Daşoguz": (41.8167, 59.9831),
    "Mary": (37.6, 61.8333),
    "Balkanabat": (39.5119, 54.365),
    "Sidon": (33.5606, 35.3758),
    "Tyre": (33.2667, 35.2),
    "Nabatieh": (33.3772, 35.4836),
    "Jounieh": (33.9697, 35.6156),
    "Zahlé": (33.8439, 35.9072),
    "San Salvador": (13.6976, -89.1912),
    "Ahuachapán": (13.9234, -89.8463),
    "Santa Tecla": (13.6737, -89.2886),
    "Bukit Merah": (1.2819, 103.8239),
    "Aarhus": (56.1564, 10.2097),
    "Odense": (55.4, 10.3833),
    "Aalborg": (57.05, 9.9167),
    "Frederiksberg": (55.6779, 12.5327),
    "Espoo": (60.2056, 24.6556),
    "Tampere": (61.4981, 23.76),
    "Vantaa": (60.2944, 25.0403),
    "Oulu": (65.0142, 25.4719),
    "Turku": (60.4517, 22.2669),
    "Jyväskylä": (62.2403, 25.7444),
    "Kuopio": (62.8925, 27.6783),
    "Lahti": (60.9804, 25.655),
    "Trondheim": (63.44, 10.4),
    "Stavanger": (58.9701, 5.7333),
    "Sandvika": (59.89, 10.5264),
    "Košice": (48.7167, 21.25),
    "Christchurch": (-43.5311, 172.6361),
    "Tauranga": (-37.6858, 176.1667),
    "Dunedin": (-45.8742, 170.5036),
    "Lower Hutt": (-41.2167, 174.9167),
    "San José (Costa Rica)": (9.9327, -84.0796),
    "Monrovia": (6.3106, -10.8047),
    "Bangui": (4.3732, 18.5628),
    "Bimbo": (4.3313, 18.5163),
    "Cork": (51.9, -8.4731),
    "Muscat": (23.6139, 58.5922),
    "Seeb": (23.6667, 58.1617),
    "Salalah": (17.0197, 54.0897),
    "Sohar": (24.342, 56.7299),
    "Sur (Oman)": (22.5667, 59.5289),
    "Nouakchott": (18.0858, -15.9785),
    "Nouadhibou": (20.9148, -17.0502),
    "San Miguelito District": (9.05, -79.5),
    "Colón": (9.3598, -79.9004),
    "Las Cumbres": (9.08, -79.53),
    "Santiago de Veraguas": (8.1057, -80.9711),
    "Kuwait": (29.375, 47.98),
    "Tbilisi": (41.7225, 44.7925),
    "Batumi": (41.6458, 41.6417),
    "Kutaisi": (42.2717, 42.7056),
    "Rustavi": (41.5436, 45.0117),
    "Split": (43.51, 16.44),
    "Rijeka": (45.3333, 14.4333),
    "Sarajevo": (43.8564, 18.4131),
    "Banja Luka": (44.7725, 17.1925),
    "Tuzla": (44.5391, 18.6752),
    "Asmara": (15.3333, 38.9167),
    "Keren": (15.7833, 38.45),
    "Ciudad de la Costa": (-34.8167, -55.95),
    "Ulaanbaatar": (47.9214, 106.9055),
    "Mörön": (49.6375, 100.1614),
    "Erdenet": (49.0333, 104.0833),
    "Yerevan": (40.1814, 44.5144),
    "Gyumri": (40.7833, 43.8333),
    "Kaunas": (54.9, 23.9333),
    "Klaipėda": (55.7125, 21.135),
    "Šiauliai": (55.9281, 23.3167),
    "Tirana": (41.3283, 19.8181),
    "Durrës": (41.3133, 19.4458),
    "Portmore": (17.9667, -76.8667),
    "Spanish Town": (17.9961, -76.9547),
    "Montego Bay": (18.4667, -77.9167),
    "Doha": (25.2861, 51.5294),
    "Windhoek": (-22.57, 17.0836),
    "Rundu": (-17.9167, 19.7667),
    "Walvis Bay": (-22.9494, 14.5069),
    "Gaborone": (-24.6569, 25.9086),
    "Chișinău": (47.0228, 28.8353),
    "Tiraspol": (46.85, 29.6333),
    "Maribor": (46.55, 15.6333),
    "Libreville": (0.4078, 9.4403),
    "Port-Gentil": (-0.7167, 8.7833),
    "Franceville": (-1.6333, 13.5833),
    "Maseru": (-29.31, 27.48),
    "Bissau": (11.86, -15.5833),
    "Malabo": (3.7521, 8.7737),
    "Bata (Equatorial Guinea)": (1.865, 9.77),
    "Skopje": (41.9961, 21.4317),
    "Manama": (26.2167, 50.5833),
    "Al-Muharraq": (26.2533, 50.6083),
    "Hamad Town": (26.1128, 50.5139),
    "Riffa": (26.13, 50.555),
    "Dili": (-8.5536, 125.5783),
    "Limassol": (34.675, 33.0443),
    "Port Louis": (-20.1619, 57.4989),
    "Lobamba": (-26.4465, 31.2064),
    "Manzini": (-26.4833, 31.3667),
    "Djibouti": (11.595, 43.1481),
    "Georgetown": (6.8058, -58.1508),
    "Thimphu": (27.4714, 89.6337),
    "Luxembourg": (49.6114, 6.13),
    "Podgorica": (42.4414, 19.2628),
    "Paramaribo": (5.8667, -55.1667),
    "Praia": (14.9177, -23.5092),
    "Malé": (4.175, 73.5083),
    "Reykjavík": (64.1475, -21.935),
    "Willemstad": (12.108, -68.935),
    # Indonesia — imported from Wikidata by scripts/import_cities.py (min pop 100000)
    "Ambon": (-3.6967, 128.1783),
    "Balikpapan": (-1.2379, 116.8563),
    "Banda Aceh": (5.55, 95.3175),
    "Bandar Lampung": (-5.4294, 105.2625),
    "Bandung": (-6.9218, 107.6071),
    "Banjar": (-7.3667, 108.5333),
    "Banjarbaru": (-3.4389, 114.8309),
    "Banjarmasin": (-3.3144, 114.5923),
    "Batam": (1.13, 104.0531),
    "Batu": (-7.8672, 112.5239),
    "Bau Bau": (-5.4622, 122.6058),
    "Bekasi": (-6.2333, 107.0),
    "Bengkulu": (-3.7956, 102.2592),
    "Bima": (-8.46, 118.7267),
    "Binjai": (3.6, 98.4853),
    "Bitung": (1.4472, 125.1978),
    "Blitar": (-8.1, 112.1667),
    "Bogor": (-6.5978, 106.7993),
    "Bontang": (0.1333, 117.5),
    "Bukittinggi": (-0.3097, 100.3753),
    "Cilegon": (-6.0167, 106.0167),
    "Cimahi": (-6.88, 107.5385),
    "Cirebon": (-6.7397, 108.553),
    "Denpasar": (-8.6717, 115.2339),
    "Depok": (-6.405, 106.8173),
    "Dumai": (1.6667, 101.45),
    "Gorontalo": (0.5422, 123.0614),
    "Gunungsitoli": (1.1167, 97.5667),
    "Jambi": (-1.6288, 103.6082),
    "Jayapura": (-2.5425, 140.7045),
    "Kediri": (-7.8111, 112.0047),
    "Kendari": (-3.9675, 122.5947),
    "Kotamobagu": (0.7333, 124.3167),
    "Kupang": (-10.1633, 123.5778),
    "Langsa": (4.4667, 97.95),
    "Lhokseumawe": (5.18, 97.1506),
    "Lubuk Linggau": (-3.2967, 102.8617),
    "Magelang": (-7.4706, 110.2178),
    "Makassar": (-5.1331, 119.4136),
    "Malang": (-7.9689, 112.6327),
    "Manado": (1.4931, 124.8413),
    "Mataram": (-8.5833, 116.1167),
    "Medan": (3.5894, 98.6739),
    "Metro": (-5.1167, 105.3),
    "Mojokerto": (-7.4722, 112.4336),
    "Padang": (-0.9556, 100.3606),
    "Padangsidimpuan": (1.3667, 99.2667),
    "Pagar Alam": (-4.0217, 103.2522),
    "Palangka Raya": (-2.21, 113.92),
    "Palembang": (-2.9833, 104.7644),
    "Palopo": (-3.0, 120.2),
    "Palu": (-0.895, 119.8594),
    "Pangkal Pinang": (-2.1, 106.1),
    "Parepare": (-4.0167, 119.6236),
    "Pasuruan": (-7.6406, 112.9065),
    "Payakumbuh": (-0.2244, 100.6325),
    "Pekalongan": (-6.8883, 109.6753),
    "Pekanbaru": (0.5333, 101.45),
    "Pematangsiantar": (2.96, 99.06),
    "Pontianak": (-0.0833, 109.3667),
    "Prabumulih": (-3.4328, 104.2356),
    "Probolinggo": (-7.75, 113.2167),
    "Salatiga": (-7.3389, 110.5022),
    "Samarinda": (-0.5022, 117.1536),
    "Semarang": (-6.99, 110.4225),
    "Serang": (-6.12, 106.1503),
    "Singkawang": (0.9, 108.9833),
    "Sorong": (-0.8796, 131.261),
    "South Tangerang": (-6.2889, 106.7181),
    "Sukabumi": (-6.932, 106.9186),
    "Sungai Penuh": (-2.0636, 101.3961),
    "Surabaya": (-7.2463, 112.7378),
    "Surakarta": (-7.5667, 110.8167),
    "Tangerang": (-6.1703, 106.6403),
    "Tanjungbalai": (2.9667, 99.8),
    "Tanjungpinang": (0.9188, 104.4554),
    "Tarakan": (3.3, 117.6333),
    "Tasikmalaya": (-7.3258, 108.2202),
    "Tebing Tinggi": (3.3283, 99.1625),
    "Tegal": (-6.8675, 109.1375),
    "Ternate": (0.7833, 127.3667),
    "Tidore Islands": (0.6094, 127.5697),
    "Yogyakarta": (-7.8005, 110.3913),
    # Hungary — imported from Wikidata by scripts/import_cities.py (min pop 1000)
    "Abasár": (47.8024, 20.0075),
    "Abda": (47.6951, 17.5419),
    "Acsa": (47.7971, 19.3815),
    "Adács": (47.6911, 19.9747),
    "Akasztó": (46.6897, 19.2011),
    "Alap": (46.81, 18.6805),
    "Alattyán": (47.4263, 20.0409),
    "Alcsútdoboz": (47.4258, 18.6026),
    "Algyő": (46.3333, 20.2),
    "Almásfüzitő": (47.7287, 18.2554),
    "Alsónémedi": (47.3137, 19.1665),
    "Alsópáhok": (46.7606, 17.1735),
    "Alsószentmárton": (45.7904, 18.3056),
    "Alsóvadász": (48.2396, 20.9039),
    "Alsóörs": (46.9945, 17.9736),
    "Anarcs": (48.1833, 22.1167),
    "Andornaktálya": (47.8446, 20.4118),
    "Apagy": (47.9573, 21.9363),
    "Apaj": (47.1142, 19.0888),
    "Apc": (47.7961, 19.6898),
    "Apostag": (46.8808, 18.9597),
    "Apátfalva": (46.1667, 20.5833),
    "Aranyosapáti": (48.2, 22.2667),
    "Arló": (48.1728, 20.2599),
    "Arnót": (48.132, 20.8599),
    "Aszaló": (48.2212, 20.9606),
    "Atkár": (47.7211, 19.8912),
    "Babócsa": (46.0379, 17.3445),
    "Babót": (47.5749, 17.0756),
    "Bag": (47.6357, 19.4837),
    "Bagamér": (47.45, 22.0),
    "Bagod": (46.8853, 16.7439),
    "Baj": (47.6495, 18.3615),
    "Bajna": (47.6521, 18.5988),
    "Bajót": (47.7272, 18.5558),
    "Bak": (46.729, 16.8444),
    "Bakonszeg": (47.1833, 21.45),
    "Bakonybél": (47.2529, 17.7296),
    "Bakonycsernye": (47.3193, 18.087),
    "Bakonyszentlászló": (47.3875, 17.8007),
    "Bakonyszombathely": (47.4721, 17.9602),
    "Baks": (46.55, 20.1),
    "Balatonakarattya": (47.0181, 18.1578),
    "Balatonberény": (46.7068, 17.3209),
    "Balatonederics": (46.8056, 17.3779),
    "Balatonendréd": (46.8404, 17.9781),
    "Balatonfenyves": (46.7132, 17.4801),
    "Balatonfőkajár": (47.0205, 18.2108),
    "Balatongyörök": (46.7616, 17.3487),
    "Balatonkeresztúr": (46.6956, 17.3715),
    "Balatonszabadi": (46.893, 18.1366),
    "Balatonszemes": (46.8094, 17.7711),
    "Balatonszentgyörgy": (46.6885, 17.2953),
    "Balatonszárszó": (46.8295, 17.8351),
    "Balatonvilágos": (46.9744, 18.1653),
    "Ballószög": (46.8667, 19.5833),
    "Balogunyom": (47.1587, 16.6456),
    "Balotaszállás": (46.35, 19.5333),
    "Balástya": (46.4167, 20.0167),
    "Bana": (47.6499, 17.9212),
    "Baracs": (46.9105, 18.8664),
    "Baracska": (47.2874, 18.757),
    "Becsehely": (46.446, 16.7871),
    "Bekecs": (48.1514, 21.171),
    "Bercel": (47.8707, 19.4032),
    "Berekböszörmény": (47.0667, 21.6833),
    "Berekfürdő": (47.3833, 20.85),
    "Beremend": (45.7838, 18.4323),
    "Berente": (48.2442, 20.6633),
    "Berzence": (46.2119, 17.151),
    "Berzék": (48.0244, 20.9531),
    "Besenyőtelek": (47.7, 20.432),
    "Besnyő": (47.1891, 18.7898),
    "Bezenye": (47.9617, 17.2156),
    "Biharnagybajom": (47.2167, 21.2333),
    "Biri": (47.8167, 21.85),
    "Bocfölde": (46.7814, 16.8426),
    "Boconád": (47.6429, 20.1881),
    "Bocskaikert": (47.6457, 21.6614),
    "Bodroghalom": (48.3, 21.7052),
    "Bogyiszló": (46.3858, 18.8312),
    "Bogács": (47.9031, 20.5319),
    "Bogád": (46.0833, 18.3333),
    "Bokod": (47.4926, 18.2481),
    "Boldog": (47.6031, 19.6881),
    "Boldogkőváralja": (48.338, 21.2358),
    "Boldva": (48.2172, 20.7885),
    "Bordány": (46.3167, 19.9167),
    "Borota": (46.266, 19.221),
    "Borsodszirák": (48.2606, 20.7688),
    "Bucsa": (47.205, 20.997),
    "Budajenő": (47.55, 18.8),
    "Bugac": (46.6833, 19.6833),
    "Bugyi": (47.2233, 19.1499),
    "Buj": (48.0964, 21.6475),
    "Buják": (47.8833, 19.55),
    "Buzsák": (46.6431, 17.585),
    "Bácsbokod": (46.1244, 19.1583),
    "Bácsborsód": (46.0942, 19.1589),
    "Bágyogszovát": (47.5895, 17.3604),
    "Bánhorváti": (48.2253, 20.5044),
    "Bánréve": (48.2984, 20.355),
    "Bárdudvarnok": (46.3283, 17.6862),
    "Báránd": (47.293, 21.228),
    "Báta": (46.1286, 18.7703),
    "Bátmonostor": (46.1045, 18.9296),
    "Bátya": (46.4833, 18.95),
    "Békésszentandrás": (46.8667, 20.4833),
    "Békéssámson": (46.4167, 20.6333),
    "Bénye": (47.35, 19.55),
    "Bócsa": (46.6, 19.5),
    "Böhönye": (46.4119, 17.3929),
    "Bököny": (47.7333, 21.75),
    "Bölcske": (46.7333, 18.9667),
    "Börcs": (47.6868, 17.4996),
    "Bükkaranyos": (47.9859, 20.7799),
    "Bükkszentkereszt": (48.0645, 20.6334),
    "Bükkábrány": (47.8884, 20.6793),
    "Bükkösd": (46.1, 17.9833),
    "Bőcs": (48.0432, 20.9643),
    "Bőny": (47.65, 17.8667),
    "Bősárkány": (47.6884, 17.2479),
    "Cece": (46.7715, 18.6346),
    "Ceglédbercel": (47.2225, 19.6664),
    "Cibakháza": (46.9667, 20.2),
    "Csabacsűd": (46.8167, 20.65),
    "Csabdi": (47.523, 18.6102),
    "Csabrendek": (47.0137, 17.2932),
    "Csanytelek": (46.6062, 20.1048),
    "Csanádapáca": (46.55, 20.8833),
    "Csemő": (47.1167, 19.7),
    "Csengele": (46.5333, 19.8667),
    "Csengőd": (46.7167, 19.2667),
    "Cserkeszőlő": (46.8667, 20.1833),
    "Cserszegtomaj": (46.7928, 17.2149),
    "Csetény": (47.3177, 17.9988),
    "Csobánka": (47.6425, 18.9678),
    "Csokonyavisonta": (46.081, 17.4461),
    "Csolnok": (47.6935, 18.713),
    "Csomád": (47.6667, 19.2333),
    "Csopak": (46.9783, 17.9188),
    "Csákberény": (47.3459, 18.33),
    "Csákánydoroszló": (46.9728, 16.5044),
    "Csány": (47.645, 19.825),
    "Császár": (47.502, 18.1421),
    "Császártöltés": (46.422, 19.179),
    "Csátalja": (46.036, 18.943),
    "Csávoly": (46.191, 19.147),
    "Csépa": (46.8, 20.1333),
    "Csévharaszt": (47.3, 19.4333),
    "Csókakő": (47.3533, 18.2731),
    "Csólyospálos": (46.4178, 19.8408),
    "Csór": (47.2039, 18.257),
    "Csökmő": (47.0333, 21.3),
    "Csömör": (47.5467, 19.2244),
    "Csörög": (47.7314, 19.2029),
    "Dalmand": (46.492, 18.1899),
    "Darnózseli": (47.8493, 17.4267),
    "Daruszentmiklós": (46.8684, 18.8624),
    "Decs": (46.2846, 18.7569),
    "Dejtár": (48.0333, 19.1667),
    "Derekegyház": (46.5833, 20.3667),
    "Deszk": (46.2167, 20.25),
    "Detk": (47.75, 20.1),
    "Diósd": (47.4042, 18.9458),
    "Diósjenő": (47.9425, 19.0442),
    "Doboz": (46.7333, 21.25),
    "Domaszék": (46.25, 20.0167),
    "Dombegyház": (46.3386, 21.1311),
    "Domony": (47.6531, 19.4381),
    "Domoszló": (47.831, 20.119),
    "Drégelypalánk": (48.05, 19.05),
    "Dudar": (47.3072, 17.9394),
    "Dunaalmás": (47.7296, 18.3217),
    "Dunabogdány": (47.7944, 19.0294),
    "Dunaegyháza": (46.8333, 18.95),
    "Dunakiliti": (47.9651, 17.2885),
    "Dunapataj": (46.643, 18.996),
    "Dunaszeg": (47.7684, 17.5414),
    "Dunaszekcső": (46.0833, 18.7667),
    "Dunaszentgyörgy": (46.5333, 18.8333),
    "Dunasziget": (47.9389, 17.3513),
    "Dusnok": (46.383, 18.966),
    "Dánszentmiklós": (47.2143, 19.5434),
    "Dány": (47.5193, 19.5452),
    "Dávod": (46.0, 18.9167),
    "Dédestapolcsány": (48.1803, 20.4835),
    "Dég": (46.8708, 18.4422),
    "Délegyháza": (47.25, 19.0667),
    "Döbrököz": (46.4167, 18.25),
    "Döge": (48.2667, 22.0833),
    "Dömsöd": (47.0833, 19.0),
    "Dömös": (47.7648, 18.9144),
    "Ebes": (47.4667, 21.5),
    "Ecseg": (47.8971, 19.6041),
    "Ecsegfalva": (47.15, 20.9167),
    "Ecser": (47.4444, 19.326),
    "Ecséd": (47.731, 19.769),
    "Egerbakta": (47.935, 20.291),
    "Egercsehi": (48.053, 20.261),
    "Egerszalók": (47.8667, 20.3333),
    "Egerszólát": (47.883, 20.267),
    "Egervár": (46.9355, 16.8536),
    "Egyek": (47.6317, 20.8889),
    "Egyházasrádóc": (47.0881, 16.6161),
    "Előszállás": (46.825, 18.8266),
    "Encsencs": (47.7333, 22.1167),
    "Endrefalva": (48.1279, 19.5733),
    "Enese": (47.6457, 17.4205),
    "Eperjeske": (48.3453, 22.2139),
    "Erdőkertes": (47.6749, 19.3158),
    "Erdőtelek": (47.683, 20.317),
    "Erk": (47.617, 20.083),
    "Esztár": (47.2833, 21.7833),
    "Etes": (48.1098, 19.7188),
    "Etyek": (47.4472, 18.7496),
    "Fadd": (46.4665, 18.8202),
    "Fajsz": (46.418, 18.919),
    "Farkaslyuk": (48.1865, 20.3119),
    "Farmos": (47.3667, 19.85),
    "Farád": (47.6055, 17.2005),
    "Fehérvárcsurgó": (47.2901, 18.2687),
    "Felcsút": (47.4555, 18.5848),
    "Feldebrő": (47.8139, 20.2361),
    "Felgyő": (46.65, 20.1),
    "Felsőlajos": (47.0654, 19.4947),
    "Felsőnyék": (46.7887, 18.2909),
    "Felsőpakony": (47.3433, 19.2393),
    "Felsőszentiván": (46.195, 19.1903),
    "Felsőtárkány": (47.973, 20.417),
    "Felsőörs": (47.0144, 17.9522),
    "Fertőrákos": (47.7167, 16.65),
    "Fertőszéplak": (47.6158, 16.838),
    "Foktő": (46.528, 18.923),
    "Forráskút": (46.3667, 19.9167),
    "Forró": (48.321, 21.0857),
    "Furta": (47.1333, 21.4667),
    "Fábiánháza": (47.8462, 22.3543),
    "Fábiánsebestyén": (46.6833, 20.4667),
    "Fényeslitke": (48.2667, 22.1),
    "Földes": (47.2876, 21.3653),
    "Földeák": (46.3167, 20.5),
    "Fülöp": (47.6, 22.0667),
    "Fülöpjakab": (46.742, 19.722),
    "Fülöpszállás": (46.821, 19.235),
    "Galambok": (46.522, 17.1244),
    "Galgahévíz": (47.6224, 19.5515),
    "Galgamácsa": (47.7, 19.3833),
    "Gara": (46.034, 19.04),
    "Gellénháza": (46.7693, 16.7862),
    "Gencsapáti": (47.2847, 16.5956),
    "Gerendás": (46.597, 20.86),
    "Gerjen": (46.4911, 18.9028),
    "Gesztely": (48.1057, 20.963),
    "Geszteréd": (47.7667, 21.7833),
    "Gomba": (47.3707, 19.5303),
    "Gyarmat": (47.4605, 17.4981),
    "Gyenesdiás": (46.7707, 17.293),
    "Gyermely": (47.5919, 18.6418),
    "Gyulaháza": (48.1396, 22.1098),
    "Gyömöre": (47.4979, 17.5671),
    "Gyöngyösfalu": (47.3183, 16.5806),
    "Gyöngyöshalász": (47.742, 19.922),
    "Gyöngyösoroszi": (47.826, 19.894),
    "Gyöngyössolymos": (47.814, 19.933),
    "Gyöngyöstarján": (47.8, 19.8667),
    "Gyúró": (47.3683, 18.7368),
    "Gyüre": (48.1833, 22.2833),
    "Győrladamér": (47.7553, 17.5622),
    "Győrszemere": (47.5517, 17.5633),
    "Győrság": (47.5756, 17.7531),
    "Győrtelek": (47.9333, 22.4333),
    "Győrzámoly": (47.7419, 17.5787),
    "Győrújbarát": (47.6079, 17.6464),
    "Győrújfalu": (47.7224, 17.6084),
    "Gádoros": (46.6667, 20.6),
    "Gávavencsellő": (48.1606, 21.595),
    "Gégény": (48.15, 21.95),
    "Gérce": (47.214, 17.0181),
    "Gönyű": (47.7333, 17.8333),
    "Görbeháza": (47.8333, 21.2333),
    "Görcsöny": (45.9667, 18.1333),
    "Görgeteg": (46.1473, 17.4448),
    "Hajdúbagos": (47.3927, 21.6652),
    "Hajdúszovát": (47.3927, 21.4786),
    "Hajmáskér": (47.1478, 18.0222),
    "Halimba": (47.0331, 17.5358),
    "Halmaj": (48.2463, 20.9971),
    "Halmajugra": (47.761, 20.056),
    "Halászi": (47.8897, 17.325),
    "Hangony": (48.2293, 20.1975),
    "Harka": (47.6333, 16.6),
    "Harsány": (47.9699, 20.7405),
    "Harta": (46.6945, 19.0274),
    "Hegyeshalom": (47.9129, 17.1544),
    "Hegykő": (47.6193, 16.7941),
    "Hejőbába": (47.9093, 20.9472),
    "Helvécia": (46.8333, 19.6167),
    "Hencida": (47.25, 21.7),
    "Herceghalom": (47.5, 18.75),
    "Hercegszántó": (45.95, 18.9333),
    "Hernád": (47.1625, 19.4331),
    "Hernádkak": (48.0925, 20.9608),
    "Hernádnémeti": (48.0795, 20.9724),
    "Hernádvécse": (48.4449, 21.1675),
    "Heréd": (47.709, 19.632),
    "Hetes": (46.4217, 17.6928),
    "Hidas": (46.2564, 18.4918),
    "Hodász": (47.9194, 22.1994),
    "Homokmégy": (46.4833, 19.0667),
    "Hort": (47.69, 19.78),
    "Hortobágy": (47.5832, 21.1517),
    "Hosszúhetény": (46.1633, 18.3514),
    "Hosszúpályi": (47.4, 21.75),
    "Hédervár": (47.8324, 17.456),
    "Héreg": (47.647, 18.5106),
    "Hévízgyörk": (47.6316, 19.52),
    "Hőgyész": (46.4966, 18.4178),
    "Igrici": (47.8674, 20.8827),
    "Iharosberény": (46.3663, 17.1112),
    "Ikervár": (47.2093, 16.8953),
    "Iklad": (47.6581, 19.4511),
    "Ikrény": (47.653, 17.5276),
    "Ilk": (48.1167, 22.2333),
    "Inke": (46.3925, 17.1916),
    "Ináncs": (48.2836, 21.0681),
    "Inárcs": (47.2667, 19.3333),
    "Iregszemcse": (46.6977, 18.1806),
    "Istenmezeje": (48.086, 20.055),
    "Iszkaszentgyörgy": (47.2386, 18.3014),
    "Iván": (47.4463, 16.9122),
    "Iváncsa": (47.154, 18.8269),
    "Izsófalva": (48.3098, 20.653),
    "Jakabszállás": (46.7617, 19.6011),
    "Jenő": (47.1095, 18.2481),
    "Jobbágyi": (47.8293, 19.6821),
    "Juta": (46.4062, 17.7364),
    "Ják": (47.1393, 16.5824),
    "Jánkmajtis": (47.95, 22.65),
    "Jánoshida": (47.3833, 20.0667),
    "Járdánháza": (48.1579, 20.2513),
    "Jármi": (47.9667, 22.25),
    "Jászalsószentgyörgy": (47.3667, 20.1),
    "Jászboldogháza": (47.3667, 20.0),
    "Jászdózsa": (47.5667, 20.0167),
    "Jászfelsőszentgyörgy": (47.5, 19.8),
    "Jászjákóhalma": (47.5167, 20.0),
    "Jászkarajenő": (47.0506, 20.0706),
    "Jászladány": (47.3667, 20.1667),
    "Jászszentandrás": (47.5833, 20.1833),
    "Jászszentlászló": (46.5667, 19.7667),
    "Jásztelek": (47.4821, 20.001),
    "Kajdacs": (46.5622, 18.6244),
    "Kajárpéc": (47.488, 17.6344),
    "Kajászó": (47.3239, 18.7198),
    "Kakasd": (46.3486, 18.591),
    "Kakucs": (47.25, 19.3667),
    "Kaposfő": (46.3586, 17.6622),
    "Kaposmérő": (46.3601, 17.7024),
    "Kaposszekcső": (46.3288, 18.1289),
    "Karancsalja": (48.1319, 19.7561),
    "Karancskeszi": (48.1633, 19.6996),
    "Karancslapujtő": (48.1538, 19.7317),
    "Karancsság": (48.1144, 19.6558),
    "Karcsa": (48.3105, 21.7973),
    "Kartal": (47.6675, 19.5283),
    "Karácsond": (47.73, 20.027),
    "Karád": (46.6919, 17.8433),
    "Kaszaper": (46.4667, 20.8333),
    "Katymár": (46.0322, 19.2108),
    "Kazár": (48.0489, 19.8514),
    "Kecskéd": (47.5231, 18.3068),
    "Kehidakustány": (46.8433, 17.0944),
    "Kelebia": (46.1974, 19.6058),
    "Kengyel": (47.0833, 20.3333),
    "Kerecsend": (47.796, 20.343),
    "Kesznyéten": (47.967, 21.0444),
    "Kesztölc": (47.7136, 18.7952),
    "Keszü": (46.0167, 18.1833),
    "Kevermes": (46.4167, 21.1833),
    "Kimle": (47.8216, 17.3691),
    "Kincsesbánya": (47.2645, 18.2758),
    "Kisapostag": (46.8938, 18.9317),
    "Kisgyőr": (48.0122, 20.6893),
    "Kiskunlacháza": (47.2, 19.0167),
    "Kisláng": (46.9612, 18.3843),
    "Kisléta": (47.8333, 22.0167),
    "Kislőd": (47.1455, 17.6224),
    "Kismarja": (47.25, 21.8333),
    "Kismaros": (47.8377, 19.0047),
    "Kisszállás": (46.2798, 19.4913),
    "Kistokaj": (48.0447, 20.8406),
    "Kiszombor": (46.1833, 20.4333),
    "Kocs": (47.6067, 18.2154),
    "Kocsola": (46.5277, 18.1821),
    "Kocsord": (47.95, 22.3833),
    "Kocsér": (47.0, 19.9167),
    "Komoró": (48.3, 22.1167),
    "Kompolt": (47.741, 20.253),
    "Kondoros": (46.75, 20.8),
    "Konyár": (47.3167, 21.6667),
    "Koroncó": (47.6007, 17.5293),
    "Kosd": (47.8046, 19.1773),
    "Kulcs": (47.0718, 18.9185),
    "Kunadacs": (46.95, 19.3167),
    "Kunbaja": (46.085, 19.4208),
    "Kunfehértó": (46.3589, 19.4161),
    "Kunmadaras": (47.4333, 20.8),
    "Kunsziget": (47.7395, 17.5208),
    "Kunszállás": (46.7667, 19.75),
    "Kunágota": (46.4333, 21.05),
    "Kurityán": (48.3131, 20.6238),
    "Kutas": (46.3347, 17.4563),
    "Kál": (47.7305, 20.2628),
    "Kálló": (47.7513, 19.4939),
    "Kállósemjén": (47.8586, 21.9306),
    "Kálmánháza": (47.8833, 21.5833),
    "Káloz": (46.9573, 18.4847),
    "Kántorjánosi": (47.9333, 22.15),
    "Kápolna": (47.7591, 20.2471),
    "Kápolnásnyék": (47.243, 18.6846),
    "Kék": (48.1167, 21.8833),
    "Kékcse": (48.25, 22.0),
    "Kétegyháza": (46.5333, 21.1833),
    "Kéthely": (46.6461, 17.394),
    "Kétsoprony": (46.7167, 20.8833),
    "Kóka": (47.4865, 19.5788),
    "Kóny": (47.6298, 17.3628),
    "Kópháza": (47.6382, 16.6428),
    "Kótaj": (48.05, 21.7167),
    "Kölcse": (48.0506, 22.7169),
    "Kölesd": (46.5103, 18.5904),
    "Kömlő (Heves)": (47.601, 20.441),
    "Kömlőd": (47.5468, 18.2605),
    "Környe": (47.5453, 18.327),
    "Köröm": (47.9825, 20.9553),
    "Köröstarcsa": (46.8833, 21.0333),
    "Kötegyán": (46.7333, 21.4833),
    "Kübekháza": (46.1495, 20.2763),
    "Kőröshegy": (46.8333, 17.9),
    "Kőszárhegy": (47.0907, 18.3406),
    "Kőtelek": (47.3333, 20.45),
    "Kővágószőlős": (46.0813, 18.1239),
    "Ladánybene": (47.0333, 19.45),
    "Lajoskomárom": (46.8421, 18.3393),
    "Lakitelek": (46.8833, 19.9833),
    "Legyesbénye": (48.162, 21.1468),
    "Lepsény": (46.9931, 18.2456),
    "Lesencetomaj": (46.8566, 17.3684),
    "Letkés": (47.8833, 18.7833),
    "Levelek": (47.9667, 21.9853),
    "Levél": (47.8937, 17.2005),
    "Leányfalu": (47.7267, 19.0894),
    "Leányvár": (47.6827, 18.7702),
    "Litér": (47.0994, 18.0065),
    "Lovasberény": (47.3139, 18.5469),
    "Lovászi": (46.55, 16.5667),
    "Lovászpatona": (47.4325, 17.626),
    "Ludányhalászi": (48.1332, 19.5228),
    "Lukácsháza": (47.35, 16.5833),
    "Lábod": (46.2014, 17.4569),
    "Lánycsók": (46.0, 18.6333),
    "Látrány": (46.7484, 17.7462),
    "Lövő": (47.5018, 16.7852),
    "Lőkösháza": (46.4333, 21.2333),
    "Madaras": (46.055, 19.2619),
    "Madocsa": (46.688, 18.949),
    "Magyaralmás": (47.2917, 18.3234),
    "Magyarbánhegyes": (46.45, 20.9667),
    "Magyarcsanád": (46.167, 20.617),
    "Magyarkeszi": (46.7483, 18.2356),
    "Magyarnándor": (47.9674, 19.3488),
    "Magyarpolány": (47.1702, 17.5465),
    "Majosháza": (47.2624, 19.0015),
    "Maklár": (47.81, 20.41),
    "Makád": (47.0889, 18.9253),
    "Markaz": (47.824, 20.056),
    "Maroslele": (46.267, 20.35),
    "Mecseknádasd": (46.224, 18.4641),
    "Megyaszó": (48.1859, 21.0494),
    "Mende": (47.4294, 19.4576),
    "Mernye": (46.509, 17.8244),
    "Mesztegnyő": (46.4982, 17.4253),
    "Mezőcsokonya": (46.4316, 17.6465),
    "Mezőfalva": (46.9378, 18.7867),
    "Mezőnyárád": (47.8471, 20.6742),
    "Mezőszemere": (47.746, 20.519),
    "Mezőszentgyörgy": (46.992, 18.2772),
    "Mezőszilas": (46.8177, 18.4744),
    "Mezőtárkány": (47.721, 20.476),
    "Mezőzombor": (48.1527, 21.2587),
    "Mihályi": (47.5167, 17.1),
    "Mikepércs": (47.45, 21.6333),
    "Miske": (46.4333, 19.0333),
    "Mocsa": (47.6712, 18.1847),
    "Mogyoród": (47.6, 19.25),
    "Monok": (48.2102, 21.1477),
    "Monorierdő": (47.3085, 19.5),
    "Monostorapáti": (46.9273, 17.5568),
    "Monostorpályi": (47.4, 21.7833),
    "Mosonszentmiklós": (47.7296, 17.4264),
    "Mosonszolnok": (47.85, 17.1667),
    "Murakeresztúr": (46.3524, 16.8777),
    "Murony": (46.7667, 21.05),
    "Mád": (48.1898, 21.271),
    "Mályi": (48.0147, 20.8233),
    "Mány": (47.5328, 18.6546),
    "Máriakálnok": (47.8599, 17.3217),
    "Márkó": (47.122, 17.8132),
    "Mártély": (46.4833, 20.2167),
    "Mátraderecske": (47.95, 20.086),
    "Mátranovák": (48.0375, 19.9839),
    "Mátraszőlős": (47.9594, 19.6889),
    "Mátraterenye": (48.0328, 19.9475),
    "Mátraverebély": (47.9793, 19.7752),
    "Máza": (46.279, 18.4042),
    "Méhkerék": (46.7833, 21.45),
    "Méra": (48.3591, 21.1523),
    "Mérk": (47.7811, 22.3797),
    "Múcsony": (48.2687, 20.6819),
    "Nagybajcs": (47.7616, 17.6873),
    "Nagybaracska": (46.05, 18.9),
    "Nagyberki": (46.3612, 18.0076),
    "Nagyberény": (46.7978, 18.1684),
    "Nagybánhegyes": (46.4667, 20.9),
    "Nagycenk": (47.6, 16.7),
    "Nagycserkesz": (47.9667, 21.5333),
    "Nagydobos": (48.05, 22.3167),
    "Nagydorog": (46.6225, 18.6594),
    "Nagyesztergár": (47.2768, 17.9052),
    "Nagyfüged": (47.6875, 20.1063),
    "Nagyharsány": (45.8469, 18.3977),
    "Nagyhegyes": (47.5398, 21.3445),
    "Nagyigmánd": (47.6325, 18.0557),
    "Nagykamarás": (46.4667, 21.1167),
    "Nagykarácsony": (46.8667, 18.7667),
    "Nagykereki": (47.1872, 21.7877),
    "Nagykovácsi": (47.58, 18.88),
    "Nagykozár": (46.0645, 18.3202),
    "Nagykörű": (47.2764, 20.4472),
    "Nagylóc": (48.0326, 19.5759),
    "Nagymágocs": (46.5833, 20.5),
    "Nagyoroszi": (48.0034, 19.0906),
    "Nagyrábé": (47.2, 21.3333),
    "Nagyrécse": (46.4895, 17.0526),
    "Nagyréde": (47.767, 19.8524),
    "Nagyszentjános": (47.7104, 17.8707),
    "Nagyszénás": (46.6833, 20.6667),
    "Nagysáp": (47.6848, 18.6016),
    "Nagytarcsa": (47.5258, 19.2862),
    "Nagyvarsány": (48.1667, 22.2833),
    "Nagyvenyim": (46.959, 18.859),
    "Nagyvázsony": (46.9835, 17.6941),
    "Napkor": (47.9333, 21.8833),
    "Naszály": (47.6987, 18.2608),
    "Nemesnádudvar": (46.3333, 19.05),
    "Nemesvámos": (47.0537, 17.8721),
    "Neszmély": (47.7329, 18.3451),
    "Noszvaj": (47.937, 20.475),
    "Novaj": (47.858, 20.478),
    "Novajidrány": (48.4031, 21.1706),
    "Nyirád": (47.0034, 17.4505),
    "Nyáregyháza": (47.2616, 19.5089),
    "Nyárlőrinc": (46.8592, 19.8791),
    "Nyársapát": (47.1012, 19.8033),
    "Nyíracsád": (47.6, 21.9833),
    "Nyírbogdány": (48.05, 21.8833),
    "Nyírbogát": (47.8017, 22.0606),
    "Nyírbéltek": (47.6978, 22.1275),
    "Nyírcsaholy": (47.9032, 22.3359),
    "Nyírcsászári": (47.8644, 22.1748),
    "Nyírgyulaj": (47.8833, 22.1),
    "Nyíribrony": (48.0167, 21.9667),
    "Nyírkarász": (48.0964, 22.1007),
    "Nyírkáta": (47.8631, 22.2458),
    "Nyírmeggyes": (47.9133, 22.2639),
    "Nyírmihálydi": (47.7326, 21.9612),
    "Nyírmártonfalva": (47.5833, 21.9),
    "Nyírpazony": (47.9833, 21.8),
    "Nyírtass": (48.1167, 22.0333),
    "Nyírtura": (48.0167, 21.8333),
    "Nyírvasvári": (47.8167, 22.1833),
    "Nyírábrány": (47.55, 22.0333),
    "Nyúl": (47.5813, 17.6807),
    "Nádasd": (46.9667, 16.6167),
    "Nádasdladány": (47.1341, 18.2357),
    "Nárai": (47.2, 16.5667),
    "Németkér": (46.718, 18.765),
    "Nézsa": (47.8458, 19.2934),
    "Nógrád": (47.9048, 19.0516),
    "Nógrádmegyer": (48.0687, 19.624),
    "Nőtincs": (47.8833, 19.15),
    "Okány": (46.9, 21.35),
    "Olaszfalu": (47.2434, 17.9053),
    "Olaszliszka": (48.2426, 21.4302),
    "Onga": (48.119, 20.9074),
    "Orfű": (46.1382, 18.1554),
    "Orgovány": (46.75, 19.4667),
    "Ormosbánya": (48.3364, 20.6484),
    "Ostoros": (47.867, 20.429),
    "Ozora": (46.7529, 18.3985),
    "Palotás": (47.7946, 19.5986),
    "Pap": (48.2167, 22.15),
    "Papkeszi": (47.0806, 18.0805),
    "Parasznya": (48.1697, 20.6415),
    "Parád": (47.9167, 20.0333),
    "Paszab": (48.1473, 21.6669),
    "Pellérd": (46.0333, 18.15),
    "Penc": (47.8, 19.25),
    "Perbál": (47.5913, 18.7599),
    "Pereszteg": (47.5927, 16.7306),
    "Perkáta": (47.048, 18.7834),
    "Petneháza": (48.0667, 22.0833),
    "Petőfibánya": (47.7658, 19.7019),
    "Petőfiszállás": (46.6167, 19.8333),
    "Petőháza": (47.5948, 16.8934),
    "Pilisborosjenő": (47.606, 18.9919),
    "Piliscsaba": (47.6336, 18.8269),
    "Piliscsév": (47.6771, 18.8149),
    "Pilisjászfalu": (47.6547, 18.7953),
    "Pilismarót": (47.784, 18.8755),
    "Pilisszentiván": (47.6167, 18.9),
    "Pilisszentkereszt": (47.7, 18.9),
    "Pilisszentlászló": (47.7167, 18.9887),
    "Pilisszántó": (47.6667, 18.9),
    "Pincehely": (46.6839, 18.4387),
    "Piricse": (47.7667, 22.15),
    "Pitvaros": (46.317, 20.733),
    "Pocsaj": (47.2833, 21.8167),
    "Pogány": (45.9821, 18.2562),
    "Porcsalma": (47.8825, 22.5683),
    "Poroszló": (47.647, 20.652),
    "Prügy": (48.0817, 21.2431),
    "Pusztadobos": (48.0667, 22.2333),
    "Pusztaföldvár": (46.5333, 20.8),
    "Pusztamonostor": (47.55, 19.8),
    "Pusztaszer": (46.55, 19.9833),
    "Pusztavacs": (47.1714, 19.5011),
    "Pusztavám": (47.4328, 18.2327),
    "Pusztazámor": (47.4029, 18.7826),
    "Pácin": (48.3309, 21.8309),
    "Páhi": (46.7167, 19.4),
    "Pákozd": (47.2211, 18.5449),
    "Pálfa": (46.712, 18.613),
    "Pálmonostora": (46.6333, 19.95),
    "Pánd": (47.35, 19.6333),
    "Pápateszér": (47.3823, 17.7019),
    "Pátka": (47.2763, 18.4948),
    "Pátroha": (48.1667, 22.0),
    "Páty": (47.5167, 18.8333),
    "Pázmánd": (47.2874, 18.6543),
    "Pázmándfalu": (47.5705, 17.7813),
    "Pély": (47.494, 20.343),
    "Pér": (47.6131, 17.8011),
    "Péteri": (47.3833, 19.4167),
    "Pétfürdő": (47.1587, 18.1237),
    "Pócsmegyer": (47.7167, 19.1),
    "Pócspetri": (47.8833, 22.0),
    "Püspökhatvan": (47.7667, 19.3667),
    "Rajka": (47.9969, 17.1983),
    "Ramocsaháza": (48.0416, 21.988),
    "Ravazd": (47.5173, 17.7512),
    "Recsk": (47.934, 20.111),
    "Remeteszőlős": (47.5601, 18.9189),
    "Rezi": (46.8333, 17.2167),
    "Ricse": (48.3258, 21.9689),
    "Rimóc": (48.0379, 19.5298),
    "Rohod": (48.0333, 22.1333),
    "Romhány": (47.9255, 19.2582),
    "Rum": (47.1302, 16.8445),
    "Ruzsa": (46.2833, 19.75),
    "Rábapatona": (47.6333, 17.4833),
    "Rábapaty": (47.3, 16.9333),
    "Ráckeresztúr": (47.2738, 18.8348),
    "Rád": (47.7923, 19.2177),
    "Rákócziújfalu": (47.0598, 20.2614),
    "Réde": (47.4306, 17.9186),
    "Rém": (46.25, 19.15),
    "Rétközberencs": (48.2, 22.0167),
    "Révfülöp": (46.8326, 17.6277),
    "Rózsaszentmárton": (47.784, 19.741),
    "Röszke": (46.1833, 20.05),
    "Sajóhídvég": (48.0042, 20.9505),
    "Sajókaza": (48.2833, 20.5828),
    "Sajókeresztúr": (48.1696, 20.7761),
    "Sajólád": (48.0436, 20.903),
    "Sajópetri": (48.0342, 20.8895),
    "Sajószöged": (47.9459, 20.9955),
    "Sajóvámos": (48.1816, 20.8316),
    "Sajóörös": (47.9532, 21.0198),
    "Sarkadkeresztúr": (46.8, 21.3833),
    "Sarród": (47.6317, 16.8604),
    "Segesd": (46.3434, 17.3475),
    "Seregélyes": (47.1109, 18.5773),
    "Sirok": (47.9311, 20.1969),
    "Sióagárd": (46.3893, 18.6498),
    "Sokorópátka": (47.4833, 17.7),
    "Soltszentimre": (46.7667, 19.2833),
    "Solymár": (47.591, 18.929),
    "Somberek": (46.0811, 18.6623),
    "Somogyjád": (46.4892, 17.7119),
    "Somogyszob": (46.2928, 17.2958),
    "Somogysárd": (46.4136, 17.5968),
    "Somogyvár": (46.5856, 17.6501),
    "Somoskőújfalu": (48.1613, 19.8211),
    "Soponya": (47.0132, 18.4555),
    "Sopronkövesd": (47.55, 16.75),
    "Sukoró": (47.2409, 18.601),
    "Szabadbattyán": (47.118, 18.3629),
    "Szabadegyháza": (47.0784, 18.6923),
    "Szabadkígyós": (46.6167, 21.1),
    "Szabolcsbáka": (48.15, 22.15),
    "Szabolcsveresmart": (48.3, 22.0167),
    "Szada": (47.6333, 19.3167),
    "Szajol": (47.1833, 20.3),
    "Szakmár": (46.55, 19.0667),
    "Szakoly": (47.7667, 21.9167),
    "Szakály": (46.5244, 18.3797),
    "Szalaszend": (48.386, 21.1231),
    "Szalkszentmárton": (46.9667, 19.0167),
    "Szalánta": (45.95, 18.2333),
    "Szamosszeg": (48.05, 22.3667),
    "Szank": (46.554, 19.667),
    "Szany": (47.4611, 17.3011),
    "Szatmárcseke": (48.0859, 22.631),
    "Szatymaz": (46.35, 20.05),
    "Szederkény": (46.0, 18.45),
    "Szedres": (46.4789, 18.6849),
    "Szegvár": (46.5833, 20.2333),
    "Szendehely": (47.8558, 19.103),
    "Szendrőlád": (48.3405, 20.7458),
    "Szentgál": (47.1138, 17.7341),
    "Szentistván": (47.773, 20.6575),
    "Szentkirály": (46.9178, 19.9189),
    "Szentkirályszabadja": (47.0572, 17.971),
    "Szentlőrinckáta": (47.5167, 19.75),
    "Szentmártonkáta": (47.45, 19.7),
    "Szepetnek": (46.4331, 16.8979),
    "Szeremle": (46.15, 18.8833),
    "Szerep": (47.2333, 21.15),
    "Szigetbecse": (47.1333, 18.95),
    "Szigetcsép": (47.2667, 18.9833),
    "Szigetmonostor": (47.6833, 19.1167),
    "Szigetszentmárton": (47.2333, 18.9667),
    "Szigetújfalu": (47.235, 18.9231),
    "Szihalom": (47.772, 20.483),
    "Szil": (47.5014, 17.233),
    "Szilvásvárad": (48.104, 20.387),
    "Szirmabesenyő": (48.1496, 20.7958),
    "Szirák": (47.8314, 19.5319),
    "Szokolya": (47.8673, 19.0079),
    "Szomolya": (47.8919, 20.4964),
    "Szomor": (47.593, 18.6633),
    "Szomód": (47.6825, 18.3418),
    "Szurdokpüspöki": (47.852, 19.6958),
    "Szákszend": (47.5571, 18.1631),
    "Szár": (47.4775, 18.5161),
    "Szárföld": (47.5945, 17.1204),
    "Szárliget": (47.5174, 18.4951),
    "Szászvár": (46.2667, 18.3667),
    "Székely": (48.0586, 21.9337),
    "Székkutas": (46.5, 20.5333),
    "Szügy": (48.0361, 19.3209),
    "Sződ": (47.7167, 19.2),
    "Sződliget": (47.7333, 19.15),
    "Szőlősgyörök": (46.711, 17.6747),
    "Szűcsi": (47.802, 19.762),
    "Ságvár": (46.8371, 18.102),
    "Sály": (47.9481, 20.6643),
    "Sárisáp": (47.6747, 18.681),
    "Sárkeresztes": (47.2516, 18.3531),
    "Sárkeresztúr": (47.0024, 18.5477),
    "Sármellék": (46.7119, 17.1693),
    "Sárosd": (47.0415, 18.6489),
    "Sárrétudvari": (47.2333, 21.2),
    "Sárszentmihály": (47.1555, 18.3232),
    "Sárszentágota": (46.9705, 18.5616),
    "Sáránd": (47.4, 21.6333),
    "Sáta": (48.1844, 20.3931),
    "Sé": (47.2429, 16.5561),
    "Sényő": (48.0, 21.8833),
    "Sóskút": (47.4063, 18.8285),
    "Söjtör": (46.6727, 16.8537),
    "Súr": (47.3708, 18.0288),
    "Sükösd": (46.2833, 19.0),
    "Sülysáp": (47.452, 19.534),
    "Süttő": (47.7567, 18.4414),
    "Tahitótfalu": (47.7522, 19.0781),
    "Taksony": (47.3319, 19.0631),
    "Taktaharkány": (48.091, 21.128),
    "Taktakenéz": (48.0504, 21.2174),
    "Taktaszada": (48.1102, 21.1688),
    "Tar": (47.9505, 19.7413),
    "Tarany": (46.1823, 17.309),
    "Tarcal": (48.1313, 21.3427),
    "Tard": (47.8786, 20.5988),
    "Tardos": (47.6618, 18.4436),
    "Tarján": (47.6106, 18.5108),
    "Tarnalelesz": (48.05, 20.183),
    "Tarnaméra": (47.656, 20.158),
    "Tarnazsadány": (47.674, 20.155),
    "Tarnaörs": (47.598, 20.056),
    "Tarpa": (48.1039, 22.5272),
    "Tass": (47.019, 19.029),
    "Taszár": (46.3762, 17.9058),
    "Tataháza": (46.1728, 19.3),
    "Tatárszentgyörgy": (47.0814, 19.3697),
    "Telekgerendás": (46.65, 20.9667),
    "Telki": (47.55, 18.8333),
    "Tengelic": (46.5289, 18.7093),
    "Tenk": (47.656, 20.341),
    "Teskánd": (46.8556, 16.7805),
    "Tetétlen": (47.3161, 21.3052),
    "Tevel": (46.4115, 18.4555),
    "Tibolddaróc": (47.9213, 20.637),
    "Tihany": (46.9089, 17.8792),
    "Timár": (48.1667, 21.4667),
    "Tinnye": (47.6167, 18.7833),
    "Tiszaalpár": (46.8167, 19.9833),
    "Tiszabecs": (48.0987, 22.8222),
    "Tiszabercel": (48.15, 21.65),
    "Tiszabezdéd": (48.3667, 22.15),
    "Tiszabura": (47.45, 20.4667),
    "Tiszabő": (47.3, 20.4833),
    "Tiszadada": (48.0333, 21.25),
    "Tiszadob": (48.0044, 21.1711),
    "Tiszaeszlár": (48.05, 21.4667),
    "Tiszajenő": (47.0333, 20.15),
    "Tiszakanyár": (48.25, 21.9667),
    "Tiszakarád": (48.2057, 21.7281),
    "Tiszakeszi": (47.7881, 20.9929),
    "Tiszakürt": (46.8833, 20.1167),
    "Tiszalúc": (48.0367, 21.0629),
    "Tiszanagyfalu": (48.092, 21.472),
    "Tiszanána": (47.561, 20.523),
    "Tiszapalkonya": (47.8852, 21.0552),
    "Tiszapüspöki": (47.2167, 20.3167),
    "Tiszaroff": (47.4, 20.45),
    "Tiszaszentimre": (47.4833, 20.7333),
    "Tiszaszentmárton": (48.3833, 22.2333),
    "Tiszasziget": (46.1667, 20.1667),
    "Tiszaszőlős": (47.5591, 20.7195),
    "Tiszasüly": (47.388, 20.388),
    "Tiszatarján": (47.8333, 21.0002),
    "Tiszatelek": (48.2, 21.8),
    "Tiszatenyő": (47.1333, 20.3833),
    "Tiszavárkony": (47.0667, 20.1833),
    "Tiszaörs": (47.5167, 20.8333),
    "Tokod": (47.7227, 18.6579),
    "Tokodaltáró": (47.7326, 18.689),
    "Tolcsva": (48.2839, 21.4462),
    "Tordas": (47.3437, 18.7476),
    "Tornyospálca": (48.2667, 22.1833),
    "Torony": (47.2363, 16.5371),
    "Tunyogmatolcs": (47.9713, 22.4555),
    "Tuzsér": (48.3358, 22.1203),
    "Tyukod": (47.8522, 22.5506),
    "Táborfalva": (47.0833, 19.4667),
    "Tác": (47.0793, 18.4051),
    "Tállya": (48.2347, 21.2261),
    "Tápióbicske": (47.3625, 19.6872),
    "Tápiógyörgye": (47.3333, 19.95),
    "Tápiószecső": (47.45, 19.6),
    "Tápiószentmárton": (47.3404, 19.7438),
    "Tápiószőlős": (47.2855, 19.8536),
    "Tápióság": (47.4035, 19.6231),
    "Táplánszentkereszt": (47.1973, 16.6997),
    "Tárkány": (47.5913, 18.0037),
    "Tárnok": (47.3597, 18.8586),
    "Tázlár": (46.55, 19.5167),
    "Tényő": (47.5333, 17.65),
    "Tépe": (47.3167, 21.5833),
    "Tóalmás": (47.5167, 19.6667),
    "Tószeg": (47.1, 20.15),
    "Tótszerdahely": (46.4, 16.8),
    "Tótvázsony": (47.0091, 17.7876),
    "Tök": (47.5667, 18.7333),
    "Töltéstava": (47.6167, 17.7333),
    "Tömörkény": (46.6167, 20.0333),
    "Törtel": (47.1167, 19.9333),
    "Türje": (46.9819, 17.1053),
    "Ugod": (47.3235, 17.5994),
    "Vajdácska": (48.322, 21.654),
    "Vajszló": (45.8633, 17.9761),
    "Valkó": (47.5665, 19.492),
    "Vanyarc": (47.8212, 19.4523),
    "Varbó": (48.1614, 20.6181),
    "Varsány": (48.0416, 19.4889),
    "Vasad": (47.3201, 19.4087),
    "Vaskút": (46.1133, 18.9867),
    "Vasmegyer": (48.1167, 21.8167),
    "Vasszécseny": (47.1828, 16.7661),
    "Vaszar": (47.4032, 17.5161),
    "Verseg": (47.7227, 19.5506),
    "Verőce": (47.8333, 19.0333),
    "Veszkény": (47.6, 17.0833),
    "Vilmány": (48.4158, 21.2312),
    "Visonta": (47.78, 20.03),
    "Visznek": (47.642, 20.027),
    "Vitnyéd": (47.5882, 16.9858),
    "Vizslás": (48.0492, 19.8202),
    "Vonyarcvashegy": (46.7619, 17.3162),
    "Vácduka": (47.7466, 19.2099),
    "Váchartyán": (47.7247, 19.2596),
    "Vácrátót": (47.7109, 19.2353),
    "Vácszentlászló": (47.5731, 19.5353),
    "Vál": (47.3637, 18.6753),
    "Vámosgyörk": (47.685, 19.925),
    "Vámosmikola": (47.9764, 18.7833),
    "Vámosszabadi": (47.7566, 17.6473),
    "Várdomb": (46.2487, 18.687),
    "Városföld": (46.8167, 19.7667),
    "Városlőd": (47.1437, 17.6503),
    "Végegyháza": (46.3833, 20.8667),
    "Véménd": (46.15, 18.6167),
    "Vértesacsa": (47.3689, 18.5789),
    "Vértessomló": (47.5097, 18.367),
    "Vértesszőlős": (47.6188, 18.3803),
    "Zagyvarékas": (47.2667, 20.1333),
    "Zagyvaszántó": (47.773, 19.668),
    "Zalaapáti": (46.7333, 17.1167),
    "Zalacsány": (46.8075, 17.0959),
    "Zalahaláp": (46.9147, 17.4571),
    "Zalakomár": (46.5366, 17.1812),
    "Zalaszentiván": (46.8922, 16.8995),
    "Zebegény": (47.7976, 18.91),
    "Zomba": (46.4117, 18.5648),
    "Zsadány": (46.9167, 21.5),
    "Zsombó": (46.3333, 19.9833),
    "Zsáka": (47.1333, 21.4333),
    "Zsámbok": (47.5428, 19.6071),
    "Zákány": (46.2517, 16.9492),
    "Zákányszék": (46.2667, 19.9167),
    "Zámoly": (47.3177, 18.4076),
    "Ádánd": (46.8573, 18.1534),
    "Ágasegyháza": (46.8419, 19.4475),
    "Ágfalva": (47.689, 16.5169),
    "Álmosd": (47.4167, 21.9833),
    "Áporka": (47.2332, 19.0119),
    "Ároktő": (47.7308, 20.9423),
    "Ásotthalom": (46.2, 19.7833),
    "Ásványráró": (47.8313, 17.4968),
    "Ászár": (47.5095, 18.0031),
    "Átány": (47.6161, 20.3636),
    "Écs": (47.558, 17.7053),
    "Érpatak": (47.8, 21.7667),
    "Érsekcsanád": (46.25, 18.9833),
    "Érsekvadkert": (47.9966, 19.199),
    "Ófehértó": (47.9368, 22.036),
    "Ónod": (47.9997, 20.9143),
    "Ópusztaszer": (46.4833, 20.0833),
    "Ópályi": (48.0, 22.3167),
    "Öcsöd": (46.8997, 20.3916),
    "Ököritófülpös": (47.9181, 22.5094),
    "Öreglak": (46.6052, 17.6251),
    "Öskü": (47.1608, 18.072),
    "Öttevény": (47.7255, 17.4891),
    "Újhartyán": (47.2192, 19.3908),
    "Újlengyel": (47.2286, 19.4522),
    "Újléta": (47.4667, 21.8833),
    "Újszentiván": (46.1833, 20.1833),
    "Újszentmargita": (47.7333, 21.1),
    "Újszilvás": (47.2782, 19.9239),
    "Úrhida": (47.1306, 18.3319),
    "Úri": (47.4164, 19.5178),
    "Úrkút": (47.0819, 17.6446),
    "Üllés": (46.3333, 19.85),
    "Üröm": (47.6, 19.0167),
    "Őcsény": (46.3126, 18.7579),
    "Őr": (47.9833, 22.2),
    "Őrbottyán": (47.6872, 19.2832),
    "Őrhalom": (48.0761, 19.4042),
    "Ősi": (47.142, 18.1884),
    # Hungary
    "Aba": (47.10, 18.53), "Abaújszántó": (48.30, 21.21), "Abony": (47.18, 20.01),
    "Abádszalók": (47.47, 20.60), "Adony": (47.12, 18.87), "Ajak": (48.09, 21.89),
    "Ajka": (47.10, 17.55), "Albertirsa": (47.24, 19.60), "Alsózsolca": (48.09, 20.92),
    "Aszód": (47.65, 19.49), "Badacsonytomaj": (46.80, 17.48), "Baja": (46.18, 18.96),
    "Baktalórántháza": (47.98, 21.99), "Balassagyarmat": (48.08, 19.30),
    "Balatonalmádi": (47.03, 18.02), "Balatonboglár": (46.77, 17.65),
    "Balatonföldvár": (46.85, 17.90), "Balatonfüred": (46.96, 17.89),
    "Balatonfűzfő": (47.06, 18.04), "Balatonkenese": (47.05, 18.12),
    "Balatonlelle": (46.79, 17.70), "Balkány": (47.77, 21.87),
    "Balmazújváros": (47.62, 21.35), "Barcs": (45.96, 17.46), "Battonya": (46.28, 21.01),
    "Beled": (47.48, 17.06), "Berettyóújfalu": (47.22, 21.55), "Berhida": (47.11, 18.17),
    "Besenyszög": (47.20, 20.25), "Biatorbágy": (47.46, 18.82), "Bicske": (47.49, 18.64),
    "Biharkeresztes": (47.13, 21.72), "Bodajk": (47.32, 18.26), "Bonyhád": (46.30, 18.53),
    "Borsodnádasd": (48.12, 20.10), "Budakalász": (47.62, 19.04), "Budakeszi": (47.51, 18.91),
    "Budapest": (47.50, 19.04), "Budaörs": (47.45, 18.97),
    "Bábolna": (47.68, 18.04), "Bácsalmás": (46.12, 19.33), "Bátaszék": (46.21, 18.72),
    "Bátonyterenye": (47.96, 19.82), "Békés": (46.77, 21.13), "Békéscsaba": (46.68, 21.09),
    "Bélapátfalva": (47.99, 20.26), "Bóly": (45.94, 18.52), "Bük": (47.38, 16.75),
    "Cegléd": (47.17, 19.80), "Celldömölk": (47.26, 17.15), "Cigánd": (48.34, 21.82),
    "Csanádpalota": (46.25, 20.73), "Csenger": (47.84, 22.69), "Csepreg": (47.39, 16.70),
    "Csongrád": (46.71, 20.14), "Csorna": (47.61, 17.26), "Csorvás": (46.62, 20.83),
    "Csurgó": (46.25, 17.10), "Csákvár": (47.39, 18.47), "Dabas": (47.18, 19.32),
    "Debrecen": (47.53, 21.63), "Demecser": (47.92, 21.97), "Derecske": (47.36, 21.57),
    "Devecser": (47.10, 17.44), "Dombrád": (48.23, 21.86), "Dombóvár": (46.38, 18.13),
    "Dorog": (47.72, 18.73), "Dunaföldvár": (46.80, 18.92), "Dunaharaszti": (47.35, 19.09),
    "Dunakeszi": (47.63, 19.14), "Dunavarsány": (47.29, 18.98), "Dunavecse": (46.91, 18.97),
    "Dunaújváros": (46.98, 18.93), "Dévaványa": (47.02, 20.96), "Edelény": (48.30, 20.73),
    "Eger": (47.90, 20.37), "Elek": (46.53, 21.26), "Emőd": (47.95, 20.84),
    "Encs": (48.33, 21.12), "Enying": (46.93, 18.24), "Ercsi": (47.25, 18.88),
    "Esztergom": (47.79, 18.74), "Fegyvernek": (47.26, 20.52), "Fehérgyarmat": (47.99, 22.52),
    "Felsőzsolca": (48.11, 20.88), "Fertőd": (47.63, 16.86), "Fertőszentmiklós": (47.58, 16.85),
    "Fonyód": (46.75, 17.56), "Fót": (47.61, 19.20), "Füzesabony": (47.75, 20.42),
    "Füzesgyarmat": (46.98, 21.23), "Gyomaendrőd": (46.93, 20.83), "Gyula": (46.65, 21.28),
    "Gyál": (47.38, 19.24), "Gyömrő": (47.42, 19.40), "Gyöngyös": (47.78, 19.93),
    "Gyöngyöspata": (47.82, 19.85), "Gyönk": (46.47, 18.49), "Győr": (47.68, 17.63),
    "Gárdony": (47.20, 18.61), "Göd": (47.69, 19.13), "Gödöllő": (47.60, 19.36),
    "Gönc": (48.47, 21.26), "Hajdúböszörmény": (47.67, 21.52), "Hajdúdorog": (47.82, 21.66),
    "Hajdúhadház": (47.68, 21.66), "Hajdúnánás": (47.85, 21.43),
    "Hajdúszoboszló": (47.45, 21.40), "Hajdúsámson": (47.58, 21.77), "Hajós": (46.40, 19.10),
    "Halásztelek": (47.35, 18.99), "Harkány": (45.86, 18.23), "Hatvan": (47.67, 19.67),
    "Herend": (47.14, 17.75), "Heves": (47.60, 20.28), "Hévíz": (46.79, 17.19),
    "Hódmezővásárhely": (46.42, 20.33), "Ibrány": (48.13, 21.72), "Igal": (46.38, 17.79),
    "Isaszeg": (47.54, 19.46), "Izsák": (46.78, 19.35), "Jánoshalma": (46.30, 19.32),
    "Jánosháza": (47.12, 17.16), "Jánossomorja": (47.78, 17.14), "Jászapáti": (47.51, 20.15),
    "Jászberény": (47.50, 19.92), "Jászfényszaru": (47.55, 19.71), "Jászkisér": (47.44, 20.21),
    "Jászárokszállás": (47.63, 19.99), "Kaba": (47.36, 21.26), "Kadarkút": (46.24, 17.63),
    "Kalocsa": (46.53, 18.99), "Kaposvár": (46.36, 17.80), "Kapuvár": (47.60, 17.03),
    "Karcag": (47.32, 20.93), "Kazincbarcika": (48.25, 20.65), "Kecel": (46.52, 19.26),
    "Kecskemét": (46.91, 19.69), "Kemecse": (47.92, 21.77), "Kenderes": (47.25, 20.71),
    "Kerekegyháza": (46.97, 19.47), "Kerepes": (47.60, 19.29), "Keszthely": (46.77, 17.24),
    "Kisbér": (47.51, 18.03), "Kiskunfélegyháza": (46.71, 19.84),
    "Kiskunhalas": (46.43, 19.49), "Kiskunmajsa": (46.49, 19.74), "Kisköre": (47.52, 20.51),
    "Kiskőrös": (46.62, 19.29), "Kistarcsa": (47.55, 19.27), "Kistelek": (46.47, 20.01),
    "Kisvárda": (48.22, 22.08), "Kisújszállás": (47.20, 20.76), "Komló": (46.20, 18.26),
    "Komádi": (47.00, 21.51), "Komárom": (47.74, 18.12), "Kozármisleny": (46.02, 18.25),
    "Kunhegyes": (47.37, 20.64), "Kunszentmiklós": (46.93, 19.12),
    "Kunszentmárton": (46.84, 20.28), "Körmend": (47.01, 16.60),
    "Körösladány": (46.93, 21.17), "Kőszeg": (47.39, 16.54), "Lajosmizse": (46.98, 19.57),
    "Lengyeltóti": (46.67, 17.72), "Lenti": (46.63, 16.54), "Letenye": (46.43, 16.71),
    "Lábatlan": (47.74, 18.61), "Lébény": (47.72, 17.38), "Létavértes": (47.38, 21.91),
    "Lőrinci": (47.74, 19.68), "Maglód": (47.45, 19.38), "Makó": (46.22, 20.48),
    "Marcali": (46.58, 17.41), "Martfű": (47.01, 20.29), "Martonvásár": (47.32, 18.79),
    "Medgyesegyháza": (46.51, 21.05), "Mezőberény": (46.82, 21.02),
    "Mezőcsát": (47.83, 20.91), "Mezőhegyes": (46.32, 20.83),
    "Mezőkeresztes": (47.83, 20.70), "Mezőkovácsháza": (46.42, 20.91),
    "Mezőkövesd": (47.82, 20.57), "Mezőtúr": (47.00, 20.62), "Mindszent": (46.52, 20.18),
    "Miskolc": (48.10, 20.78), "Mohács": (45.99, 18.68), "Monor": (47.35, 19.45),
    "Mosonmagyaróvár": (47.87, 17.27), "Mágocs": (46.34, 18.22), "Mándok": (48.33, 22.11),
    "Máriapócs": (47.83, 22.04), "Mátészalka": (47.95, 22.32), "Mélykút": (46.19, 19.39),
    "Mór": (47.38, 18.21), "Mórahalom": (46.22, 19.89), "Nagyatád": (46.23, 17.36),
    "Nagybajom": (46.39, 17.52), "Nagyecsed": (47.88, 22.40), "Nagyhalász": (48.08, 21.76),
    "Nagykanizsa": (46.45, 16.99), "Nagykálló": (47.87, 22.01), "Nagykáta": (47.41, 19.74),
    "Nagykőrös": (47.04, 19.78), "Nagymaros": (47.78, 18.96), "Nagymányok": (46.28, 18.36),
    "Nyergesújfalu": (47.76, 18.55), "Nyékládháza": (48.07, 20.93),
    "Nyíradony": (47.69, 21.92), "Nyírbátor": (47.83, 22.12), "Nyíregyháza": (47.95, 21.72),
    "Nyírlugos": (47.71, 22.07), "Nyírmada": (48.04, 22.22), "Nyírtelek": (47.98, 21.64),
    "Nádudvar": (47.42, 21.17), "Orosháza": (46.57, 20.67), "Oroszlány": (47.49, 18.31),
    "Pacsa": (46.70, 17.02), "Paks": (46.63, 18.86), "Pannonhalma": (47.55, 17.76),
    "Pilis": (47.28, 19.55), "Pilisvörösvár": (47.62, 18.91), "Polgár": (47.87, 21.11),
    "Polgárdi": (47.08, 18.29), "Pomáz": (47.64, 19.02), "Pusztaszabolcs": (47.14, 18.77),
    "Putnok": (48.30, 20.43), "Pálháza": (48.50, 21.41), "Pápa": (47.33, 17.47),
    "Pásztó": (47.92, 19.70), "Pécel": (47.49, 19.38), "Pécs": (46.07, 18.23),
    "Pécsvárad": (46.16, 18.42), "Pétervására": (47.92, 20.08),
    "Püspökladány": (47.32, 21.12), "Rakamaz": (48.04, 21.47), "Rudabánya": (48.38, 20.63),
    "Rácalmás": (47.10, 18.93), "Ráckeve": (47.16, 19.00), "Rákóczifalva": (47.09, 20.11),
    "Répcelak": (47.43, 17.00), "Rétság": (47.93, 19.11), "Sajóbábony": (48.10, 20.94),
    "Sajószentpéter": (48.22, 20.73), "Salgótarján": (48.10, 19.80), "Sarkad": (46.75, 21.39),
    "Sellye": (45.87, 17.85), "Siklós": (45.86, 18.30), "Simontornya": (46.75, 18.56),
    "Siófok": (46.91, 18.05), "Solt": (46.79, 18.97), "Soltvadkert": (46.58, 19.30),
    "Sopron": (47.68, 16.59), "Szabadszállás": (46.87, 19.22), "Szarvas": (46.87, 20.56),
    "Szeged": (46.25, 20.15), "Szeghalom": (47.02, 21.17), "Szekszárd": (46.35, 18.71),
    "Szendrő": (48.41, 20.74), "Szentendre": (47.67, 19.07), "Szentes": (46.65, 20.26),
    "Szentgotthárd": (46.95, 16.28), "Szentlőrinc": (46.05, 17.99),
    "Szerencs": (48.16, 21.20), "Szigethalom": (47.31, 18.98),
    "Szigetszentmiklós": (47.34, 19.05), "Szigetvár": (46.04, 17.80),
    "Szikszó": (48.21, 20.94), "Szob": (47.81, 18.87), "Szolnok": (47.17, 20.19),
    "Szombathely": (47.23, 16.62), "Százhalombatta": (47.32, 18.91),
    "Szécsény": (48.08, 19.52), "Székesfehérvár": (47.19, 18.41),
    "Sándorfalva": (46.35, 20.05), "Sárbogárd": (46.89, 18.62),
    "Sárospatak": (48.32, 21.57), "Sárvár": (47.25, 16.94), "Sásd": (46.25, 18.10),
    "Sátoraljaújhely": (48.40, 21.66), "Sümeg": (46.98, 17.28), "Tab": (46.74, 18.04),
    "Tamási": (46.63, 18.28), "Tapolca": (46.88, 17.44), "Tata": (47.65, 18.33),
    "Tatabánya": (47.57, 18.40), "Tiszacsege": (47.71, 21.05),
    "Tiszaföldvár": (47.02, 20.26), "Tiszafüred": (47.62, 20.76),
    "Tiszakécske": (46.93, 20.10), "Tiszalök": (47.97, 21.35),
    "Tiszavasvári": (47.96, 21.56), "Tiszaújváros": (47.92, 21.05),
    "Tokaj": (48.12, 21.41), "Tolna": (46.43, 18.79), "Tompa": (46.19, 19.53),
    "Tura": (47.60, 19.61), "Tápiószele": (47.37, 19.89), "Tát": (47.77, 18.67),
    "Téglás": (47.72, 21.68), "Tét": (47.50, 17.52), "Tótkomlós": (46.42, 20.75),
    "Tököl": (47.32, 18.97), "Törökbálint": (47.43, 18.89),
    "Törökszentmiklós": (47.18, 20.41), "Túrkeve": (47.10, 20.74), "Vaja": (47.95, 22.11),
    "Vasvár": (47.06, 16.80), "Vecsés": (47.41, 19.27), "Velence": (47.24, 18.65),
    "Veresegyház": (47.67, 19.30), "Verpelét": (47.86, 20.21), "Veszprém": (47.09, 17.91),
    "Villány": (45.87, 18.46), "Visegrád": (47.79, 18.97), "Vác": (47.78, 19.13),
    "Vámospércs": (47.54, 21.93), "Várpalota": (47.20, 18.14),
    "Vásárosnamény": (48.12, 22.32), "Vép": (47.30, 16.77), "Vésztő": (46.92, 21.26),
    "Zalaegerszeg": (46.84, 16.84), "Zalakaros": (46.55, 17.14),
    "Zalalövő": (46.85, 16.57), "Zalaszentgrót": (46.93, 17.08), "Zamárdi": (46.88, 17.96),
    "Zirc": (47.26, 17.87), "Zsámbék": (47.54, 18.72), "Záhony": (48.40, 22.18),
    "Ács": (47.71, 18.00), "Érd": (47.39, 18.90), "Ócsa": (47.29, 19.22),
    "Ózd": (48.22, 20.30), "Örkény": (47.13, 19.36), "Újfehértó": (47.81, 21.68),
    "Újkígyós": (46.60, 21.08), "Újszász": (47.29, 20.12), "Üllő": (47.38, 19.33),
    "Őriszentpéter": (46.87, 16.42),
    # Austria
    "Vienna": (48.21, 16.37), "Graz": (47.07, 15.44), "Salzburg": (47.80, 13.04),
    # Germany
    "Berlin": (52.52, 13.40), "Munich": (48.14, 11.58), "Hamburg": (53.55, 10.00),
    "Frankfurt": (50.11, 8.68), "Cologne": (50.94, 6.96), "Düsseldorf": (51.23, 6.78),
    "Stuttgart": (48.78, 9.18), "Leipzig": (51.34, 12.38), "Nürnberg": (49.45, 11.08),
    "Dresden": (51.05, 13.74), "Hannover": (52.37, 9.74),
    # Switzerland
    "Zurich": (47.38, 8.54), "Bern": (46.95, 7.45), "Geneva": (46.20, 6.15),
    # UK
    "London": (51.51, -0.13), "Manchester": (53.48, -2.24), "Birmingham": (52.48, -1.90),
    "Edinburgh": (55.95, -3.19), "Bristol": (51.45, -2.59),
    # Ireland
    "Dublin": (53.33, -6.25),
    # USA
    "New York": (40.71, -74.01), "Los Angeles": (34.05, -118.24), "Chicago": (41.88, -87.63),
    "San Francisco": (37.77, -122.42), "Seattle": (47.61, -122.33), "Boston": (42.36, -71.06),
    "Austin": (30.27, -97.74), "Portland": (45.52, -122.68), "Denver": (39.74, -104.98),
    "Miami": (25.77, -80.19), "Atlanta": (33.75, -84.39), "Minneapolis": (44.98, -93.27),
    "Philadelphia": (39.95, -75.16), "Detroit": (42.33, -83.05),
    # Canada
    "Toronto": (43.65, -79.38), "Vancouver": (49.25, -123.12), "Montreal": (45.51, -73.55),
    "Calgary": (51.04, -114.07), "Ottawa": (45.42, -75.70),
    # Australia
    "Sydney": (-33.87, 151.21), "Melbourne": (-37.81, 144.96), "Brisbane": (-27.47, 153.02),
    "Perth": (-31.95, 115.86), "Adelaide": (-34.93, 138.60),
    # New Zealand
    "Auckland": (-36.85, 174.76), "Wellington": (-41.29, 174.78),
    # France
    "Paris": (48.86, 2.35), "Lyon": (45.75, 4.83), "Marseille": (43.30, 5.37),
    "Toulouse": (43.60, 1.44), "Nice": (43.71, 7.26), "Bordeaux": (44.84, -0.58),
    "Strasbourg": (48.58, 7.75), "Nantes": (47.22, -1.55),
    # Belgium
    "Brussels": (50.85, 4.35), "Antwerp": (51.22, 4.40),
    # Netherlands
    "Amsterdam": (52.37, 4.90), "Rotterdam": (51.92, 4.47), "The Hague": (52.08, 4.31),
    # Spain
    "Madrid": (40.42, -3.70), "Barcelona": (41.39, 2.17), "Seville": (37.39, -5.99),
    "Valencia": (39.47, -0.38), "Bilbao": (43.26, -2.93), "Zaragoza": (41.65, -0.88),
    # Portugal
    "Lisbon": (38.72, -9.14), "Porto": (41.16, -8.63),
    # Italy
    "Rome": (41.90, 12.50), "Milan": (45.47, 9.19), "Florence": (43.77, 11.25),
    "Turin": (45.07, 7.69), "Naples": (40.85, 14.27), "Bologna": (44.49, 11.34),
    # Poland
    "Warsaw": (52.23, 21.01), "Krakow": (50.06, 19.94), "Wroclaw": (51.11, 17.04),
    "Gdansk": (54.35, 18.65), "Poznan": (52.41, 16.93),
    # Czech Republic
    "Prague": (50.08, 14.44), "Brno": (49.19, 16.61),
    # Slovakia
    "Bratislava": (48.15, 17.11),
    # Hungary → Slovenia
    "Ljubljana": (46.05, 14.51),
    # Romania
    "Bucharest": (44.43, 26.10), "Cluj-Napoca": (46.77, 23.59),
    # Serbia
    "Belgrade": (44.82, 20.46),
    # Croatia
    "Zagreb": (45.81, 15.98),
    # Bulgaria
    "Sofia": (42.70, 23.32),
    # Ukraine
    "Kyiv": (50.45, 30.52),
    # Baltic
    "Riga": (56.95, 24.11), "Tallinn": (59.44, 24.75), "Vilnius": (54.69, 25.28),
    # Greece
    "Athens": (37.98, 23.73), "Thessaloniki": (40.64, 22.94),
    # Scandinavia
    "Copenhagen": (55.68, 12.57), "Stockholm": (59.33, 18.07), "Oslo": (59.91, 10.75),
    "Helsinki": (60.17, 24.94), "Gothenburg": (57.71, 11.97), "Malmö": (55.61, 13.00),
    # Turkey
    "Istanbul": (41.01, 28.95), "Ankara": (39.93, 32.86),
    # Middle East
    "Dubai": (25.20, 55.27), "Tel Aviv": (32.08, 34.78), "Beirut": (33.89, 35.50),
    # Latin America
    "Mexico City": (19.43, -99.13), "Buenos Aires": (-34.60, -58.38),
    "Bogota": (4.71, -74.07), "Lima": (-12.05, -77.04), "Santiago": (-33.45, -70.67),
    "Sao Paulo": (-23.55, -46.63), "Rio de Janeiro": (-22.91, -43.17),
    "Guadalajara": (20.67, -103.35), "Medellin": (6.23, -75.57),
    "Montevideo": (-34.90, -56.19), "Quito": (-0.22, -78.51),
    # Africa
    "Cape Town": (-33.93, 18.42), "Johannesburg": (-26.20, 28.04),
    "Cairo": (30.06, 31.25), "Lagos": (6.52, 3.38), "Nairobi": (-1.29, 36.82),
    "Accra": (5.56, -0.20), "Casablanca": (33.59, -7.61),
    # Japan
    "Tokyo": (35.69, 139.69), "Osaka": (34.69, 135.50), "Kyoto": (35.02, 135.76),
    # Korea
    "Seoul": (37.57, 126.98), "Busan": (35.10, 129.04),
    # China
    "Beijing": (39.91, 116.39), "Shanghai": (31.23, 121.47),
    "Shenzhen": (22.54, 114.06), "Chengdu": (30.57, 104.07),
    # SE Asia
    "Singapore": (1.35, 103.82), "Bangkok": (13.76, 100.50), "Taipei": (25.05, 121.56),
    "Kuala Lumpur": (3.14, 101.69), "Hong Kong": (22.28, 114.17),
    "Jakarta": (-6.21, 106.85), "Manila": (14.60, 120.98),
    "Ho Chi Minh City": (10.82, 106.63), "Hanoi": (21.03, 105.85),
    # India
    "Bangalore": (12.97, 77.59), "Mumbai": (19.08, 72.88), "Delhi": (28.61, 77.21),
    "Chennai": (13.08, 80.27), "Hyderabad": (17.39, 78.49), "Pune": (18.52, 73.86),
    # --- Germany (GeoNames, state-confirmed) ---
    "Aach": (47.8424, 8.8538),
    "Aachen": (50.7766, 6.0834),
    "Aalen": (48.8378, 10.0933),
    "Abenberg": (49.2428, 10.964),
    "Abensberg": (48.8168, 11.8498),
    "Achern": (48.6311, 8.0761),
    "Achim": (53.0142, 9.0263),
    "Adelsheim": (49.4015, 9.3925),
    "Adenau": (50.3824, 6.9329),
    "Adorf": (50.3201, 12.2599),
    "Ahaus": (52.0794, 7.0134),
    "Ahlen": (51.7634, 7.8887),
    "Ahrensburg": (53.6766, 10.237),
    "Aichach": (48.4575, 11.1341),
    "Aichtal": (48.6264, 9.2631),
    "Aken (Elbe)": (51.8527, 12.0446),
    "Albstadt": (48.2164, 9.026),
    "Alfeld": (51.9838, 9.8199),
    "Allendorf (Lumda)": (51.0299, 8.6723),
    "Allstedt": (51.4038, 11.3869),
    "Alpirsbach": (48.3451, 8.402),
    "Alsdorf": (50.8767, 6.164),
    "Alsfeld": (50.7518, 9.2708),
    "Alsleben (Saale)": (51.7016, 11.6765),
    "Altdorf bei Nürnberg": (49.3856, 11.3573),
    "Altena": (51.2947, 7.6734),
    "Altenberg": (50.7656, 13.7533),
    "Altenburg": (50.9876, 12.4368),
    "Altenkirchen": (50.6859, 7.6418),
    "Altensteig": (48.5865, 8.6039),
    "Altentreptow": (53.6927, 13.2561),
    "Altlandsberg": (52.565, 13.7281),
    "Altötting": (48.2253, 12.6767),
    "Alzenau": (50.0888, 9.0646),
    "Alzey": (49.7466, 8.1151),
    "Amberg": (49.4429, 11.8627),
    "Amöneburg": (50.7959, 8.9233),
    "Amorbach": (49.6444, 9.2218),
    "Andernach": (50.4311, 7.4043),
    "Angermünde": (53.015, 13.9992),
    "Anklam": (53.8564, 13.6897),
    "Annaberg-Buchholz": (50.5795, 13.0063),
    "Annaburg": (51.733, 13.0473),
    "Annweiler am Trifels": (49.2061, 7.9753),
    "Ansbach": (49.3048, 10.5931),
    "Apolda": (51.0262, 11.5164),
    "Arendsee": (52.8807, 11.4862),
    "Arneburg": (52.6756, 12.0051),
    "Arnis": (54.6306, 9.932),
    "Arnsberg": (51.3833, 8.0833),
    "Arnstadt": (50.8405, 10.952),
    "Arnstein (Bavaria)": (49.9777, 9.9698),
    "Artern": (51.3643, 11.2917),
    "Arzberg": (50.0577, 12.1868),
    "Aschaffenburg": (49.977, 9.1521),
    "Aschersleben": (51.7574, 11.4608),
    "Asperg": (48.9053, 9.135),
    "Aßlar": (50.5916, 8.4627),
    "Attendorn": (51.1264, 7.9033),
    "Aub": (49.5527, 10.0653),
    "Auerbach in der Oberpfalz": (49.692, 11.6333),
    "Auerbach (Vogtland)": (50.5115, 12.4008),
    "Augsburg": (48.3715, 10.8985),
    "Augustusburg": (50.8119, 13.102),
    "Aulendorf": (47.9508, 9.6374),
    "Aurich": (53.4696, 7.4824),
    "Babenhausen": (49.9652, 8.9513),
    "Bacharach": (50.0573, 7.7695),
    "Backnang": (48.9474, 9.4372),
    "Bad Aibling": (47.8638, 12.0106),
    "Bad Arolsen": (51.3798, 9.0145),
    "Bad Belzig": (52.1418, 12.5927),
    "Bad Bentheim": (52.3007, 7.1576),
    "Bad Bergzabern": (49.1024, 8.0009),
    "Bad Berka": (50.8998, 11.2825),
    "Bad Berleburg": (51.0522, 8.3923),
    "Bad Berneck im Fichtelgebirge": (50.0456, 11.6724),
    "Bad Bevensen": (53.0792, 10.5813),
    "Bad Bibra": (51.208, 11.5852),
    "Bad Blankenburg": (50.6819, 11.2737),
    "Bad Bramstedt": (53.9183, 9.8824),
    "Bad Breisig": (50.5052, 7.2886),
    "Bad Brückenau": (50.3085, 9.7898),
    "Bad Buchau": (48.0623, 9.6124),
    "Bad Camberg": (50.297, 8.269),
    "Bad Doberan": (54.1071, 11.9005),
    "Bad Driburg": (51.733, 9.0197),
    "Bad Düben": (51.5917, 12.5849),
    "Bad Dürkheim": (49.4618, 8.1724),
    "Bad Dürrenberg": (51.2955, 12.0658),
    "Bad Dürrheim": (48.0209, 8.5306),
    "Bad Elster": (50.2819, 12.2343),
    "Bad Ems": (50.3354, 7.7137),
    "Bad Fallingbostel": (52.866, 9.6949),
    "Bad Frankenhausen/Kyffhäuser": (51.3561, 11.0998),
    "Bad Freienwalde (Oder)": (52.7873, 14.0304),
    "Bad Gandersheim": (51.8717, 10.0254),
    "Bad Gottleuba-Berggießhübel": (50.8529, 13.9438),
    "Bad Griesbach im Rottal": (48.4518, 13.1933),
    "Bad Harzburg": (51.8827, 10.5616),
    "Bad Herrenalb": (48.7979, 8.4362),
    "Bad Hersfeld": (50.872, 9.7089),
    "Bad Homburg vor der Höhe": (50.2268, 8.6182),
    "Bad Honnef": (50.6434, 7.2278),
    "Bad Hönningen": (50.5169, 7.312),
    "Bad Iburg": (52.1549, 8.0422),
    "Bad Karlshafen": (51.6426, 9.4548),
    "Bad Kissingen": (50.2023, 10.0778),
    "Bad König": (49.7432, 9.0075),
    "Bad Königshofen im Grabfeld": (50.3008, 10.4689),
    "Bad Köstritz": (50.9303, 12.01),
    "Bad Kötzting": (49.1719, 12.8567),
    "Bad Kreuznach": (49.8414, 7.8671),
    "Bad Krozingen": (47.9167, 7.7),
    "Bad Laasphe": (50.9314, 8.425),
    "Bad Langensalza": (51.1077, 10.646),
    "Bad Lauchstädt": (51.3865, 11.8696),
    "Bad Lausick": (51.145, 12.6445),
    "Bad Lauterberg im Harz": (51.6327, 10.4703),
    "Bad Liebenstein": (50.8157, 10.3512),
    "Bad Liebenwerda": (51.5183, 13.3946),
    "Bad Liebenzell": (48.7743, 8.7297),
    "Bad Lippspringe": (51.7833, 8.8168),
    "Bad Lobenstein": (50.4522, 11.6393),
    "Bad Marienberg": (50.6495, 7.9496),
    "Bad Mergentheim": (49.4925, 9.7736),
    "Bad Münder am Deister": (52.1955, 9.4642),
    "Bad Münstereifel": (50.5567, 6.7642),
    "Bad Muskau": (51.5505, 14.7124),
    "Bad Nauheim": (50.3646, 8.7386),
    "Bad Nenndorf": (52.337, 9.379),
    "Bad Neuenahr-Ahrweiler": (50.5432, 7.1113),
    "Bad Neustadt an der Saale": (50.3217, 10.2067),
    "Bad Oeynhausen": (52.207, 8.8036),
    "Bad Oldesloe": (53.8117, 10.3742),
    "Bad Orb": (50.2279, 9.3478),
    "Bad Pyrmont": (51.9859, 9.2525),
    "Bad Rappenau": (49.2385, 9.1018),
    "Bad Reichenhall": (47.7295, 12.8782),
    "Bad Rodach": (50.3402, 10.7787),
    "Bad Sachsa": (51.595, 10.5555),
    "Bad Säckingen": (47.5537, 7.9461),
    "Bad Salzdetfurth": (52.0578, 10.0058),
    "Bad Salzuflen": (52.0862, 8.7443),
    "Bad Salzungen": (50.8134, 10.2361),
    "Bad Saulgau": (48.0168, 9.5006),
    "Bad Schandau": (50.9174, 14.1549),
    "Bad Schmiedeberg": (51.6852, 12.7348),
    "Bad Schussenried": (48.0047, 9.6574),
    "Bad Schwalbach": (50.142, 8.0696),
    "Bad Schwartau": (53.9189, 10.6969),
    "Bad Segeberg": (53.9378, 10.3074),
    "Bad Sobernheim": (49.7864, 7.6515),
    "Bad Soden am Taunus": (50.1408, 8.5045),
    "Bad Soden-Salmünster": (50.2757, 9.3671),
    "Bad Sooden-Allendorf": (51.2709, 9.9748),
    "Bad Staffelstein": (50.102, 11.0013),
    "Bad Sulza": (51.0893, 11.6247),
    "Bad Sülze": (54.1108, 12.6605),
    "Bad Teinach-Zavelstein": (48.6905, 8.6928),
    "Bad Tennstedt": (51.1545, 10.8387),
    "Bad Tölz": (47.7611, 11.5589),
    "Bad Urach": (48.4911, 9.4001),
    "Bad Vilbel": (50.1787, 8.7376),
    "Bad Waldsee": (47.9203, 9.7549),
    "Bad Wildbad": (48.7507, 8.5504),
    "Bad Wildungen": (51.1196, 9.1248),
    "Bad Wilsnack": (52.9565, 11.9485),
    "Bad Wimpfen": (49.2297, 9.1565),
    "Bad Windsheim": (49.5027, 10.4154),
    "Bad Wörishofen": (48.0067, 10.5967),
    "Bad Wünnenberg": (51.52, 8.6993),
    "Bad Wurzach": (47.908, 9.8969),
    "Baden-Baden": (48.7606, 8.2398),
    "Baesweiler": (50.9096, 6.1887),
    "Baiersdorf": (49.6581, 11.0359),
    "Balingen": (48.2752, 8.8546),
    "Ballenstedt": (51.719, 11.2326),
    "Balve": (51.3315, 7.8642),
    "Bamberg": (49.8987, 10.9007),
    "Barby (Elbe)": (51.9671, 11.8826),
    "Bargteheide": (53.7299, 10.2674),
    "Barmstedt": (53.7918, 9.7702),
    "Bärnau": (49.8108, 12.4332),
    "Barntrup": (51.9904, 9.1164),
    "Barsinghausen": (52.3, 9.45),
    "Barth": (54.3635, 12.7249),
    "Baruth/Mark": (52.0447, 13.5027),
    "Bassum": (52.8506, 8.7279),
    "Battenberg": (51.0139, 8.646),
    "Baumholder": (49.6174, 7.3338),
    "Baunach": (49.9859, 10.8518),
    "Baunatal": (51.2518, 9.4075),
    "Bautzen": (51.1803, 14.4349),
    "Bayreuth": (49.9478, 11.5789),
    "Bebra": (50.9744, 9.7956),
    "Beckum": (51.7557, 8.0407),
    "Bedburg": (50.9926, 6.5713),
    "Beelitz": (52.2381, 12.9714),
    "Beeskow": (52.1729, 14.246),
    "Beilngries": (49.0341, 11.4739),
    "Beilstein": (49.0414, 9.3137),
    "Bendorf": (50.4229, 7.5792),
    "Bensheim": (49.6837, 8.6184),
    "Berching": (49.1069, 11.4414),
    "Bergen": (52.8084, 9.9637),
    "Bergen auf Rügen": (54.4182, 13.4335),
    "Bergheim": (50.9557, 6.6399),
    "Bergisch Gladbach": (50.9856, 7.133),
    "Bergkamen": (51.6163, 7.6445),
    "Bergneustadt": (51.025, 7.656),
    "Bernau bei Berlin": (52.6798, 13.5871),
    "Bernburg": (51.7946, 11.7401),
    "Bernkastel-Kues": (49.916, 7.0766),
    "Bernsdorf": (51.3735, 14.0689),
    "Bersenbrück": (52.5502, 7.9483),
    "Besigheim": (48.998, 9.1427),
    "Betzdorf": (50.7909, 7.8719),
    "Betzenstein": (49.6817, 11.4177),
    "Beverungen": (51.668, 9.3742),
    "Bexbach": (49.3462, 7.2553),
    "Biberach an der Riß": (48.0934, 9.7905),
    "Biedenkopf": (50.9113, 8.5302),
    "Bielefeld": (52.0333, 8.5333),
    "Biesenthal": (52.7662, 13.6442),
    "Bietigheim-Bissingen": (48.9441, 9.1175),
    "Billerbeck": (51.9783, 7.2926),
    "Bingen am Rhein": (49.9667, 7.8992),
    "Birkenfeld": (49.6525, 7.1667),
    "Bischofsheim in der Rhön": (50.4024, 10.0075),
    "Bischofswerda": (51.1277, 14.1797),
    "Bismark (Altmark)": (52.6615, 11.5549),
    "Bitburg": (49.9679, 6.5273),
    "Bitterfeld-Wolfen": (51.6236, 12.3239),
    "Blankenburg (Harz)": (51.7903, 10.9551),
    "Blankenhain": (50.8599, 11.3439),
    "Blaubeuren": (48.4121, 9.7843),
    "Blaustein": (48.4166, 9.9174),
    "Bleckede": (53.2873, 10.7349),
    "Bleicherode": (51.4403, 10.572),
    "Blieskastel": (49.2372, 7.2562),
    "Blomberg": (51.9433, 9.0907),
    "Blumberg": (47.8406, 8.5333),
    "Bobingen": (48.2709, 10.8339),
    "Böblingen": (48.6821, 9.0117),
    "Bocholt": (51.8388, 6.6153),
    "Bochum": (51.4817, 7.2165),
    "Bockenem": (52.0099, 10.132),
    "Bodenwerder": (51.9716, 9.5193),
    "Bogen": (48.9112, 12.6896),
    "Böhlen": (51.2006, 12.3862),
    "Boizenburg": (53.3808, 10.7376),
    "Bonn": (50.7344, 7.0955),
    "Bonndorf im Schwarzwald": (47.8186, 8.3414),
    "Bönnigheim": (49.0402, 9.0939),
    "Bopfingen": (48.8585, 10.3542),
    "Boppard": (50.2308, 7.5899),
    "Borgentreich": (51.5692, 9.2411),
    "Borgholzhausen": (52.1034, 8.3021),
    "Borken": (51.8438, 6.8577),
    "Borken (Hesse)": (51.045, 9.2844),
    "Borkum": (53.5809, 6.6915),
    "Borna": (51.1242, 12.4964),
    "Bornheim": (50.7631, 6.9909),
    "Bottrop": (51.5239, 6.9285),
    "Boxberg": (49.4796, 9.6401),
    "Brackenheim": (49.0779, 9.066),
    "Brake (Unterweser)": (53.3333, 8.4833),
    "Brakel": (51.7175, 9.186),
    "Bramsche": (52.4084, 7.9833),
    "Brandenburg an der Havel": (52.4167, 12.55),
    "Brand-Erbisdorf": (50.8664, 13.3229),
    "Brandis": (51.336, 12.6102),
    "Braubach": (50.2736, 7.6451),
    "Braunfels": (50.5155, 8.3892),
    "Braunlage": (51.7265, 10.6109),
    "Bräunlingen": (47.9296, 8.4481),
    "Braunsbedra": (51.286, 11.8899),
    "Braunschweig": (52.2659, 10.5267),
    "Breckerfeld": (51.2593, 7.4681),
    "Bredstedt": (54.6187, 8.9644),
    "Breisach": (48.0328, 7.5829),
    "Bremen": (53.0758, 8.8072),
    "Bremerhaven": (53.5536, 8.5755),
    "Bremervörde": (53.4851, 9.1464),
    "Bretten": (49.0369, 8.7074),
    "Brilon": (51.3946, 8.5715),
    "Bruchköbel": (50.1785, 8.9231),
    "Bruchsal": (49.1243, 8.598),
    "Brück": (52.1977, 12.7687),
    "Brüel": (53.7371, 11.7154),
    "Brühl": (50.8293, 6.905),
    "Brunsbüttel": (53.8962, 9.1046),
    "Brüssow": (53.3997, 14.1253),
    "Buchen (Odenwald)": (48.4405, 7.9892),
    "Buchholz in der Nordheide": (53.3304, 9.866),
    "Buchloe": (48.0372, 10.7255),
    "Bückeburg": (52.2606, 9.0494),
    "Buckow": (52.5661, 14.0743),
    "Büdelsdorf": (54.3182, 9.673),
    "Büdingen": (50.2901, 9.1114),
    "Bühl": (48.6968, 8.1352),
    "Bünde": (52.1984, 8.5864),
    "Büren": (51.5511, 8.5596),
    "Burg": (51.4228, 11.9933),
    "Burgau": (48.4316, 10.4099),
    "Burgbernheim": (49.451, 10.3239),
    "Burgdorf": (52.4463, 10.0064),
    "Bürgel": (50.9422, 11.7563),
    "Burghausen": (48.1692, 12.8314),
    "Burgkunstadt": (50.1409, 11.2521),
    "Burglengenfeld": (49.2038, 12.0445),
    "Burgstädt": (50.9133, 12.806),
    "Burg Stargard": (53.4954, 13.3102),
    "Burladingen": (48.2911, 9.1129),
    "Burscheid": (51.0847, 7.1139),
    "Bürstadt": (49.6427, 8.4594),
    "Butzbach": (50.434, 8.6712),
    "Bützow": (53.8413, 11.9815),
    "Buxtehude": (53.4699, 9.6897),
    "Calau": (51.744, 13.9533),
    "Calbe (Saale)": (51.9067, 11.7748),
    "Calw": (48.7142, 8.7403),
    "Castrop-Rauxel": (51.5566, 7.3116),
    "Celle": (52.6226, 10.0805),
    "Cham": (49.2257, 12.655),
    "Chemnitz": (50.8357, 12.9292),
    "Clausthal-Zellerfeld": (51.8095, 10.3382),
    "Clingen": (51.2321, 10.9328),
    "Cloppenburg": (52.8475, 8.0474),
    "Coburg": (50.2594, 10.9638),
    "Cochem": (50.1451, 7.1638),
    "Coesfeld": (51.9435, 7.1681),
    "Colditz": (51.1282, 12.8029),
    "Coswig": (51.132, 13.5831),
    "Coswig (Anhalt)": (51.8862, 12.4501),
    "Cottbus": (51.7577, 14.3289),
    "Crailsheim": (49.1344, 10.0719),
    "Creglingen": (49.4694, 10.0312),
    "Creußen": (49.8449, 11.6268),
    "Crimmitschau": (50.8164, 12.3904),
    "Crivitz": (53.5736, 11.6514),
    "Cuxhaven": (53.8683, 8.699),
    "Daaden": (50.7333, 7.9667),
    "Dachau": (48.26, 11.434),
    "Dahlen": (51.365, 12.9988),
    "Dahme/Mark": (51.8701, 13.4274),
    "Dahn": (49.151, 7.7784),
    "Damme": (52.5215, 8.1977),
    "Dannenberg (Elbe)": (53.0967, 11.09),
    "Dargun": (53.9009, 12.8501),
    "Darmstadt": (49.8717, 8.6503),
    "Dassel": (51.8018, 9.689),
    "Dassow": (53.911, 10.9755),
    "Datteln": (51.656, 7.3453),
    "Daun": (50.1972, 6.8294),
    "Degenfeld": (48.7276, 9.8783),
    "Deggendorf": (48.8409, 12.9607),
    "Deidesheim": (49.4078, 8.1845),
    "Delbrück": (51.765, 8.5622),
    "Delitzsch": (51.5255, 12.3428),
    "Delmenhorst": (53.0511, 8.6309),
    "Demmin": (53.9076, 13.0314),
    "Dessau-Roßlau": (51.8386, 12.2455),
    "Detmold": (51.9385, 8.8732),
    "Dettelbach": (49.803, 10.1652),
    "Dieburg": (49.8974, 8.8461),
    "Diemelstadt": (51.4778, 9.0107),
    "Diepholz": (52.6069, 8.3703),
    "Dierdorf": (50.5465, 7.6527),
    "Dietenheim": (48.2107, 10.0716),
    "Dietzenbach": (50.0098, 8.7778),
    "Diez": (50.3742, 8.0074),
    "Dillenburg": (50.7411, 8.287),
    "Dillingen an der Donau": (48.5815, 10.4953),
    "Dingelstädt": (51.3153, 10.3174),
    "Dingolfing": (48.6424, 12.4928),
    "Dinkelsbühl": (49.0694, 10.3199),
    "Dinklage": (52.662, 8.126),
    "Dinslaken": (51.5623, 6.7434),
    "Dippoldiswalde": (50.8962, 13.6691),
    "Ditzingen": (48.8267, 9.067),
    "Döbeln": (51.1221, 13.1103),
    "Doberlug-Kirchhain": (51.6258, 13.5623),
    "Döbern": (51.6159, 14.5981),
    "Dohna": (50.9562, 13.8584),
    "Dömitz": (53.1408, 11.2502),
    "Dommitzsch": (51.6407, 12.8794),
    "Donaueschingen": (47.9551, 8.4971),
    "Donauwörth": (48.718, 10.7793),
    "Donzdorf": (48.6854, 9.8105),
    "Dorfen": (48.2704, 12.1606),
    "Dormagen": (51.0968, 6.8317),
    "Dornhan": (48.3501, 8.509),
    "Dornstetten": (48.472, 8.4982),
    "Dorsten": (51.6617, 6.9651),
    "Dortmund": (51.5149, 7.466),
    "Dransfeld": (51.4991, 9.7618),
    "Drebkau": (51.6541, 14.2232),
    "Dreieich": (50.02, 8.6961),
    "Drensteinfurt": (51.7953, 7.7382),
    "Drolshagen": (51.0236, 7.7736),
    "Duderstadt": (51.5131, 10.2595),
    "Duisburg": (51.4325, 6.7652),
    "Dülmen": (51.8315, 7.2808),
    "Düren": (50.8043, 6.493),
    "Ebeleben": (51.2828, 10.73),
    "Eberbach": (49.4668, 8.9902),
    "Ebermannstadt": (49.7815, 11.1817),
    "Ebern": (50.0954, 10.7983),
    "Ebersbach an der Fils": (48.716, 9.5236),
    "Ebersberg": (48.0771, 11.9706),
    "Eberswalde": (52.8349, 13.8195),
    "Eckartsberga": (51.1238, 11.5604),
    "Eckernförde": (54.4685, 9.8382),
    "Edenkoben": (49.2839, 8.1271),
    "Egeln": (51.9438, 11.4327),
    "Eggenfelden": (48.4051, 12.7575),
    "Eggesin": (53.6783, 14.0853),
    "Ehingen (Donau)": (48.2826, 9.7275),
    "Ehrenfriedersdorf": (50.6493, 12.9701),
    "Eibelstadt": (49.7239, 9.9996),
    "Eibenstock": (50.4943, 12.5998),
    "Eichstätt": (48.8885, 11.1967),
    "Eilenburg": (51.4598, 12.6334),
    "Einbeck": (51.8202, 9.8696),
    "Eisenach": (50.9807, 10.3152),
    "Eisenberg": (50.9686, 11.9021),
    "Eisenberg (Pfalz)": (49.5586, 8.072),
    "Eisenhüttenstadt": (52.15, 14.65),
    "Eisfeld": (50.4265, 10.907),
    "Eisleben": (51.5275, 11.5483),
    "Eislingen/Fils": (48.6951, 9.7068),
    "Ellingen": (49.0608, 10.9678),
    "Ellrich": (51.5866, 10.6633),
    "Ellwangen": (48.9616, 10.1317),
    "Elmshorn": (53.7491, 9.6618),
    "Elsdorf": (50.9374, 6.5683),
    "Elsfleth": (53.2375, 8.4566),
    "Elsterberg": (50.6084, 12.1679),
    "Elsterwerda": (51.4604, 13.52),
    "Elstra": (51.2217, 14.132),
    "Elterlein": (50.5766, 12.8684),
    "Eltmann": (49.9715, 10.6671),
    "Eltville am Rhein": (50.0286, 8.1175),
    "Elzach": (48.1725, 8.0699),
    "Elze": (52.1226, 9.736),
    "Emden": (53.3659, 7.2085),
    "Emmelshausen": (50.1548, 7.5518),
    "Emmendingen": (48.121, 7.8536),
    "Emmerich am Rhein": (51.8393, 6.2479),
    "Emsdetten": (52.1734, 7.5278),
    "Endingen": (48.1422, 7.7005),
    "Engen": (47.8553, 8.7734),
    "Enger": (52.1406, 8.5577),
    "Ennepetal": (51.2985, 7.3629),
    "Ennigerloh": (51.8384, 8.0309),
    "Eppelheim": (49.4019, 8.6364),
    "Eppingen": (49.1365, 8.9123),
    "Eppstein": (50.1428, 8.3923),
    "Erbach (Donau)": (48.3284, 9.8875),
    "Erbach": (49.6615, 8.994),
    "Erbendorf": (49.8398, 12.0459),
    "Erding": (48.306, 11.9069),
    "Erftstadt": (50.8148, 6.7939),
    "Erfurt": (50.9773, 11.0354),
    "Erkelenz": (51.0795, 6.3153),
    "Erkner": (52.42, 13.7544),
    "Erkrath": (51.2223, 6.9083),
    "Erlangen": (49.591, 11.0078),
    "Erlenbach am Main": (49.8034, 9.1631),
    "Erlensee": (50.163, 8.9782),
    "Erwitte": (51.6127, 8.3384),
    "Eschborn": (50.1433, 8.5711),
    "Eschenbach in der Oberpfalz": (49.7554, 11.8331),
    "Eschershausen": (51.9266, 9.6428),
    "Eschwege": (51.1839, 10.0533),
    "Eschweiler": (50.8185, 6.2718),
    "Esens": (53.6487, 7.6127),
    "Espelkamp": (52.3814, 8.623),
    "Essen": (51.4566, 7.0123),
    "Esslingen am Neckar": (48.7396, 9.3047),
    "Ettenheim": (48.257, 7.8125),
    "Ettlingen": (48.9409, 8.4076),
    "Euskirchen": (50.6606, 6.7872),
    "Eutin": (54.135, 10.6115),
    "Falkensee": (52.5601, 13.0927),
    "Falkenstein/Harz": (51.7332, 11.3355),
    "Fehmarn": (54.4378, 11.1935),
    "Fellbach": (48.8091, 9.277),
    "Felsberg": (51.1376, 9.4214),
    "Feuchtwangen": (49.1629, 10.3385),
    "Filderstadt": (48.657, 9.2205),
    "Finsterwalde": (51.6339, 13.7066),
    "Fladungen": (50.5205, 10.1458),
    "Flensburg": (54.788, 9.4372),
    "Flöha": (50.8561, 13.0741),
    "Flörsheim am Main": (50.0131, 8.4278),
    "Florstadt": (50.3167, 8.8667),
    "Floß": (49.724, 12.2759),
    "Forchheim": (49.7175, 11.0588),
    "Forchtenberg": (49.2887, 9.5603),
    "Forst (Lausitz)": (51.7354, 14.6397),
    "Frankenau": (51.0927, 8.9345),
    "Frankenberg (Eder)": (51.0589, 8.8008),
    "Frankenberg/Sa.": (50.913, 13.0401),
    "Frankenthal (Pfalz)": (49.5341, 8.3536),
    "Frankfurt (Oder)": (52.3471, 14.5506),
    "Franzburg": (54.1856, 12.8824),
    "Frauenstein": (50.8028, 13.5379),
    "Frechen": (50.9149, 6.8118),
    "Freiberg am Neckar": (48.932, 9.2024),
    "Freiberg": (50.9109, 13.3388),
    "Freiburg im Breisgau": (47.9959, 7.8522),
    "Freilassing": (47.8409, 12.9811),
    "Freinsheim": (49.5065, 8.2119),
    "Freising": (48.4035, 11.7488),
    "Freital": (51.0017, 13.6488),
    "Freren": (52.4868, 7.5442),
    "Freudenberg (Baden-Württemberg)": (49.7535, 9.3275),
    "Freudenberg (North Rhine-Westphalia)": (50.8974, 7.8742),
    "Freudenstadt": (48.4669, 8.4137),
    "Freyburg (Unstrut)": (51.2136, 11.768),
    "Freystadt": (49.2001, 11.3303),
    "Freyung": (48.8095, 13.5477),
    "Fridingen an der Donau": (48.0196, 8.9232),
    "Friedberg (Bavaria)": (48.3569, 10.9846),
    "Friedberg (Hesse)": (50.3374, 8.7559),
    "Friedland (Mecklenburg-Western Pomerania)": (53.672, 13.5506),
    "Friedland (Brandenburg)": (52.1049, 14.264),
    "Friedrichroda": (50.8575, 10.5651),
    "Friedrichsdorf": (50.2496, 8.6428),
    "Friedrichshafen": (47.6569, 9.4755),
    "Friedrichstadt": (54.3757, 9.0867),
    "Friedrichsthal": (49.3279, 7.0962),
    "Friesack": (52.7376, 12.5797),
    "Friesoythe": (53.0205, 7.8588),
    "Fritzlar": (51.1318, 9.2756),
    "Frohburg": (51.0572, 12.5575),
    "Fröndenberg": (51.4756, 7.7695),
    "Fulda": (50.5516, 9.6752),
    "Fürstenau": (52.5167, 7.6767),
    "Fürstenberg/Havel": (53.1853, 13.1455),
    "Fürstenfeldbruck": (48.179, 11.2547),
    "Fürstenwalde": (52.3607, 14.0618),
    "Fürth": (49.4759, 10.9886),
    "Furth im Wald": (49.3096, 12.8416),
    "Furtwangen im Schwarzwald": (48.0516, 8.2072),
    "Füssen": (47.5714, 10.7017),
    "Gadebusch": (53.7018, 11.1166),
    "Gaggenau": (48.8, 8.3333),
    "Gaildorf": (49.0003, 9.7695),
    "Gammertingen": (48.2524, 9.2235),
    "Garbsen": (52.4137, 9.5899),
    "Garching bei München": (48.249, 11.651),
    "Gardelegen": (52.5252, 11.3952),
    "Garding": (54.3306, 8.7806),
    "Gartz": (53.2083, 14.3923),
    "Garz/Rügen": (54.3184, 13.3513),
    "Gau-Algesheim": (49.9567, 8.0157),
    "Gebesee": (51.1149, 10.9345),
    "Gedern": (50.4248, 9.1984),
    "Geesthacht": (53.4366, 10.3734),
    "Gefell": (50.4405, 11.8593),
    "Gefrees": (50.0954, 11.7377),
    "Gehrden": (52.3136, 9.6003),
    "Geilenkirchen": (50.9674, 6.1176),
    "Geisa": (50.7146, 9.9507),
    "Geiselhöring": (48.825, 12.3965),
    "Geisenfeld": (48.6843, 11.6123),
    "Geisenheim": (49.9847, 7.9684),
    "Geisingen": (47.925, 8.65),
    "Geislingen (bei Balingen)": (48.2877, 8.8124),
    "Geislingen an der Steige": (48.6242, 9.8274),
    "Geithain": (51.0553, 12.6967),
    "Geldern": (51.5191, 6.3236),
    "Gelnhausen": (50.2016, 9.1874),
    "Gelsenkirchen": (51.5051, 7.0965),
    "Gemünden am Main": (50.0495, 9.7059),
    "Gemünden (Wohra)": (50.3609, 8.4061),
    "Gengenbach": (48.4048, 8.0143),
    "Genthin": (52.4067, 12.1592),
    "Georgsmarienhütte": (52.203, 8.0448),
    "Gera": (50.8803, 12.0819),
    "Gerabronn": (48.9707, 9.9199),
    "Gerbstedt": (51.6328, 11.6267),
    "Geretsried": (47.8578, 11.4805),
    "Geringswalde": (51.0768, 12.9072),
    "Gerlingen": (48.7995, 9.0632),
    "Germering": (48.1339, 11.3765),
    "Germersheim": (49.2144, 8.3669),
    "Gernsbach": (48.7703, 8.3431),
    "Gernsheim": (49.7531, 8.4886),
    "Gerolstein": (50.2222, 6.6598),
    "Gerolzhofen": (49.9002, 10.3483),
    "Gersfeld (Rhön)": (50.4514, 9.9142),
    "Gersthofen": (48.4243, 10.8727),
    "Gescher": (51.954, 7.0048),
    "Geseke": (51.6409, 8.5109),
    "Gevelsberg": (51.3197, 7.3392),
    "Geyer": (50.6263, 12.9207),
    "Giengen an der Brenz": (48.6222, 10.2431),
    "Giessen": (50.5873, 8.6755),
    "Gifhorn": (52.4777, 10.5511),
    "Ginsheim-Gustavsburg": (49.9711, 8.3453),
    "Gladbeck": (51.5708, 6.9859),
    "Gladenbach": (50.7685, 8.5808),
    "Glashütte": (50.852, 13.7798),
    "Glauchau": (50.8199, 12.5449),
    "Glinde": (53.5405, 10.213),
    "Glücksburg": (54.835, 9.5522),
    "Glückstadt": (53.7882, 9.4241),
    "Gnoien": (53.9687, 12.711),
    "Goch": (51.6787, 6.1589),
    "Goldberg": (53.5888, 12.0882),
    "Goldkronach": (50.0109, 11.6875),
    "Golßen": (51.972, 13.6012),
    "Gommern": (52.0739, 11.823),
    "Göppingen": (48.7035, 9.6521),
    "Görlitz": (51.1552, 14.9885),
    "Goslar": (51.9042, 10.4277),
    "Gößnitz": (50.889, 12.4329),
    "Gotha": (50.9482, 10.7019),
    "Göttingen": (51.5344, 9.9323),
    "Grabow": (53.279, 11.5637),
    "Grafenau": (48.8577, 13.3974),
    "Gräfenberg": (49.6443, 11.2497),
    "Gräfenhainichen": (51.7289, 12.4565),
    "Gräfenthal": (50.5246, 11.3068),
    "Grafenwöhr": (49.7173, 11.9064),
    "Grafing bei München": (48.046, 11.968),
    "Gransee": (53.007, 13.1575),
    "Grebenau": (50.7424, 9.4731),
    "Grebenstein": (51.4465, 9.4125),
    "Greding": (49.047, 11.357),
    "Greifswald": (54.0891, 13.4024),
    "Greiz": (50.6578, 12.1992),
    "Greußen": (51.2296, 10.9442),
    "Greven": (52.0936, 7.594),
    "Grevenbroich": (51.091, 6.5827),
    "Grevesmühlen": (53.8613, 11.1905),
    "Griesheim": (49.8608, 8.5725),
    "Grimma": (51.2337, 12.7196),
    "Grimmen": (54.1121, 13.0405),
    "Gröditz": (51.4141, 13.4487),
    "Groitzsch": (51.1554, 12.2828),
    "Gronau (Leine)": (52.0846, 9.7768),
    "Gronau (Westf.)": (52.211, 7.0224),
    "Gröningen": (51.9374, 11.216),
    "Großalmerode": (51.2586, 9.7845),
    "Groß-Bieberau": (49.8006, 8.8243),
    "Großbottwar": (49.0015, 9.2935),
    "Großbreitenbach": (50.5834, 11.0096),
    "Großenhain": (51.2895, 13.5335),
    "Groß-Gerau": (49.9214, 8.4825),
    "Großräschen": (51.5876, 14.0109),
    "Großröhrsdorf": (51.1453, 14.0192),
    "Großschirma": (50.966, 13.2859),
    "Groß-Umstadt": (49.869, 8.9321),
    "Grünberg": (50.594, 8.9587),
    "Grünsfeld": (49.6095, 9.7472),
    "Grünstadt": (49.563, 8.1628),
    "Guben": (51.9499, 14.7055),
    "Gudensberg": (51.1771, 9.3675),
    "Güglingen": (49.0664, 9.0017),
    "Gummersbach": (51.0261, 7.5647),
    "Gundelsheim": (49.2833, 9.1604),
    "Günzburg": (48.456, 10.2769),
    "Gunzenhausen": (49.1166, 10.7597),
    "Güsten": (51.7964, 11.6125),
    "Güstrow": (53.7972, 12.1734),
    "Gütersloh": (51.9069, 8.3785),
    "Gützkow": (53.7239, 13.1076),
    "Haan": (51.1938, 7.0133),
    "Haar": (48.1088, 11.7265),
    "Hachenburg": (50.66, 7.8228),
    "Hadamar": (50.4459, 8.0425),
    "Hagen": (51.3608, 7.4717),
    "Hagenbach": (49.0173, 8.2502),
    "Hagenow": (53.432, 11.1916),
    "Haiger": (50.7416, 8.2078),
    "Haigerloch": (48.3661, 8.8036),
    "Hainichen": (50.9704, 13.1229),
    "Haiterbach": (48.5207, 8.6443),
    "Halberstadt": (51.8956, 11.0562),
    "Haldensleben": (52.2891, 11.4098),
    "Halle (Saale)": (51.4816, 11.9795),
    "Halle": (52.0601, 8.3608),
    "Hallenberg": (51.1112, 8.6201),
    "Hallstadt": (49.929, 10.8754),
    "Haltern am See": (51.743, 7.1816),
    "Halver": (51.1861, 7.4982),
    "Hameln": (52.104, 9.3562),
    "Hamm": (51.6803, 7.8209),
    "Hammelburg": (50.1163, 9.8914),
    "Hamminkeln": (51.7326, 6.5903),
    "Hanau": (50.1342, 8.9142),
    "Hann. Münden": (51.4151, 9.6505),
    "Harburg": (48.7867, 10.6893),
    "Hardegsen": (51.6523, 9.8305),
    "Haren (Ems)": (52.7931, 7.2413),
    "Harsewinkel": (51.9622, 8.2277),
    "Hartenstein": (50.6624, 12.6697),
    "Hartha": (51.0986, 12.9739),
    "Harzgerode": (51.6419, 11.1433),
    "Haselünne": (52.6731, 7.4867),
    "Haslach im Kinzigtal": (48.2777, 8.0894),
    "Haßfurt": (50.0352, 10.5156),
    "Hattingen": (51.3989, 7.1856),
    "Hatzfeld (Eder)": (50.9933, 8.5457),
    "Hausach": (48.2843, 8.176),
    "Hauzenberg": (48.6496, 13.6265),
    "Havelberg": (52.8309, 12.0755),
    "Hayingen": (48.2753, 9.4776),
    "Hechingen": (48.3515, 8.9632),
    "Hecklingen": (51.8471, 11.5342),
    "Heide": (54.1956, 9.0974),
    "Heideck": (49.1337, 11.1273),
    "Heidelberg": (49.4077, 8.6908),
    "Heidenau": (50.9722, 13.8674),
    "Heidenheim an der Brenz": (48.678, 10.1516),
    "Heilbad Heiligenstadt": (51.3782, 10.1374),
    "Heilbronn": (49.1399, 9.2205),
    "Heiligenhafen": (54.3704, 10.9763),
    "Heiligenhaus": (51.3266, 6.9711),
    "Heilsbronn": (49.3357, 10.7874),
    "Heimbach": (50.6369, 6.469),
    "Heimsheim": (48.8066, 8.8674),
    "Heinsberg": (51.0636, 6.0998),
    "Heitersheim": (47.8747, 7.6572),
    "Heldburg": (50.2792, 10.7245),
    "Helmbrechts": (50.2356, 11.7159),
    "Helmstedt": (52.2279, 11.0099),
    "Hemau": (49.054, 11.782),
    "Hemer": (51.3871, 7.7702),
    "Hemmingen": (52.3143, 9.7236),
    "Hemmoor": (53.6884, 9.1524),
    "Hemsbach": (49.5907, 8.6478),
    "Hennef (Sieg)": (50.7756, 7.2831),
    "Hennigsdorf": (52.636, 13.2042),
    "Heppenheim": (49.6414, 8.6321),
    "Herbolzheim": (48.2188, 7.7775),
    "Herborn": (50.6814, 8.3037),
    "Herbrechtingen": (48.6217, 10.176),
    "Herbstein": (50.5611, 9.3459),
    "Herdecke": (51.4, 7.4358),
    "Herdorf": (50.777, 7.9537),
    "Herford": (52.1146, 8.6734),
    "Heringen/Helme": (51.447, 10.8761),
    "Heringen": (50.888, 10.0072),
    "Hermeskeil": (49.6553, 6.9441),
    "Hermsdorf": (50.8969, 11.8555),
    "Herne": (51.5388, 7.2257),
    "Herrenberg": (48.5952, 8.8665),
    "Herrieden": (49.2378, 10.5035),
    "Herrnhut": (51.0162, 14.7438),
    "Hersbruck": (49.5108, 11.4315),
    "Herten": (51.5964, 7.1439),
    "Herzberg am Harz": (51.6555, 10.3394),
    "Herzberg (Elster)": (51.6869, 13.2202),
    "Herzogenaurach": (49.568, 10.8857),
    "Herzogenrath": (50.8687, 6.0932),
    "Hessisch Lichtenau": (51.1995, 9.7186),
    "Hessisch Oldendorf": (52.1727, 9.2491),
    "Hettingen": (48.216, 9.2317),
    "Hettstedt": (51.6503, 11.5115),
    "Heubach": (48.7927, 9.9337),
    "Heusenstamm": (50.0555, 8.8008),
    "Hilchenbach": (50.9969, 8.1106),
    "Hildburghausen": (50.4255, 10.7318),
    "Hilden": (51.1682, 6.9309),
    "Hildesheim": (52.1508, 9.9511),
    "Hillesheim": (50.2918, 6.6696),
    "Hilpoltstein": (49.1905, 11.1906),
    "Hirschau": (49.544, 11.9462),
    "Hirschberg": (50.4054, 11.8183),
    "Hirschhorn (Neckar)": (49.4457, 8.8959),
    "Hitzacker": (53.1525, 11.0442),
    "Hochheim am Main": (50.0144, 8.3522),
    "Höchstadt an der Aisch": (49.7062, 10.8133),
    "Höchstädt an der Donau": (48.6112, 10.5682),
    "Hockenheim": (49.3233, 8.5519),
    "Hof": (50.313, 11.9126),
    "Hofgeismar": (51.4961, 9.385),
    "Hofheim am Taunus": (50.0902, 8.4493),
    "Hofheim": (47.7153, 11.215),
    "Hohenberg an der Eger": (50.095, 12.2201),
    "Hohenleuben": (50.7113, 12.0543),
    "Hohenmölsen": (51.1577, 12.1),
    "Hohen Neuendorf": (52.6774, 13.2789),
    "Hohenstein-Ernstthal": (50.8006, 12.7129),
    "Hohnstein": (50.9799, 14.1141),
    "Höhr-Grenzhausen": (50.4347, 7.669),
    "Hollfeld": (49.9379, 11.2915),
    "Holzgerlingen": (48.6397, 9.0115),
    "Holzminden": (51.828, 9.4455),
    "Homberg (Efze)": (51.0299, 9.4026),
    "Homberg (Ohm)": (51.0299, 9.4026),
    "Homburg (Saar)": (49.3264, 7.3387),
    "Horb am Neckar": (48.4442, 8.6913),
    "Hornbach": (49.1878, 7.3688),
    "Horn-Bad Meinberg": (51.8855, 8.9624),
    "Hornberg": (48.2107, 8.2327),
    "Hörstel": (52.2976, 7.5838),
    "Horstmar": (52.081, 7.3054),
    "Höxter": (51.775, 9.3816),
    "Hoya": (52.8086, 9.1404),
    "Hoyerswerda": (51.4379, 14.2355),
    "Hückelhoven": (51.0555, 6.2266),
    "Hückeswagen": (51.1498, 7.3447),
    "Hüfingen": (47.9254, 8.4883),
    "Hünfeld": (50.6797, 9.7673),
    "Hungen": (50.4737, 8.8933),
    "Hürth": (50.8708, 6.8676),
    "Husum": (54.4858, 9.0524),
    "Ibbenbüren": (52.2796, 7.7146),
    "Ichenhausen": (48.3712, 10.3071),
    "Idar-Oberstein": (49.7144, 7.3078),
    "Idstein": (50.2177, 8.2668),
    "Illertissen": (48.2234, 10.1035),
    "Ilmenau": (50.6832, 10.9186),
    "Ilsenburg": (51.867, 10.6782),
    "Ilshofen": (49.1701, 9.9183),
    "Immenhausen": (51.4276, 9.4802),
    "Immenstadt im Allgäu": (47.56, 10.2139),
    "Ingelfingen": (49.3003, 9.653),
    "Ingelheim am Rhein": (49.9708, 8.0588),
    "Ingolstadt": (48.7651, 11.4237),
    "Iphofen": (49.7024, 10.2604),
    "Iserlohn": (51.3755, 7.7028),
    "Isny im Allgäu": (47.6926, 10.0386),
    "Isselburg": (51.8323, 6.4643),
    "Itzehoe": (53.921, 9.5153),
    "Jarmen": (53.9239, 13.3403),
    "Jena": (50.9288, 11.5899),
    "Jerichow": (52.5005, 12.0238),
    "Jessen (Elster)": (51.7934, 12.9576),
    "Jever": (53.5734, 7.9004),
    "Joachimsthal": (52.9794, 13.7449),
    "Johanngeorgenstadt": (50.4325, 12.7114),
    "Jöhstadt": (50.5123, 13.0946),
    "Jüchen": (51.1, 6.5),
    "Jülich": (50.9215, 6.3627),
    "Jüterbog": (51.9961, 13.0798),
    "Kaarst": (51.2293, 6.6188),
    "Kahla": (50.8065, 11.5852),
    "Kaisersesch": (50.2315, 7.1386),
    "Kaiserslautern": (49.443, 7.7716),
    "Kalbe (Milde)": (52.6567, 11.3865),
    "Kalkar": (51.7391, 6.291),
    "Kaltenkirchen": (53.8324, 9.9604),
    "Kaltennordheim": (50.6265, 10.1592),
    "Kamen": (51.5923, 7.6638),
    "Kamenz": (51.268, 14.0937),
    "Kamp-Lintfort": (51.5047, 6.5459),
    "Kandel": (49.0828, 8.1972),
    "Kandern": (47.7139, 7.6624),
    "Kappeln": (54.6612, 9.9313),
    "Karben": (50.2302, 8.7715),
    "Karlsruhe": (49.0094, 8.4044),
    "Karlstadt": (49.9603, 9.7724),
    "Kassel": (51.3167, 9.5),
    "Kastellaun": (50.0692, 7.4415),
    "Katzenelnbogen": (50.2674, 7.9732),
    "Kaub": (50.0883, 7.7607),
    "Kaufbeuren": (47.8824, 10.6219),
    "Kehl": (48.573, 7.8152),
    "Kelbra (Kyffhäuser)": (51.4353, 11.0414),
    "Kelheim": (48.9173, 11.8862),
    "Kelkheim (Taunus)": (50.137, 8.4502),
    "Kellinghusen": (53.952, 9.7196),
    "Kelsterbach": (50.0613, 8.5292),
    "Kemberg": (51.7719, 12.6323),
    "Kemnath": (49.8701, 11.8908),
    "Kempen": (51.3643, 6.4186),
    "Kenzingen": (48.1963, 7.7697),
    "Kerpen": (50.8699, 6.6969),
    "Ketzin/Havel": (52.4781, 12.8453),
    "Kevelaer": (51.5824, 6.246),
    "Kiel": (54.3213, 10.1349),
    "Kierspe": (51.134, 7.5907),
    "Kirchberg": (50.6219, 12.5245),
    "Kirchberg an der Jagst": (49.2006, 9.9823),
    "Kirchberg (Hunsrück)": (49.944, 7.407),
    "Kirchen (Sieg)": (50.8085, 7.8863),
    "Kirchenlamitz": (50.1519, 11.9483),
    "Kirchhain": (50.8272, 8.9281),
    "Kirchheimbolanden": (49.6625, 8.0151),
    "Kirchheim unter Teck": (48.6468, 9.4538),
    "Kirn": (49.7891, 7.4577),
    "Kirtorf": (50.7694, 9.1039),
    "Kitzingen": (49.7397, 10.1507),
    "Kitzscher": (51.1644, 12.5526),
    "Kleve": (51.7883, 6.1387),
    "Klingenberg am Main": (49.7851, 9.1802),
    "Klötze": (52.6279, 11.1675),
    "Klütz": (53.9649, 11.1639),
    "Knittlingen": (49.0249, 8.7561),
    "Koblenz": (50.3536, 7.5788),
    "Kolbermoor": (47.8496, 12.067),
    "Kölleda": (51.1874, 11.2449),
    "Königsberg in Bayern": (50.0808, 10.5676),
    "Königsbrück": (51.2645, 13.9054),
    "Königsbrunn": (48.2751, 10.8918),
    "Königsee": (50.6614, 11.0975),
    "Königslutter": (52.2512, 10.8168),
    "Königstein im Taunus": (50.1794, 8.4713),
    "Königstein (Sächsische Schweiz)": (50.9157, 14.0719),
    "Königswinter": (50.6773, 7.1925),
    "Königs Wusterhausen": (52.3014, 13.633),
    "Könnern": (51.6712, 11.7707),
    "Konstanz": (47.6603, 9.1758),
    "Konz": (49.7004, 6.5765),
    "Korbach": (51.2756, 8.873),
    "Korntal-Münchingen": (48.8322, 9.1214),
    "Kornwestheim": (48.8616, 9.1857),
    "Korschenbroich": (51.1914, 6.5135),
    "Köthen": (51.7518, 11.9709),
    "Kraichtal": (49.1462, 8.7328),
    "Krakow am See": (53.6514, 12.2675),
    "Kranichfeld": (50.8545, 11.2006),
    "Krautheim": (49.3879, 9.6355),
    "Krefeld": (51.3364, 6.5538),
    "Kremmen": (52.7622, 13.0252),
    "Krempe": (53.8373, 9.4902),
    "Kreuztal": (50.9678, 7.9885),
    "Kronach": (50.2396, 11.3331),
    "Kronberg im Taunus": (50.181, 8.513),
    "Kröpelin": (54.0712, 11.795),
    "Kroppenstedt": (51.9421, 11.3084),
    "Krumbach": (48.2418, 10.3632),
    "Kühlungsborn": (54.1476, 11.7432),
    "Kulmbach": (50.1007, 11.4503),
    "Külsheim": (49.6694, 9.5236),
    "Künzelsau": (49.2818, 9.6835),
    "Kupferberg": (50.1396, 11.5776),
    "Kuppenheim": (48.8279, 8.2542),
    "Kusel": (49.5377, 7.4047),
    "Kyllburg": (50.0386, 6.5948),
    "Kyritz": (52.9421, 12.397),
    "Laage": (53.9263, 12.3494),
    "Laatzen": (52.3151, 9.7974),
    "Ladenburg": (49.4731, 8.609),
    "Lage": (51.9922, 8.793),
    "Lahnstein": (50.3, 7.6167),
    "Lahr/Schwarzwald": (48.3404, 7.8689),
    "Laichingen": (48.4894, 9.6861),
    "Lambrecht (Pfalz)": (49.3706, 8.0726),
    "Lampertheim": (49.5979, 8.4725),
    "Landau an der Isar": (48.6725, 12.6932),
    "Landau in der Pfalz": (49.1984, 8.1169),
    "Landsberg am Lech": (48.0482, 10.8828),
    "Landsberg": (51.527, 12.1608),
    "Landshut": (48.5296, 12.1618),
    "Landstuhl": (49.4131, 7.5702),
    "Langelsheim": (51.9379, 10.3326),
    "Langen (Hesse)": (49.9896, 8.6685),
    "Langenau": (48.4962, 10.1185),
    "Langenburg": (49.254, 9.8567),
    "Langenfeld (Rhineland)": (51.1082, 6.9483),
    "Langenhagen": (52.4476, 9.7374),
    "Langenselbold": (50.1766, 9.04),
    "Langenzenn": (49.4946, 10.7923),
    "Lassan": (53.9483, 13.8497),
    "Laubach": (50.542, 8.9903),
    "Laucha an der Unstrut": (51.2242, 11.6799),
    "Lauchhammer": (51.4881, 13.7662),
    "Lauchheim": (48.8713, 10.2422),
    "Lauda-Königshofen": (49.5653, 9.7082),
    "Lauenburg": (53.372, 10.5565),
    "Lauf an der Pegnitz": (49.5139, 11.2825),
    "Laufen": (47.9357, 12.9286),
    "Laufenburg": (47.5651, 8.0604),
    "Lauffen am Neckar": (49.0734, 9.1457),
    "Lauingen (Donau)": (48.5677, 10.4271),
    "Laupheim": (48.2279, 9.8787),
    "Lauscha": (50.4769, 11.1596),
    "Lauta": (51.4542, 14.1049),
    "Lauter-Bernsbach": (50.5626, 12.7351),
    "Lauterbach (Hesse)": (50.6356, 9.3978),
    "Lauterecken": (49.6499, 7.5926),
    "Lebach": (49.4112, 6.9099),
    "Lebus": (52.4272, 14.5323),
    "Leer": (53.2316, 7.461),
    "Lehesten": (50.9833, 11.5833),
    "Lehrte": (52.3719, 9.9792),
    "Leichlingen": (51.1063, 7.0187),
    "Leimen": (49.3474, 8.6873),
    "Leinefelde-Worbis": (51.388, 10.3262),
    "Leinfelden-Echterdingen": (48.6941, 9.1681),
    "Leingarten": (49.1464, 9.1169),
    "Leipheim": (48.45, 10.2228),
    "Leisnig": (51.1574, 12.9279),
    "Lemgo": (52.0279, 8.899),
    "Lengenfeld": (50.5694, 12.3641),
    "Lengerich": (52.1866, 7.8604),
    "Lennestadt": (51.1172, 8.0671),
    "Lenzen": (53.0918, 11.4745),
    "Leonberg": (48.8, 9.0167),
    "Leun": (50.5513, 8.3584),
    "Leuna": (51.3178, 12.0159),
    "Leutenberg": (50.5635, 11.4562),
    "Leutershausen": (49.2987, 10.4119),
    "Leutkirch im Allgäu": (47.8267, 10.0205),
    "Leverkusen": (51.0303, 6.9843),
    "Lich": (50.5209, 8.8157),
    "Lichtenau (Baden-Württemberg)": (48.7261, 8.0049),
    "Lichtenau (North Rhine-Westphalia)": (51.6171, 8.8966),
    "Lichtenberg": (50.3834, 11.6762),
    "Lichtenfels (Bavaria)": (50.1457, 11.0593),
    "Liebenau": (51.497, 9.2821),
    "Liebenwalde": (52.8713, 13.3947),
    "Lieberose": (51.9849, 14.2999),
    "Liebstadt": (50.8642, 13.8569),
    "Limbach-Oberfrohna": (50.8588, 12.7616),
    "Limburg an der Lahn": (50.3836, 8.0503),
    "Lindau (Bodensee)": (47.5461, 9.6843),
    "Linden": (50.5278, 8.6768),
    "Lindenberg im Allgäu": (47.6028, 9.8855),
    "Lindenfels": (49.6837, 8.7815),
    "Lindow (Mark)": (52.9669, 12.985),
    "Lingen": (52.5227, 7.3255),
    "Linnich": (50.98, 6.2705),
    "Linz am Rhein": (50.5688, 7.2844),
    "Lippstadt": (51.6737, 8.3448),
    "Löbau": (51.0995, 14.6674),
    "Löffingen": (47.8841, 8.3438),
    "Lohmar": (50.8387, 7.214),
    "Lohne (Lower Saxony)": (52.6656, 8.2383),
    "Löhne": (52.1885, 8.6922),
    "Lohr am Main": (49.9892, 9.5722),
    "Loitz": (53.4419, 13.3884),
    "Lollar": (50.6465, 8.705),
    "Lommatzsch": (51.1954, 13.3092),
    "Löningen": (52.7364, 7.7574),
    "Lorch (Baden-Württemberg)": (48.7983, 9.6914),
    "Lorch (Hesse)": (50.0462, 7.8042),
    "Lörrach": (47.615, 7.6646),
    "Lorsch": (49.65, 8.5667),
    "Lößnitz": (50.6218, 12.7315),
    "Löwenstein": (49.0956, 9.38),
    "Lübbecke": (52.307, 8.6142),
    "Lübben (Spreewald)": (51.9381, 13.8883),
    "Lübbenau": (51.8622, 13.9517),
    "Lübeck": (53.8689, 10.6873),
    "Lübtheen": (53.3008, 11.0827),
    "Lübz": (53.4626, 12.0292),
    "Lüchow (Wendland)": (52.9674, 11.158),
    "Lucka": (51.0973, 12.3334),
    "Luckau": (51.8524, 13.7073),
    "Luckenwalde": (52.0903, 13.1677),
    "Lüdenscheid": (51.2198, 7.6273),
    "Lüdinghausen": (51.7683, 7.4438),
    "Ludwigsburg": (48.8973, 9.1916),
    "Ludwigsfelde": (52.3032, 13.254),
    "Ludwigshafen am Rhein": (49.4812, 8.4464),
    "Ludwigslust": (53.3245, 11.4971),
    "Ludwigsstadt": (50.486, 11.3873),
    "Lügde": (51.9583, 9.2471),
    "Lüneburg": (53.2512, 10.4155),
    "Lünen": (51.6163, 7.5287),
    "Lunzenau": (50.9627, 12.7559),
    "Lütjenburg": (54.2919, 10.5894),
    "Lützen": (51.2567, 12.1416),
    "Lychen": (53.2109, 13.3156),
    "Magdala": (50.907, 11.448),
    "Magdeburg": (52.1313, 11.6319),
    "Mahlberg": (48.2864, 7.8141),
    "Mainbernheim": (49.7079, 10.219),
    "Mainburg": (48.6418, 11.7809),
    "Maintal": (50.15, 8.8333),
    "Mainz": (49.9819, 8.2801),
    "Malchin": (53.7384, 12.7688),
    "Malchow": (53.4748, 12.4221),
    "Mannheim": (49.4891, 8.4669),
    "Manderscheid": (50.0967, 6.8098),
    "Mansfeld": (51.5923, 11.4522),
    "Marbach am Neckar": (48.9396, 9.2599),
    "Marburg": (50.809, 8.7707),
    "Marienberg": (50.6505, 13.1612),
    "Markdorf": (47.7192, 9.3903),
    "Markgröningen": (48.9049, 9.0806),
    "Märkisch Buchholz": (52.1096, 13.7654),
    "Markkleeberg": (51.2755, 12.3691),
    "Markneukirchen": (50.3114, 12.3295),
    "Markranstädt": (51.3015, 12.2202),
    "Marktbreit": (49.6654, 10.1481),
    "Marktheidenfeld": (49.8454, 9.6036),
    "Marktleuthen": (50.1301, 12.0023),
    "Marktoberdorf": (47.7796, 10.6171),
    "Marktredwitz": (50.0044, 12.0859),
    "Marktsteft": (49.6961, 10.1363),
    "Marl": (51.6567, 7.0904),
    "Marlow": (54.1544, 12.5726),
    "Marne": (54.3417, 8.7599),
    "Marsberg": (51.4617, 8.8495),
    "Maulbronn": (48.9996, 8.8034),
    "Maxhütte-Haidhof": (49.1996, 12.0923),
    "Mayen": (50.328, 7.2228),
    "Mechernich": (50.593, 6.6522),
    "Meckenheim": (50.6239, 7.0294),
    "Medebach": (51.1971, 8.7064),
    "Meerane": (50.8469, 12.4647),
    "Meerbusch": (51.2527, 6.6881),
    "Meersburg": (47.6942, 9.2711),
    "Meinerzhagen": (51.1074, 7.6484),
    "Meiningen": (50.5679, 10.4152),
    "Meisenheim": (49.7072, 7.6677),
    "Meissen": (51.1616, 13.4737),
    "Meldorf": (54.0918, 9.0687),
    "Melle": (52.202, 8.3383),
    "Mellrichstadt": (50.4285, 10.3033),
    "Melsungen": (51.1303, 9.5524),
    "Memmingen": (47.9837, 10.1853),
    "Menden (Sauerland)": (51.4434, 7.7782),
    "Mendig": (50.3667, 7.2833),
    "Mengen": (48.0495, 9.33),
    "Meppen": (52.6906, 7.291),
    "Merkendorf": (49.2036, 10.7042),
    "Merseburg": (51.3548, 11.9892),
    "Merzig": (49.4433, 6.6387),
    "Meschede": (51.3502, 8.2833),
    "Meßkirch": (47.9946, 9.1148),
    "Meßstetten": (48.1832, 8.9657),
    "Mettmann": (51.2504, 6.9754),
    "Metzingen": (48.5369, 9.2833),
    "Meuselwitz": (51.0431, 12.2994),
    "Meyenburg": (53.0452, 14.2369),
    "Michelstadt": (49.6757, 9.0037),
    "Miesbach": (47.789, 11.8338),
    "Miltenberg": (49.7045, 9.2673),
    "Mindelheim": (48.0458, 10.4922),
    "Minden": (52.2895, 8.9146),
    "Mirow": (53.5043, 11.5016),
    "Mittenwalde": (52.2601, 13.5395),
    "Mitterteich": (49.9514, 12.2421),
    "Mittweida": (50.9862, 12.9754),
    "Möckern": (52.141, 11.952),
    "Möckmühl": (49.3249, 9.3584),
    "Moers": (51.4534, 6.6326),
    "Mölln": (53.6207, 10.6875),
    "Mönchengladbach": (51.1854, 6.4417),
    "Monheim am Rhein": (51.0916, 6.8922),
    "Monheim": (48.8439, 10.8583),
    "Monschau": (50.5546, 6.24),
    "Montabaur": (50.4359, 7.8232),
    "Moosburg": (48.4709, 11.9381),
    "Mörfelden-Walldorf": (49.9947, 8.5836),
    "Moringen": (51.6992, 9.8711),
    "Mosbach": (49.3536, 9.1511),
    "Mössingen": (48.4057, 9.0542),
    "Mücheln (Geiseltal)": (51.2969, 11.8076),
    "Mügeln": (51.2362, 13.0457),
    "Mühlacker": (48.9475, 8.8368),
    "Mühlberg (Elbe)": (51.4345, 13.2218),
    "Mühldorf": (48.2467, 12.5215),
    "Mühlhausen": (51.209, 10.4527),
    "Mühlheim am Main": (50.1167, 8.8333),
    "Mühlheim an der Donau": (48.0312, 8.8836),
    "Mülheim an der Ruhr": (51.4322, 6.8797),
    "Mülheim-Kärlich": (50.3851, 7.4989),
    "Müllheim": (47.8082, 7.6303),
    "Müllrose": (52.2474, 14.4179),
    "Münchberg": (50.1895, 11.7882),
    "Müncheberg": (52.507, 14.1372),
    "Münchenbernsdorf": (50.8211, 11.9323),
    "Munderkingen": (48.2357, 9.644),
    "Münnerstadt": (50.2464, 10.2019),
    "Münsingen": (48.4113, 9.497),
    "Munster (Lower Saxony)": (52.9854, 10.0899),
    "Münster": (51.9624, 7.6257),
    "Münstermaifeld": (50.2464, 7.3621),
    "Münzenberg": (50.4535, 8.7743),
    "Murrhardt": (48.9819, 9.5705),
    "Nabburg": (49.4535, 12.18),
    "Nagold": (48.5498, 8.7237),
    "Naila": (50.3303, 11.7046),
    "Nassau": (50.3145, 7.8003),
    "Nastätten": (50.1988, 7.8589),
    "Nauen": (52.607, 12.8737),
    "Naumburg (Hesse)": (51.2482, 9.1657),
    "Naumburg": (51.1499, 11.8098),
    "Naunhof": (51.2777, 12.5883),
    "Nebra": (51.2881, 11.5775),
    "Neckarbischofsheim": (49.2963, 8.9638),
    "Neckargemünd": (49.389, 8.7959),
    "Neckarsteinach": (49.4074, 8.8434),
    "Neckarsulm": (49.1891, 9.2253),
    "Neresheim": (48.7551, 10.3304),
    "Netphen": (50.9167, 8.1),
    "Nettetal": (51.3167, 6.2833),
    "Netzschkau": (50.6141, 12.2438),
    "Neu-Anspach": (50.3167, 8.5),
    "Neubrandenburg": (53.5573, 13.261),
    "Neubukow": (54.0323, 11.6733),
    "Neubulach": (48.6609, 8.6961),
    "Neuburg an der Donau": (48.7322, 11.1871),
    "Neudenau": (49.2918, 9.2698),
    "Neuenbürg": (48.8452, 8.5957),
    "Neuenburg am Rhein": (47.8143, 7.5601),
    "Neuenhaus": (52.4973, 6.9666),
    "Neuenrade": (51.2828, 7.7825),
    "Neuenstadt am Kocher": (49.235, 9.3322),
    "Neuenstein": (49.2049, 9.58),
    "Neuerburg": (50.001, 6.9483),
    "Neuffen": (48.5546, 9.3755),
    "Neuhaus am Rennweg": (50.5101, 11.1379),
    "Neu-Isenburg": (50.0483, 8.6941),
    "Neukalen": (53.8227, 12.7902),
    "Neukirchen": (50.8691, 9.3466),
    "Neukirchen-Vluyn": (51.4466, 6.5519),
    "Neukloster": (53.8666, 11.6878),
    "Neumark": (51.0797, 11.247),
    "Neumarkt in der Oberpfalz": (49.2803, 11.4628),
    "Neumarkt-Sankt Veit": (48.3605, 12.5072),
    "Neumünster": (54.074, 9.9846),
    "Neunburg vorm Wald": (49.3478, 12.3862),
    "Neunkirchen": (49.3445, 7.1805),
    "Neuötting": (48.241, 12.69),
    "Neuruppin": (52.9282, 12.8031),
    "Neusalza-Spremberg": (51.0395, 14.5356),
    "Neusäß": (48.3925, 10.8333),
    "Neuss": (51.1981, 6.685),
    "Neustadt an der Aisch": (49.5795, 10.6113),
    "Neustadt an der Donau": (48.807, 11.7695),
    "Neustadt an der Waldnaab": (49.7329, 12.1777),
    "Neustadt am Kulm": (49.8264, 11.8391),
    "Neustadt am Rübenberge": (52.5046, 9.4587),
    "Neustadt an der Orla": (50.7364, 11.7462),
    "Neustadt an der Weinstraße": (49.3501, 8.1389),
    "Neustadt bei Coburg": (50.3297, 11.1206),
    "Neustadt (Dosse)": (53.1667, 12.7667),
    "Neustadt-Glewe": (53.3785, 11.5926),
    "Neustadt (Hesse)": (49.8178, 9.0342),
    "Neustadt in Holstein": (54.1071, 10.8145),
    "Neustadt in Sachsen": (51.0284, 14.2179),
    "Neustrelitz": (53.3602, 13.0726),
    "Neutraubling": (48.9874, 12.201),
    "Neu-Ulm": (48.3928, 10.0111),
    "Neuwied": (50.4336, 7.4706),
    "Nidda": (50.4133, 9.0064),
    "Nidderau": (50.2381, 8.867),
    "Nideggen": (50.6927, 6.4844),
    "Niebüll": (54.7866, 8.8285),
    "Niedenstein": (51.2334, 9.3103),
    "Niederkassel": (50.815, 7.0378),
    "Niedernhall": (49.2952, 9.616),
    "Nieder-Olm": (49.9117, 8.2053),
    "Niederstetten": (49.4, 9.9194),
    "Niederstotzingen": (48.5413, 10.235),
    "Nieheim": (51.805, 9.113),
    "Niemegk": (52.0739, 12.6895),
    "Nienburg (Saale)": (51.8375, 11.7698),
    "Nienburg": (52.6444, 9.2166),
    "Nierstein": (49.87, 8.3365),
    "Niesky": (51.2924, 14.8211),
    "Nittenau": (49.1942, 12.2674),
    "Norden": (53.5955, 7.2062),
    "Nordenham": (53.501, 8.4896),
    "Norderney": (53.708, 7.1572),
    "Norderstedt": (53.7018, 9.9933),
    "Nordhausen": (51.5018, 10.7957),
    "Nordhorn": (52.4308, 7.0683),
    "Nördlingen": (48.8512, 10.4887),
    "Northeim": (51.7066, 10.0),
    "Nortorf": (54.1674, 9.8544),
    "Nossen": (51.058, 13.2965),
    "Nürtingen": (48.6257, 9.342),
    "Oberasbach": (49.4228, 10.9577),
    "Oberderdingen": (49.0656, 8.8031),
    "Oberhausen": (51.4781, 6.8625),
    "Oberhof": (50.7043, 10.7272),
    "Oberkirch": (48.5324, 8.0786),
    "Oberkochen": (48.7838, 10.1052),
    "Oberlungwitz": (50.7823, 12.7079),
    "Obermoschel": (49.728, 7.7727),
    "Obernburg am Main": (49.8358, 9.131),
    "Obernkirchen": (52.2721, 9.1291),
    "Ober-Ramstadt": (49.8308, 8.7489),
    "Oberriexingen": (48.9265, 9.027),
    "Obertshausen": (50.0714, 8.8512),
    "Oberursel (Taunus)": (50.2073, 8.5775),
    "Oberviechtach": (49.4581, 12.4167),
    "Oberwesel": (50.1078, 7.7252),
    "Oberzent": (49.5677, 8.9737),
    "Ochsenfurt": (49.6643, 10.0623),
    "Ochsenhausen": (48.0703, 9.9503),
    "Ochtrup": (52.208, 7.1899),
    "Oderberg": (52.8657, 14.0451),
    "Oederan": (50.8606, 13.1716),
    "Oelde": (51.8289, 8.1472),
    "Oelsnitz": (50.4147, 12.1695),
    "Oer-Erkenschwick": (51.642, 7.2645),
    "Oerlinghausen": (51.9545, 8.6622),
    "Oettingen in Bayern": (48.9527, 10.6046),
    "Offenbach am Main": (50.1006, 8.7665),
    "Offenburg": (48.4738, 7.945),
    "Ohrdruf": (50.8333, 10.8167),
    "Öhringen": (49.1988, 9.5072),
    "Olbernhau": (50.6587, 13.3425),
    "Olching": (48.2, 11.3333),
    "Oldenburg": (53.1404, 8.2148),
    "Oldenburg in Holstein": (54.295, 10.8904),
    "Olfen": (51.7079, 7.3789),
    "Olpe": (51.029, 7.8514),
    "Olsberg": (51.3561, 8.489),
    "Oppenau": (48.4733, 8.1597),
    "Oppenheim": (49.8547, 8.3597),
    "Oranienburg": (52.748, 13.2519),
    "Orlamünde": (50.7749, 11.5193),
    "Ornbau": (49.1762, 10.658),
    "Ortenberg": (50.3558, 9.056),
    "Ortrand": (51.3751, 13.7598),
    "Oschatz": (51.3, 13.1098),
    "Oschersleben (Bode)": (52.0304, 11.229),
    "Osnabrück": (52.2726, 8.0498),
    "Osterburg (Altmark)": (52.787, 11.7543),
    "Osterburken": (49.43, 9.4225),
    "Osterfeld": (51.0801, 11.9305),
    "Osterhofen": (48.7, 13.0222),
    "Osterholz-Scharmbeck": (53.2266, 8.7923),
    "Osterode am Harz": (51.7269, 10.2509),
    "Osterwieck": (51.9699, 10.7104),
    "Ostfildern": (48.727, 9.2495),
    "Ostheim vor der Rhön": (50.46, 10.2306),
    "Osthofen": (49.7038, 8.3242),
    "Östringen": (49.2191, 8.7119),
    "Ostritz": (51.0145, 14.9306),
    "Otterberg": (49.503, 7.7699),
    "Otterndorf": (53.8116, 8.9021),
    "Ottweiler": (49.4013, 7.1642),
    "Overath": (50.9327, 7.2839),
    "Owen": (48.5874, 9.4498),
    "Paderborn": (51.7191, 8.7544),
    "Papenburg": (53.0777, 7.4152),
    "Pappenheim": (48.9338, 10.9743),
    "Parchim": (53.4263, 11.8488),
    "Parsberg": (49.1607, 11.7183),
    "Pasewalk": (53.5063, 13.99),
    "Passau": (48.5665, 13.4312),
    "Pattensen": (52.2645, 9.7644),
    "Pegau": (51.1671, 12.2514),
    "Pegnitz": (49.7522, 11.5419),
    "Peine": (52.3193, 10.2352),
    "Peitz": (51.8584, 14.4114),
    "Penig": (50.9334, 12.7042),
    "Penkun": (53.2985, 14.237),
    "Penzberg": (47.7529, 11.377),
    "Penzlin": (53.504, 13.0854),
    "Perleberg": (53.0752, 11.8574),
    "Petershagen": (52.3751, 8.9654),
    "Pfaffenhofen an der Ilm": (48.5305, 11.505),
    "Pfarrkirchen": (48.432, 12.9381),
    "Pforzheim": (48.8844, 8.6989),
    "Pfreimd": (49.4911, 12.1807),
    "Pfullendorf": (47.9261, 9.2578),
    "Pfullingen": (48.4646, 9.228),
    "Pfungstadt": (49.8056, 8.6031),
    "Philippsburg": (49.2317, 8.4607),
    "Pinneberg": (53.6589, 9.797),
    "Pirmasens": (49.2015, 7.6053),
    "Pirna": (50.9584, 13.937),
    "Plattling": (48.7787, 12.8751),
    "Plau am See": (53.4582, 12.2625),
    "Plaue": (50.7784, 10.8997),
    "Plauen": (50.4973, 12.1378),
    "Plettenberg": (51.2095, 7.8726),
    "Pleystein": (49.6491, 12.4063),
    "Plochingen": (48.7107, 9.4195),
    "Plön": (54.162, 10.4276),
    "Pocking": (48.4015, 13.3132),
    "Polch": (50.2997, 7.3132),
    "Porta Westfalica": (52.2296, 8.9161),
    "Pößneck": (50.6936, 11.5923),
    "Potsdam": (52.3989, 13.0657),
    "Pottenstein": (49.7713, 11.4078),
    "Preetz": (54.2358, 10.2793),
    "Premnitz": (52.5318, 12.3484),
    "Prenzlau": (53.317, 13.864),
    "Pressath": (49.7686, 11.9397),
    "Preußisch Oldendorf": (52.3059, 8.4934),
    "Prichsenstadt": (49.8193, 10.3477),
    "Pritzwalk": (53.1495, 12.174),
    "Prüm": (50.2079, 6.4202),
    "Puchheim": (48.15, 11.35),
    "Pulheim": (50.9997, 6.8063),
    "Pulsnitz": (51.1832, 14.0142),
    "Putbus": (54.3508, 13.4817),
    "Putlitz": (53.249, 12.0418),
    "Püttlingen": (49.2855, 6.8872),
    "Quakenbrück": (52.674, 7.949),
    "Quedlinburg": (51.7884, 11.1501),
    "Querfurt": (51.3812, 11.6005),
    "Quickborn": (53.7282, 9.9108),
    "Rabenau": (50.9648, 13.6431),
    "Radeberg": (51.1111, 13.912),
    "Radebeul": (51.1065, 13.6605),
    "Radeburg": (51.2152, 13.7281),
    "Radevormwald": (51.2022, 7.3603),
    "Radolfzell am Bodensee": (47.7419, 8.971),
    "Rahden": (52.4342, 8.6127),
    "Rain": (48.6903, 10.9161),
    "Ramstein-Miesenbach": (49.4445, 7.5553),
    "Ranis": (50.6613, 11.5691),
    "Ransbach-Baumbach": (50.465, 7.7283),
    "Rastatt": (48.8585, 8.2096),
    "Rastenberg": (51.175, 11.4203),
    "Rathenow": (52.6066, 12.337),
    "Ratingen": (51.2972, 6.8493),
    "Ratzeburg": (53.6996, 10.7726),
    "Rauenberg": (49.2694, 8.7034),
    "Raunheim": (50.0132, 8.4525),
    "Rauschenberg": (50.8833, 8.9186),
    "Ravensburg": (47.782, 9.6106),
    "Recklinghausen": (51.6138, 7.1974),
    "Rees": (51.7626, 6.3978),
    "Regen": (48.9719, 13.1282),
    "Regensburg": (49.0151, 12.1016),
    "Regis-Breitingen": (51.0888, 12.4384),
    "Rehau": (50.2492, 12.0342),
    "Rehburg-Loccum": (52.4695, 9.1996),
    "Rehna": (53.7799, 11.0521),
    "Reichelsheim (Wetterau)": (49.7121, 8.839),
    "Reichenbach (Vogtland)": (50.6228, 12.3034),
    "Reichenbach (Oberlausitz)": (51.1414, 14.8027),
    "Reinbek": (53.5177, 10.2486),
    "Reinheim": (49.8292, 8.8357),
    "Remagen": (50.5788, 7.227),
    "Remscheid": (51.1798, 7.1925),
    "Remseck am Neckar": (48.8721, 9.2733),
    "Renchen": (48.5885, 8.0132),
    "Rendsburg": (54.3018, 9.6717),
    "Rennerod": (50.6082, 8.067),
    "Renningen": (48.7697, 8.9387),
    "Rerik": (54.1054, 11.6128),
    "Rethem": (52.785, 9.3787),
    "Reutlingen": (48.4914, 9.2043),
    "Rheda-Wiedenbrück": (51.8497, 8.3002),
    "Rhede": (51.8354, 6.696),
    "Rheinau": (48.666, 7.9366),
    "Rheinbach": (50.6256, 6.9491),
    "Rheinberg": (51.5465, 6.5953),
    "Rheinböllen": (50.0113, 7.6725),
    "Rheine": (52.2851, 7.4405),
    "Rheinfelden": (47.5601, 7.7871),
    "Rheinsberg": (53.0997, 12.8988),
    "Rheinstetten": (48.9685, 8.307),
    "Rhens": (50.2812, 7.6175),
    "Rhinow": (52.7509, 12.3419),
    "Ribnitz-Damgarten": (54.2422, 12.4567),
    "Richtenberg": (54.2013, 12.8941),
    "Riedenburg": (48.9638, 11.6888),
    "Riedlingen": (48.1546, 9.4756),
    "Riedstadt": (49.8341, 8.4962),
    "Rieneck": (50.0935, 9.648),
    "Riesa": (51.3078, 13.2917),
    "Rietberg": (51.8092, 8.4284),
    "Rinteln": (52.186, 9.0792),
    "Rochlitz": (51.0501, 12.7975),
    "Rockenhausen": (49.6297, 7.8213),
    "Rodalben": (49.2394, 7.6396),
    "Rodenberg": (52.3115, 9.3564),
    "Rödental": (50.2952, 11.0412),
    "Rödermark": (49.974, 8.8282),
    "Rodewisch": (50.5308, 12.4133),
    "Rodgau": (50.0263, 8.8859),
    "Roding": (49.1943, 12.5196),
    "Römhild": (50.3964, 10.5389),
    "Romrod": (50.7134, 9.2201),
    "Ronneburg": (50.8634, 12.1867),
    "Ronnenberg": (52.3194, 9.6554),
    "Rosbach vor der Höhe": (50.3033, 8.6898),
    "Rosenfeld": (48.2864, 8.7236),
    "Rosenheim": (47.8564, 12.1225),
    "Rosenthal": (50.9744, 8.8674),
    "Rösrath": (50.8956, 7.1818),
    "Roßwein": (51.0659, 13.1831),
    "Rostock": (54.0887, 12.1405),
    "Rotenburg an der Fulda": (50.9956, 9.7284),
    "Rotenburg an der Wümme": (53.1103, 9.4036),
    "Roth": (49.2476, 11.0911),
    "Rötha": (51.1978, 12.4145),
    "Röthenbach an der Pegnitz": (49.483, 11.2412),
    "Rothenburg": (51.334, 14.9687),
    "Rothenburg ob der Tauber": (49.3788, 10.1871),
    "Rothenfels": (49.8914, 9.5926),
    "Röttingen": (49.5097, 9.9708),
    "Rottweil": (48.1678, 8.6272),
    "Rötz": (49.3432, 12.5296),
    "Rüdesheim": (49.9789, 7.9244),
    "Rudolstadt": (50.7204, 11.3405),
    "Ruhla": (50.893, 10.3657),
    "Ruhland": (51.4575, 13.8664),
    "Runkel": (50.4057, 8.1546),
    "Rüsselsheim": (49.9896, 8.4225),
    "Rutesheim": (48.8081, 8.9454),
    "Rüthen": (51.4909, 8.436),
    "Saalfeld": (50.6483, 11.3654),
    "Saarbrücken": (49.2326, 7.0098),
    "Saarburg": (49.6064, 6.5437),
    "Saarlouis": (49.3137, 6.7515),
    "Sachsenhagen": (52.3973, 9.2679),
    "Sachsenheim": (48.96, 9.0647),
    "Salzgitter": (52.157, 10.4154),
    "Salzkotten": (51.6717, 8.6009),
    "Salzwedel": (52.853, 11.1529),
    "Sandau (Elbe)": (52.7897, 12.0458),
    "Sangerhausen": (51.4722, 11.2953),
    "Sankt Augustin": (50.7754, 7.197),
    "Sankt Goar": (50.1488, 7.7072),
    "Sankt Goarshausen": (50.1584, 7.7137),
    "Sarstedt": (52.2349, 9.8541),
    "Sassenberg": (51.9922, 8.0407),
    "Sassnitz": (54.5157, 13.6445),
    "Sayda": (50.7112, 13.4217),
    "Schalkau": (50.3954, 11.0073),
    "Schauenstein": (50.2783, 11.7417),
    "Scheer": (48.0729, 9.2949),
    "Scheibenberg": (50.5402, 12.9122),
    "Scheinfeld": (49.6693, 10.4655),
    "Schelklingen": (48.3757, 9.7327),
    "Schenefeld": (54.0464, 9.4815),
    "Scheßlitz": (49.9757, 11.033),
    "Schieder-Schwalenberg": (51.8771, 9.1954),
    "Schifferstadt": (49.3842, 8.3775),
    "Schillingsfürst": (49.2878, 10.2628),
    "Schiltach": (48.2893, 8.3417),
    "Schkeuditz": (51.3968, 12.2214),
    "Schkölen": (51.0417, 11.8214),
    "Schleiden": (50.529, 6.4769),
    "Schleiz": (50.5787, 11.8102),
    "Schleswig": (54.5202, 9.5683),
    "Schlettau": (50.5588, 12.9527),
    "Schleusingen": (50.5108, 10.7566),
    "Schlieben": (51.7238, 13.383),
    "Schlitz": (50.6742, 9.561),
    "Schlüchtern": (50.3489, 9.5253),
    "Schlüsselfeld": (49.7562, 10.6187),
    "Schmalkalden": (50.7214, 10.4439),
    "Schmallenberg": (51.1547, 8.2851),
    "Schmölln": (50.8968, 12.3534),
    "Schnackenburg": (53.0373, 11.5645),
    "Schnaittenbach": (49.5469, 12.0018),
    "Schneeberg": (50.5947, 12.6414),
    "Schneverdingen": (53.1174, 9.7924),
    "Schömberg": (48.7871, 8.6449),
    "Schönau": (49.4367, 8.8088),
    "Schönau im Schwarzwald": (47.7862, 7.8944),
    "Schönberg": (53.8492, 10.9312),
    "Schönebeck": (52.0168, 11.7307),
    "Schönewalde": (51.679, 13.6025),
    "Schongau": (47.8124, 10.8966),
    "Schöningen": (52.138, 10.9674),
    "Schönsee": (49.5103, 12.5476),
    "Schönwald": (50.1997, 12.085),
    "Schopfheim": (47.651, 7.8209),
    "Schöppenstedt": (52.1431, 10.7745),
    "Schorndorf": (48.8054, 9.5272),
    "Schortens": (53.538, 7.9477),
    "Schotten": (50.5035, 9.1252),
    "Schramberg": (48.224, 8.3858),
    "Schraplau": (51.4375, 11.6682),
    "Schriesheim": (49.4737, 8.6636),
    "Schrobenhausen": (48.5607, 11.2607),
    "Schrozberg": (49.3453, 9.9794),
    "Schüttorf": (52.3228, 7.2218),
    "Schwaan": (53.9396, 12.1109),
    "Schwabach": (49.3305, 11.0235),
    "Schwäbisch Gmünd": (48.7995, 9.7981),
    "Schwäbisch Hall": (49.1113, 9.7391),
    "Schwabmünchen": (48.1793, 10.7568),
    "Schwaigern": (49.1449, 9.0552),
    "Schwalbach am Taunus": (50.15, 8.5333),
    "Schwalmstadt": (50.9333, 9.2167),
    "Schwandorf": (49.3253, 12.1098),
    "Schwanebeck": (51.9679, 11.1239),
    "Schwarzenbach am Wald": (50.2846, 11.6249),
    "Schwarzenbach an der Saale": (50.2228, 11.935),
    "Schwarzenbek": (53.5051, 10.4823),
    "Schwarzenberg/Erzgeb.": (50.5379, 12.7852),
    "Schwarzenborn": (50.9098, 9.4466),
    "Schwarzheide": (51.4767, 13.8556),
    "Schwedt": (53.0596, 14.2815),
    "Schweich": (49.8222, 6.7526),
    "Schweinfurt": (50.0494, 10.2218),
    "Schwelm": (51.2863, 7.2939),
    "Schwerin": (53.6294, 11.4132),
    "Schwerte": (51.4439, 7.5675),
    "Schwetzingen": (49.3822, 8.5823),
    "Sebnitz": (50.9754, 14.2758),
    "Seehausen (Altmark)": (52.8874, 11.7521),
    "Seelow": (52.5339, 14.3813),
    "Seelze": (52.3963, 9.5973),
    "Seesen": (51.8909, 10.1785),
    "Sehnde": (52.3139, 9.9682),
    "Seifhennersdorf": (50.9349, 14.6019),
    "Selb": (50.1706, 12.1305),
    "Selbitz": (50.317, 11.7502),
    "Seligenstadt": (50.0432, 8.9739),
    "Selm": (51.6969, 7.4681),
    "Selters (Westerwald)": (50.5325, 7.7558),
    "Senden": (48.3244, 10.0444),
    "Sendenhorst": (51.843, 7.83),
    "Senftenberg": (51.5252, 14.0016),
    "Seßlach": (50.1897, 10.842),
    "Siegburg": (50.8002, 7.2077),
    "Siegen": (50.8748, 8.0243),
    "Sigmaringen": (48.0883, 9.2303),
    "Simbach am Inn": (48.2655, 13.0231),
    "Simmern": (49.982, 7.5235),
    "Sindelfingen": (48.7, 9.0167),
    "Singen": (47.7593, 8.8403),
    "Sinsheim": (49.2529, 8.8787),
    "Sinzig": (50.5438, 7.2464),
    "Soest": (51.5756, 8.1062),
    "Solingen": (51.1734, 7.0845),
    "Solms": (50.5362, 8.407),
    "Soltau": (52.9854, 9.8398),
    "Sömmerda": (51.1591, 11.1152),
    "Sondershausen": (51.3697, 10.8701),
    "Sonneberg": (50.3592, 11.1746),
    "Sonnewalde": (51.6922, 13.6473),
    "Sonthofen": (47.5182, 10.2826),
    "Sontra": (51.0717, 9.9356),
    "Spaichingen": (48.0748, 8.7351),
    "Spalt": (49.1755, 10.9245),
    "Spangenberg": (51.1164, 9.6627),
    "Speicher": (49.9333, 6.6333),
    "Spenge": (52.1402, 8.4848),
    "Speyer": (49.3208, 8.4311),
    "Spremberg": (51.5696, 14.3738),
    "Springe": (52.2084, 9.5542),
    "Sprockhövel": (51.3467, 7.2434),
    "Stade": (53.5941, 9.473),
    "Stadtallendorf": (50.8226, 9.0129),
    "Stadtbergen": (48.3664, 10.8464),
    "Stadthagen": (52.3233, 9.2031),
    "Stadtilm": (50.776, 11.0826),
    "Stadtlohn": (51.994, 6.9192),
    "Stadtoldendorf": (51.8824, 9.6265),
    "Stadtprozelten": (49.7847, 9.4118),
    "Stadtroda": (50.8568, 11.7268),
    "Stadtsteinach": (50.1643, 11.5035),
    "Stadt Wehlen": (50.9582, 14.0309),
    "Starnberg": (48.0019, 11.3442),
    "Staßfurt": (51.8519, 11.5851),
    "Staufen im Breisgau": (47.8823, 7.7282),
    "Staufenberg": (50.662, 8.7316),
    "St. Blasien": (47.7625, 8.1271),
    "Stein": (49.4158, 11.016),
    "Steinach": (50.4313, 11.1591),
    "Steinau an der Straße": (50.314, 9.4634),
    "Steinbach-Hallenberg": (50.6962, 10.5654),
    "Steinbach (Taunus)": (50.7799, 8.1855),
    "Steinfurt": (52.1504, 7.3366),
    "Steinheim an der Murr": (48.9682, 9.2771),
    "Steinheim": (51.8707, 9.0914),
    "Stendal": (52.6058, 11.8609),
    "Sternberg": (53.7124, 11.8268),
    "Stockach": (47.8511, 9.0091),
    "Stolberg (Rhld.)": (50.7737, 6.226),
    "Stollberg": (50.71, 12.7803),
    "Stolpen": (51.049, 14.0794),
    "Storkow (Mark)": (52.2566, 13.9334),
    "Stößen": (51.1144, 11.924),
    "Straelen": (51.4419, 6.2664),
    "Stralsund": (54.3091, 13.0818),
    "Strasburg (Uckermark)": (53.5069, 13.7445),
    "Straubing": (48.8813, 12.5739),
    "Strausberg": (52.5786, 13.8874),
    "Strehla": (51.3525, 13.2266),
    "Stromberg": (49.9439, 7.7729),
    "Stühlingen": (47.7458, 8.4481),
    "Suhl": (50.6091, 10.694),
    "Sulingen": (52.6829, 8.8127),
    "Sulz am Neckar": (48.3624, 8.6331),
    "Sulzbach/ Saar": (49.2988, 7.057),
    "Sulzbach-Rosenberg": (49.5013, 11.746),
    "Sulzburg": (47.8412, 7.7078),
    "Sundern": (51.3281, 8.0037),
    "Süßen": (48.6793, 9.7553),
    "Syke": (52.9134, 8.8221),
    "Tambach-Dietharz": (50.7925, 10.6157),
    "Tamm": (48.9199, 9.1156),
    "Tangerhütte": (52.4353, 11.8072),
    "Tangermünde": (52.5446, 11.9765),
    "Tann (Rhön)": (50.6428, 10.0238),
    "Tanna": (50.4946, 11.8573),
    "Tauberbischofsheim": (49.6247, 9.6628),
    "Taucha": (51.3833, 12.4833),
    "Taunusstein": (50.1499, 8.1521),
    "Tecklenburg": (52.2196, 7.8136),
    "Tegernsee": (47.7123, 11.7582),
    "Telgte": (51.98, 7.7829),
    "Teltow": (52.4031, 13.2601),
    "Templin": (53.1187, 13.5022),
    "Tengen": (47.8213, 8.6612),
    "Tessin": (54.0276, 12.4652),
    "Teterow": (53.7736, 12.5755),
    "Tettnang": (47.6686, 9.5913),
    "Teublitz": (49.2229, 12.0873),
    "Teuchern": (51.1209, 12.0241),
    "Teupitz": (52.1297, 13.6196),
    "Teuschnitz": (50.3984, 11.3824),
    "Thale": (51.7486, 11.041),
    "Thannhausen": (48.2833, 10.4692),
    "Tharandt": (50.9853, 13.5803),
    "Themar": (50.5046, 10.6154),
    "Thum": (50.6708, 12.9509),
    "Tirschenreuth": (49.8826, 12.3311),
    "Titisee-Neustadt": (47.921, 8.1906),
    "Tittmoning": (48.0616, 12.7676),
    "Todtnau": (47.8294, 7.9438),
    "Töging am Inn": (48.2602, 12.5846),
    "Tönisvorst": (51.3209, 6.4941),
    "Tönning": (54.3173, 8.941),
    "Torgau": (51.5602, 12.9962),
    "Torgelow": (53.6337, 14.0123),
    "Tornesch": (53.6994, 9.7172),
    "Traben-Trarbach": (49.9508, 7.1156),
    "Traunreut": (47.9627, 12.5923),
    "Traunstein": (47.8683, 12.6433),
    "Trebbin": (52.2167, 13.225),
    "Trebsen/Mulde": (51.289, 12.755),
    "Treffurt": (51.1369, 10.2336),
    "Trendelburg": (51.5741, 9.4209),
    "Treuchtlingen": (48.9547, 10.9083),
    "Treuen": (50.5425, 12.3034),
    "Treuenbrietzen": (52.0975, 12.8726),
    "Triberg im Schwarzwald": (48.1317, 8.2332),
    "Tribsees": (54.0956, 12.7568),
    "Trier": (49.7557, 6.6394),
    "Triptis": (50.7357, 11.8702),
    "Trochtelfingen": (48.3084, 9.2449),
    "Troisdorf": (50.809, 7.1497),
    "Trossingen": (48.0767, 8.6441),
    "Trostberg": (48.028, 12.558),
    "Tübingen": (48.5227, 9.0522),
    "Tuttlingen": (47.9846, 8.8177),
    "Twistringen": (52.8001, 8.6389),
    "Übach-Palenberg": (50.9177, 6.1234),
    "Überlingen": (47.7698, 9.1714),
    "Ueckermünde": (53.7379, 14.0447),
    "Uelzen": (52.9645, 10.567),
    "Uetersen": (53.6888, 9.662),
    "Uffenheim": (49.5442, 10.2329),
    "Uhingen": (48.7047, 9.5857),
    "Ulm": (48.3984, 9.9916),
    "Ulmen": (50.2094, 6.9794),
    "Ulrichstein": (50.5755, 9.1927),
    "Ummerstadt": (50.2586, 10.8115),
    "Unkel": (50.5965, 7.2189),
    "Unna": (51.538, 7.6897),
    "Unterschleißheim": (48.2804, 11.5768),
    "Usedom": (53.8754, 13.9239),
    "Usingen": (50.3355, 8.5369),
    "Uslar": (51.6569, 9.635),
    "Vacha": (50.8279, 10.0219),
    "Vaihingen an der Enz": (48.9356, 8.9604),
    "Vallendar": (50.3959, 7.6243),
    "Varel": (53.3969, 8.1362),
    "Vechta": (52.7306, 8.2897),
    "Velbert": (51.3354, 7.0435),
    "Velburg": (49.2321, 11.6716),
    "Velden": (48.3663, 12.256),
    "Velen": (51.8945, 6.9881),
    "Vellberg": (49.0843, 9.8791),
    "Vellmar": (51.3581, 9.4797),
    "Velten": (52.6885, 13.1768),
    "Verden": (52.9233, 9.238),
    "Veringenstadt": (48.1852, 9.2108),
    "Verl": (51.8833, 8.5167),
    "Versmold": (52.0401, 8.1527),
    "Vetschau/Spreewald": (51.7864, 14.0794),
    "Viechtach": (49.08, 12.8857),
    "Viernheim": (49.5403, 8.5782),
    "Viersen": (51.2544, 6.3944),
    "Villingen-Schwenningen": (48.0623, 8.4936),
    "Vilsbiburg": (48.453, 12.356),
    "Vilseck": (49.6148, 11.8026),
    "Vilshofen": (48.627, 13.1922),
    "Visselhövede": (52.986, 9.581),
    "Vlotho": (52.1653, 8.86),
    "Voerde (Niederrhein)": (51.597, 6.6863),
    "Vogtsburg im Kaiserstuhl": (48.0969, 7.6418),
    "Vohburg an der Donau": (48.7698, 11.6184),
    "Vohenstrauß": (49.6238, 12.3381),
    "Vöhrenbach": (48.05, 8.3),
    "Vöhringen": (48.2784, 10.0824),
    "Volkach": (49.8635, 10.2281),
    "Völklingen": (49.2516, 6.8587),
    "Volkmarsen": (51.4089, 9.1181),
    "Vreden": (52.0379, 6.828),
    "Wachenheim an der Weinstraße": (49.4404, 8.1804),
    "Wächtersbach": (50.2551, 9.2956),
    "Wadern": (49.5412, 6.8877),
    "Waghäusel": (49.2499, 8.5126),
    "Wahlstedt": (53.9531, 10.2127),
    "Waiblingen": (48.8324, 9.3164),
    "Waibstadt": (49.2951, 8.9177),
    "Waischenfeld": (49.8464, 11.3481),
    "Waldbröl": (50.8758, 7.6169),
    "Waldeck": (51.2062, 9.0629),
    "Waldenbuch": (48.6383, 9.1326),
    "Waldenburg (Saxony)": (50.8765, 12.5992),
    "Waldenburg (Baden-Württemberg)": (49.1847, 9.6399),
    "Waldershof": (49.9814, 12.0629),
    "Waldheim": (51.0728, 13.02),
    "Waldkappel": (51.1446, 9.877),
    "Waldkirch": (48.0958, 7.9637),
    "Waldkirchen": (48.7327, 13.6008),
    "Waldkraiburg": (48.2085, 12.3989),
    "Waldmohr": (49.3833, 7.3333),
    "Waldmünchen": (49.378, 12.709),
    "Waldsassen": (50.0017, 12.3043),
    "Waldshut-Tiengen": (47.6232, 8.2172),
    "Walldorf": (49.3064, 8.6424),
    "Walldürn": (49.5836, 9.3664),
    "Wallenfels": (50.2685, 11.4706),
    "Walsrode": (52.861, 9.5928),
    "Waltershausen": (50.8983, 10.5579),
    "Waltrop": (51.6213, 7.4024),
    "Wanfried": (51.1821, 10.1728),
    "Wangen im Allgäu": (47.6895, 9.8325),
    "Warburg": (51.4901, 9.1464),
    "Waren (Müritz)": (53.5199, 12.6813),
    "Warendorf": (51.9511, 7.9876),
    "Warin": (53.8013, 11.7104),
    "Warstein": (51.4449, 8.3485),
    "Wassenberg": (51.1001, 6.1548),
    "Wasserburg am Inn": (48.0525, 12.2234),
    "Wassertrüdingen": (49.0433, 10.5991),
    "Wasungen": (50.6619, 10.3695),
    "Wedel": (53.5837, 9.6983),
    "Weener": (53.1654, 7.3497),
    "Wegberg": (51.1422, 6.2844),
    "Wegeleben": (51.8838, 11.1735),
    "Wehr": (47.6298, 7.9042),
    "Weida": (50.7745, 12.0603),
    "Weikersheim": (49.4787, 9.8998),
    "Weil am Rhein": (47.5933, 7.6208),
    "Weilburg": (50.4844, 8.2625),
    "Weil der Stadt": (48.7495, 8.8718),
    "Weilheim an der Teck": (48.6157, 9.5375),
    "Weilheim in Oberbayern": (47.8415, 11.1548),
    "Weimar": (50.9803, 11.329),
    "Weingarten": (47.8101, 9.6386),
    "Weinheim": (49.5489, 8.667),
    "Weinsberg": (49.1513, 9.2876),
    "Weismain": (50.0851, 11.2402),
    "Weißenberg": (51.1964, 14.6587),
    "Weißenburg in Bayern": (49.0309, 10.9722),
    "Weißenfels": (51.2015, 11.9684),
    "Weißenhorn": (48.305, 10.1605),
    "Weißensee": (51.1999, 11.0691),
    "Weißenthurm": (50.4172, 7.4507),
    "Weißwasser": (51.504, 14.6402),
    "Weiterstadt": (49.9039, 8.5887),
    "Welzheim": (48.8768, 9.6343),
    "Welzow": (51.5838, 14.1708),
    "Wemding": (48.8746, 10.7245),
    "Wendlingen am Neckar": (48.6712, 9.3763),
    "Werben (Elbe)": (52.8599, 11.9816),
    "Werdau": (50.736, 12.3753),
    "Werder (Havel)": (52.3787, 12.934),
    "Werdohl": (51.2601, 7.7661),
    "Werl": (51.5549, 7.914),
    "Werlte": (52.8513, 7.6749),
    "Wermelskirchen": (51.1397, 7.2158),
    "Wernau (Neckar)": (48.6931, 9.4153),
    "Werne": (51.6645, 7.6342),
    "Werneuchen": (52.6328, 13.7344),
    "Wernigerode": (51.8365, 10.7822),
    "Wertheim": (49.759, 9.5085),
    "Werther (Westf.)": (52.0777, 8.4179),
    "Wertingen": (48.5631, 10.6815),
    "Wesel": (51.6669, 6.6204),
    "Wesenberg": (53.2803, 12.9694),
    "Wesselburen": (54.2123, 8.9237),
    "Wesseling": (50.8271, 6.9747),
    "Westerburg": (50.5594, 7.9748),
    "Westerstede": (53.2568, 7.9274),
    "Wetter (Ruhr)": (51.3875, 7.3928),
    "Wetter (Hesse)": (50.9025, 8.7237),
    "Wetzlar": (50.5611, 8.5049),
    "Widdern": (49.3182, 9.4221),
    "Wiehl": (50.9495, 7.5506),
    "Wiesbaden": (50.086, 8.2444),
    "Wiesensteig": (48.5613, 9.6254),
    "Wiesloch": (49.295, 8.6985),
    "Wiesmoor": (53.4151, 7.7364),
    "Wildau": (52.3167, 13.6333),
    "Wildberg": (48.6234, 8.7452),
    "Wildenfels": (50.6678, 12.6089),
    "Wildeshausen": (52.8945, 8.4337),
    "Wilhelmshaven": (53.5476, 8.1039),
    "Wilkau-Haßlau": (50.675, 12.5148),
    "Willebadessen": (51.6256, 9.0369),
    "Willich": (51.2637, 6.5473),
    "Wilsdruff": (51.052, 13.5366),
    "Wilster": (53.9225, 9.3747),
    "Wilthen": (51.0975, 14.3929),
    "Windischeschenbach": (49.8011, 12.1571),
    "Windsbach": (49.2479, 10.8265),
    "Winnenden": (48.8756, 9.3982),
    "Winsen (Luhe)": (53.3578, 10.2116),
    "Winterberg": (51.1925, 8.5347),
    "Wipperfürth": (51.1161, 7.3986),
    "Wirges": (50.4719, 7.7984),
    "Wismar": (53.8922, 11.4556),
    "Wissen": (50.7792, 7.7347),
    "Witten": (51.4436, 7.3526),
    "Wittenberg": (51.8661, 12.6497),
    "Wittenberge": (53.0001, 11.7494),
    "Wittenburg": (53.5065, 11.079),
    "Wittichenau": (51.385, 14.244),
    "Wittlich": (49.986, 6.8931),
    "Wittingen": (52.7269, 10.7361),
    "Wittmund": (53.5768, 7.7757),
    "Witzenhausen": (51.341, 9.8554),
    "Woldegk": (53.4594, 13.5829),
    "Wolfach": (48.2932, 8.2158),
    "Wolfenbüttel": (52.1644, 10.541),
    "Wolfhagen": (51.3261, 9.1701),
    "Wolframs-Eschenbach": (49.2268, 10.7277),
    "Wolfratshausen": (47.9129, 11.4217),
    "Wolfsburg": (52.4245, 10.7815),
    "Wolfstein": (49.5841, 7.605),
    "Wolgast": (54.052, 13.7711),
    "Wolkenstein": (50.6555, 13.0713),
    "Wolmirstedt": (52.2486, 11.6295),
    "Worms": (49.6328, 8.3592),
    "Wörrstadt": (49.8486, 8.1242),
    "Wörth am Rhein": (49.0489, 8.2596),
    "Wörth an der Donau": (49.0009, 12.4054),
    "Wörth am Main": (49.7972, 9.1539),
    "Wriezen": (52.7209, 14.1342),
    "Wülfrath": (51.282, 7.0382),
    "Wunsiedel": (50.0392, 12.0034),
    "Wunstorf": (52.4238, 9.4359),
    "Wuppertal": (51.2563, 7.1482),
    "Würselen": (50.8181, 6.1347),
    "Wurzbach": (50.4636, 11.5378),
    "Würzburg": (49.7939, 9.9512),
    "Wurzen": (51.3707, 12.7394),
    "Wustrow (Wendland)": (52.9237, 11.1285),
    "Wyk auf Föhr": (54.6914, 8.567),
    "Xanten": (51.6588, 6.453),
    "Zarrentin am Schaalsee": (53.5489, 10.9167),
    "Zehdenick": (52.9785, 13.3316),
    "Zeil am Main": (50.0099, 10.5947),
    "Zeitz": (51.0496, 12.1369),
    "Zell am Harmersbach": (48.3465, 8.067),
    "Zell im Wiesental": (47.7056, 7.8525),
    "Zell (Mosel)": (50.0292, 7.1823),
    "Zella-Mehlis": (50.6564, 10.6605),
    "Zerbst": (51.9662, 12.0852),
    "Zeulenroda-Triebes": (50.6503, 11.9838),
    "Zeven": (53.2957, 9.2756),
    "Ziegenrück": (50.6075, 11.6498),
    "Zierenberg": (51.3695, 9.3016),
    "Ziesar": (52.2662, 12.29),
    "Zirndorf": (49.4424, 10.9541),
    "Zittau": (50.8977, 14.8076),
    "Zörbig": (51.6289, 12.1174),
    "Zossen": (52.216, 13.4491),
    "Zschopau": (50.7482, 13.0769),
    "Zülpich": (50.6945, 6.6541),
    "Zweibrücken": (49.2469, 7.3698),
    "Zwenkau": (51.2187, 12.3301),
    "Zwickau": (50.7272, 12.4884),
    "Zwiesel": (49.0169, 13.2377),
    "Zwingenberg": (49.7239, 8.6108),
    "Zwönitz": (50.6303, 12.81),
    # --- Sweden (GeoNames backfill) ---
    "Göteborg": (57.7229, 11.9458),
    "Uppsala": (60.0, 17.75),
    "Linköping": (58.4105, 15.6187),
    "Västerås": (59.6491, 16.5645),
    "Örebro": (59.5, 15.0),
    "Helsingborg": (56.0565, 12.7872),
    "Jönköping": (57.5, 14.5),
    "Norrköping": (58.6114, 16.3209),
    "Umeå": (63.8299, 20.2548),
    "Lund": (55.669, 13.3641),
    "Borås": (57.7378, 12.9408),
    "Huddinge": (59.2143, 18.0158),
    "Nacka": (59.3108, 18.1755),
    "Eskilstuna": (59.3736, 16.4901),
    "Halmstad": (56.7392, 12.9734),
    "Gävle": (60.6426, 17.1207),
    "Södertälje": (59.1812, 17.6276),
    "Haninge": (59.1738, 18.1549),
    "Karlstad": (59.4741, 13.5996),
    "Sundsvall": (62.4791, 16.9265),
    "Växjö": (56.9453, 14.8998),
    "Botkyrka": (59.2, 17.8167),
    "Järfälla": (59.4239, 17.8386),
    "Solna": (59.3655, 18.0073),
    "Kungsbacka": (57.4506, 12.1506),
    "Kristianstad": (56.0, 14.15),
    "Luleå": (65.6833, 22.1667),
    "Täby": (59.4374, 18.0653),
    "Sollentuna": (59.445, 17.9352),
    "Skellefteå": (64.8026, 20.6441),
    "Kalmar": (57.3333, 16.0),
    "Mölndal": (57.6026, 12.0989),
    "Varberg": (57.1721, 12.418),
    "Norrtälje": (59.8612, 18.7057),
    "Karlskrona": (56.2817, 15.6685),
    "Östersund": (63.2207, 14.9086),
    "Visby": (57.6409, 18.296),
    "Falun": (60.6048, 15.6349),
    "Trollhättan": (58.2121, 12.3503),
    "Skövde": (58.4349, 13.9036),
    "Nyköping": (58.8176, 16.8995),
    "Sundbyberg": (59.3667, 17.9702),
    "Uddevalla": (58.341, 11.8474),
    "Örnsköldsvik": (63.5146, 18.271),
    "Märsta": (59.6216, 17.8548),
    "Hässleholm": (56.2069, 13.7237),
    "Borlänge": (60.458, 15.3987),
    "Upplands Väsby": (59.5238, 17.91),
    "Kungälv": (57.897, 11.8888),
    "Åkersberga": (59.4794, 18.2997),
    "Tyresö": (59.2437, 18.2297),
    "Enköping": (59.6857, 17.1167),
    "Lidingö": (59.363, 18.151),
    "Landskrona": (55.8862, 12.8588),
    "Trelleborg": (55.4127, 13.2686),
    "Falkenberg": (57.0432, 12.7299),
    "Gustavsberg": (59.3315, 18.3962),
    "Ängelholm": (56.2681, 12.9662),
    "Lerum": (57.7745, 12.2697),
    "Motala": (58.6513, 15.1911),
    "Alingsås": (58.0, 12.5),
    "Piteå": (65.3723, 20.8515),
    "Partille": (57.7331, 12.1307),
    "Lidköping": (58.5, 13.0),
    "Vänersborg": (58.3983, 12.2755),
    "Mölnlycke": (57.6589, 12.1179),
    "Strängnäs": (59.3611, 17.088),
    "Sandviken": (60.5503, 16.6846),
    "Hudiksvall": (61.7662, 16.7706),
    "Västervik": (57.8524, 16.3894),
    "Vallentuna": (59.5915, 18.21),
    "Eslöv": (55.8361, 13.3752),
    "Värnamo": (57.1713, 14.0532),
    "Katrineholm": (59.0173, 16.2605),
    "Falköping": (58.1405, 13.5328),
    "Kävlinge": (55.7947, 13.0736),
    "Ystad": (55.4637, 13.9115),
    "Karlshamn": (56.25, 14.8667),
    "Nässjö": (57.6089, 14.6404),
    "Nynäshamn": (58.9561, 17.8869),
    "Karlskoga": (59.3738, 14.5672),
    "Mjölby": (58.3458, 15.1385),
    "Boden": (66.0439, 21.2998),
    "Ronneby": (56.3103, 15.2404),
    "Gislaved": (57.3034, 13.4755),
    "Höganäs": (56.2252, 12.597),
    "Ljungby": (56.8068, 13.8272),
    "Stenungsund": (58.0625, 11.9047),
    "Staffanstorp": (55.65, 13.2),
    "Vetlanda": (57.3686, 15.1847),
    "Ludvika": (60.1711, 14.7264),
    "Oskarshamn": (57.3734, 16.383),
    "Laholm": (56.4917, 13.1914),
    "Bollnäs": (61.3142, 16.4172),
    "Köping": (59.5664, 15.8722),
    "Arvika": (59.7048, 12.632),
    "Ulricehamn": (57.8206, 13.4612),
    "Mariestad": (58.7086, 13.8542),
    "Härnösand": (62.7667, 17.6833),
    "Söderhamn": (61.2537, 16.9488),
    "Kristinehamn": (59.2529, 14.1359),
    "Bålsta": (59.5671, 17.5278),
    "Lindesberg": (59.6412, 15.3406),
    "Kumla": (59.1272, 15.1465),
    "Sala": (59.9722, 16.4737),
    "Kiruna": (68.1701, 20.5486),
    "Avesta": (60.2125, 16.3623),
    "Knivsta": (59.737, 17.7814),
    "Finspång": (58.8057, 15.7878),
    "Mora": (61.0066, 14.5375),
    "Östhammar": (60.2416, 18.2697),
    "Tierp": (60.3721, 17.6363),
    "Arlöv": (55.6325, 13.0714),
    "Nybro": (56.8244, 15.8897),
    "Alvesta": (56.8466, 14.4749),
    "Sjöbo": (55.6515, 13.7556),
    "Simrishamn": (55.5862, 14.2378),
    "Skara": (58.3776, 13.4755),
    "Tranås": (58.0291, 14.8346),
    "Ljusdal": (61.8553, 15.5069),
    "Sollefteå": (63.3833, 16.9167),
    "Höör": (55.9459, 13.5169),
    "Klippan": (56.1114, 13.2384),
    "Eksjö": (57.6201, 15.2181),
    "Älmhult": (56.5856, 14.155),
    "Timrå": (62.616, 17.3785),
    "Sölvesborg": (56.0891, 14.6452),
    "Salem": (59.2457, 17.6988),
    "Kramfors": (62.9725, 17.9227),
    "Skurup": (55.4732, 13.5455),
    "Gällivare": (67.3935, 19.7083),
    "Skoghall": (59.3232, 13.4655),
    "Hallstahammar": (59.617, 16.228),
    "Åstorp": (56.14, 12.9726),
    "Mörbylånga": (56.5, 16.5),
    "Leksand": (60.7323, 14.998),
    "Bjuv": (56.0541, 12.9654),
    "Hallsberg": (59.038, 15.1847),
    "Båstad": (56.401, 12.7991),
    "Skärhamn": (57.9866, 11.5574),
    "Vara": (58.2439, 13.0729),
    "Krokom": (63.7333, 14.2),
    "Hörby": (55.8377, 13.7428),
    "Henån": (58.2385, 11.676),
    "Vimmerby": (57.6901, 15.8674),
    "Kalix": (65.9612, 22.9867),
    "Flen": (59.05, 16.7167),
    "Hedemora": (60.3466, 16.0735),
    "Vaggeryd": (57.4721, 14.128),
    "Trosa": (58.8962, 17.5481),
    "Säffle": (59.1956, 12.8644),
    "Söderköping": (58.4206, 16.4451),
    "Svalöv": (55.965, 13.1317),
    "Lilla Edet": (58.1357, 12.1434),
    "Heby": (60.1, 17.0),
    "Arboga": (59.3945, 15.7926),
    "Lysekil": (58.3472, 11.4669),
    "Broby": (56.2552, 14.078),
    "Tomelilla": (55.6196, 14.0198),
    "Strömstad": (58.9489, 11.2554),
    "Habo": (59.6495, 17.507),
    "Hultsfred": (57.4, 15.8),
    "Sunne": (59.9038, 13.0442),
    "Götene": (58.551, 13.4787),
    "Mönsterås": (57.0662, 16.3862),
    "Fagersta": (59.9483, 15.8833),
    "Öckerö": (57.6953, 11.6483),
    "Osby": (56.4408, 14.1184),
    "Järpen": (63.3462, 13.4657),
    "Olofström": (56.3231, 14.5656),
    "Tidaholm": (58.1484, 13.9434),
    "Tanumshede": (58.7238, 11.3259),
    "Vårgårda": (57.9877, 12.7689),
    "Bromölla": (56.1193, 14.5115),
    "Nykvarn": (59.1948, 17.4063),
    "Lycksele": (64.5985, 18.676),
    "Kil": (59.5041, 13.3174),
    "Oxelösund": (58.6752, 17.0841),
    "Tranemo": (57.5, 13.4333),
    "Tingsryd": (56.5333, 14.9667),
    "Vaxholm": (59.4039, 18.3417),
    "Åmål": (59.0457, 12.5958),
    "Sävsjö": (57.3288, 14.5943),
    "Åtvidaberg": (58.228, 16.1252),
    "Askersund": (58.8805, 14.9842),
    "Forshaga": (59.646, 13.5057),
    "Hagfors": (60.09, 13.6076),
    "Gnesta": (59.0921, 17.1453),
    "Tibro": (58.4492, 14.2266),
    "Torsby": (60.5694, 12.9384),
    "Edsbyn": (61.3769, 15.8175),
    "Säter": (60.3689, 15.7138),
    "Rättvik": (60.8863, 15.1179),
    "Strömsund": (64.2488, 15.2937),
    "Smedjebacken": (60.0947, 15.452),
    "Borgholm": (57.0975, 16.9409),
    "Svenljunga": (57.3925, 13.0522),
    "Nora": (59.5346, 14.9078),
    "Örkelljunga": (56.3246, 13.3394),
    "Djurås": (60.5606, 15.1328),
    "Munkedal": (58.5733, 11.701),
    "Malung": (60.5667, 13.6667),
    "Sveg": (62.0341, 14.3658),
    "Hyltebruk": (56.9989, 13.2396),
    "Kisa": (57.9898, 15.6292),
    "Markaryd": (56.5442, 13.6235),
    "Bollebygd": (57.7426, 12.6145),
    "Surahammar": (59.7749, 16.1188),
    "Årjäng": (59.4359, 12.1037),
    "Filipstad": (59.8735, 14.1471),
    "Skutskär": (60.6251, 17.4155),
    "Herrljunga": (58.0116, 13.1206),
    "Hjo": (58.2964, 14.2075),
    "Bergsjö": (61.9813, 17.0655),
    "Degerfors": (59.1558, 14.4335),
    "Vännäs": (63.9496, 19.7083),
    "Hofors": (60.4962, 16.4109),
    "Töreboda": (58.68, 14.1614),
    "Mellerud": (58.6911, 12.414),
    "Gnosjö": (57.3623, 13.8049),
    "Haparanda": (65.9166, 23.8461),
    "Kungshamn": (58.3631, 11.2594),
    "Ånge": (62.5123, 15.6445),
    "Bengtsfors": (59.0333, 12.2167),
    "Åseda": (57.1694, 15.3462),
    "Grums": (59.4041, 13.0219),
    "Emmaboda": (56.6333, 15.5333),
    "Vingåker": (59.0667, 15.8833),
    "Fjugesta": (59.1737, 14.8723),
    "Kungsör": (59.4025, 16.0775),
    "Charlottenberg": (59.8842, 12.304),
    "Lessebo": (56.7639, 15.3136),
    "Älvsbyn": (65.732, 20.7141),
    "Mullsjö": (57.9543, 13.8224),
    "Valdemarsvik": (58.2, 16.6),
    "Vadstena": (58.4078, 14.8616),
    "Svenstavik": (62.7667, 14.4353),
    "Perstorp": (56.1886, 13.3768),
    "Karlsborg": (58.6099, 14.4542),
    "Nordmaling": (63.5706, 19.4997),
    "Torsås": (56.4265, 15.8913),
    "Orsa": (61.12, 14.6143),
    "Aneby": (57.8699, 14.798),
    "Älvdalen": (61.6134, 13.2726),
    "Vansbro": (60.4612, 14.2784),
    "Robertsfors": (64.166, 20.7842),
    "Hällefors": (59.7574, 14.615),
    "Färgelanda": (58.6163, 12.0114),
    "Vilhelmina": (64.6297, 16.6457),
    "Arvidsjaur": (65.5955, 19.1671),
    "Bräcke": (62.8547, 15.6661),
    "Ockelbo": (60.8873, 16.7161),
    "Pajala": (67.2407, 22.9167),
    "Grästorp": (58.3333, 12.6667),
    "Storuman": (65.4519, 16.2581),
    "Nossebro": (58.1881, 12.716),
    "Boxholm": (58.1484, 15.1099),
    "Vindeln": (64.1937, 19.7026),
    "Laxå": (58.9091, 14.5306),
    "Norberg": (60.09, 15.9597),
    "Ödeshög": (58.1753, 14.6161),
    "Hammarstrand": (63.1104, 16.3538),
    "Högsby": (57.1554, 15.9167),
    "Gullspång": (58.933, 14.1805),
    "Jokkmokk": (66.605, 19.8413),
    "Ed": (58.9084, 11.9264),
    "Kopparberg": (61.0, 14.5),
    "Skinnskatteberg": (59.7972, 15.7146),
    "Övertorneå": (66.3902, 23.6571),
    "Norsjö": (64.9146, 19.476),
    "Storfors": (59.4948, 14.2473),
    "Munkfors": (59.8162, 13.4946),
    "Österbymo": (57.8246, 15.2736),
    "Överkalix": (66.327, 22.8429),
    "Malå": (65.1838, 18.741),
    "Åsele": (64.1637, 17.3548),
    "Arjeplog": (66.0519, 17.8845),
    "Sorsele": (65.5418, 17.5149),
    "Bjurholm": (63.931, 19.211),
    "Dorotea": (64.5798, 15.8061),
    "Ale": (57.9506, 12.0808),
    "Danderyd": (59.4092, 18.0485),
    "Lomma": (55.7036, 13.0732),
    "Vellinge": (55.4478, 13.0092),
    "Kinna": (57.5083, 12.6961),
    "Svedala": (55.5432, 13.2686),
    "Ekerö": (59.322, 17.6358),
    "Upplands-Bro": (59.5187, 17.6431),
}


def _ensure_community_id(record: dict) -> dict:
    if not record.get("community_id"):
        import hashlib
        key = f"{record.get('name', '').lower()}|{record.get('city', '').lower()}"
        record = dict(record, community_id=hashlib.sha256(key.encode()).hexdigest()[:12])
    if "community_url" not in record:
        city_sl = _slugify(record.get("city", ""))
        name_sl = _slugify(record.get("name", ""))
        record = dict(record, community_url=f"/{city_sl}/{name_sl}")
    return record


def _db() -> Path:
    return app_state.db_path or DATA_DIR / "scraper.db"


_HU_SORT_MAP = str.maketrans({
    # Each accented char maps to base + a byte > 'z'(122) so it sorts after all base-char words
    # Multiple variants of same base use ascending bytes: ó < ö < ő, ú < ü < ű
    ord('á'): 'a\x7f', ord('Á'): 'a\x7f',
    ord('é'): 'e\x7f', ord('É'): 'e\x7f',
    ord('í'): 'i\x7f', ord('Í'): 'i\x7f',
    ord('ó'): 'o\x7d', ord('Ó'): 'o\x7d',
    ord('ö'): 'o\x7e', ord('Ö'): 'o\x7e',
    ord('ő'): 'o\x7f', ord('Ő'): 'o\x7f',
    ord('ú'): 'u\x7d', ord('Ú'): 'u\x7d',
    ord('ü'): 'u\x7e', ord('Ü'): 'u\x7e',
    ord('ű'): 'u\x7f', ord('Ű'): 'u\x7f',
})


def _hu_sort_key(name: str) -> str:
    """Sort key for Hungarian alphabetical order: á after all a-words, é after e-words, etc."""
    return name.lower().translate(_HU_SORT_MAP)


def _city_from_slug(city_slug: str) -> str | None:
    for city in (app_state.cities or []):
        if _slugify(city.name) == city_slug:
            return city.name
    return None


def _city_locale(city_name: str) -> str:
    for city in (app_state.cities or []):
        if city.name == city_name:
            return city.locale or "en"
    return "en"


#: Links shown in one related block. A screenful, not a dump: Budapest has
#: hundreds of communities, and the Tailwind CDN scans the whole DOM before the
#: page paints.
_RELATED_LIMIT = 12


def related_communities(records: list[dict], *, exclude_key: str, topic: str | None,
                        locale: str, limit: int = _RELATED_LIMIT) -> dict:
    """Neighbours of one community, grouped as a reader would ask for them.

    Returns `{"same_topic": [...], "other_topics": [...]}` of link dicts. The
    grouping is the point: "other running clubs here" and "what else happens in
    this town" are two different questions, and a single mixed list answers
    neither. Both are also crawl paths — 23,461 of our pages were fetched and
    judged not worth indexing while each one stood alone.
    """
    labels = get_topic_labels(locale)
    same, other = [], []
    for rec in records:
        name = (rec.get("name") or "").strip()
        city = (rec.get("city") or "").strip()
        if not name or not city:
            continue
        if _community_record_key(name, city, rec.get("topic") or "") == exclude_key:
            continue
        rec_topic = rec.get("topic") or ""
        item = {
            "name": name,
            "url": f"/{_slugify(city)}/{_slugify(name)}",
            "note": labels.get(rec_topic, rec_topic.replace("_", " ").title()),
        }
        (same if topic and rec_topic == topic else other).append(item)
    return {"same_topic": same[:limit], "other_topics": other[:limit],
            "same_topic_total": len(same), "other_topics_total": len(other)}


def _topic_url_slug(topic_name: str, locale: str) -> str:
    """Return the URL slug for a topic in the given locale."""
    labels = get_topic_labels(locale)
    label = labels.get(topic_name, topic_name.replace("_", " ").title())
    return _slugify(label)


def _topic_from_url_slug(slug: str, locale: str) -> str | None:
    """Given a localized URL slug and locale, return the canonical topic name."""
    labels = get_topic_labels(locale)
    for topic_name, label in labels.items():
        if _slugify(label) == slug:
            return topic_name
    # fall back to English
    if locale != "en":
        en_labels = get_topic_labels("en")
        for topic_name, label in en_labels.items():
            if _slugify(label) == slug:
                return topic_name
    return None


def _load_communities(city: str, topic: str) -> list[dict]:
    return [_ensure_community_id(r) for r in get_communities(_db(), city, topic)]


def _find_community(community_id: str) -> dict | None:
    r = find_community_by_id(_db(), community_id)
    return _ensure_community_id(r) if r else None


def _hu_city_names() -> set[str]:
    """Return the set of city names that belong to Hungary."""
    return {c.name for c in (app_state.cities or []) if c.country == "Hungary"}


def _public_community_url(name: str, city: str) -> str:
    """Public community page URL for admin views. Hungarian cities exist on the
    current (kozossegek) domain, so a relative path suffices; every other city
    only resolves on meetapedia.com."""
    path = f"/{_slugify(city)}/{_slugify(name)}"
    if city in _hu_city_names():
        return path
    return f"https://meetapedia.com{path}"


def _site_cities(request: Request) -> list:
    from .i18n import _detect_site
    cities = app_state.cities or []
    if _detect_site(request) == "kozossegek":
        return [c for c in cities if c.country == "Hungary"]
    return cities


def _canonical_base(request: Request, city_name: str) -> str:
    """Canonical domain for a city-scoped public page.

    Hungarian-city pages are served with identical paths on both domains;
    kozossegek.com is their canonical home so Google doesn't consolidate the
    duplicates toward meetapedia.com. Everything else self-canonicalizes.
    """
    from .i18n import _detect_site
    if _detect_site(request) == "kozossegek" or city_name in _hu_city_names():
        return "https://kozossegek.com"
    return "https://meetapedia.com"


def _crumbs(request: Request, *pairs: tuple[str, str]) -> list[dict]:
    """Build breadcrumb items from (name, path) pairs. Each gets an absolute `url`
    (for BreadcrumbList JSON-LD) on the serving site plus the relative `path` (for
    the visible nav). A leading Home crumb is prepended automatically."""
    from .i18n import lang_context as _lc
    ctx = _lc(request)
    base = ctx["site_url"]
    items = [{"name": ctx["t"]("breadcrumb_home"), "path": "/", "url": f"{base}/"}]
    for name, path in pairs:
        if name:
            items.append({"name": name, "path": path, "url": f"{base}{path}"})
    return items


def _hu_redirect(request: Request, city_name: str | None):
    """On meetapedia.com, Hungarian-city pages **301** to kozossegek.com.

    A `rel=canonical` was only a hint that Google ignored — it kept the HU
    duplicates indexed on meetapedia and starved kozossegek (GSC 2026-07:
    meetapedia won 551 HU impressions to kozossegek's 33). A 301 removes the
    duplicate outright. kozossegek serves the identical path, so path + query are
    preserved. Returns a RedirectResponse or None (no redirect needed).
    See [[seo-cross-domain-canonical]].
    """
    from .i18n import _detect_site
    if _detect_site(request) == "meetapedia" and city_name and city_name in _hu_city_names():
        q = request.url.query
        return RedirectResponse(
            f"https://kozossegek.com{request.url.path}" + (f"?{q}" if q else ""),
            status_code=301,
        )
    return None


def _sister_url(request: Request, city_name: str | None) -> str | None:
    """Twin-page URL for city-scoped pages, or None when there is no twin.

    meetapedia.com carries every city; kozossegek.com only Hungarian ones and
    bounces the rest to its home page (see public_city). Offering a link that
    lands on an unrelated page is worse than offering none, so non-Hungarian
    city content on meetapedia.com gets no sister link. The reverse direction
    always has a twin — meetapedia is the superset.
    """
    from .i18n import _detect_site
    from .i18n import sister_url as _twin
    if _detect_site(request) == "meetapedia" and city_name and city_name not in _hu_city_names():
        return None
    return _twin(request)


def _global_topic_counts() -> dict[str, int]:
    return get_topic_counts(_db())


def _hu_topic_counts() -> dict[str, int]:
    """Topic counts restricted to Hungarian cities (single SQL query)."""
    hu = _hu_city_names()
    if not hu:
        return get_topic_counts(_db())
    return get_topic_counts_for_cities(_db(), hu)


# ISO-3166-1 alpha-2 → country name as used in cities.yaml
_ISO2_COUNTRY: dict[str, str] = {
    "AR": "Argentina", "AU": "Australia", "AT": "Austria", "BE": "Belgium",
    "BR": "Brazil", "BG": "Bulgaria", "CA": "Canada", "CL": "Chile",
    "CN": "China", "CO": "Colombia", "HR": "Croatia", "CZ": "Czech Republic",
    "DK": "Denmark", "EC": "Ecuador", "EG": "Egypt", "EE": "Estonia",
    "FI": "Finland", "FR": "France", "DE": "Germany", "GH": "Ghana",
    "GR": "Greece", "HK": "Hong Kong", "HU": "Hungary", "IN": "India",
    "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IT": "Italy",
    "JP": "Japan", "KE": "Kenya", "LV": "Latvia", "LB": "Lebanon",
    "LT": "Lithuania", "MY": "Malaysia", "MX": "Mexico", "MA": "Morocco",
    "NL": "Netherlands", "NZ": "New Zealand", "NG": "Nigeria", "NO": "Norway",
    "PE": "Peru", "PH": "Philippines", "PL": "Poland", "PT": "Portugal",
    "RO": "Romania", "RS": "Serbia", "SG": "Singapore", "SK": "Slovakia",
    "SI": "Slovenia", "ZA": "South Africa", "KR": "South Korea", "ES": "Spain",
    "SE": "Sweden", "CH": "Switzerland", "TW": "Taiwan", "TH": "Thailand",
    "TR": "Turkey", "AE": "UAE", "UA": "Ukraine", "GB": "United Kingdom",
    "US": "United States", "UY": "Uruguay", "VN": "Vietnam",
}

# language tag → likely ISO2 country code (Accept-Language fallback)
_LANG_COUNTRY: dict[str, str] = {
    "hu": "HU", "de": "DE", "fr": "FR", "es": "ES", "it": "IT",
    "pt": "PT", "ru": "RU", "uk": "UA", "zh": "CN", "ja": "JP",
    "ko": "KR", "ar": "EG", "fa": "IR", "he": "IL", "hi": "IN",
    "tr": "TR", "id": "ID", "nl": "NL", "pl": "PL", "sv": "SE",
    "cs": "CZ", "ro": "RO", "el": "GR", "vi": "VN", "th": "TH",
    "da": "DK", "no": "NO", "fi": "FI", "sk": "SK", "hr": "HR",
    "bg": "BG", "sr": "RS", "ms": "MY", "tl": "PH",
}


def _detect_country(request: Request) -> str | None:
    """Return the full country name for the visitor, or None."""
    # Cloudflare adds this header automatically
    cf = request.headers.get("CF-IPCountry", "").strip().upper()
    if cf and cf != "XX" and cf in _ISO2_COUNTRY:
        return _ISO2_COUNTRY[cf]

    # Generic reverse-proxy headers
    for header in ("X-Country-Code", "X-GeoIP-Country", "X-Real-IP-Country"):
        val = request.headers.get(header, "").strip().upper()
        if val and val in _ISO2_COUNTRY:
            return _ISO2_COUNTRY[val]

    # Fall back to primary Accept-Language tag  (best-effort, not accurate)
    al = request.headers.get("Accept-Language", "")
    for part in re.split(r"[,;]", al):
        tag = part.strip().split("-")
        if len(tag) >= 2:
            iso2 = tag[1].upper()
            if iso2 in _ISO2_COUNTRY:
                return _ISO2_COUNTRY[iso2]
        if len(tag) == 1:
            code = _LANG_COUNTRY.get(tag[0].lower())
            if code and code in _ISO2_COUNTRY:
                return _ISO2_COUNTRY[code]
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# PUBLIC ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@_fastapi.get("/", response_class=HTMLResponse)
async def public_home(request: Request, city: str = ""):
    from .i18n import _detect_site
    global _home_stats_cache
    site = _detect_site(request)
    site_cities = _site_cities(request)
    site_city_names = {c.name for c in site_cities}
    topics = app_state.topics or []
    topic_url_slugs = {t.name: _topic_url_slug(t.name, "hu") for t in topics}
    if site not in _home_stats_cache:
        if site == "kozossegek":
            topic_counts = _hu_topic_counts()
        else:
            # meetapedia serves HU cities too, so its stats include them —
            # keeping the totals consistent with the browsable content.
            topic_counts = _global_topic_counts()
        venue_counts = {k: v for k, v in (get_venue_counts(_db()) if app_state.db_path else {}).items() if k in site_city_names}
        person_counts = {k: v for k, v in (get_person_counts(_db()) if app_state.db_path else {}).items() if k in site_city_names}
        city_totals = dict(get_city_totals(_db())) if app_state.db_path else {}
        # Popularity ranking: topic diversity first, then count excluding the
        # generic 'other' bucket — stops small towns with 100 'Egyéb/Nyugdíjas'
        # rows from outranking real cities.
        from ..db import get_city_topic_counts as _gctc, get_recently_added_communities
        per_city_topics = _gctc(_db()) if app_state.db_path else {}
        def _pop_key(name: str, count: int):
            t = per_city_topics.get(name, {})
            diverse = sum(1 for k, v in t.items() if k != "other" and v > 0)
            adj = count - t.get("other", 0)
            return (-diverse, -adj, _hu_sort_key(name))
        city_list = sorted(
            [{
                "name": c.name,
                "slug": _slugify(c.name),
                "count": city_totals.get(c.name, 0),
                "country": c.country or "",
                "lat": CITY_COORDS.get(c.name, (None, None))[0],
                "lng": CITY_COORDS.get(c.name, (None, None))[1],
            } for c in site_cities],
            key=lambda x: _pop_key(x["name"], x["count"]),
        )
        recent = [r for r in get_recently_added_communities(_db(), limit=40)
                  if r.get("city") in site_city_names][:6] if app_state.db_path else []
        # meetapedia: top 3 cities per country, sorted by total count desc
        country_city_groups: dict = {}
        if site == "meetapedia":
            for c in city_list:
                if c["country"] and c["count"] > 0:  # no empty city chips
                    country_city_groups.setdefault(c["country"], []).append(c)
            country_city_groups = dict(sorted(
                country_city_groups.items(),
                key=lambda x: -sum(e["count"] for e in x[1]),
            ))
            for k in country_city_groups:
                country_city_groups[k] = country_city_groups[k][:3]
        _home_stats_cache[site] = {
            "recent_communities": recent,
            "topic_counts": topic_counts,
            "total_records": sum(topic_counts.values()),
            "total_venues": sum(venue_counts.values()),
            "total_persons": sum(person_counts.values()),
            "city_list": city_list,
            "country_city_groups": country_city_groups,
        }
    topic_counts = _home_stats_cache[site]["topic_counts"]
    city_list = _home_stats_cache[site]["city_list"]
    # ONE list feeds both the search box (MpAutocomplete) and the "cities near
    # you" panel. They used to ship as two JSON blobs holding the same cities
    # under different key names — 214 KB of a 242 KB page, most of it duplicate.
    # Short keys, no `country` (neither consumer reads it), lat/lng only where
    # known. Alphabetical rather than popularity-ordered so the
    # accent-insensitive matcher has a stable base; the widget does its own
    # ranking.
    _home_lang = lang_context(request)
    cities_json = json.dumps(
        [
            {"n": c["name"], "s": c["slug"], "c": c["count"],
             **({"lat": c["lat"], "lng": c["lng"]} if c.get("lat") is not None else {})}
            for c in sorted(city_list, key=lambda c: _hu_sort_key(c["name"]))
        ],
        ensure_ascii=False, separators=(",", ":"),
    )
    return templates.TemplateResponse(request, "public_home.html", {
        "cities": site_cities,
        "topics": topics,
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        "selected_city": city,
        "topic_counts": topic_counts,
        "topic_url_slugs": topic_url_slugs,
        "recent_communities": _home_stats_cache[site].get("recent_communities", []),
        "total_records": _home_stats_cache[site]["total_records"],
        "total_venues": _home_stats_cache[site]["total_venues"],
        "total_persons": _home_stats_cache[site]["total_persons"],
        "hu_city_list": city_list[:12],
        "country_city_groups": _home_stats_cache[site]["country_city_groups"],
        "cities_json": cities_json,
        # WebSite + SearchAction + Organization. The search action is the one
        # part that can surface in a result — it is what a sitelinks search box
        # is built from — so the path must be the real route, which is
        # /kereses on both editions (see public_base.html's search form).
        "schema_json": site_jsonld(
            _home_lang["site_name"], _home_lang["site_url"], "/kereses",
            _home_lang["t"]("home_og_desc",
                            total=_home_stats_cache[site]["total_records"],
                            cities=len(site_cities))),
        **_home_lang,
    })


#: Cached (value, monotonic_ts) for the health endpoint's record count.
_HEALTH_COUNT_CACHE: tuple[int, float] = (0, 0.0)
_HEALTH_COUNT_TTL = 60.0
#: How long /healthz will wait for the community count before answering without
#: it. Named rather than inlined because a test has to sleep *past* it to prove
#: a busy database still gets a green check, and a literal in both places is two
#: numbers that can drift apart — the test's was ten times this one.
_HEALTH_COUNT_TIMEOUT_S = 3.0


@_fastapi.get("/healthz")
async def healthz():
    """Liveness, not readiness — deliberately.

    The Docker healthcheck calls this, and Traefik pulls the container out of
    rotation when it fails. It used to run a COUNT over `communities` on every
    call, so while the pipeline held a write lock the check timed out, the
    container was marked unhealthy, and **every public request 404'd** until the
    lock cleared. That produced four separate "the site is down" reports on
    2026-08-16 with a perfectly healthy process behind them.

    The count is now cached and computed off the event loop, and a database that
    is slow or locked reports `db: "busy"` **without** failing the check: the app
    is up and serving, which is the only question a liveness probe should ask.
    """
    import time as _t

    global _HEALTH_COUNT_CACHE
    count, ts = _HEALTH_COUNT_CACHE
    db_state = "ok"
    if (_t.monotonic() - ts) > _HEALTH_COUNT_TTL:
        try:
            count = await asyncio.wait_for(
                asyncio.to_thread(get_total_community_count, _db()),
                timeout=_HEALTH_COUNT_TIMEOUT_S)
            _HEALTH_COUNT_CACHE = (count, _t.monotonic())
        except (asyncio.TimeoutError, Exception):
            # Stale count, and say so — but stay green. A locked database is a
            # slow database, not a dead application.
            db_state = "busy"
    return {
        "ok": True,
        "version": app_state.version,
        "db": db_state,
        "total_records": count,
    }


def _nearby_cities(request: Request, city_name: str, limit: int = 6) -> list[dict]:
    """Closest same-site cities with data — internal-linking block for city pages."""
    import math
    origin = CITY_COORDS.get(city_name)
    if not origin or not app_state.db_path:
        return []
    from ..db import get_city_totals
    totals = dict(get_city_totals(_db()))
    out = []
    for c in _site_cities(request):
        if c.name == city_name or totals.get(c.name, 0) <= 0:
            continue
        coords = CITY_COORDS.get(c.name)
        if not coords:
            continue
        dlat = math.radians(coords[0] - origin[0])
        dlng = math.radians(coords[1] - origin[1])
        a = (math.sin(dlat / 2) ** 2
             + math.cos(math.radians(origin[0])) * math.cos(math.radians(coords[0]))
             * math.sin(dlng / 2) ** 2)
        km = 6371 * 2 * math.asin(math.sqrt(a))
        out.append({"name": c.name, "slug": _slugify(c.name),
                    "count": totals[c.name], "km": round(km)})
    out.sort(key=lambda x: x["km"])
    return out[:limit]


#: Cities per country on a site-wide topic page; each shows ten groups and
#: links to its full topic listing.
_EXPLORE_TOPIC_CITIES = 12


async def _render_explore(
    request: Request,
    city: str = "",
    topic: list[str] | None = None,
    tag: str = "",
    subscribed: str = "",
) -> HTMLResponse:
    if topic is None:
        topic = []
    if (redirect := _hu_redirect(request, city)):
        return redirect
    cities = app_state.cities or []
    topics = app_state.topics or []

    _topic_labels = get_topic_labels(lang_context(request)["lang"])

    sections: list[dict] = []
    total = 0
    for t in topic:
        records = sorted(_load_communities(city, t) if city else [],
                         key=lambda r: _hu_sort_key(r.get("name", "")))
        total += len(records)
        sections.append({
            "topic": t,
            "label": _topic_labels.get(t, t.replace("_", " ").title()),
            "icon": TOPIC_ICONS.get(t, "circle"),
            "records": records,
        })

    available_topics: dict[str, int] = {}
    if city:
        # Uses the module-level import: a function-local import here would make
        # the name local to the whole function and crash the no-city branch
        # below with UnboundLocalError.
        counts = get_topic_counts_for_city(_db(), city) if app_state.db_path else {}
        available_topics = {t.name: counts[t.name] for t in topics if counts.get(t.name, 0) > 0}

    # City page with no topic filter: show all communities (small chips will filter client-side)
    if city and not topic and available_topics:
        for t_name, count in available_topics.items():
            # Alphabetical inside each topic section: the page carries an A-Z
            # jump bar and a name filter, both of which need a scannable order.
            records = sorted(_load_communities(city, t_name),
                             key=lambda r: _hu_sort_key(r.get("name", "")))
            total += len(records)
            sections.append({
                "topic": t_name,
                "label": _topic_labels.get(t_name, t_name.replace("_", " ").title()),
                "icon": TOPIC_ICONS.get(t_name, "circle"),
                "records": records,
            })

    # Country-grouped multi-city view (when no specific city is selected and no tag)
    country_sections: list[dict] = []
    if not city and not tag:
        user_country = _detect_country(request)
        city_totals = dict(get_city_totals(_db()))
        cities_map = {c.name: c.country for c in cities}

        # Group cities by country, only include site-appropriate cities that have data
        site_city_names = {c.name for c in _site_cities(request)}
        country_cities: dict[str, list[tuple[str, int]]] = {}
        for name, country in cities_map.items():
            if name not in site_city_names:
                continue
            count = city_totals.get(name, 0)
            if count > 0:
                country_cities.setdefault(country, []).append((name, count))
        for v in country_cities.values():
            v.sort(key=lambda x: x[1], reverse=True)

        # Order: user's country first, then top 3 others by total community count
        country_order: list[tuple[str, bool]] = []
        if user_country and user_country in country_cities:
            country_order.append((user_country, True))
        other_sorted = sorted(
            [(c, clist) for c, clist in country_cities.items() if c != user_country],
            key=lambda x: sum(cnt for _, cnt in x[1]),
            reverse=True,
        )
        for c_name, _ in other_sorted[:3]:
            country_order.append((c_name, False))

        all_city_topic_counts = get_city_topic_counts(_db())
        for country, is_user in country_order:
            all_city_entries = country_cities.get(country, [])
            if topic:
                # The cities with the most groups in these topics, a sample of
                # each, and "see all" into the city's own topic page. Rendering
                # every record of every city made /felfedezes/vallas 4.9 MB
                # (2026-09-24) — a page proportional to the database, which
                # this site does not serve. Counts come from the aggregate, so
                # "see all N" still matches the page it links to.
                in_topic = {
                    name: sum(all_city_topic_counts.get(name, {}).get(t, 0) for t in topic)
                    for name, _count in all_city_entries}
                city_entries = sorted(((n, c) for n, c in in_topic.items() if c),
                                      key=lambda x: x[1], reverse=True)[:_EXPLORE_TOPIC_CITIES]
            else:
                # Without topic, a 3-city sample for discovery.
                city_entries = all_city_entries[:3]
            city_sections: list[dict] = []
            for city_name, city_count in city_entries:
                if topic:
                    recs: list[dict] = []
                    for t in topic:
                        recs.extend(_load_communities(city_name, t))
                    recs = recs[:10]
                else:
                    recs = [_ensure_community_id(r) for r in get_communities_for_city(_db(), city_name)][:10]
                if recs:
                    city_url = "/" + _slugify(city_name)
                    if topic and len(topic) == 1:
                        city_url += "/" + _topic_url_slug(topic[0], _city_locale(city_name))
                    topic_count = city_count
                    city_locale_str = _city_locale(city_name)
                    city_chips = sorted(
                        [
                            {
                                "name": t_obj.name,
                                "label": _topic_labels.get(t_obj.name, t_obj.name.replace("_", " ").title()),
                                "icon": TOPIC_ICONS.get(t_obj.name, "circle"),
                                "count": cnt,
                                "url": f"/{_slugify(city_name)}/{_topic_url_slug(t_obj.name, city_locale_str)}",
                            }
                            for t_obj in topics
                            if (cnt := all_city_topic_counts.get(city_name, {}).get(t_obj.name, 0)) > 0
                        ],
                        key=lambda x: x["count"],
                        reverse=True,
                    )
                    city_sections.append({
                        "city": city_name,
                        "country": country,
                        "records": recs,
                        "total": topic_count,
                        "city_url": city_url,
                        "topic_chips": city_chips,
                    })
            if city_sections:
                country_sections.append({
                    "country": country,
                    "is_user_country": is_user,
                    "cities": city_sections,
                })

    # Tag-based search: filter across communities by free-form tag
    tag_records: list[dict] = []
    if tag:
        tag_records = [_ensure_community_id(r)
                       for r in search_communities_by_tag(_db(), tag, city)]
        total += len(tag_records)

    all_records: list = []
    for s in sections:
        all_records.extend(s["records"])
    for cs in country_sections:
        for cs_city in cs["cities"]:
            all_records.extend(cs_city["records"])
    all_records.extend(tag_records)
    schema_json = records_to_jsonld(all_records)

    topic_venues: list[dict] = []
    if city and len(topic) == 1 and app_state.db_path:
        topic_venues = get_venues_by_city_topic(app_state.db_path, city, topic[0])

    city_locale = _city_locale(city) if city else "en"
    topic_url_slugs = {t.name: _topic_url_slug(t.name, city_locale) for t in (app_state.topics or [])}

    city_coords_for_js: dict[str, list[float]] = {}
    for cs in country_sections:
        for cs_city in cs["cities"]:
            coords = CITY_COORDS.get(cs_city["city"])
            if coords:
                city_coords_for_js[cs_city["city"]] = [coords[0], coords[1]]

    # BreadcrumbList JSON-LD (base renders no visible bar — each page keeps its own).
    # Home → City → Topic (city+single topic), Home → City (city page), or
    # Home → Topic (global /felfedezes/<topic>). Multi-topic filters have no
    # canonical URL, so no crumb beyond the city.
    _tl = get_topic_labels(lang_context(request)["lang"])
    def _tlabel(t):
        return _tl.get(t, t.replace("_", " ").title())
    if city and len(topic) == 1:
        _bc = _crumbs(request, (city, f"/{_slugify(city)}"),
                      (_tlabel(topic[0]),
                       f"/{_slugify(city)}/{_topic_url_slug(topic[0], _city_locale(city))}"))
    elif city:
        _bc = _crumbs(request, (city, f"/{_slugify(city)}"))
    elif len(topic) == 1:
        _bc = _crumbs(request, (_tlabel(topic[0]), request.url.path))
    else:
        _bc = None

    return templates.TemplateResponse(request, "public_explore.html", {
        "city": city,
        "topics": topics,
        "selected_topics": topic,
        "sections": sections,
        "total": total,
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        "available_topics": available_topics,
        "country_sections": country_sections,
        "cities": cities,
        "subscribed": subscribed == "1",
        "schema_json": schema_json,
        "tag": tag,
        "tag_records": tag_records,
        "topic_venues": topic_venues,
        "topic_url_slugs": topic_url_slugs,
        "city_coords_for_js": city_coords_for_js,
        "canonical_base": _canonical_base(request, city) if city else None,
        "page_noindex": bool(city and topic and total == 0),
        "nearby_cities": _nearby_cities(request, city) if city else [],
        "breadcrumbs": _bc,
        **lang_context(request),
        # After lang_context — it would otherwise overwrite the city-aware value.
        "sister_url": _sister_url(request, city or None),
    })


@_fastapi.get("/felfedezes", response_class=HTMLResponse)
async def public_explore(
    request: Request,
    city: str = "",
    topic: list[str] = Query(default=[]),
    tag: str = "",
    subscribed: str = "",
):
    city_sl = _slugify(city) if city else ""
    if not tag:
        if city_sl and len(topic) == 1:
            qs = "?subscribed=1" if subscribed == "1" else ""
            topic_slug = _topic_url_slug(topic[0], _city_locale(city)) if city else topic[0]
            return RedirectResponse(f"/{city_sl}/{topic_slug}{qs}", status_code=301)
        if city_sl and not topic:
            return RedirectResponse(f"/{city_sl}", status_code=301)
    return await _render_explore(request, city=city, topic=topic, tag=tag, subscribed=subscribed)


@_fastapi.get("/felfedezes/{topic_slug}", response_class=HTMLResponse)
async def public_explore_topic_slug(request: Request, topic_slug: str):
    topic_name = _topic_from_url_slug(topic_slug, "hu")
    if not topic_name:
        return RedirectResponse("/felfedezes", status_code=302)
    return await _render_explore(request, topic=[topic_name])


@_fastapi.get("/explore", response_class=HTMLResponse)
async def public_explore_en(request: Request):
    qs = str(request.url.query)
    return RedirectResponse("/felfedezes" + (f"?{qs}" if qs else ""), status_code=302)


@_fastapi.get("/community/{community_id}", response_class=HTMLResponse)
async def public_community_legacy(request: Request, community_id: str):
    record = _find_community(community_id)
    if not record:
        return RedirectResponse("/", status_code=302)
    return RedirectResponse(record["community_url"], status_code=301)




@_fastapi.get("/source/{url_hash}", response_class=HTMLResponse)
async def public_source_page(request: Request, url_hash: str):
    """Public provenance page: search queries, scraped text, prompt, extracted records."""
    if not app_state.cache_manager:
        return RedirectResponse("/", status_code=302)
    entry = app_state.cache_manager.get_entry(url_hash)
    if not entry:
        return RedirectResponse("/", status_code=302)

    cfg = app_state.pipeline_cfg
    max_text_chars = cfg.deepseek_max_text_chars if cfg else 6000

    extract_user_prompt = ""
    if entry.get("raw_text") and entry.get("topic") and entry.get("city"):
        extract_user_prompt = USER_PROMPT_TEMPLATE.format(
            topic=entry.get("topic", ""),
            city=entry.get("city", ""),
            source_url=entry.get("url", ""),
            page_text=entry.get("raw_text", "")[:max_text_chars],
        )

    # Look up search cache to find what queries led to this URL
    search_queries: list[str] = entry.get("source_queries") or []

    return templates.TemplateResponse(request, "public_source.html", {
        "entry": entry,
        "extract_system_prompt": SYSTEM_PROMPT,
        "extract_user_prompt": extract_user_prompt,
        "search_queries": search_queries,
        "topic_labels": TOPIC_LABELS,
        **lang_context(request),
    })


#: Deliberately loose: one "@", a dot in the domain, no whitespace, RFC length.
_EMAIL_RE = re.compile(r"(?=.{3,254}$)[^@\s]+@[^@\s]+\.[^@\s]+")

#: Free text a visitor may type: a name, a new value, a note, a message.
_FORM_NAME_MAX = 200
_FORM_TEXT_MAX = 5000

#: A town someone asks us to add: letters, spaces, dots, hyphens, apostrophes.
#: The scanner's "1')" and "98766" are not towns.
_TOWN_NAME_RE = re.compile(r"[^\W\d_](?:[^\W\d_]|[ .'\-]){1,99}")


def _form_email(value: str) -> str:
    """The address if it looks like one, else ""."""
    value = (value or "").strip()
    return value if _EMAIL_RE.fullmatch(value) else ""


def _own_page_url(request: Request, url: str) -> str:
    """A page of this site, or "". It is printed as a link in the operator's mail,
    so a `javascript:` URL or someone else's site must not survive."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""
    if parts.scheme in ("http", "https") and parts.hostname and parts.hostname == request.url.hostname:
        return url[:2000]
    return ""


def _known_city(name: str) -> str:
    return name if any(c.name == name for c in (app_state.cities or [])) else ""


def _known_topic(name: str) -> str:
    return name if any(t.name == name for t in (app_state.topics or [])) else ""


@_fastapi.post("/subscribe")
async def public_subscribe(
    request: Request,
    email: str = Form(...),
    city: str = Form(...),
    topics: list[str] = Form(default=[]),
):
    # Only values the site itself offers. An SQL-injection scanner (2026-10-03)
    # turned each probe into a stored row and a notification e-mail; the insert
    # is parameterised, so nothing ran, but a probe is not a subscription.
    topics = [t for t in topics if _known_topic(t)]
    city = _known_city(city)
    email = _form_email(email)
    city_sl = _slugify(city) if city else ""
    city_locale = _city_locale(city) if city else "en"
    if not app_state.db_path or not email or not city or not topics:
        if city_sl and len(topics) == 1:
            return RedirectResponse(f"/{city_sl}/{_topic_url_slug(topics[0], city_locale)}", status_code=302)
        return RedirectResponse(
            f"/felfedezes?city={city}&" + "&".join(f"topic={t}" for t in topics),
            status_code=302,
        )
    from ..db import save_subscription
    for t in topics:
        save_subscription(app_state.db_path, email, city, t)

    if _FEEDBACK_EMAIL and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            # Off the loop: a blocking HTTPS round-trip to Resend stalled every
            # request on the site for its duration.
            await asyncio.to_thread(resend.Emails.send, {
                "from": _RESEND_FROM,
                "to": _FEEDBACK_EMAIL,
                "subject": f"[kozossegek.com] Új feliratkozás — {city}",
                "html": (
                    f"<p><b>Email:</b> {html.escape(email)}<br>"
                    f"<b>Város:</b> {html.escape(city)}<br>"
                    f"<b>Kategóriák:</b> {html.escape(', '.join(topics))}</p>"
                ),
            })
        except Exception as exc:
            log.warning("subscribe_email_failed", error=str(exc))

    if city_sl and len(topics) == 1:
        return RedirectResponse(f"/{city_sl}/{_topic_url_slug(topics[0], city_locale)}?subscribed=1", status_code=302)
    qs = f"city={city}&" + "&".join(f"topic={t}" for t in topics) + "&subscribed=1"
    return RedirectResponse(f"/felfedezes?{qs}", status_code=302)


_FEEDBACK_EMAIL = os.environ.get("FEEDBACK_EMAIL", "")
_RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
_RESEND_FROM = os.environ.get("RESEND_FROM", "onboarding@resend.dev")


@_fastapi.post("/feedback")
async def public_feedback(
    request: Request,
    community_name: str = Form(""),
    city: str = Form(""),
    topic: str = Form(""),
    page_url: str = Form(""),
    message: str = Form(""),
    user_email: str = Form(""),
):
    # The message is the visitor's own words; everything around it is context
    # the page supplied, so a value the site never offered is dropped.
    message = message.strip()[:_FORM_TEXT_MAX]
    community_name = community_name.strip()[:_FORM_NAME_MAX]
    city, topic = _known_city(city), _known_topic(topic)
    user_email = _form_email(user_email)
    page_url = _own_page_url(request, page_url)
    if _FEEDBACK_EMAIL and message and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            safe_user_email = html.escape(user_email)
            safe_page_url = html.escape(page_url, quote=True)
            safe_message = html.escape(message).replace("\n", "<br>")
            reply_line = f"<b>Reply-to:</b> {safe_user_email}<br>" if user_email else ""
            # Off the loop: a blocking HTTPS round-trip to Resend stalled every
            # request on the site for its duration.
            await asyncio.to_thread(resend.Emails.send, {
                "from": _RESEND_FROM,
                "to": _FEEDBACK_EMAIL,
                "reply_to": user_email or None,
                "subject": f"[kozossegek.com feedback] {community_name} - {city}",
                "html": (
                    f"<p><b>Community:</b> {html.escape(community_name)}<br>"
                    f"<b>City:</b> {html.escape(city)}<br>"
                    f"<b>Topic:</b> {html.escape(topic)}<br>"
                    f"{reply_line}"
                    f"<b>Page:</b> <a href='{safe_page_url}'>{safe_page_url}</a></p>"
                    f"<hr><p>{safe_message}</p>"
                ),
            })
            log.info("feedback_email_sent", to=_FEEDBACK_EMAIL, community=community_name)
        except Exception as exc:
            log.warning("feedback_email_failed", error=str(exc))
    return JSONResponse({"ok": True})


@_fastapi.post("/claim-community")
async def public_claim_community(
    request: Request,
    community_id: str = Form(""),
    community_name: str = Form(""),
    city: str = Form(""),
    page_url: str = Form(""),
    claimant_email: str = Form(""),
):
    if not community_id or not claimant_email:
        return JSONResponse({"ok": False, "error": "missing_fields"})
    claimant_email = _form_email(claimant_email)
    if not claimant_email:
        return JSONResponse({"ok": False, "error": "invalid_email"})
    # The name and city come from the record, never the form: a claim names a
    # community that exists, and the operator's mail must not carry spoofed text.
    record = find_community_by_id(_db(), community_id) if app_state.db_path else None
    if not record:
        return JSONResponse({"ok": False, "error": "unknown_community"})
    community_name, city = record.get("name", ""), record.get("city", "")
    page_url = _own_page_url(request, page_url)
    # Persist before mailing. A claim is the strongest signal the public site
    # produces — someone running the group types their address in and asks for
    # it — and it was going out as an email and nowhere else. With no key set,
    # or a Resend failure, it vanished while the visitor was told "ok".
    if app_state.db_path:
        try:
            from ..db import init_db, save_edit_request
            await asyncio.to_thread(init_db, app_state.db_path)
            await asyncio.to_thread(
                save_edit_request, app_state.db_path, "community", community_id,
                community_name, city, "", "", "claim", None, page_url, claimant_email)
        except Exception as exc:  # noqa: BLE001 — never fail the visitor's request
            log.warning("claim_store_failed", error=str(exc))
    if _FEEDBACK_EMAIL and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            safe_page = html.escape(page_url, quote=True)
            # Off the loop: a blocking HTTPS round-trip to Resend stalled every
            # request on the site for its duration.
            await asyncio.to_thread(resend.Emails.send, {
                "from": _RESEND_FROM,
                "to": _FEEDBACK_EMAIL,
                "reply_to": claimant_email or None,
                "subject": f"[kozossegek.com] Közösség igénylés — {community_name} ({city})",
                "html": (
                    f"<p><b>Közösség:</b> {html.escape(community_name)}<br>"
                    f"<b>Város:</b> {html.escape(city)}<br>"
                    f"<b>Igénylő email:</b> {html.escape(claimant_email)}<br>"
                    f"<b>Oldal:</b> <a href='{safe_page}'>{safe_page}</a></p>"
                ),
            })
            log.info("claim_email_sent", community=community_name, claimant=claimant_email)
        except Exception as exc:
            log.warning("claim_email_failed", error=str(exc))
    return JSONResponse({"ok": True})


@_fastapi.post("/report-not-community")
async def public_report_not_community(
    request: Request,
    community_id: str = Form(""),
    community_name: str = Form(""),
    city: str = Form(""),
    topic: str = Form(""),
    source_url: str = Form(""),
    page_url: str = Form(""),
):
    if not community_id or not app_state.db_path:
        return JSONResponse({"ok": False})
    record = find_community_by_id(_db(), community_id)
    if not record:
        return JSONResponse({"ok": False, "error": "unknown_community"})
    community_name, city = record.get("name", ""), record.get("city", "")
    # One community_id spans its topic rows; keep the page's topic if it is real.
    topic = _known_topic(topic) or record.get("topic", "")
    own_urls = {record.get("source_url")} | set(record.get("source_urls") or [])
    source_url = source_url if source_url in own_urls else ""
    page_url = _own_page_url(request, page_url)
    save_not_community_report(
        _db(), community_id, community_name, city, topic, source_url, page_url
    )
    log.info("not_community_reported", name=community_name, city=city)
    if _FEEDBACK_EMAIL and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            safe_page = html.escape(page_url, quote=True)
            # Off the loop: a blocking HTTPS round-trip to Resend stalled every
            # request on the site for its duration.
            await asyncio.to_thread(resend.Emails.send, {
                "from": _RESEND_FROM,
                "to": _FEEDBACK_EMAIL,
                "subject": f"[kozossegek.com] Nem közösség — {community_name}",
                "html": (
                    f"<p><b>Közösség:</b> {html.escape(community_name)}<br>"
                    f"<b>Város:</b> {html.escape(city)}<br>"
                    f"<b>Topic:</b> {html.escape(topic)}<br>"
                    f"<b>Oldal:</b> <a href='{safe_page}'>{safe_page}</a></p>"
                ),
            })
        except Exception as exc:
            log.warning("report_not_community_email_failed", error=str(exc))
    return JSONResponse({"ok": True})


@_fastapi.post("/suggest-edit")
async def public_suggest_edit(
    entity_type: str = Form("community"),
    entity_id: str = Form(""),
    entity_name: str = Form(""),
    entity_city: str = Form(""),
    entity_topic: str = Form(""),
    record_key: str = Form(""),
    change_type: str = Form(""),
    new_value: str = Form(""),
    notes: str = Form(""),
    email: str = Form(""),
):
    if not entity_name or not change_type:
        return JSONResponse({"ok": False, "error": "missing_fields"})
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    if entity_type not in {"community", "venue"}:
        return JSONResponse({"ok": False, "error": "invalid_entity_type"})
    community_change_types = {"wrong_city", "wrong_topic", "name_correction", "archive", "delete"}
    venue_change_types = {"wrong_info", "closed", "name_correction"}
    valid_types = community_change_types if entity_type == "community" else venue_change_types
    if change_type not in valid_types:
        return JSONResponse({"ok": False, "error": "invalid_change_type"})
    if change_type in {"wrong_city", "wrong_topic", "name_correction"} and not new_value.strip():
        return JSONResponse({"ok": False, "error": "missing_new_value"})
    # Approval applies the edit by record_key, so the request must resolve to
    # a record that exists; its name, city and topic come from that record.
    if entity_type == "community":
        record = get_community_by_record_key(_db(), record_key) if record_key else None
        if record:
            entity_id = record.get("community_id", "")
            entity_topic = record.get("topic", "")
    else:
        from ..db import get_venue_by_record_key
        record = get_venue_by_record_key(_db(), record_key)
        if record:
            entity_id, entity_topic = record.get("venue_id", ""), ""
    if not record:
        return JSONResponse({"ok": False, "error": "unknown_entity"})
    entity_name, entity_city = record.get("name", ""), record.get("city", "")
    if email.strip() and not _form_email(email):
        return JSONResponse({"ok": False, "error": "invalid_email"})
    new_value = new_value.strip()[:_FORM_NAME_MAX]
    notes = notes.strip()[:_FORM_TEXT_MAX]
    if (change_type == "wrong_city" and not _known_city(new_value)) or (
            change_type == "wrong_topic" and not _known_topic(new_value)):
        return JSONResponse({"ok": False, "error": "invalid_new_value"})
    save_edit_request(
        _db(), entity_type, entity_id, entity_name, entity_city, entity_topic,
        record_key, change_type, new_value.strip() or None, notes.strip(), email.strip(),
    )
    log.info("edit_request_submitted", entity=entity_name, change_type=change_type)
    if _FEEDBACK_EMAIL and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            _type_labels = {
                "wrong_city": "Rossz város", "wrong_topic": "Rossz kategória",
                "name_correction": "Névpontosítás", "archive": "Megszűnt",
                "delete": "Törlés", "wrong_info": "Hibás adat", "closed": "Bezárt",
            }
            type_label = _type_labels.get(change_type, change_type)
            new_val_clean = new_value.strip()
            notes_clean = notes.strip()
            email_clean = email.strip()
            # Off the loop: a blocking HTTPS round-trip to Resend stalled every
            # request on the site for its duration.
            await asyncio.to_thread(resend.Emails.send, {
                "from": _RESEND_FROM,
                "to": _FEEDBACK_EMAIL,
                "reply_to": email_clean or None,
                "subject": f"[kozossegek.com] Szerkesztési kérés — {entity_name} ({type_label})",
                "html": (
                    f"<p><b>{html.escape(entity_type).title()}:</b> {html.escape(entity_name)}<br>"
                    f"<b>Város:</b> {html.escape(entity_city)}<br>"
                    f"<b>Változás:</b> {html.escape(type_label)}"
                    f"{'<br><b>Új érték:</b> ' + html.escape(new_val_clean) if new_val_clean else ''}"
                    f"{'<br><b>Megjegyzés:</b> ' + html.escape(notes_clean) if notes_clean else ''}"
                    f"{'<br><b>Email:</b> ' + html.escape(email_clean) if email_clean else ''}"
                    f"</p>"
                ),
            })
        except Exception as exc:
            log.warning("suggest_edit_email_failed", error=str(exc))
    return JSONResponse({"ok": True})


@_fastapi.get("/unsubscribe", response_class=HTMLResponse)
async def public_unsubscribe(request: Request, token: str = ""):
    removed = False
    if token and app_state.db_path:
        from ..db import delete_subscription
        removed = delete_subscription(app_state.db_path, token)
    return templates.TemplateResponse(request, "public_unsubscribe.html", {"removed": removed, **lang_context(request)})


@_fastapi.get("/api/city-topics")
async def api_city_topics(city: str = ""):
    """Return per-topic community counts for a city (used by home page JS)."""
    if not city:
        return JSONResponse({})
    # One indexed GROUP BY, off the loop. It loaded and parsed every record of
    # each of ~36 topics just to count them, on the event loop, and the home
    # page calls it as the visitor types.
    counts = await asyncio.to_thread(get_topic_counts_for_city, _db(), city) \
        if app_state.db_path else {}
    return JSONResponse({t.name: counts.get(t.name, 0) for t in (app_state.topics or [])})


@_fastapi.get("/set-lang")
async def set_lang(lang: str = "en", next: str = "/"):
    from .i18n import LANGUAGES
    if lang not in LANGUAGES:
        lang = "en"
    safe_next = _safe_redirect_target(next, "/")
    resp = RedirectResponse(safe_next, status_code=302, headers={"X-Robots-Tag": "noindex, nofollow"})
    resp.set_cookie("lang", lang, max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


async def _render_map(request: Request):
    from ..db import get_city_totals
    city_totals = dict(get_city_totals(_db())) if app_state.db_path else {}
    cities_data = []
    for city in _site_cities(request):
        coords = CITY_COORDS.get(city.name)
        if not coords:
            continue
        count = city_totals.get(city.name, 0)
        cities_data.append({
            "name": city.name,
            "lat": coords[0],
            "lng": coords[1],
            "count": count,
        })

    total = sum(c["count"] for c in cities_data)
    cities_with_data = [c for c in cities_data if c["count"] > 0]
    return templates.TemplateResponse(request, "public_map.html", {
        "cities_json": json.dumps(cities_with_data),
        "total": total,
        "cities_with_data": len(cities_with_data),
        "cities_tracked": len(cities_data),
        **lang_context(request),
    })


@_fastapi.get("/terkep", response_class=HTMLResponse)
async def public_map(request: Request):
    from .i18n import _detect_site
    if _detect_site(request) == "meetapedia":
        return RedirectResponse("/map", status_code=301)
    return await _render_map(request)


def _country_from_slug(slug: str) -> str | None:
    for c in (app_state.cities or []):
        if c.country and _slugify(c.country) == slug:
            return c.country
    return None


def _render_cities_page(request: Request, requested: str, country: str):
    from .i18n import _detect_site
    city_totals = dict(get_city_totals(_db())) if app_state.db_path else {}
    site_cities = _site_cities(request)
    if country:
        site_cities = [c for c in site_cities if c.country == country]
    # Alphabetical, not count-first: the page now carries thousands of cities and
    # the A-Z jump bar plus the free-text filter are how people find one. A
    # popularity ordering makes both of those useless — you cannot scan to "L".
    cities_list = sorted(
        [{"name": c.name, "slug": _slugify(c.name), "count": city_totals.get(c.name, 0)} for c in site_cities],
        key=lambda c: _hu_sort_key(c["name"]),
    )
    # country index above the flat city grid (intl site, unfiltered view only)
    countries_list: list[dict] = []
    if not country and _detect_site(request) == "meetapedia":
        by_country: dict[str, dict] = {}
        for c in site_cities:
            if not c.country:
                continue
            e = by_country.setdefault(c.country, {
                "name": c.country, "slug": _slugify(c.country), "cities": 0, "count": 0})
            e["cities"] += 1
            e["count"] += city_totals.get(c.name, 0)
        # Alphabetical, matching the city grid below it — a reader looking for
        # one country scans, they do not rank.
        countries_list = sorted(by_country.values(), key=lambda e: e["name"])
    # Cities with content are server-rendered as real links: they carry the
    # internal linking this page exists for. Empty ones are shipped as compact
    # JSON and built client-side — they are already non-links (a "soon" chip),
    # so nothing is lost for crawlers, while ~250 bytes of Tailwind markup each
    # becomes ~35. At 3.8K cities that is the difference between a 1.3 MB page
    # and a 0.3 MB one, and the CDN Tailwind JIT scans the whole initial DOM
    # before the page paints. See [[indexing-strategy]] for why empty pages are
    # deliberately not linked.
    linked_cities = [c for c in cities_list if c["count"] > 0]
    pending_cities = [c for c in cities_list if c["count"] <= 0]
    return templates.TemplateResponse(request, "public_cities.html", {
        "cities_list": linked_cities,
        "pending_json": json.dumps(
            [{"n": c["name"], "s": c["slug"]} for c in pending_cities],
            ensure_ascii=False, separators=(",", ":"),
        ),
        "pending_count": len(pending_cities),
        "total_cities": len(cities_list),
        "requested": requested,
        "country_filter": country,
        "countries_list": countries_list,
        **lang_context(request),
        # A country page only has a twin when that country exists on the other
        # site — kozossegek.com knows Hungary alone.
        "sister_url": (_sister_url(request, None) if not country
                       else (_sister_url(request, None) if country == "Hungary"
                             or _detect_site(request) == "kozossegek" else None)),
    })


@_fastapi.get("/cities", response_class=HTMLResponse)
@_fastapi.get("/varosok", response_class=HTMLResponse)
async def public_cities(request: Request, requested: str = "", country: str = ""):
    country = country.strip()
    if country:
        # legacy query-param form → permanent redirect to the path-based URL
        cities_url = lang_context(request)["cities_url"]
        slug = _slugify(country)
        if _country_from_slug(slug):
            return RedirectResponse(f"{cities_url}/{slug}", status_code=301)
        return RedirectResponse(cities_url, status_code=302)
    return _render_cities_page(request, requested, "")


@_fastapi.get("/cities/{country_slug}", response_class=HTMLResponse)
@_fastapi.get("/varosok/{country_slug}", response_class=HTMLResponse)
async def public_cities_country(request: Request, country_slug: str):
    country = _country_from_slug(country_slug)
    if not country or not any(c.country == country for c in _site_cities(request)):
        return RedirectResponse(lang_context(request)["cities_url"], status_code=302)
    return _render_cities_page(request, "", country)


@_fastapi.post("/varosok/kerelem")
async def request_city(request: Request, city_name: str = Form(""), email: str = Form("")):
    city_name = city_name.strip()
    # A town we do not list yet, so it cannot be checked against cities.yaml —
    # only against looking like a place name (the 2026-10-03 scanner sent "1')").
    if not _TOWN_NAME_RE.fullmatch(city_name):
        return RedirectResponse("/varosok", status_code=303)
    if app_state.db_path:
        save_city_request(app_state.db_path, city_name, _form_email(email))
    return RedirectResponse("/varosok?requested=" + _url_quote(city_name), status_code=303)


@_fastapi.get("/admin", response_class=HTMLResponse)
async def admin_root_redirect():
    return RedirectResponse("/admin/", status_code=301)


@_fastapi.get("/rolunk", response_class=HTMLResponse)
async def public_about(request: Request):
    # Counts only. The page used to carry every topic tile and every city of
    # the site A–Z (1,312 on kozossegek.com), which no visitor reads on an
    # About page; the explore and cities pages are where those lists belong.
    site_cities = _site_cities(request)
    from .i18n import _detect_site
    site = _detect_site(request)
    site_topic_counts = _hu_topic_counts() if site == "kozossegek" else _global_topic_counts()
    return templates.TemplateResponse(request, "public_about.html", {
        "city_count": len({c.name for c in site_cities}),
        "total_records": sum(site_topic_counts.values()),
        **lang_context(request),
    })


@_fastapi.get("/about", response_class=HTMLResponse)
async def public_about_en(request: Request):
    return RedirectResponse("/rolunk", status_code=302)


@_fastapi.get("/map", response_class=HTMLResponse)
async def public_map_en(request: Request):
    from .i18n import _detect_site
    if _detect_site(request) == "kozossegek":
        return RedirectResponse("/terkep", status_code=301)
    return await _render_map(request)


@_fastapi.get("/api/cities.json")
async def public_cities_json(request: Request):
    """City names for the pickers that used to be server-rendered on every page.

    One cacheable download shared by every page on the site, instead of 176 KB
    of <option> inlined into each of 42,091 documents. Site-scoped: the HU
    domain offers Hungarian cities, exactly as the old server-rendered list did.
    """
    from fastapi.responses import JSONResponse as _JSONResponse
    names = sorted((c.name for c in _site_cities(request)), key=_hu_sort_key)
    return _JSONResponse(
        {"cities": names},
        # The list changes when a city is added — a deploy. A day is safe and
        # makes this free for anyone reading more than one page.
        headers={"Cache-Control": "public, max-age=86400"},
    )


@_fastapi.get("/submit-community", response_class=HTMLResponse)
@_fastapi.get("/kozosseg-bekuldes", response_class=HTMLResponse)
async def submit_community_get(request: Request, city: str = "", topic: str = ""):
    init_db(_db())
    submitted = request.query_params.get("submitted") == "1"
    # Site-scoped, like every other city list on the public site. It was
    # offering all 3,914 configured cities on both domains, so a visitor to the
    # Hungarian site could submit a Swedish community to a site that will never
    # show it — and paid 273 KB of <option> for the privilege. This select is
    # `required` and posted as a plain form, so it stays server-rendered: on
    # the page where a broken control costs the conversion outright, half the
    # weight is worth more than all of it.
    all_cities = sorted((c.name for c in _site_cities(request)), key=_hu_sort_key)
    _topic_labels = get_topic_labels(lang_context(request)["lang"])
    all_topics = [
        {"name": t.name, "label": _topic_labels.get(t.name, t.name.replace("_", " ").title())}
        for t in sorted(app_state.topics or [], key=lambda t: t.name)
    ]
    return templates.TemplateResponse(request, "public_submit_community.html", {
        "submitted": submitted,
        "city": city,
        "topic": topic,
        "all_cities": all_cities,
        "all_topics": all_topics,
        **lang_context(request),
    })


@_fastapi.post("/submit-community")
@_fastapi.post("/kozosseg-bekuldes")
async def submit_community_post(
    request: Request,
    name: str = Form(""),
    city: str = Form(""),
    topic: str = Form(""),
    source_url: str = Form(""),
    submitter_email: str = Form(""),
):
    if not all([name.strip(), city.strip(), topic.strip(), source_url.strip()]):
        return JSONResponse({"error": "missing_required_field"}, status_code=400)
    if not is_public_http_url(source_url.strip()):
        return JSONResponse({"error": "invalid_source_url"}, status_code=400)
    # Both are <select>s on the form; anything else did not come from it.
    if not _known_city(city.strip()) or not _known_topic(topic.strip()):
        return JSONResponse({"error": "invalid_city_or_topic"}, status_code=400)
    init_db(_db())
    save_community_submission(
        _db(), name.strip()[:_FORM_NAME_MAX], city.strip(), topic.strip(),
        source_url.strip(), _form_email(submitter_email) or None,
    )
    return RedirectResponse(lang_context(request)["submit_url"] + "?submitted=1", status_code=302)


@_fastapi.get("/robots.txt")
async def robots_txt(request: Request):
    from fastapi.responses import PlainTextResponse
    from .i18n import _detect_site
    site = _detect_site(request)
    site_url = "https://meetapedia.com" if site == "meetapedia" else "https://kozossegek.com"
    return PlainTextResponse(
        "User-agent: facebookexternalhit\n"
        "Allow: /\n"
        "\n"
        "User-agent: *\n"
        "Disallow: /admin\n"
        "Disallow: /source/\n"
        "Disallow: /api/\n"
        "Disallow: /set-lang\n"
        "Disallow: /unsubscribe\n"
        "Disallow: /community/\n"
        "Disallow: /healthz\n"
        "Disallow: /kereses\n"
        # contentsignals.org: what may be done with what is crawled here.
        # Being read and cited by an answer engine is the point of the site,
        # so all three are yes — a `no` would not keep the data private (it is
        # public) and would only keep us out of the answers.
        "Content-Signal: search=yes, ai-input=yes, ai-train=yes\n"
        "\n"
        f"Sitemap: {site_url}/sitemap.xml\n"
        f"# A plain-text map of this site for language models: {site_url}/llms.txt\n"
    )


@_fastapi.get("/indexnow-key.txt")
async def indexnow_key():
    """Proof for IndexNow that we own this host: the file holds the key."""
    from fastapi.responses import PlainTextResponse
    from ..indexnow import valid_key
    key = valid_key(os.environ.get("INDEXNOW_KEY"))
    if not key:
        return PlainTextResponse("Not found", status_code=404)
    return PlainTextResponse(key)


#: llms.txt per site, with the time it was built. It reads the whole corpus's
#: city totals; an hour is fresher than any assistant refetches it.
_LLMS_TXT_CACHE: dict[str, tuple[float, str]] = {}


@_fastapi.get("/llms.txt")
async def llms_txt(request: Request):
    """A plain-text map of the site for language models (llmstxt.org).

    Every sentence about what the site is and how its data is made comes from
    the About page's own i18n strings, so the two cannot disagree; the numbers
    are counted, not written. No instructions to models — only a description
    and links.
    """
    from fastapi.responses import PlainTextResponse
    ctx = lang_context(request)
    cached = _LLMS_TXT_CACHE.get(ctx["site"])
    if cached and time.monotonic() - cached[0] < 3600:
        return PlainTextResponse(cached[1])
    text = await asyncio.to_thread(_build_llms_txt, request, ctx)
    _LLMS_TXT_CACHE[ctx["site"]] = (time.monotonic(), text)
    return PlainTextResponse(text)


def _build_llms_txt(request: Request, ctx: dict) -> str:
    t, base, site = ctx["t"], ctx["site_url"], ctx["site"]
    hu = site == "kozossegek"
    site_names = {c.name for c in _site_cities(request)}
    if not hu:
        site_names -= _hu_city_names()
    totals = [(c, n) for c, n in (get_city_totals(_db()) if app_state.db_path else [])
              if c in site_names and n > 0]
    communities = sum(n for _, n in totals)
    guides = get_data_guides(_db(), site, limit=50) if app_state.db_path else []

    def num(n: int) -> str:
        # Hungarian groups thousands with a space; "73,219" reads as a decimal.
        return f"{n:,}".replace(",", "\u00a0") if hu else f"{n:,}"

    summary = (f"{num(communities)} helyi közösség, klub és csoport {num(len(totals))} "
               f"magyarországi településen, nyilvános weboldalakról gyűjtve, városonként és témánként."
               if hu else
               f"{num(communities)} local communities, clubs and groups in {num(len(totals))} cities "
               f"worldwide, collected from public web pages, by city and by interest.")
    lines = [
        f"# {ctx['site_name']}", "", f"> {summary}", "",
        t("about_description").format(site_name=ctx["site_name"]), "",
        f"{t('about_how_it_works')}: {t('about_how_it_works_text')}", "",
        f"{t('about_data_quality')}: {t('about_data_quality_text')}", "",
    ]
    if hu:
        lines += [
            "## Fő oldalak", "",
            f"- [Települések]({base}/varosok): minden település, ahol van közösség",
            f"- [Adatútmutatók]({base}/utmutatok): város és téma szerinti összefoglalók a gyűjtött adatokból",
            f"- [Térkép]({base}/terkep): a közösségek térképen",
            f"- [Helyszínek]({base}/helyszinek): ahol a közösségek találkoznak",
            f"- [Rólunk]({base}/rolunk): mi ez az oldal, és hogyan készül az adat",
            f"- [Közösség beküldése]({base}/kozosseg-bekuldes): hiányzó közösség bejelentése",
            "- [International edition](https://meetapedia.com): ugyanez a világ többi részére, angolul",
        ]
    else:
        lines += [
            "## Main pages", "",
            f"- [Cities]({base}/cities): every city with listed communities, by country",
            f"- [Map]({base}/map): communities on a map",
            f"- [About]({base}/rolunk): what this site is and how the data is made",
            f"- [Submit a community]({base}/submit-community): report a missing group",
            "- [Hungarian edition](https://kozossegek.com): the same directory for Hungary, in Hungarian",
        ]
        if guides:
            lines.append(f"- [Data guides]({base}/guides): city × topic summaries of the collected data")
    if guides:
        prefix = "/utmutatok" if hu else "/guides"
        lines += ["", "## Adatútmutatók" if hu else "## Data guides", ""]
        lines += [f"- [{g['title']}]({base}{prefix}/{g['slug']}): {' '.join((g.get('summary') or '').split())}"
                  for g in guides]
    if totals:
        lines += ["", "## Települések" if hu else "## Cities", ""]
        word = "közösség" if hu else "communities"
        lines += [f"- [{c}]({base}/{_slugify(c)}): {n} {word}" for c, n in totals[:60]]
    return "\n".join(lines).rstrip() + "\n"


#: Sitemap entries per site, with the time they were built. Pure SQL + string
#: assembly over the whole corpus; Google refetches on its own schedule and
#: does not need it fresher than this.
_SITEMAP_CACHE: dict[str, tuple[float, list]] = {}
_SITEMAP_TTL = 3600.0
#: URLs per sitemap file. The protocol's hard limit is 50,000, and a file over
#: it is rejected whole: meetapedia.com served 64,666 in one `<urlset>` after
#: venue and person pages were added (found 2026-09-24). Above this
#: `/sitemap.xml` becomes an index of `/sitemap-N.xml` parts.
_SITEMAP_MAX_URLS = 45_000


@_fastapi.get("/utmutatok", response_class=HTMLResponse)
@_fastapi.get("/guides", response_class=HTMLResponse)
async def public_guides(request: Request, page: int = Query(1, ge=1)):
    """Bounded, paginated index of the daily data-derived guides."""
    from .i18n import _detect_site
    site = _detect_site(request)
    canonical_path = "/utmutatok" if site == "kozossegek" else "/guides"
    if request.url.path != canonical_path:
        return RedirectResponse(canonical_path, status_code=301)
    init_db(_db())
    per_page = 24
    total = count_data_guides(_db(), site)
    guides = get_data_guides(_db(), site, limit=per_page, offset=(page - 1) * per_page)
    return templates.TemplateResponse(request, "public_guides.html", {
        "guides": guides, "page": page, "pages": max(1, (total + per_page - 1) // per_page),
        "canonical_path": canonical_path, **lang_context(request),
    })


@_fastapi.get("/utmutatok/{slug}", response_class=HTMLResponse)
@_fastapi.get("/guides/{slug}", response_class=HTMLResponse)
async def public_guide(request: Request, slug: str):
    from .i18n import _detect_site
    site = _detect_site(request)
    prefix = "/utmutatok" if site == "kozossegek" else "/guides"
    if not request.url.path.startswith(prefix + "/"):
        return RedirectResponse(f"{prefix}/{slug}", status_code=301)
    init_db(_db())
    guide = get_data_guide(_db(), slug, site)
    if not guide:
        return HTMLResponse("Not found", status_code=404)
    data = guide["data"]
    city_locale = _city_locale(guide["city"])
    page_url = f"{lang_context(request)['site_url']}{prefix}/{slug}"
    schema_json = article_jsonld(
        guide["title"], guide["summary"], page_url,
        guide["published_at"], guide["updated_at"], lang_context(request)["site_name"])
    return templates.TemplateResponse(request, "public_guide.html", {
        "guide": guide, "guide_data": data, "canonical_path": f"{prefix}/{slug}",
        "guides_path": prefix,
        "listing_url": f"/{_slugify(guide['city'])}/{_topic_url_slug(guide['topic'], city_locale)}",
        "schema_json": schema_json,
        "breadcrumbs": [
            {"name": lang_context(request)["site_name"], "url": lang_context(request)["site_url"] + "/"},
            {"name": "Útmutatók" if guide["locale"] == "hu" else "Guides",
             "url": lang_context(request)["site_url"] + prefix},
            {"name": guide["title"], "url": page_url},
        ],
        **lang_context(request),
    })


async def _sitemap_entries(request: Request) -> tuple[dict, list]:
    # Local import: other functions in this module already bind `_time`
    # locally, and a module-level name would be shadowed inside them.
    import time as _time

    ctx = lang_context(request)
    cache_key = ctx.get("site") or "kozossegek"
    cached = _SITEMAP_CACHE.get(cache_key)
    if cached and (_time.monotonic() - cached[0]) < _SITEMAP_TTL:
        return ctx, cached[1]
    # Built in a worker thread: every query below is blocking sqlite, and on the
    # event loop it stalls unrelated requests for as long as it runs.
    entries = await asyncio.to_thread(_build_sitemap, ctx)
    _SITEMAP_CACHE[cache_key] = (_time.monotonic(), entries)
    return ctx, entries


@_fastapi.get("/sitemap.xml")
async def sitemap(request: Request):
    from fastapi.responses import Response as _Response
    ctx, entries = await _sitemap_entries(request)
    if len(entries) <= _SITEMAP_MAX_URLS:
        return _Response(_render_urlset(entries), media_type="application/xml")
    parts = (len(entries) + _SITEMAP_MAX_URLS - 1) // _SITEMAP_MAX_URLS
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    lines += [f"  <sitemap><loc>{ctx['site_url']}/sitemap-{n}.xml</loc></sitemap>"
              for n in range(1, parts + 1)]
    lines.append("</sitemapindex>")
    return _Response("\n".join(lines), media_type="application/xml")


@_fastapi.get("/sitemap-{part}.xml")
async def sitemap_part(request: Request, part: int):
    from fastapi.responses import Response as _Response
    _ctx, entries = await _sitemap_entries(request)
    chunk = entries[(part - 1) * _SITEMAP_MAX_URLS: part * _SITEMAP_MAX_URLS] if part >= 1 else []
    if not chunk:
        return _Response("Not found", status_code=404)
    return _Response(_render_urlset(chunk), media_type="application/xml")


def _build_sitemap(ctx: dict) -> list[tuple[str, str | None]]:
    """Every canonical URL of this site with its lastmod, deduplicated."""
    base = ctx["site_url"]
    is_meetapedia = ctx.get("site") == "meetapedia"
    site_city_names = {
        c.name for c in (app_state.cities or [])
        if is_meetapedia or (c.country == "Hungary")
    }
    if is_meetapedia:
        # HU-city pages canonicalize to kozossegek.com — a sitemap must only
        # list canonical URLs, so they are omitted here.
        site_city_names -= _hu_city_names()
    if is_meetapedia:
        # About/explore still render at their HU paths on both domains; the
        # English aliases redirect and must not be submitted as canonicals.
        static_paths = ["/", "/rolunk", "/map", "/people", "/cities", "/felfedezes", "/submit-community", "/guides"]
    else:
        static_paths = ["/", "/rolunk", "/terkep", "/varosok", "/felfedezes", "/helyszinek", "/emberek", "/kozosseg-bekuldes", "/utmutatok"]

    locs: list[str] = [base + p for p in static_paths]
    lastmods: dict[str, str] = {}  # loc → YYYY-MM-DD (community pages only)

    if app_state.db_path:
        init_db(app_state.db_path)
        lastmod_map = get_community_lastmods(_db())
        guide_prefix = "/guides" if is_meetapedia else "/utmutatok"
        for slug, lastmod in get_data_guide_sitemap_rows(
                _db(), "meetapedia" if is_meetapedia else "kozossegek"):
            loc = f"{base}{guide_prefix}/{slug}"
            locs.append(loc)
            lastmods[loc] = lastmod

        if is_meetapedia:
            # country landing pages (/cities/<slug>) — only countries with live
            # content; Hungary is omitted because HU content is kozossegek-canonical
            totals = dict(get_city_totals(_db()))
            countries = sorted({
                c.country for c in (app_state.cities or [])
                if c.country and c.name in site_city_names and totals.get(c.name, 0) > 0
            })
            locs.extend(f"{base}/cities/{_slugify(cn)}" for cn in countries)

        counts = get_city_topic_counts(_db())
        configured_topics = {t.name for t in (app_state.topics or [])}
        # One query for every community, instead of one per city×topic pair.
        # At 3.8K cities the old loop issued thousands of queries on the event
        # loop and took >30s, blocking every other request behind it.
        by_pair = get_sitemap_communities(_db())
        for city_name, topics in counts.items():
            if city_name not in site_city_names:
                continue
            city_sl = _slugify(city_name)
            city_locale = _city_locale(city_name)
            locs.append(f"{base}/{city_sl}")
            for topic_name in topics:
                if topic_name in configured_topics:
                    topic_sl = _topic_url_slug(topic_name, city_locale)
                    locs.append(f"{base}/{city_sl}/{topic_sl}")
                # Legacy DB topics may no longer have a listing route, but
                # their community detail URLs remain valid and indexable.
                for record in by_pair.get((city_name, topic_name), ()):
                    # Described or not, every visible community is submitted.
                    # The page used to be noindexed without a description, so
                    # listing it here would have contradicted its own meta tag;
                    # now it carries its neighbours and is indexable, and
                    # leaving it out would mean Google can only find it by
                    # crawling — with 7,470 URLs already stuck in "Discovered –
                    # currently not indexed", that is the slow road to nowhere.
                    name_sl = _slugify(record["name"])
                    if name_sl:
                        loc = f"{base}/{city_sl}/{name_sl}"
                        locs.append(loc)
                        lm = lastmod_map.get((city_name, name_sl))
                        if lm and len(lm) == 10:  # YYYY-MM-DD
                            lastmods.setdefault(loc, lm)

        # Venue and person pages, on BOTH editions. They were kozossegek-only
        # until 2026-09-20, and the reason was a pair of prefixes — "/venue/"
        # and "/person/" — written for an English URL scheme that does not
        # exist: /stockholm/venue/x answers 404 while /stockholm/helyszin/x
        # answers 200 on the same domain. So meetapedia.com submitted 38,108
        # URLs and not one of its 9,282 venues or 11,554 people, and the fix is
        # to use the routes that exist rather than to add the ones that do not.
        #
        # HU cities are already out of `site_city_names` on meetapedia (they
        # canonicalize to kozossegek), so this adds the international corpus
        # there and nothing that would duplicate.
        for v in get_all_venues(app_state.db_path):
            if v.get("city", "") not in site_city_names:
                continue
            city_sl = _slugify(v.get("city", ""))
            name_sl = _slugify(v.get("name", ""))
            if city_sl and name_sl:
                locs.append(f"{base}/{city_sl}/helyszin/{name_sl}")

        seen_persons: set[tuple[str, str]] = set()
        for p in get_publishable_persons(app_state.db_path):
            if p.get("city", "") not in site_city_names:
                continue
            city_sl = _slugify(p.get("city", ""))
            name_sl = _slugify(p.get("name", ""))
            if city_sl and name_sl and (city_sl, name_sl) not in seen_persons:
                seen_persons.add((city_sl, name_sl))
                locs.append(f"{base}/{city_sl}/ember/{name_sl}")

    # Deduplicate while preserving order.
    return [(loc, lastmods.get(loc)) for loc in dict.fromkeys(locs)]


def _render_urlset(entries: list[tuple[str, str | None]]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc, lastmod in entries:
        stamp = f"<lastmod>{lastmod}</lastmod>" if lastmod else ""
        lines.append(f"  <url><loc>{loc}</loc>{stamp}<changefreq>weekly</changefreq></url>")
    lines.append("</urlset>")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES  (prefix: /admin, protected by _BasicAuth)
# ═══════════════════════════════════════════════════════════════════════════════

@admin.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    next_run = None
    schedule_cron = None
    if app_state.scheduler:
        jobs = app_state.scheduler.get_jobs()
        if jobs and jobs[0].next_run_time:
            next_run = jobs[0].next_run_time.strftime("%Y-%m-%d %H:%M UTC")
        if jobs:
            schedule_cron = str(jobs[0].trigger)

    cache_defaults = {}
    if app_state.pipeline_cfg:
        cache_defaults = {
            "skip_scraped": app_state.pipeline_cfg.cache_skip_scraped,
            "skip_extracted": app_state.pipeline_cfg.cache_skip_extracted,
        }

    run_history = []
    if app_state.db_path:
        from ..db import get_run_history
        run_history = get_run_history(app_state.db_path, limit=10)

    try:
        _settings = yaml.safe_load((CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"))
        _pipe = _settings.get("pipeline", {})
        test_mode = _pipe.get("test_mode", False)
        test_cities = _pipe.get("test_cities", [])
    except Exception:
        test_mode = False
        test_cities = []

    all_cities = app_state.cities or []
    run_countries = sorted({c.country for c in all_cities if c.country})
    run_cities = sorted([{"name": c.name, "country": c.country} for c in all_cities],
                        key=lambda c: (c["country"], c["name"]))

    return templates.TemplateResponse(request, "dashboard.html", {
        "is_running": app_state.is_running,
        "last_run_at": app_state.last_run_at,
        "next_run": next_run,
        "schedule_cron": schedule_cron,
        "cache_defaults": cache_defaults,
        "run_history": run_history,
        "test_mode": test_mode,
        "test_cities": test_cities,
        "current_run_mode": app_state.current_run_mode,
        "run_countries": run_countries,
        "run_cities": run_cities,
    })


@admin.get("/results", response_class=HTMLResponse)
async def results(request: Request):
    city_topic_counts = get_city_topic_counts(_db())
    cities_map = {c.name: c.country for c in (app_state.cities or [])}
    rows = []
    for city, topics in city_topic_counts.items():
        country = cities_map.get(city, "")
        for topic, count in topics.items():
            rows.append({"city": city, "country": country, "topic": topic, "count": count})
    return templates.TemplateResponse(request, "results.html", {"rows": rows})


@admin.get("/results/{city}/{topic}", response_class=HTMLResponse)
async def result_detail(request: Request, city: str, topic: str):
    import hashlib
    records_data = get_communities(_db(), city, topic)
    records = [CommunityRecord.model_validate(r) for r in records_data]
    url_hashes = {
        r.source_url: hashlib.sha256(r.source_url.encode()).hexdigest()[:16]
        for r in records if r.source_url
    }
    return templates.TemplateResponse(request, "result_detail.html", {
        "city": city,
        "topic": topic,
        "records": records,
        "url_hashes": url_hashes,
    })


@admin.post("/false-positive/add")
async def fp_add_route(
    name: str = Form(...),
    city: str = Form(...),
    topic: str = Form(...),
    reason: str = Form(...),
    source_url: str = Form(""),
    fp_type: str = Form("extraction"),
    redirect_to: str = Form(""),
):
    fp_add(_db(), name, city, topic, reason, source_url, fp_type=fp_type)
    return RedirectResponse(_safe_redirect_target(redirect_to, "/admin/progress"), status_code=302)


@admin.post("/false-positive/remove")
async def fp_remove_route(
    name: str = Form(...),
    city: str = Form(...),
    topic: str = Form(...),
    fp_type: str = Form("extraction"),
    redirect_to: str = Form(""),
):
    fp_remove(_db(), name, city, topic, fp_type=fp_type)
    return RedirectResponse(_safe_redirect_target(redirect_to, "/admin/progress"), status_code=302)


@admin.get("/providers", response_class=HTMLResponse)
async def providers_page(request: Request):
    """Free-tier fleet: quality ranking, today's quota burn, learned ceilings."""
    from ..providers import load_catalogue
    from ..router import QuotaLedger, utc_day

    init_db(_db())  # provider_usage may not exist on an older production DB
    catalogue = load_catalogue()
    day = utc_day()
    ledger = QuotaLedger(_db() if app_state.db_path else None, day=day)
    providers = ledger.snapshot(catalogue)

    # Which model produced each cached extraction — the input to the upgrade
    # sweep, and the honest answer to "how good is the corpus actually".
    quality_mix: list[dict] = []
    try:
        quality_mix = get_extraction_quality_mix(_db())
    except Exception as exc:
        log.warning("provider_quality_mix_failed", error=str(exc))

    # The spend guard's state, shown whether or not it has tripped. A breaker
    # nobody can see is a breaker nobody trusts — and this one was added
    # because four days of paid overspend were invisible in a table of call
    # counts (martinfowler.com/bliki/CircuitBreaker.html: state changes get
    # logged, state gets revealed, operators get a control).
    paid_budget = float(catalogue.router.daily_budget_usd or 0.0)
    paid_names = {p.name for p in catalogue.providers if p.paid}
    paid_spent = ledger.cost_total(paid_names)

    return templates.TemplateResponse(request, "providers.html", {
        "providers": providers,
        "day": day,
        "router_enabled": catalogue.router.enabled,
        "allow_paid": catalogue.router.allow_paid,
        "configured_count": sum(1 for p in providers if p["configured"]),
        "quality_mix": quality_mix,
        "paid_budget_usd": paid_budget,
        "paid_spent_usd": paid_spent,
        "paid_blocked": bool(paid_names) and (
            not catalogue.router.allow_paid
            or paid_budget <= 0
            or paid_spent >= paid_budget),
    })


@admin.get("/quarantine", response_class=HTMLResponse)
async def quarantine_page(request: Request):
    """Pages the pipeline has stopped paying to re-extract, and the way back.

    The operator half of the guard: what it is holding, why, and a button to
    release one. Without that a quarantine is indistinguishable from a page
    that silently disappeared from the corpus.
    """
    from ..db import get_extract_failures
    from ..extract import get_extract_fingerprint

    init_db(_db())
    _, _, cfg = load_config(app_state.db_path)
    fingerprint = get_extract_fingerprint(
        cfg.deepseek_fingerprint_model or cfg.deepseek_model)
    threshold = int(cfg.extract_max_page_failures or 0)
    rows = get_extract_failures(_db(), fingerprint=fingerprint, min_count=1, limit=500)
    return templates.TemplateResponse(request, "quarantine.html", {
        "rows": rows,
        "threshold": threshold,
        "fingerprint": fingerprint,
        "held": sum(1 for r in rows if threshold > 0 and r["fail_count"] >= threshold),
    })


@admin.post("/quarantine/release")
async def quarantine_release(
    request: Request,
    url_hash: str = Form(...),
    redirect_to: str = Form(""),
):
    """Let one page be attempted again — every fingerprint, not just the current
    one, because the operator is saying "try this", not "try this once"."""
    from ..db import clear_extract_failure

    clear_extract_failure(_db(), url_hash, None)
    log.info("extract_quarantine_released", url_hash=url_hash)
    return RedirectResponse(
        _safe_redirect_target(redirect_to, "/admin/quarantine"), status_code=302)


@admin.get("/prompts", response_class=HTMLResponse)
async def prompts_page(request: Request):
    fps = fp_load(_db())

    def _versioned(fp_type: str, base: str) -> list[dict]:
        history = fp_load_history(_db(), fp_type)
        out = []
        for i, v in enumerate(reversed(history)):
            prev = history[-(i + 2)]["content"] if i + 1 < len(history) else base
            out.append({**v, "diff_html": fp_diff_html(prev, v["content"])})
        return out

    nc_reports = get_not_community_reports(_db()) if app_state.db_path else []
    extraction_rules = [fp for fp in fps if fp.get("fp_type") == "extraction_rule"]
    active_overrides = get_prompt_overrides(_db()) if app_state.db_path else {}
    return templates.TemplateResponse(request, "prompts.html", {
        "extraction_history": _versioned("extraction", SYSTEM_PROMPT),
        "enrichment_history": _versioned("enrichment", ENRICH_SYSTEM_PROMPT),
        "extraction_prompt": get_prompt("extraction_system") + build_prompt_section(fps, fp_type="extraction"),
        "enrichment_prompt": get_prompt("enrich_system") + build_prompt_section(fps, fp_type="enrichment"),
        "venue_prompt": get_prompt("venue_system"),
        "venue_user_template": get_prompt("venue_user"),
        "venue_schema": json.dumps(VENUE_SCHEMA, indent=2),
        "person_prompt": get_prompt("person_system"),
        "person_user_template": get_prompt("person_user"),
        "person_schema": json.dumps(PERSON_SCHEMA, indent=2),
        "false_positives": fps,
        "nc_reports": nc_reports,
        "extraction_rules": extraction_rules,
        "prompt_overrides": active_overrides,
        "prompt_defaults": {k: PROMPT_KEYS[k]() for k in PROMPT_KEYS},
    })


@admin.post("/prompts/save")
async def admin_prompt_save(key: str = Form(...), content: str = Form(...)):
    """Save an edited prompt override to DB and activate it immediately."""
    if key not in PROMPT_KEYS or not app_state.db_path:
        return JSONResponse({"ok": False, "error": "invalid key"})
    upsert_prompt_override(_db(), key, content)
    set_prompt_override(key, content)
    if key in ("extraction_system", "enrich_system"):
        _reload_fp_history(key)
    return JSONResponse({"ok": True})


@admin.post("/prompts/reset")
async def admin_prompt_reset(key: str = Form(...)):
    """Delete a prompt override (revert to hardcoded default)."""
    if key not in PROMPT_KEYS or not app_state.db_path:
        return JSONResponse({"ok": False, "error": "invalid key"})
    delete_prompt_override(_db(), key)
    set_prompt_override(key, None)
    return JSONResponse({"ok": True})


def _reload_fp_history(key: str) -> None:
    """Record a new prompt history entry when a base prompt is edited."""
    fp_type = "extraction" if key == "extraction_system" else "enrichment"
    from ..false_positives import _record_history
    _record_history(_db(), fp_type)


async def _ai_chat(user_msg: str, temperature: float = 0.3) -> str:
    """Chat via the configured extractor (DeepSeek)."""
    cfg = app_state.pipeline_cfg
    if not cfg:
        raise RuntimeError("Pipeline not configured")
    extractor = _build_extractor(cfg)
    return await extractor.chat(user_msg, temperature)


@admin.post("/prompts/nc-assist")
async def prompts_nc_assist(notes: str = Form("")):
    """Ask DeepSeek to formulate a prompt addition based on not-community reports + admin notes."""
    if not app_state.db_path:
        return JSONResponse({"ok": False, "suggestion": ""})
    from ..extract import SYSTEM_PROMPT as _SP
    reports = get_not_community_reports(_db())

    examples_part = ""
    if reports:
        examples_part = "Flagged items (extracted but NOT real communities):\n" + "\n".join(
            f'- "{r["community_name"]}" ({r["city"]}, {r["topic"]})'
            for r in reports[:30]
        ) + "\n\n"

    notes_part = f"Admin context / reason:\n{notes.strip()}\n\n" if notes.strip() else ""

    user_msg = (
        f"{examples_part}"
        f"{notes_part}"
        "Current extraction system prompt (excerpt):\n"
        f"```\n{_SP[:2000]}\n```\n\n"
        "Write a short, concrete addition (1–5 sentences) to insert into the system prompt "
        "that will help the extractor avoid similar mistakes in the future. "
        "Write only the text to add — no commentary, no markdown headers, no quotes around it."
    )

    try:
        suggestion = await _ai_chat(user_msg, temperature=0.3)
        return JSONResponse({"ok": True, "suggestion": suggestion})
    except Exception as exc:
        log.warning("nc_assist_failed", error=str(exc))
        return JSONResponse({"ok": False, "suggestion": f"Error: {exc}"})


async def _global_rule_needs_confirm(confirm: str) -> JSONResponse | None:
    """A global extraction rule invalidates every cached extraction.

    Adding or removing one re-extracts the whole corpus (~200K pages, weeks of
    the fleet's work), so the admin must confirm with the number in front of
    them; a stray click used to do it silently.
    """
    if confirm == "re-extract-all":
        return None
    pages = await asyncio.to_thread(count_extracted_pages, _db())
    return JSONResponse({"ok": False, "needs_confirm": True, "pages": pages})


@admin.post("/prompts/nc-accept")
async def prompts_nc_accept(rule_text: str = Form(...), confirm: str = Form("")):
    """Save an AI-generated extraction rule into the prompt."""
    if not app_state.db_path or not rule_text.strip():
        return JSONResponse({"ok": False})
    if (refusal := await _global_rule_needs_confirm(confirm)):
        return refusal
    from ..false_positives import add as fp_add
    fp_add(
        _db(),
        name=f"[AI rule] {rule_text[:60]}",
        city="",
        topic="",
        reason=rule_text.strip(),
        source_url="",
        fp_type="extraction_rule",
    )
    return JSONResponse({"ok": True})


@admin.post("/prompts/nc-rule-remove")
async def prompts_nc_rule_remove(name: str = Form(...), confirm: str = Form("")):
    """Remove an AI-generated extraction rule."""
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    if (refusal := await _global_rule_needs_confirm(confirm)):
        return refusal
    from ..false_positives import remove as fp_remove
    fp_remove(_db(), name=name, city="", topic="", fp_type="extraction_rule")
    return JSONResponse({"ok": True})


@admin.get("/submissions", response_class=HTMLResponse)
async def admin_submissions_list(request: Request):
    init_db(_db())
    submissions = get_community_submissions(_db(), status="pending")
    return templates.TemplateResponse(request, "submissions.html", {
        "submissions": submissions,
    })


@admin.post("/submissions/{sub_id}/approve")
async def admin_submission_approve(sub_id: int, background_tasks: BackgroundTasks):
    if not app_state.db_path or not app_state.pipeline_cfg:
        return JSONResponse({"ok": False, "error": "not_configured"})
    sub_rows = get_community_submissions(_db(), status="pending")
    sub = next((r for r in sub_rows if r["id"] == sub_id), None)
    if not sub:
        return JSONResponse({"ok": False, "error": "not_found"})
    try:
        await assert_safe_public_url(sub["source_url"])
    except UnsafeURLError as exc:
        return JSONResponse(
            {"ok": False, "error": "unsafe_source_url", "detail": str(exc)},
            status_code=400,
        )
    resolve_community_submission(_db(), sub_id, "approved")
    background_tasks.add_task(
        _bg_scrape_submission,
        app_state.db_path,
        app_state.pipeline_cfg,
        sub_id,
        sub["city"],
        sub["topic"],
        sub["source_url"],
    )
    return JSONResponse({"ok": True})


async def _bg_scrape_submission(db_path: Path, cfg, sub_id: int,
                                city: str, topic: str, url: str) -> None:
    """Scrape an approved submission; on failure re-queue it as pending so it
    doesn't silently vanish from the inbox with no community created."""
    ok = False
    try:
        ok = await scrape_submitted_url(db_path, cfg, city, topic, url)
    except Exception as exc:
        log.error("submission_scrape_crashed", sub_id=sub_id, url=url, error=str(exc))
    finally:
        if not ok:
            resolve_community_submission(db_path, sub_id, "pending")
            log.error("submission_scrape_failed_requeued", sub_id=sub_id, url=url)


@admin.post("/submissions/{sub_id}/reject")
async def admin_submission_reject(sub_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False, "error": "not_configured"})
    resolve_community_submission(_db(), sub_id, "rejected")
    return JSONResponse({"ok": True})


@admin.post("/communities/{community_id}/reai")
async def admin_community_reai(community_id: str, background_tasks: BackgroundTasks):
    if not app_state.db_path or not app_state.pipeline_cfg:
        return JSONResponse({"ok": False, "error": "not_configured"})
    community = find_community_by_id(_db(), community_id)
    if not community:
        return JSONResponse({"ok": False, "error": "not_found"})
    background_tasks.add_task(
        reextract_community,
        app_state.db_path,
        app_state.pipeline_cfg,
        community_id,
    )
    return JSONResponse({"ok": True})


# ── Cached public home stats ─────────────────────────────────────────────────

_home_stats_cache: dict[str, dict] = {}  # keyed by site ("kozossegek" | "meetapedia")

@admin.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, saved: Optional[str] = None, error: Optional[str] = None):
    software = await _build_software_info()

    sub_count = 0
    if app_state.db_path:
        from ..db import get_subscriptions
        sub_count = len(get_subscriptions(app_state.db_path))

    return templates.TemplateResponse(request, "config.html", {
        "cities_yaml": (CONFIG_DIR / "cities.yaml").read_text(encoding="utf-8"),
        "topics_yaml": (CONFIG_DIR / "topics.yaml").read_text(encoding="utf-8"),
        "settings_yaml": (CONFIG_DIR / "settings.yaml").read_text(encoding="utf-8"),
        "saved": saved,
        "error": error,
        "software": software,
        "sub_count": sub_count,
    })


@admin.post("/config/{name}")
async def save_config(request: Request, name: str):
    """Refused on purpose: every file under config/ is version-controlled.

    These forms used to write the file and reload the config, which looked like
    it worked — until the next deploy replaced the container and the edit was
    gone. `/app/config` ships in the image and is not persisted. The settings
    editor was closed after the 2026-08-18 concurrency settings were lost that
    way; the city and topic editors lost their edits the same way until
    2026-09-24 (and a city added there also got no map coordinates). A form that
    silently loses its input is worse than no form.
    """
    del name
    return _config_error_redirect(Exception(
        "config/ is version-controlled — edit the file in the repository and "
        "deploy. A change saved here would be silently reverted by the next "
        "deploy, which is how the 2026-08-18 concurrency settings were lost."))


@admin.get("/subscriptions", response_class=HTMLResponse)
async def subscriptions_page(request: Request):
    subs = []
    if app_state.db_path:
        from ..db import get_subscriptions
        subs = get_subscriptions(app_state.db_path)
    return templates.TemplateResponse(request, "subscriptions.html", {
        "subs": subs,
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
    })


@admin.get("/stats")
async def stats_redirect():
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/admin/stats/adatminoseg", status_code=302)


@admin.get("/stats/adatminoseg", response_class=HTMLResponse)
async def stats_quality_page(request: Request):
    from ..db import get_data_quality_stats
    stats: dict = {}
    if app_state.db_path and app_state.db_path.exists():
        stats = get_data_quality_stats(app_state.db_path)
    return templates.TemplateResponse(request, "stats_quality.html", {"stats": stats})



@admin.get("/stats/aktivitas", response_class=HTMLResponse)
async def stats_activity_page(request: Request):
    return templates.TemplateResponse(request, "stats_activity.html", {})


@admin.get("/api/stats/timeline")
async def stats_timeline(period: str = "24h"):
    from ..db import get_activity_timeline
    if period not in ("24h", "7d", "12m"):
        period = "24h"
    rows = get_activity_timeline(app_state.db_path, period) if app_state.db_path else []
    return JSONResponse(rows)


_COVERAGE_PAGE_SIZE = 50  # was 2 — a shipped 'for fast loading during testing' leftover


@admin.get("/coverage", response_class=HTMLResponse)
async def admin_coverage(request: Request, country: str = "", page: int = 1):
    from ..config import extract_quarantine_threshold
    from ..db import get_city_topic_states, get_fully_processed_pairs
    from ..extract import get_extract_fingerprint
    current_fp = get_extract_fingerprint()
    states: dict[str, dict[str, dict]] = {}
    done_pairs: set[tuple[str, str]] = set()
    if app_state.db_path and app_state.db_path.exists():
        states = get_city_topic_states(app_state.db_path, current_fp)
        # Same question the pipeline asks: a quarantined page is not work
        # waiting to be done, and a view that says otherwise sends someone
        # looking for a run that will never pick it up.
        done_pairs = get_fully_processed_pairs(
            app_state.db_path, current_fp,
            quarantine_threshold=extract_quarantine_threshold())
    topic_names = [t.name for t in (app_state.topics or [])]
    countries: dict[str, list[str]] = {}
    for city in (app_state.cities or []):
        c = getattr(city, "country", "Other") or "Other"
        countries.setdefault(c, []).append(city.name)
    all_countries = list(countries.keys())
    active_country: str | None = None
    if app_state.is_running and app_state.current_city:
        for c, cities_list in countries.items():
            if app_state.current_city in cities_list:
                active_country = c
                break
    default_country = active_country or (all_countries[0] if all_countries else "")
    selected_country = country if country in all_countries else default_country
    all_cities = countries.get(selected_country, [])
    total_cities = len(all_cities)
    total_pages = max(1, (total_cities + _COVERAGE_PAGE_SIZE - 1) // _COVERAGE_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * _COVERAGE_PAGE_SIZE
    filtered_cities = all_cities[offset: offset + _COVERAGE_PAGE_SIZE]
    # Page number where the active city lives (for jump-to-active across pages)
    active_city_page: int | None = None
    if app_state.current_city and app_state.current_city in all_cities:
        idx = all_cities.index(app_state.current_city)
        active_city_page = idx // _COVERAGE_PAGE_SIZE + 1
    return templates.TemplateResponse(request, "coverage.html", {
        "all_countries": all_countries,
        "selected_country": selected_country,
        "cities": filtered_cities,
        "topic_names": topic_names,
        "states": states,
        "done_pairs": done_pairs,
        "is_running": app_state.is_running,
        "current_city": app_state.current_city,
        "current_topic": app_state.current_topic,
        "active_country": active_country,
        "page": page,
        "total_pages": total_pages,
        "total_cities": total_cities,
        "active_city_page": active_city_page,
    })


@admin.get("/api/coverage/current")
async def api_coverage_current():
    return {
        "city": app_state.current_city,
        "topic": app_state.current_topic,
        "is_running": app_state.is_running,
    }


_coverage_state_cache: dict = {"ts": 0.0, "fp": None, "states": None, "done": None}


def _coverage_state(fp: str):
    """get_city_topic_states + get_fully_processed_pairs with a ~3 s memo — the
    coverage page polls /api/coverage/cell every 3 s and each call otherwise
    re-scanned cache_pages + hashed every search_cache URL."""
    import time as _time
    from ..config import extract_quarantine_threshold
    from ..db import get_city_topic_states, get_fully_processed_pairs
    now = _time.monotonic()
    c = _coverage_state_cache
    if c["states"] is None or c["fp"] != fp or now - c["ts"] > 3.0:
        c.update(ts=now, fp=fp,
                 states=get_city_topic_states(app_state.db_path, fp),
                 done=get_fully_processed_pairs(
                     app_state.db_path, fp,
                     quarantine_threshold=extract_quarantine_threshold()))
    return c["states"], c["done"]


@admin.get("/api/coverage/cell")
async def api_coverage_cell(city: str = "", topic: str = ""):
    """Return the current state for a single (city, topic) cell."""
    from ..extract import get_extract_fingerprint
    if not city or not topic or not app_state.db_path:
        return {"community_count": 0, "page_count": 0, "current_fp_count": 0, "is_done": False}
    current_fp = get_extract_fingerprint()
    states, done_pairs = _coverage_state(current_fp)
    cell = states.get(city, {}).get(topic, {})
    return {
        "community_count": cell.get("community_count", 0),
        "page_count": cell.get("page_count", 0),
        "current_fp_count": cell.get("current_fp_count", 0),
        "is_done": (city, topic) in done_pairs,
    }


@admin.post("/api/send-daily-report")
async def api_send_daily_report(day: str = Form("")):
    """Send the daily summary email now (optional ?day=YYYY-MM-DD, default yesterday)."""
    from ..report import send_daily_report
    if not app_state.db_path:
        return JSONResponse({"error": "no db"}, status_code=400)
    hu = _hu_city_names()
    result = await send_daily_report(app_state.db_path, hu, day.strip() or None)
    return JSONResponse(result, status_code=200 if result.get("ok") else 400)


@admin.post("/api/enrich")
async def api_enrich(scope: str = Form("hungary"), limit: int = Form(20),
                     dry_run: str = Form(""), fetch_missing: str = Form("1")):
    """Staged description enrichment for one scope ('hungary' or a country name).
    Admin-triggered, hard-capped; generates short + long descriptions from each
    community's page text (cached, or fetched fresh when missing). Returns stats +
    before/after samples. See the docs enrichment plan."""
    from ..enrich import enrich_batch
    if not app_state.db_path or not app_state.pipeline_cfg:
        return JSONResponse({"error": "no db/config"}, status_code=400)
    if scope.strip().lower() in ("hungary", "hu"):
        city_names = _hu_city_names()
    else:
        city_names = {c.name for c in (app_state.cities or [])
                      if (c.country or "").lower() == scope.strip().lower()}
    if not city_names:
        return JSONResponse({"error": f"no cities for scope {scope!r}"}, status_code=400)
    extractor = _build_extractor(app_state.pipeline_cfg)
    if extractor.exhausted:
        return JSONResponse({"error": "no extractor configured (DEEPSEEK_API_KEY unset)"},
                            status_code=400)
    def _truthy(s: str) -> bool:
        return s.strip().lower() in ("1", "true", "yes", "on")
    is_dry, do_fetch = _truthy(dry_run), _truthy(fetch_missing)
    # `_enrich_running` is the single enrichment mutex shared with the managed
    # off-peak job (main.py:_enrich_run) so the two can't run the same candidate
    # pool at once. Enrichment deliberately does NOT reserve the pipeline
    # coordinator — it coexists with the ai_only extractor.
    if app_state._enrich_running:
        return JSONResponse({"error": "enrichment already running"}, status_code=409)
    app_state._enrich_running = True
    db_path, blocked = app_state.db_path, app_state.pipeline_cfg.fetch_blocked_domains

    # Run in the background — a batch of sequential LLM calls easily exceeds the
    # request/proxy timeout. Poll GET /admin/api/enrich/result for the outcome.
    async def _run():
        try:
            app_state.last_enrich_result = await enrich_batch(
                db_path, extractor, city_names, limit=int(limit),
                dry_run=is_dry, fetch_missing=do_fetch, blocked_domains=blocked)
        except asyncio.CancelledError:
            app_state.last_enrich_result = {"error": "cancelled"}
            raise
        except Exception as exc:
            app_state.last_enrich_result = {"error": str(exc)}
        finally:
            app_state._enrich_running = False
            app_state._enrich_task = None

    app_state.last_enrich_result = None
    # Store the task so /api/stop can cancel this (paid, long-running) batch —
    # enrichment doesn't use the run coordinator, so it needs its own handle.
    app_state._enrich_task = asyncio.create_task(_run())
    return JSONResponse({"started": True, "scope": scope.strip(), "limit": int(limit),
                         "dry_run": is_dry, "poll": "/admin/api/enrich/result"})


@admin.get("/api/enrich/result")
async def api_enrich_result():
    """Result of the most recent /api/enrich batch (null while still running)."""
    return JSONResponse({"running": app_state._enrich_running,
                         "result": app_state.last_enrich_result})


@admin.post("/api/reset-city")
async def api_reset_city(city: str = Form("")):
    """Full fresh start for one city: wipe its communities/venues/persons,
    search_cache and cache_pages rows, so every pair re-searches from scratch.

    Needed because green pairs (any visible community) are skipped by the
    done-pair pre-filter forever — clearing caches alone would leave old bad
    results in place. NB: cache_pages.city is last-write-wins, so pages shared
    with another city may also be cleared (they simply re-fetch next run).
    """
    from ..db import _connect, init_db
    city = city.strip()
    if not city or not app_state.db_path:
        return JSONResponse({"error": "city is required"}, status_code=400)
    known = {c.name for c in (app_state.cities or [])}
    if known and city not in known:
        return JSONResponse({"error": f"unknown city: {city}"}, status_code=400)
    init_db(app_state.db_path)
    counts = {}
    with _connect(app_state.db_path) as conn:
        for label, sql in [
            ("communities", "DELETE FROM communities WHERE city=?"),
            ("venues", "DELETE FROM venues WHERE city=?"),
            ("persons", "DELETE FROM persons WHERE city=?"),
            ("search_cache", "DELETE FROM search_cache WHERE city=?"),
            ("cache_pages", "DELETE FROM cache_pages WHERE city=?"),
        ]:
            counts[label] = conn.execute(sql, (city,)).rowcount
        conn.commit()
    log.info("city_reset", city=city, **counts)
    return {"ok": True, "city": city, "deleted": counts}


@admin.post("/api/restamp-fingerprints")
async def api_restamp_fingerprints():
    """Restamp all cache_pages rows to the current runtime extract fingerprint.

    Use after changing the extraction prompt when existing results are still
    valid and should not be re-processed. Idempotent.
    """
    from ..db import _connect
    from ..extract import get_extract_fingerprint
    if not app_state.db_path or not app_state.db_path.exists():
        return JSONResponse({"error": "no db"}, status_code=400)
    current_fp = get_extract_fingerprint()
    with _connect(app_state.db_path) as conn:
        cur = conn.execute(
            "UPDATE cache_pages SET extract_fingerprint = ? WHERE extract_fingerprint != ? AND extract_fingerprint IS NOT NULL",
            (current_fp, current_fp),
        )
        updated = cur.rowcount
        conn.commit()
    return {"updated": updated, "fingerprint": current_fp}


@admin.get("/logs", response_class=HTMLResponse)
async def logs_page(request: Request):
    return templates.TemplateResponse(request, "logs.html", {})


@admin.get("/api/logs/history")
async def log_history():
    history = broadcaster.get_all()
    return JSONResponse(history)


@admin.get("/api/logs/stream")
async def log_stream(last_seq: int = 0):
    async def generate():
        current_seq = last_seq
        tick = 0
        while True:
            await asyncio.sleep(0.5)
            tick += 1
            new_lines = broadcaster.get_lines_after(current_seq)
            if new_lines:
                for line in new_lines:
                    current_seq = line["seq"]
                    yield f"data: {json.dumps(line)}\n\n"
            elif tick % 30 == 0:
                yield ": keepalive\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def launch_pipeline_run(
    run_mode: str,
    *,
    skip_scraped: bool = False,
    skip_extracted: bool = False,
    run_communities: bool = True,
    run_venues: bool = True,
    run_persons: bool = True,
    filter_country: str = "",
    filter_city: str = "",
    cities: "list | None" = None,
    stop_at: "datetime | None" = None,
    should_stop: "Callable[[], bool] | None" = None,
    on_finished: "Callable[[list, int], None] | None" = None,
) -> tuple[bool, str]:
    """Reserve the run slot and start a pipeline run. Returns (started, reason).

    The one place a run is launched. It was inline in the admin form handler,
    which meant every other way of starting a run — the control API, the
    scheduler — would have had to reproduce the reservation, the run record and
    the three-state outcome classification, and drift from it.
    """
    if run_mode not in ("full", "ai_only", "search_only"):
        run_mode = "full"
    mode_label = {"ai_only": "re-ai", "search_only": "collect"}.get(run_mode, "smart")
    if not app_state.run_coordinator.reserve(mode_label):
        return False, "already running"

    # `cities` lets the worker pass them in expansion-priority order; the
    # admin form runs whatever `cities.yaml` lists, in file order.
    cities = list(cities if cities is not None else (app_state.cities or []))
    if filter_city.strip():
        cities = [c for c in cities if c.name == filter_city.strip()]
    elif filter_country.strip():
        cities = [c for c in cities if c.country == filter_country.strip()]

    def _on_progress(phase: str | None, url: str | None) -> None:
        app_state.current_phase = phase
        app_state.current_url = url

    def _on_pair_start(city: str, topic: str) -> None:
        app_state.current_city = city
        app_state.current_topic = topic

    async def _run() -> None:
        from ..db import finish_run as _finish_run, start_run as _start_run
        pair_logs: list = []
        total_new = 0
        run_error: str | None = None
        _run_id = None
        try:
            started = datetime.now(timezone.utc)
            # Inside the try: a database hiccup starting the run record used to
            # skip the finally, and with it the on_finished callback the
            # scheduler waits on.
            _run_id = _start_run(app_state.db_path, started, run_mode) if app_state.db_path else None
            pair_logs, total_new = await run_pipeline(
                cities,
                app_state.topics,
                app_state.pipeline_cfg,
                cache=app_state.cache_manager,
                run_mode=run_mode,
                skip_scraped=skip_scraped,
                skip_extracted=skip_extracted,
                run_communities=run_communities,
                run_venues=run_venues,
                run_persons=run_persons,
                on_progress=_on_progress,
                on_pair_start=_on_pair_start,
                stop_at=stop_at,
                should_stop=should_stop,
            )
            app_state.last_run_at = datetime.now(timezone.utc)
        except asyncio.CancelledError:
            # A deploy or a stop. Nothing is lost — every finished pair is
            # already cached — so it is recorded as a clean stop, not a failure.
            # Three of these were reported as failed runs on 2026-08-17 purely
            # because a deploy landed mid-window.
            log.info("run_stopped", run_mode=run_mode)
            raise
        except Exception as exc:
            # Persisted, not just logged: a preflight abort or any top-level
            # failure must name itself in the run history and the daily email.
            run_error = str(exc)
            log.error("run_failed", run_mode=run_mode, error=str(exc))
        finally:
            global _home_stats_cache
            try:
                _home_stats_cache = {}
                if app_state.db_path and _run_id:
                    # Same three-state classification everywhere: a dead
                    # provider is an abort, scattered retryable pair failures are
                    # a warning, so run history never shows either as a clean ✓.
                    _outcome = classify_run_outcome(pair_logs, run_error)
                    _finish_run(app_state.db_path, _run_id, datetime.now(timezone.utc),
                                _outcome != RUN_ABORTED,
                                json.dumps(pair_logs) if pair_logs else None,
                                total_new, error=run_error, outcome=_outcome)
            finally:
                app_state.run_coordinator.release(asyncio.current_task())
                if on_finished is not None:
                    try:
                        on_finished(pair_logs, total_new)
                    except Exception as exc:
                        log.warning("run_on_finished_failed", error=str(exc))

    task = asyncio.create_task(_run())
    app_state.run_coordinator.attach(task)
    return True, "started"


@admin.post("/api/run")
async def trigger_run(
    run_mode: str = Form("full"),
    skip_scraped: str = Form("off"),
    skip_extracted: str = Form("off"),
    run_communities: str = Form("on"),
    run_venues: str = Form("on"),
    run_persons: str = Form("on"),
    filter_country: str = Form(""),
    filter_city: str = Form(""),
):
    # Asking for a run means wanting work to happen again, so it lifts a pause
    # left by Stop. Without this the button started one run and then everything
    # — the worker and enrichment — stayed paused until a restart.
    app_state.worker_paused = False
    started, reason = launch_pipeline_run(
        run_mode,
        skip_scraped=(skip_scraped == "on"),
        skip_extracted=(skip_extracted == "on"),
        run_communities=(run_communities == "on"),
        run_venues=(run_venues == "on"),
        run_persons=(run_persons == "on"),
        filter_country=filter_country,
        filter_city=filter_city,
    )
    return JSONResponse({"ok": started} if started else {"ok": False, "error": reason})


@admin.post("/api/stop")
async def stop_run():
    # Pause as well as cancel: under the continuous worker, cancelling alone
    # only ended the current run and the next one started within the minute.
    app_state.worker_paused = True
    if app_state.run_coordinator.cancel():
        log.info("run_cancelled_by_user")
    # Enrichment runs outside the coordinator (coexists with the pipeline), so
    # cancel its task separately.
    t = app_state._enrich_task
    if t is not None and not t.done():
        t.cancel()
        log.info("enrich_cancelled_by_user")
    return RedirectResponse("/admin/", status_code=302)


@admin.get("/api/status")
async def status():
    return {
        "is_running": app_state.is_running,
        "current_run_mode": app_state.current_run_mode,
        "last_run_at": app_state.last_run_at.isoformat() if app_state.last_run_at else None,
    }



def _build_extractor(cfg):
    """Build the extractor chain from PipelineConfig.

    Thin alias for `pipeline.build_extractor` so admin-triggered work routes
    over the same free-tier fleet — and spends the same quota ledger — as the
    scheduled runs. Keeping a second builder here is how the two drifted apart
    before.
    """
    from ..pipeline import build_extractor
    return build_extractor(cfg)


def _served_by(extractor) -> tuple[str, int | None]:
    """(model, quality) of the model that actually served the last call — see
    pipeline._served_by. Re-exported here so admin routes stamp the cache with
    the same provenance the pipeline does."""
    from ..pipeline import _served_by as _impl
    return _impl(extractor)


async def _queue_worker() -> None:
    while True:
        # Wait until there is a pending item
        while True:
            pending = [i for i in app_state.queue_items if i["status"] == "pending"]
            if pending:
                break
            app_state.get_queue_event().clear()
            await app_state.get_queue_event().wait()

        item = pending[0]
        fn = app_state._queue_fns.pop(item["id"], None)
        if fn is None:
            item["status"] = "error"
            item["error"] = "fn missing"
            continue

        item["status"] = "running"
        item["started_at"] = datetime.utcnow().isoformat()
        try:
            await fn()
            item["status"] = "done"
        except Exception as exc:
            item["status"] = "error"
            item["error"] = str(exc)
        finally:
            item["done_at"] = datetime.utcnow().isoformat()

        # Keep at most 30 completed items
        done = [i for i in app_state.queue_items if i["status"] in ("done", "error")]
        if len(done) > 30:
            remove_ids = {x["id"] for x in done[:-30]}
            app_state.queue_items = [i for i in app_state.queue_items if i["id"] not in remove_ids]


def _enqueue(op: str, url_hash: str, url: str, city: str, topic: str, fn,
             priority: bool = False) -> dict:
    import uuid
    item: dict = {
        "id": uuid.uuid4().hex[:8],
        "op": op,
        "url_hash": url_hash,
        "url": url,
        "city": city,
        "topic": topic,
        "status": "pending",
        "added_at": datetime.utcnow().isoformat(),
        "started_at": None,
        "done_at": None,
        "error": None,
    }
    app_state._queue_fns[item["id"]] = fn
    if priority:
        # Insert right after the currently running item (position 1), or at front if idle
        insert_at = next(
            (i + 1 for i, x in enumerate(app_state.queue_items) if x["status"] == "running"),
            0,
        )
        app_state.queue_items.insert(insert_at, item)
    else:
        app_state.queue_items.append(item)
    app_state.get_queue_event().set()
    if not app_state._queue_worker_task or app_state._queue_worker_task.done():
        app_state._queue_worker_task = asyncio.create_task(_queue_worker())
    return item



@admin.get("/api/progress")
async def api_progress():
    """Return the current pipeline phase and active URL hash for live cache indicators."""
    import hashlib
    url = app_state.current_url
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16] if url else None
    return JSONResponse({
        "phase": app_state.current_phase,
        "url_hash": url_hash,
        "url": url,
    })


@admin.get("/api/cache-count")
async def api_cache_count():
    """How many pages are cached — what the progress page polls for."""
    count = await asyncio.to_thread(count_cache_pages, app_state.db_path) \
        if app_state.db_path else 0
    return JSONResponse({"count": count})


@admin.get("/cache")
async def cache_redirect():
    return RedirectResponse("/admin/progress", status_code=301)

@admin.get("/cache/{url_hash}")
async def cache_detail_redirect(url_hash: str):
    return RedirectResponse(f"/admin/progress/{url_hash}", status_code=301)

@admin.get("/progress", response_class=HTMLResponse)
async def cache_page(
    request: Request,
    page: int = 1,
    f_city: str = "",
    f_topic: str = "",
    f_scraped: str = "",
    f_model: str = "",
    f_enrich_scraped: str = "",
    f_enrich_model: str = "",
    f_venue: str = "",
    f_person: str = "",
):
    entries = []
    if app_state.cache_manager:
        # Off the loop: it reads a row per cached page (~207K).
        entries = await asyncio.to_thread(app_state.cache_manager.get_index)
    url_counts = get_venue_person_counts_by_url(_db())

    all_cities = sorted({e.get("city") for e in entries if e.get("city")})
    all_topics = sorted({e.get("topic") for e in entries if e.get("topic")})
    all_models = sorted({e.get("extract_model") for e in entries if e.get("extract_model")})
    all_enrich_models = sorted({e.get("enrich_model") for e in entries if e.get("enrich_model")})

    def _match(e: dict) -> bool:
        if f_city and e.get("city") != f_city:
            return False
        if f_topic and e.get("topic") != f_topic:
            return False
        if f_scraped == "1" and not e.get("scraped_at"):
            return False
        if f_scraped == "0" and e.get("scraped_at"):
            return False
        if f_model == "__none__" and e.get("extract_model"):
            return False
        if f_model and f_model != "__none__" and e.get("extract_model") != f_model:
            return False
        if f_enrich_scraped == "1" and not e.get("enrich_scraped_at"):
            return False
        if f_enrich_scraped == "0" and e.get("enrich_scraped_at"):
            return False
        if f_enrich_model == "__none__" and e.get("enrich_model"):
            return False
        if f_enrich_model and f_enrich_model != "__none__" and e.get("enrich_model") != f_enrich_model:
            return False
        counts = url_counts.get(e.get("url", ""), {})
        vc = counts.get("venues", 0)
        pc = counts.get("persons", 0)
        if f_venue == "1" and not vc:
            return False
        if f_venue == "0" and vc:
            return False
        if f_person == "1" and not pc:
            return False
        if f_person == "0" and pc:
            return False
        return True

    filtered = [e for e in entries if _match(e)]
    page_size = 100
    total_all = len(entries)
    total_filtered = len(filtered)
    pages = max(1, (total_filtered + page_size - 1) // page_size)
    page = max(1, min(page, pages))
    paged = filtered[(page - 1) * page_size: page * page_size]
    filter_params = {k: v for k, v in {
        "f_city": f_city, "f_topic": f_topic, "f_scraped": f_scraped,
        "f_model": f_model, "f_enrich_scraped": f_enrich_scraped,
        "f_enrich_model": f_enrich_model, "f_venue": f_venue, "f_person": f_person,
    }.items() if v}
    return templates.TemplateResponse(request, "progress.html", {
        "entries": paged,
        "url_counts": url_counts,
        "page": page,
        "pages": pages,
        "total": total_all,
        "total_filtered": total_filtered,
        "all_cities": all_cities,
        "all_topics": all_topics,
        "all_models": all_models,
        "all_enrich_models": all_enrich_models,
        "f_city": f_city,
        "f_topic": f_topic,
        "f_scraped": f_scraped,
        "f_model": f_model,
        "f_enrich_scraped": f_enrich_scraped,
        "f_enrich_model": f_enrich_model,
        "f_venue": f_venue,
        "f_person": f_person,
        "filter_params": filter_params,
    })


@admin.get("/progress/{url_hash}", response_class=HTMLResponse)
async def cache_detail(request: Request, url_hash: str):
    if not app_state.cache_manager:
        return RedirectResponse("/admin/progress", status_code=302)

    entry = app_state.cache_manager.get_entry(url_hash)
    if not entry:
        return RedirectResponse("/admin/progress", status_code=302)

    store_records = []
    city = entry.get("city", "")
    topic = entry.get("topic", "")
    url = entry.get("url", "")
    if city and topic and url:
        all_records = get_communities(_db(), city, topic)
        store_records = [_ensure_community_id(r) for r in all_records if r.get("source_url") == url]

    schema_records = store_records or (entry.get("records") or [])
    schema_json = records_to_jsonld(schema_records)

    max_text_chars = app_state.pipeline_cfg.deepseek_max_text_chars if app_state.pipeline_cfg else 6000

    extract_user_prompt = ""
    if entry.get("raw_text") and entry.get("topic") and entry.get("city"):
        extract_user_prompt = USER_PROMPT_TEMPLATE.format(
            topic=entry.get("topic", ""),
            city=entry.get("city", ""),
            source_url=entry.get("url", ""),
            page_text=entry.get("raw_text", "")[:max_text_chars],
        )

    fps = fp_load(_db())
    fp_extraction = {(fp["name"], fp["city"], fp["topic"])
                     for fp in fps if fp.get("fp_type", "extraction") == "extraction"}
    fp_enrichment = {(fp["name"], fp["city"], fp["topic"])
                     for fp in fps if fp.get("fp_type") == "enrichment"}

    # Other cache entries from the same city/topic pair
    related_entries: list[dict] = []
    if city and topic:
        related_entries = [
            e for e in await asyncio.to_thread(app_state.cache_manager.get_index)
            if e.get("city") == city and e.get("topic") == topic and e.get("url_hash") != url_hash
        ]

    return templates.TemplateResponse(request, "progress_detail.html", {
        "entry": entry,
        "store_records": store_records,
        "schema_json": schema_json,
        "extract_system_prompt": SYSTEM_PROMPT,
        "extract_user_prompt": extract_user_prompt,
        "extract_schema": json.dumps(EXTRACTION_SCHEMA, indent=2),
        "enrich_system_prompt": ENRICH_SYSTEM_PROMPT,
        "enrich_schema": json.dumps(ENRICH_SCHEMA, indent=2),
        "related_entries": related_entries,
        "fp_extraction": fp_extraction,
        "fp_enrichment": fp_enrichment,
        "current_url_hash": url_hash,
    })


@admin.post("/progress/{url_hash}/delete-scraped")
async def cache_delete_scraped(url_hash: str):
    if app_state.cache_manager:
        app_state.cache_manager.delete_scraped(url_hash)
    return RedirectResponse("/admin/progress", status_code=302)


@admin.post("/progress/{url_hash}/delete-extracted")
async def cache_delete_extracted(url_hash: str):
    if app_state.cache_manager:
        app_state.cache_manager.delete_extracted(url_hash)
    return RedirectResponse("/admin/progress", status_code=302)


@admin.post("/progress/{url_hash}/delete")
async def cache_delete_entry(url_hash: str):
    if app_state.cache_manager:
        app_state.cache_manager.delete_entry(url_hash)
    return RedirectResponse("/admin/progress", status_code=302)


@admin.post("/progress/clear-person-cache")
async def cache_clear_persons():
    updated = 0
    if app_state.cache_manager:
        updated = app_state.cache_manager.clear_person_extracted()
    log.info("person_cache_cleared_via_admin", updated=updated)
    return RedirectResponse("/admin/progress", status_code=302)


@admin.post("/progress/{url_hash}/run-scrape")
async def cache_run_scrape(url_hash: str):
    if not app_state.cache_manager or not app_state.pipeline_cfg:
        return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)
    entry = app_state.cache_manager.get_entry(url_hash)
    if not entry:
        return RedirectResponse("/admin/progress", status_code=302)

    url = entry["url"]
    city = entry.get("city", "")
    topic = entry.get("topic", "")
    cfg = app_state.pipeline_cfg

    async def _do() -> None:
        import time as _time
        app_state.current_phase = "scrape"
        app_state.current_url = url
        try:
            t0 = _time.monotonic()
            text = await fetch_and_clean(
                url, cfg.fetch_blocked_domains, cfg.fetch_timeout,
                cfg.fetch_min_text_length, asyncio.Semaphore(1),
            )
            if text:
                app_state.cache_manager.save_scraped(
                    url, text, city, topic, duration_s=_time.monotonic() - t0
                )
        except Exception as exc:
            log.error("manual_scrape_failed", url=url, error=str(exc))
            raise
        finally:
            app_state.current_phase = None
            app_state.current_url = None

    _enqueue("scrape", url_hash, url, city, topic, _do, priority=True)
    return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)


@admin.post("/progress/{url_hash}/run-extract")
async def cache_run_extract(url_hash: str):
    if not app_state.cache_manager or not app_state.pipeline_cfg:
        return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)
    entry = app_state.cache_manager.get_entry(url_hash)
    if not entry or not entry.get("raw_text"):
        return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)

    url = entry["url"]
    city = entry.get("city", "")
    topic = entry.get("topic", "")
    raw_text = entry["raw_text"]
    cfg = app_state.pipeline_cfg
    locale = next((c.locale for c in (app_state.cities or []) if c.name == city), "en")

    async def _do() -> None:
        import time as _time
        app_state.current_phase = "extract"
        app_state.current_url = url
        try:
            extractor = _build_extractor(cfg)
            t0 = _time.monotonic()
            extracted = await extractor.extract(raw_text, city, topic, locale, url)
            extract_dur = _time.monotonic() - t0
            _served = _served_by(extractor)
            joinable = [r for r in extracted if r.joinable]
            app_state.cache_manager.save_extracted(
                url,
                joinable,
                duration_s=extract_dur,
                fingerprint=extractor.model_fingerprint,
                model=_served[0],
                quality=_served[1],
            )
            if joinable:
                save_results(city, topic, joinable, _db())
        except Exception as exc:
            log.error("manual_extract_failed", url=url, error=str(exc))
            raise
        finally:
            app_state.current_phase = None
            app_state.current_url = None

    _enqueue("extract", url_hash, url, city, topic, _do, priority=True)
    return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)


@admin.post("/progress/{url_hash}/run-enrich")
async def cache_run_enrich(url_hash: str):
    if not app_state.cache_manager or not app_state.pipeline_cfg:
        return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)
    entry = app_state.cache_manager.get_entry(url_hash)
    records = app_state.cache_manager.get_extracted(entry["url"]) if entry else None
    if not records:
        return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)

    url = entry["url"]
    city = entry.get("city", "")
    topic = entry.get("topic", "")
    cfg = app_state.pipeline_cfg

    def _on_progress(phase: str | None, p_url: str | None) -> None:
        app_state.current_phase = phase
        app_state.current_url = p_url

    async def _do() -> None:
        try:
            extractor = _build_extractor(cfg)
            search_primaries = []
            if cfg.dataforseo_login and cfg.dataforseo_password:
                search_primaries.append(DataForSEOClient(
                    cfg.dataforseo_login, cfg.dataforseo_password,
                    rate_limit_seconds=cfg.search_rate_limit,
                    budget=SearchBudget(cfg.db_path, cfg.dataforseo_daily_budget_usd),
                ))
            searxng = FallbackSearchClient(primaries=search_primaries)
            semaphore = asyncio.Semaphore(cfg.fetch_max_concurrent)
            timing = {"scrape": 0.0, "extract": 0.0, "count": 0, "needed": False}
            enriched: list = []
            for record in records:
                if _needs_enrichment(record):
                    timing["needed"] = True
                    record = await _enrich_record(
                        record, searxng, extractor, cfg, semaphore, _on_progress, timing
                    )
                enriched.append(record)
            app_state.cache_manager.save_enriched_records(url, enriched)
            if timing["needed"]:
                app_state.cache_manager.mark_enrich_scraped(url, timing["scrape"])
                app_state.cache_manager.mark_enrich_extracted(url, timing["count"], timing["extract"], model=extractor.model)
            if enriched:
                save_results(city, topic, enriched, _db())
        except Exception as exc:
            log.error("manual_enrich_failed", url=url, error=str(exc))
            raise
        finally:
            app_state.current_phase = None
            app_state.current_url = None

    _enqueue("enrich", url_hash, url, city, topic, _do, priority=True)
    return RedirectResponse(f"/admin/progress/{url_hash}", status_code=302)


@admin.get("/runs/{run_id}", response_class=HTMLResponse)
async def run_detail(request: Request, run_id: int):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    from ..db import get_run_detail
    run = get_run_detail(app_state.db_path, run_id)
    if not run:
        return RedirectResponse("/admin", status_code=302)
    pair_logs = json.loads(run["search_log"]) if run.get("search_log") else []
    # Older runs may contain minimal failure entries without the full key set the
    # template sums/iterates — merge defaults so historical rows can't 500 the page.
    from ..pipeline import _new_pair_log
    pair_logs = [{**_new_pair_log(p.get("city", ""), p.get("topic", ""), p.get("queries", [])), **p}
                 for p in pair_logs]
    return templates.TemplateResponse(request, "run_detail.html", {
        "run": run,
        "pair_logs": pair_logs,
    })


@admin.get("/venues", response_class=HTMLResponse)
async def admin_venues(request: Request, city: str = ""):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    counts = get_venue_counts(app_state.db_path)
    venues = get_all_venues(app_state.db_path)
    if city:
        venues = [v for v in venues if v.get("city", "").lower() == city.lower()]

    # Collect all community_ids across shown venues, bulk-fetch in one query
    all_cids: list[str] = []
    for v in venues:
        all_cids.extend(v.get("community_ids") or [])
    community_map: dict[str, dict] = {}
    if all_cids:
        for c in get_communities_by_ids(app_state.db_path, list(dict.fromkeys(all_cids))):
            community_map[c.get("community_id", "")] = c

    # Resolve topic labels for display
    _topic_labels = get_topic_labels("hu")

    venue_histories = {
        v.get("venue_id", ""): get_venue_history(app_state.db_path, v.get("venue_id", ""))
        for v in venues if v.get("venue_id")
    }
    return templates.TemplateResponse(request, "venues.html", {
        "venues": venues,
        "counts": counts,
        "selected_city": city,
        "cities": sorted(counts.keys()),
        "community_map": community_map,
        "topic_labels": _topic_labels,
        "topic_icons": TOPIC_ICONS,
        "venue_histories": venue_histories,
    })


@admin.get("/persons", response_class=HTMLResponse)
async def admin_persons(request: Request, city: str = "", topic: str = ""):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    counts = get_person_counts(app_state.db_path)
    all_cities = sorted(counts.keys())
    if city:
        persons = get_persons(app_state.db_path, city, topic or None)
    else:
        persons = []
        for c in all_cities:
            persons.extend(get_persons(app_state.db_path, c, topic or None))
    person_histories = {
        p.get("person_id", ""): get_person_history(app_state.db_path, p.get("person_id", ""))
        for p in persons if p.get("person_id")
    }
    return templates.TemplateResponse(request, "persons.html", {
        "persons": persons,
        "counts": counts,
        "selected_city": city,
        "selected_topic": topic,
        "cities": all_cities,
        "person_histories": person_histories,
    })


@admin.get("/not-community", response_class=HTMLResponse)
async def admin_not_community(request: Request):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    reports = get_not_community_reports(_db())
    for r in reports:
        if not r.get("page_url") and r.get("city"):
            r["page_url"] = _public_community_url(r.get("community_name", ""), r["city"])
    fps = fp_load(_db())
    fp_keys = {(fp["name"], fp["city"], fp["topic"]) for fp in fps}
    return templates.TemplateResponse(request, "not_community.html", {
        "reports": reports,
        "fp_keys": fp_keys,
        "topic_labels": TOPIC_LABELS,
        "topic_icons": TOPIC_ICONS,
    })


@admin.post("/not-community/{report_id}/approve")
async def admin_not_community_approve(report_id: int):
    """Approve a pending report, hide the record, and teach the extractor."""
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    reports = get_not_community_reports(_db())
    r = next((x for x in reports if x["id"] == report_id), None)
    if not r:
        return JSONResponse({"ok": False, "error": "not found"})
    fp_add(
        _db(),
        name=r["community_name"],
        city=r["city"] or "",
        topic=r["topic"] or "",
        reason="Flagged by user as not a community",
        source_url=r["source_url"] or "",
        fp_type="extraction",
    )
    record_key = _community_record_key(
        r["community_name"], r["city"] or "", r["topic"] or ""
    )
    set_community_hidden(_db(), record_key, True)
    delete_not_community_report(_db(), report_id)
    return JSONResponse({"ok": True})


@admin.post("/not-community/{report_id}/dismiss")
async def admin_not_community_dismiss(report_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    delete_not_community_report(_db(), report_id)
    return JSONResponse({"ok": True})


@admin.post("/not-community/ai-suggest")
async def admin_not_community_ai_suggest():
    """Ask DeepSeek to suggest prompt improvements based on flagged items."""
    if not app_state.db_path:
        return JSONResponse({"ok": False, "suggestion": ""})
    from ..extract import SYSTEM_PROMPT
    reports = get_not_community_reports(_db())
    if not reports:
        return JSONResponse({"ok": True, "suggestion": "No flagged items yet."})

    examples = "\n".join(
        f'- "{r["community_name"]}" ({r["city"]}, {r["topic"]})'
        for r in reports[:30]
    )
    user_msg = (
        "The following items were extracted by the pipeline but users flagged them "
        "as NOT being genuine community groups:\n\n"
        f"{examples}\n\n"
        "Current extraction system prompt:\n"
        f"```\n{SYSTEM_PROMPT[:2000]}\n```\n\n"
        "Based on these false positives, suggest concrete additions or changes to the "
        "system prompt that would prevent similar mistakes in the future. "
        "Be specific: quote the exact text to add or change. "
        "Output only the suggested prompt change, nothing else."
    )

    try:
        suggestion = await _ai_chat(user_msg, temperature=0.3)
        return JSONResponse({"ok": True, "suggestion": suggestion})
    except Exception as exc:
        log.warning("ai_suggest_failed", error=str(exc))
        return JSONResponse({"ok": False, "suggestion": f"Error: {exc}"})


@admin.get("/duplicates", response_class=HTMLResponse)
async def admin_duplicates(request: Request):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    init_db(_db())
    candidates = get_duplicate_candidates(_db())
    enriched = []
    from ..db import get_entity_by_record_key
    for c in candidates:
        if c["entity_type"] == "community":
            winner_data = get_community_by_record_key(_db(), c["winner_key"])
            loser_data = get_community_by_record_key(_db(), c["loser_key"])
        else:
            # Venue/person candidates used to render as "Record not found" and
            # offered deletion only.
            winner_data = get_entity_by_record_key(_db(), c["entity_type"], c["winner_key"])
            loser_data = get_entity_by_record_key(_db(), c["entity_type"], c["loser_key"])
        if c["entity_type"] == "community":
            for d in (winner_data, loser_data):
                if d:
                    d["public_url"] = _public_community_url(d.get("name", ""), d.get("city", ""))
        enriched.append({**c, "winner_data": winner_data, "loser_data": loser_data})
    return templates.TemplateResponse(request, "duplicates.html", {
        "candidates": enriched,
        "topic_labels": TOPIC_LABELS,
        "topic_icons": TOPIC_ICONS,
    })


@admin.post("/duplicates/scan")
async def admin_duplicates_scan():
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    from ..duplicates import detect_all
    count = detect_all(_db())
    return JSONResponse({"ok": True, "new_candidates": count})


@admin.post("/duplicates/flag")
async def admin_duplicates_flag(
    winner_key: str = Form(...),
    loser_key: str = Form(...),
    entity_type: str = Form("community"),
):
    """Manually flag two records as duplicates."""
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    from ..db import insert_duplicate_candidate
    if winner_key == loser_key:
        return JSONResponse({"ok": False, "error": "same record"})
    # Keep the admin's explicit choice: winner_key is the record the merge
    # keeps — sorting here used to override the UI's "keep" selection.
    inserted = insert_duplicate_candidate(
        _db(), entity_type, "", "", winner_key, loser_key, 1.0, "manual")
    return JSONResponse({"ok": True, "inserted": inserted})


@admin.get("/api/communities/search")
async def admin_communities_search(q: str = ""):
    """Return communities matching a name query (for manual duplicate flagging)."""
    if not app_state.db_path or len(q) < 2:
        return JSONResponse([])
    q_lower = q.lower()
    all_c = get_all_communities(_db())
    matches = [
        {"key": _community_record_key(r["name"], r["city"], r["topic"]),
         "label": f"{r['name']} – {r['city']} ({r.get('topic', '')})",
         "name": r["name"], "city": r["city"], "topic": r.get("topic", "")}
        for r in all_c if q_lower in r["name"].lower()
    ][:20]
    return JSONResponse(matches)


async def _ai_merge_communities(winner: dict, loser: dict) -> dict:
    """Use LLM to intelligently merge two community records."""
    fields = [
        "description", "meeting_schedule", "location", "contact", "website",
        "fee", "age_range", "skill_level", "join_process", "leader",
        "language", "frequency", "founding_year", "member_count",
        "email", "phone", "history", "tags", "social_links",
    ]
    w_summary = {k: winner.get(k) for k in ["name", "city", "topic"] + fields}
    l_summary = {k: loser.get(k) for k in ["name", "city", "topic"] + fields}
    prompt = (
        "Merge these two duplicate community records into one best record.\n\n"
        f"RECORD A:\n{json.dumps(w_summary, ensure_ascii=False, indent=2)}\n\n"
        f"RECORD B:\n{json.dumps(l_summary, ensure_ascii=False, indent=2)}\n\n"
        "Rules:\n"
        "- Keep Record A's name, city, topic unchanged\n"
        "- For text fields: pick the more informative/detailed value, or combine if both add unique info\n"
        "- For lists (tags, social_links): union both, remove duplicates\n"
        "- Omit fields that are null/empty in both records\n"
        "Output ONLY a JSON object with keys: " + ", ".join(fields) + "\n"
        "No explanation, just the JSON object."
    )
    merged_str = await _ai_chat(prompt, temperature=0.1)
    try:
        json_match = re.search(r'\{.*\}', merged_str, re.DOTALL)
        merged_fields = json.loads(json_match.group() if json_match else merged_str)
    except Exception:
        merged_fields = {}
    result = dict(winner)
    for f in fields:
        if f in merged_fields and merged_fields[f] is not None:
            result[f] = merged_fields[f]
    # Union source_urls
    w_urls = list(result.get("source_urls") or [])
    l_urls = list(loser.get("source_urls") or [])
    if result.get("source_url") and result["source_url"] not in w_urls:
        w_urls = [result["source_url"]] + w_urls
    if loser.get("source_url") and loser["source_url"] not in l_urls:
        l_urls = [loser["source_url"]] + l_urls
    result["source_urls"] = list(dict.fromkeys(w_urls + l_urls))
    return result


async def _bg_merge_community(db_path: Path, candidate_id: int, c: dict) -> None:
    winner = get_community_by_record_key(db_path, c["winner_key"])
    loser = get_community_by_record_key(db_path, c["loser_key"])
    if winner and loser:
        try:
            merged = await _ai_merge_communities(winner, loser)
            save_community_data(db_path, c["winner_key"], merged)
            set_community_hidden(db_path, c["loser_key"], True)
            log.info("ai_merge_done", winner=c["winner_key"], loser=c["loser_key"])
        except Exception as exc:
            log.warning("ai_merge_failed_fallback", error=str(exc))
            merge_community_into(db_path, c["winner_key"], c["loser_key"])
    else:
        merge_community_into(db_path, c["winner_key"], c["loser_key"])
    resolve_duplicate_candidate(db_path, candidate_id, "merged")


@admin.post("/duplicates/{candidate_id}/merge")
async def admin_duplicates_merge(candidate_id: int, background_tasks: BackgroundTasks):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    candidates = get_duplicate_candidates(_db())
    c = next((x for x in candidates if x["id"] == candidate_id), None)
    if not c:
        return JSONResponse({"ok": False, "error": "not found"})
    if c["entity_type"] == "community":
        background_tasks.add_task(_bg_merge_community, _db(), candidate_id, c)
    else:
        # Venues/persons: synchronous field-fill merge (no LLM involved).
        # Previously this only marked the candidate merged without touching
        # the records.
        from ..db import merge_entity_into
        merged = merge_entity_into(_db(), c["entity_type"],
                                   c["winner_key"], c["loser_key"])
        if not merged:
            resolve_duplicate_candidate(_db(), candidate_id, "dismissed")
            return JSONResponse({"ok": False, "error": "stale candidate — record missing"})
        resolve_duplicate_candidate(_db(), candidate_id, "merged")
    return JSONResponse({"ok": True})


@admin.post("/duplicates/{candidate_id}/dismiss")
async def admin_duplicates_dismiss(candidate_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    resolve_duplicate_candidate(_db(), candidate_id, "dismissed")
    return JSONResponse({"ok": True})


@admin.post("/duplicates/{candidate_id}/delete")
async def admin_duplicates_delete(candidate_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    delete_duplicate_candidate(_db(), candidate_id)
    return JSONResponse({"ok": True})


@admin.get("/wrong-city", response_class=HTMLResponse)
async def admin_wrong_city(request: Request):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    init_db(_db())
    enriched = []
    for c in get_wrong_city_candidates(_db()):
        record = get_community_by_record_key(_db(), c["record_key"])
        snippet = c.get("snippet") or ""
        match = c.get("matched_text") or ""
        idx = snippet.lower().find(match.lower()) if match else -1
        if idx >= 0:
            before, mid, after = snippet[:idx], snippet[idx:idx + len(match)], snippet[idx + len(match):]
        else:
            before, mid, after = snippet, "", ""
        enriched.append({
            **c,
            "record": record,
            "public_url": _public_community_url(record["name"], record["city"]) if record else None,
            "snippet_before": before, "snippet_match": mid, "snippet_after": after,
        })
    return templates.TemplateResponse(request, "wrong_city.html", {
        "candidates": enriched,
        "topic_labels": TOPIC_LABELS,
    })


@admin.post("/wrong-city/scan")
async def admin_wrong_city_scan():
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    from ..wrong_city import scan
    count = scan(_db(), [c.name for c in (app_state.cities or [])])
    return JSONResponse({"ok": True, "new_candidates": count})


@admin.post("/wrong-city/{candidate_id}/move")
async def admin_wrong_city_move(candidate_id: int):
    """Move the community to the mentioned city (same path as an approved
    wrong_city edit request — merges if the target identity already exists)."""
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    c = next((x for x in get_wrong_city_candidates(_db()) if x["id"] == candidate_id), None)
    if not c:
        return JSONResponse({"ok": False, "error": "not found"})
    applied = apply_community_edit(_db(), c["record_key"], "wrong_city", c["mentioned_city"])
    if applied not in ("ok", "merged"):
        return JSONResponse({"ok": False, "error": applied})
    resolve_wrong_city_candidate(_db(), candidate_id, "moved" if applied == "ok" else "merged")
    return JSONResponse({"ok": True, "status": applied})


@admin.post("/wrong-city/{candidate_id}/dismiss")
async def admin_wrong_city_dismiss(candidate_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    resolve_wrong_city_candidate(_db(), candidate_id, "dismissed")
    return JSONResponse({"ok": True})


@admin.get("/edit-requests", response_class=HTMLResponse)
async def admin_edit_requests(request: Request):
    if not app_state.db_path:
        return RedirectResponse("/admin", status_code=302)
    init_db(_db())
    edit_requests_list = get_edit_requests(_db(), status="pending")
    for r in edit_requests_list:
        if r.get("entity_type") == "community":
            r["public_url"] = _public_community_url(
                r.get("entity_name", ""), r.get("entity_city", ""))
    return templates.TemplateResponse(request, "edit_requests.html", {
        "requests": edit_requests_list,
        "topic_labels": TOPIC_LABELS,
    })


@admin.post("/edit-requests/{request_id}/approve")
async def admin_edit_requests_approve(request_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    edit_requests_list = get_edit_requests(_db(), status="pending")
    r = next((x for x in edit_requests_list if x["id"] == request_id), None)
    if not r:
        return JSONResponse({"ok": False, "error": "not found"})
    # The record to mutate is ALWAYS resolved from the identity the admin sees
    # (entity_name/city/topic). The form's record_key is client-controlled: a
    # submitter could pair an innocuous displayed name with another record's
    # key and have the approval mutate the unrelated record.
    # A claim asks for nothing to be changed — an organiser is telling us the
    # listing is theirs. There is no field to apply, so `apply_community_edit`
    # answers "unsupported" and the Approve button errors out, leaving Reject
    # as the only way to clear the highest-intent row on the page. Approving it
    # means acknowledging it: mark it resolved and keep the address.
    if r["change_type"] == "claim":
        await asyncio.to_thread(resolve_edit_request, _db(), request_id, "approved")
        log.info("claim_approved", request_id=request_id,
                 community=r.get("entity_name", ""), email=r.get("email", ""))
        return JSONResponse({"ok": True, "claim": True})
    if r["entity_type"] == "community":
        ckey = _community_record_key(
            r.get("entity_name", ""), r.get("entity_city", ""), r.get("entity_topic", ""))
        applied = apply_community_edit(_db(), ckey, r["change_type"], r["new_value"])
        if applied == "not_found":
            # Identity drift: the record may live under another topic or have
            # been re-keyed since submission. Fall back to a unique visible
            # name+city match before giving up.
            from ..identity import normalized_match_key
            nk = (normalized_match_key(r.get("entity_name", "")),
                  normalized_match_key(r.get("entity_city", "")))
            matches = [c for c in get_all_communities(_db())
                       if (normalized_match_key(c.get("name", "")),
                           normalized_match_key(c.get("city", ""))) == nk]
            if len(matches) == 1:
                m = matches[0]
                ckey = _community_record_key(m["name"], m["city"], m.get("topic", ""))
                applied = apply_community_edit(_db(), ckey, r["change_type"], r["new_value"])
        if applied not in ("ok", "merged"):
            log.warning("edit_request_apply_failed", request_id=request_id,
                        status=applied, change_type=r["change_type"],
                        name=r.get("entity_name"), city=r.get("entity_city"),
                        topic=r.get("entity_topic"))
            _msg = {
                "not_found": "community record not found — it may have been merged, renamed or re-keyed since the request was submitted",
                "unsupported": f"unsupported change type: {r['change_type']}",
            }
            return JSONResponse({"ok": False, "error": _msg.get(applied, applied)})
    elif r["entity_type"] == "venue" and r["change_type"] in ("closed", "name_correction"):
        from ..db import apply_venue_edit
        from ..identity import venue_record_key as _vrk
        vkey = _vrk(r.get("entity_name", ""), r.get("entity_city", ""))
        applied = apply_venue_edit(_db(), vkey, r["change_type"], r["new_value"])
        if not applied:
            return JSONResponse({"ok": False, "error": "venue not found or name conflict"})
    # venue 'wrong_info' carries free-text notes only — approving it records the
    # decision; the admin applies the described fix manually.
    resolve_edit_request(_db(), request_id, "approved")
    return JSONResponse({"ok": True})


@admin.post("/edit-requests/{request_id}/reject")
async def admin_edit_requests_reject(request_id: int):
    if not app_state.db_path:
        return JSONResponse({"ok": False})
    resolve_edit_request(_db(), request_id, "rejected")
    return JSONResponse({"ok": True})


_fastapi.include_router(admin)

# Public OpenAI-compatible LLM gateway at /v1/*. Not behind _BasicAuth (that
# guards /admin only) — it carries its own Bearer auth and is off entirely when
# ROUTER_API_KEY is unset.
from .api import router as _gateway_router  # noqa: E402
_fastapi.include_router(_gateway_router)


@_fastapi.get("/helyszinek", response_class=HTMLResponse)
async def public_venues(request: Request, city: str = "", topic: str = ""):
    if not app_state.db_path:
        return RedirectResponse("/", status_code=302)
    # Off the loop, both of them. Unfiltered, this route read every venue in the
    # database and rendered every one of them: measured live on 2026-08-21 the
    # page was **15.5 MB and took 34 seconds**, all of it on the event loop, so
    # a single request to it — a visitor's or Googlebot's — stalled the whole
    # site for the duration. The site losing minutes several times a day has
    # been chased all week as a deploy problem.
    await asyncio.to_thread(init_db, app_state.db_path)
    site_names = {c.name for c in _site_cities(request)}
    all_venues = [v for v in await asyncio.to_thread(get_all_venues, app_state.db_path)
                  if v.get("city", "") in site_names]

    # Filter
    filtered = all_venues
    if city:
        filtered = [v for v in filtered if v.get("city", "").lower() == city.lower()]
    if topic:
        filtered = [v for v in filtered if topic in (v.get("welcomed_topics") or [])]

    # Build filter options from HU dataset
    all_cities = sorted({v.get("city", "") for v in all_venues if v.get("city")})
    all_topics = sorted({
        t for v in all_venues for t in (v.get("welcomed_topics") or []) if t
    })

    # Build city grouping for unfiltered view
    from collections import defaultdict
    city_map: dict = defaultdict(list)
    for v in filtered:
        city_map[v.get("city") or "—"].append(v)
    city_sections = [
        {"name": ci, "venues": vs, "count": len(vs)}
        for ci, vs in sorted(city_map.items(), key=lambda kv: (-len(kv[1]), _hu_sort_key(kv[0])))
    ]

    return templates.TemplateResponse(request, "public_venues.html", {
        "venues": filtered,
        "city_sections": city_sections,
        "all_cities": all_cities,
        "all_topics": all_topics,
        "selected_city": city,
        "selected_topic": topic,
        "total_all": len(all_venues),
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        **lang_context(request),
    })


@_fastapi.get("/venues", response_class=HTMLResponse)
async def public_venues_en():
    return RedirectResponse("/helyszinek", status_code=301)


async def _render_people(request: Request, city: str = ""):
    """People index. Empty by default — the person list appears only after a city
    is picked (avoids dumping every city/person). No role filter/label: the only
    role we extract is 'leader'. URL is localized per site (/people vs /emberek)."""
    if not app_state.db_path:
        return templates.TemplateResponse(request, "public_people.html", {
            "city_groups": [], "total_persons": 0, "all_cities": [],
            "selected_city": city, **lang_context(request),
        })
    # Same treatment as /helyszinek: this page renders little, but it still
    # reads every person in the database, and a table scan on the event loop
    # stalls every other request for as long as it runs.
    await asyncio.to_thread(init_db, app_state.db_path)
    site_names = {c.name for c in _site_cities(request)}
    all_persons = await asyncio.to_thread(get_publishable_persons, app_state.db_path)
    # Deduplicate: one card per person (name+city slug), merged across communities
    seen: dict[tuple, dict] = {}
    for p in all_persons:
        if p.get("city", "") not in site_names:
            continue
        key = (_slugify(p.get("name", "")), _slugify(p.get("city", "")))
        seen.setdefault(key, p)
    unique = list(seen.values())
    from .i18n import _detect_site
    # Hungarian collation only on the HU site; elsewhere plain case-insensitive
    # (avoids _hu_sort_key misplacing international names like Örebro).
    sort_key = _hu_sort_key if _detect_site(request) == "kozossegek" else (lambda s: s.casefold())
    all_cities = sorted({p.get("city", "") for p in unique if p.get("city")}, key=sort_key)

    city_groups: list = []
    total = 0
    if city:
        persons = sorted((p for p in unique if p.get("city", "").lower() == city.lower()),
                         key=lambda x: x.get("name", ""))
        if persons:
            city_groups = [{"name": persons[0].get("city") or city, "persons": persons}]
            total = len(persons)
    return templates.TemplateResponse(request, "public_people.html", {
        "city_groups": city_groups,
        "total_persons": total,
        "all_cities": all_cities,
        "selected_city": city,
        **lang_context(request),
    })


@_fastapi.get("/emberek", response_class=HTMLResponse)
async def public_people(request: Request, city: str = ""):
    from .i18n import _detect_site
    if _detect_site(request) == "meetapedia":
        return RedirectResponse("/people" + (f"?city={_url_quote(city, safe='')}" if city else ""),
                                status_code=301)
    return await _render_people(request, city)


@_fastapi.get("/kereses", response_class=HTMLResponse)
async def public_search(request: Request):
    q = request.query_params.get("q", "").strip()
    results: dict = {"communities": [], "venues": [], "persons": []}
    if app_state.db_path and len(q) >= 2:
        init_db(app_state.db_path)
        # Off the loop: a LIKE over every community blob, and public.
        results = await asyncio.to_thread(
            search_all, app_state.db_path, q, 20,
            {c.name for c in _site_cities(request)})
    seen_ids: set = set()
    communities = []
    for c in results["communities"]:
        cid = c.get("community_id") or (c.get("name", ""), c.get("city", ""))
        if cid in seen_ids:
            continue  # same community indexed under multiple topics
        seen_ids.add(cid)
        communities.append(c)
    venues = results["venues"]
    persons = results["persons"]
    total = len(communities) + len(venues) + len(persons)
    return templates.TemplateResponse(request, "public_search.html", {
        "q": q,
        "communities": communities,
        "venues": venues,
        "persons": persons,
        "total": total,
        "topic_icons": TOPIC_ICONS,
        **lang_context(request),
    })


@_fastapi.get("/people", response_class=HTMLResponse)
async def public_people_en(request: Request, city: str = ""):
    from .i18n import _detect_site
    if _detect_site(request) == "kozossegek":
        return RedirectResponse("/emberek" + (f"?city={_url_quote(city, safe='')}" if city else ""),
                                status_code=301)
    return await _render_people(request, city)


@_fastapi.get("/{city_slug}/helyszin/{venue_slug}", response_class=HTMLResponse)
async def public_venue_detail(request: Request, city_slug: str, venue_slug: str):
    if not app_state.db_path:
        return RedirectResponse("/helyszinek", status_code=302)
    # An unknown city is a redirect, not a scan: the old fallback loaded and
    # JSON-parsed every venue on the event loop for any bot-typed slug.
    city_name = _city_from_slug(city_slug)
    if not city_name or city_name not in {c.name for c in _site_cities(request)}:
        # Same rule as the city pages: kozossegek.com serves Hungarian cities
        # only. It answered 200 for foreign venues with a self-canonical — a
        # duplicate of the meetapedia.com page.
        if (redirect := _hu_redirect(request, city_name)):
            return redirect
        return RedirectResponse("/helyszinek", status_code=302)
    if (redirect := _hu_redirect(request, city_name)):
        return redirect
    venues = await asyncio.to_thread(get_venues, app_state.db_path, city_name)
    venue = next((v for v in venues if _slugify(v.get("name", "")) == venue_slug), None)
    if not venue:
        return RedirectResponse("/helyszinek", status_code=302)
    community_ids = venue.get("community_ids") or []
    communities = get_communities_for_venue(
        app_state.db_path, community_ids, venue.get("name", ""), city_name
    )
    from ..identity import venue_record_key as _vrk_detail
    venue["record_key"] = _vrk_detail(venue.get("name", ""), city_name)
    city_locale = _city_locale(city_name)
    topic_url_slugs = {t.name: _topic_url_slug(t.name, city_locale) for t in (app_state.topics or [])}
    _city_records = await asyncio.to_thread(
        get_communities_for_city, app_state.db_path, city_name) if app_state.db_path else []
    _venue_canonical = (f"{_canonical_base(request, city_name)}"
                        f"/{city_slug}/helyszin/{venue_slug}")
    return templates.TemplateResponse(request, "public_venue_detail.html", {
        "v": venue,
        # A named physical place in a named town is the shape local search
        # understands best, and this page type carried no markup until
        # 2026-09-20.
        "schema_json": venue_jsonld(venue, _venue_canonical),
        # Labels in the page's language, not "hu": these notes rendered
        # Hungarian topic names on meetapedia.com's venue and person pages.
        "related": related_communities(_city_records, exclude_key="", topic=None,
                                       locale=lang_context(request)["lang"]),
        "city": city_name,
        "city_slug": city_slug,
        "communities": communities,
        "topic_url_slugs": topic_url_slugs,
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        "canonical_base": _canonical_base(request, city_name),
        "breadcrumbs": _crumbs(
            request, (city_name, f"/{city_slug}"),
            (venue.get("name", ""), f"/{city_slug}/helyszin/{venue_slug}"),
        ),
        **lang_context(request),
        "sister_url": _sister_url(request, city_name),
    })


@_fastapi.get("/{city_slug}/ember/{name_slug}", response_class=HTMLResponse)
async def public_person_detail(request: Request, city_slug: str, name_slug: str):
    if not app_state.db_path:
        return RedirectResponse("/emberek", status_code=302)
    city_name = _city_from_slug(city_slug)
    if not city_name or city_name not in {c.name for c in _site_cities(request)}:
        if (redirect := _hu_redirect(request, city_name)):
            return redirect
        return RedirectResponse("/emberek", status_code=302)
    if (redirect := _hu_redirect(request, city_name)):
        return redirect
    all_persons = await asyncio.to_thread(get_publishable_persons, app_state.db_path, city_name)
    merged = [p for p in all_persons if _slugify(p.get("name", "")) == name_slug]
    if not merged:
        # 404, not a redirect to the index: these URLs were in the sitemap
        # until 2026-10-03 (placeholders, people whose group is not listed in
        # this town), and a redirect to a list is a soft 404 that keeps them
        # in Google's queue instead of letting them drop out.
        return HTMLResponse("Not found", status_code=404)
    _person_lang = lang_context(request)
    community_entries = []
    seen: dict = {}
    for p in merged:
        community_name = p.get("community_name", "")
        topic = p.get("topic", "")
        key = (community_name, topic)
        role = p.get("role", "")
        if key in seen:
            # accumulate extra roles on the existing entry
            if role and role not in seen[key]["roles"]:
                seen[key]["roles"].append(role)
            continue
        entry = {
            "name": community_name,
            "url": f"/{city_slug}/{_slugify(community_name)}",
            "roles": [role] if role else [],
            "topic": topic,
            # Same trap as the community page's topic picker: this key is not
            # `topic_labels`, so `**lang_context(request)` never corrected it
            # and a Hungarian person page labelled its communities in English.
            "topic_label": _person_lang["topic_labels"].get(
                topic, topic.replace("_", " ").title()),
            "topic_icon": TOPIC_ICONS.get(topic, "circle"),
        }
        seen[key] = entry
        community_entries.append(entry)
    person = merged[0]
    bio = next((p.get("bio") for p in merged if p.get("bio")), None)
    website = next((p.get("website") for p in merged if p.get("website")), None)
    social_links = list(dict.fromkeys(
        lnk for p in merged for lnk in (p.get("social_links") or [])
    ))
    _city_records = await asyncio.to_thread(
        get_communities_for_city, app_state.db_path, city_name) if app_state.db_path else []
    _person_canonical = (f"{_canonical_base(request, city_name)}"
                         f"/{city_slug}/ember/{name_slug}")
    return templates.TemplateResponse(request, "public_person_detail.html", {
        "person": person,
        # memberOf is built from `community_entries`, the same list the page
        # renders, so the markup cannot drift from what a reader sees.
        "schema_json": person_jsonld(
            {**person, "bio": bio, "website": website,
             "social_links": social_links, "city": city_name},
            [{"name": c["name"], "url": _canonical_base(request, city_name) + c["url"]}
             for c in community_entries],
            _person_canonical),
        # Labels in the page's language, not "hu": these notes rendered
        # Hungarian topic names on meetapedia.com's venue and person pages.
        "related": related_communities(_city_records, exclude_key="", topic=None,
                                       locale=lang_context(request)["lang"]),
        "bio": bio,
        "website": website,
        "social_links": social_links,
        "community_entries": community_entries,
        "city": city_name,
        "city_slug": city_slug,
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        "canonical_base": _canonical_base(request, city_name),
        "breadcrumbs": _crumbs(
            request, (city_name, f"/{city_slug}"),
            (person.get("name", ""), f"/{city_slug}/ember/{name_slug}"),
        ),
        **_person_lang,
        "sister_url": _sister_url(request, city_name),
    })


@_fastapi.get("/{city_slug}/{segment}", response_class=HTMLResponse)
async def public_city_segment(
    request: Request, city_slug: str, segment: str, subscribed: str = ""
):
    city_name = _city_from_slug(city_slug)
    if not city_name:
        return RedirectResponse("/", status_code=302)
    if city_name not in {c.name for c in _site_cities(request)}:
        return RedirectResponse("/", status_code=302)
    if (redirect := _hu_redirect(request, city_name)):
        return redirect
    topic_names = {t.name for t in (app_state.topics or [])}
    city_locale = _city_locale(city_name)
    # Try localized slug first, then fall back to English slug
    actual_topic = _topic_from_url_slug(segment, city_locale)
    if actual_topic not in topic_names:
        actual_topic = segment if segment in topic_names else segment.replace("-", "_")
    if actual_topic in topic_names:
        return await _render_explore(
            request, city=city_name, topic=[actual_topic], subscribed=subscribed
        )
    # One read of the city, off the loop, used twice: to find this community
    # and for its neighbours. The lookup used to read the whole city again,
    # synchronously on the event loop, on every one of ~42k community pages.
    _city_records = await asyncio.to_thread(get_communities_for_city, _db(), city_name)
    record = next((_ensure_community_id(r) for r in _city_records
                   if _slugify(r.get("name", "")) == segment), None)
    if record and is_placeholder_leader(record.get("leader")):
        # Stored before the model rejected placeholders; the page, its person
        # link and its schema `member` would all repeat "Jane Smith".
        record = {**record, "leader": None}
    if record:
        _page_lang = lang_context(request)
        schema_json = records_to_jsonld(
            [record],
            f"{_canonical_base(request, city_name)}/{city_slug}"
            f"/{_slugify(record.get('name', ''))}")
        rec_topic = record.get("topic", "")
        city_locale = _city_locale(city_name)
        public_topic = rec_topic if rec_topic in topic_names else None
        topic_url_slugs = {t.name: _topic_url_slug(t.name, city_locale) for t in (app_state.topics or [])}

        def _community_extras():
            if not app_state.db_path:
                return [], None, []
            cid = record.get("community_id", "")
            return (get_community_history(app_state.db_path, cid),
                    get_venue_for_community(app_state.db_path, cid, city_name),
                    get_persons_for_community(app_state.db_path, record["name"], city_name))
        history, community_venue, community_persons = await asyncio.to_thread(_community_extras)
        related = related_communities(
            _city_records,
            exclude_key=_community_record_key(record["name"], city_name, rec_topic),
            topic=public_topic, locale=_page_lang["lang"])
        breadcrumb_pairs = [(city_name, f"/{city_slug}")]
        if public_topic:
            breadcrumb_pairs.append((
                get_topic_labels(lang_context(request)["lang"]).get(
                    public_topic, public_topic.replace("_", " ").title()),
                f"/{city_slug}/{_topic_url_slug(public_topic, city_locale)}",
            ))
        breadcrumb_pairs.append((
            record.get("name", ""), f"/{city_slug}/{_slugify(record.get('name', ''))}",
        ))
        return templates.TemplateResponse(request, "public_community.html", {
            "r": record,
            "related": related,
            "topic": rec_topic,
            "topic_slug": _topic_url_slug(public_topic, city_locale) if public_topic else None,
            "city": city_name,
            "schema_json": schema_json,
            "topic_icons": TOPIC_ICONS,
            "topic_labels": TOPIC_LABELS,
            "community_history": history,
            "topic_url_slugs": topic_url_slugs,
            "record_key": _community_record_key(record["name"], city_name, rec_topic),
            "community_venue": community_venue,
            "community_persons": community_persons,
            # No `all_cities` here on purpose. The wrong-city picker in the
            # report form used to render every configured city as an <option>:
            # 3,922 of them, 176 KB, **76% of the page**, byte-identical across
            # all 42,091 community pages and sitting inside two nested `hidden`
            # divs where no visitor ever saw it. Measured on the live page, the
            # document held 5,089 words of text of which 510 were the community
            # — the rest was a dropdown. That is what a near-duplicate looks
            # like to a crawler, and 23,461 of these pages are sitting in
            # "Crawled – currently not indexed". The picker now fetches
            # /api/cities.json the first time someone opens it.
            # i18n labels, not the English TOPIC_LABELS fallback. Every other
            # label on this page is translated because `topic_labels` arrives
            # through `**lang_context(request)` and overrides the explicit
            # kwarg; this list is built under its own key, so nothing overrode
            # it and a Hungarian reader picked from "Religion & Faith" and
            # "Book Club" (reported 2026-09-20). The topics with no English
            # label at all — Hagyományőrzés, Baba & Szülő, Kisállat — were the
            # only Hungarian entries, which made it look like a partial
            # translation rather than the wrong dictionary.
            "all_topic_names": [
                (t.name, _page_lang["topic_labels"].get(
                    t.name, t.name.replace("_", " ").title()))
                for t in (app_state.topics or [])],
            "canonical_base": _canonical_base(request, city_name),
            # Indexable whether or not it has a description. The guard was
            # added when a community page with no description really was
            # nothing — a name, a city, a topic, and links only to its own
            # header pickers. It now carries up to two dozen links to real
            # neighbours in the same town, which is content and a crawl path
            # at once, and 68% of the corpus was excluding itself from search
            # while 23,461 pages sat in "Crawled – currently not indexed".
            #
            # The risk is stated rather than hidden: these pages differ from
            # their siblings mainly by title until enrichment reaches them, so
            # if Google starts treating them as near-duplicates the answer is
            # to make them substantive, not to hide them again.
            "page_noindex": False,
            "breadcrumbs": _crumbs(request, *breadcrumb_pairs),
            **_page_lang,
            "sister_url": _sister_url(request, city_name),
        })
    return RedirectResponse(f"/{city_slug}", status_code=302)


@_fastapi.get("/{city_slug}", response_class=HTMLResponse)
async def public_city(request: Request, city_slug: str, subscribed: str = ""):
    city_name = _city_from_slug(city_slug)
    if not city_name:
        return RedirectResponse("/", status_code=302)
    if city_name not in {c.name for c in _site_cities(request)}:
        return RedirectResponse("/", status_code=302)
    return await _render_explore(request, city=city_name, subscribed=subscribed)
