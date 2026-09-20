import asyncio
import base64
import html
import hmac
import importlib.metadata
import json
import os
from functools import lru_cache
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote as _url_quote, urlsplit

import structlog
import yaml
from fastapi import APIRouter, BackgroundTasks, FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.types import ASGIApp, Receive, Scope, Send

from ..config import load_config, load_config_from_docs
from ..db import (
    delete_all_communities,
    get_sitemap_communities,
    find_community_by_id,
    get_extraction_quality_mix,
    get_community_history,
    get_all_communities,
    get_city_topic_counts,
    get_community_lastmods,
    get_city_totals,
    get_communities,
    get_communities_by_ids,
    get_data_guide,
    get_data_guides,
    count_data_guides,
    get_data_guide_sitemap_rows,
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
    get_all_persons,
    get_person_counts,
    get_person_history,
    get_scope_stats,
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
from ..models import CommunityRecord
from ..pipeline import (
    RUN_ABORTED,
    _enrich_record,
    _needs_enrichment,
    classify_run_outcome,
    reextract_community,
    run_pipeline,
    scrape_submitted_url,
)
from ..search import DataForSEOClient, FallbackSearchClient
from ..store import save_results
from ..url_safety import (UnsafeURLError, assert_safe_public_url,
                          is_public_http_url)
from .i18n import get_topic_labels, lang_context
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
templates.env.filters["breadcrumb_jsonld"] = breadcrumb_jsonld

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


def _read_config_yaml(name: str) -> object:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


def _validate_candidate_config(
    cities_yaml: str | None = None,
    topics_yaml: str | None = None,
    settings_yaml: str | None = None,
) -> None:
    cities_raw = yaml.safe_load(cities_yaml) if cities_yaml is not None else _read_config_yaml("cities.yaml")
    topics_raw = yaml.safe_load(topics_yaml) if topics_yaml is not None else _read_config_yaml("topics.yaml")
    settings_raw = yaml.safe_load(settings_yaml) if settings_yaml is not None else _read_config_yaml("settings.yaml")
    load_config_from_docs(_db(), cities_raw, topics_raw, settings_raw)


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


def _find_community_by_slug(city_name: str, name_slug: str) -> dict | None:
    for r in get_communities_for_city(_db(), city_name):
        r = _ensure_community_id(r)
        if _slugify(r.get("name", "")) == name_slug:
            return r
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


def _top_cities(n: int = 8) -> list[tuple[str, str, int]]:
    city_totals = get_city_totals(_db())
    cities_map = {c.name: c.country for c in (app_state.cities or [])}
    return [(name, cities_map.get(name, ""), count)
            for name, count in city_totals[:n] if count > 0]


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


def _cities_by_country(
    user_country: str | None,
    user_top: int = 20,
    other_countries: int = 3,
    other_top: int = 8,
) -> dict:
    """Return grouped city data for the home page city browser."""
    city_totals = dict(get_city_totals(_db()))
    cities_map = {c.name: c.country for c in (app_state.cities or [])}

    # Group by country
    country_cities: dict[str, list[tuple[str, int]]] = {}
    for name, country in cities_map.items():
        count = city_totals.get(name, 0)
        if count > 0:
            country_cities.setdefault(country, []).append((name, count))

    for cities_list in country_cities.values():
        cities_list.sort(key=lambda x: x[1], reverse=True)

    user_cities: list[tuple[str, str, int]] = []
    if user_country and user_country in country_cities:
        user_cities = [
            (name, user_country, count)
            for name, count in country_cities[user_country][:user_top]
        ]

    # Top other countries by total community count
    other_sorted = sorted(
        [(c, cities) for c, cities in country_cities.items() if c != user_country],
        key=lambda x: sum(cnt for _, cnt in x[1]),
        reverse=True,
    )
    other_sections = [
        {
            "country": country,
            "cities": [(name, country, count) for name, count in cities[:other_top]],
            "total": sum(cnt for _, cnt in cities),
        }
        for country, cities in other_sorted[:other_countries]
    ]

    return {
        "user_country": user_country,
        "user_cities": user_cities,
        "other_sections": other_sections,
    }


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
        counts = get_city_topic_counts(_db()).get(city, {}) if app_state.db_path else {}
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
            # When browsing by topic, show ALL cities and ALL records so counts match.
            # Without topic, show 3-city sample for discovery.
            all_city_entries = country_cities.get(country, [])
            city_entries = all_city_entries if topic else all_city_entries[:3]
            city_sections: list[dict] = []
            for city_name, city_count in city_entries:
                if topic:
                    recs: list[dict] = []
                    for t in topic:
                        recs.extend(_load_communities(city_name, t))
                else:
                    recs = [_ensure_community_id(r) for r in get_communities_for_city(_db(), city_name)][:10]
                if recs:
                    city_url = "/" + _slugify(city_name)
                    if topic and len(topic) == 1:
                        city_url += "/" + _topic_url_slug(topic[0], _city_locale(city_name))
                    topic_count = len(recs) if topic else city_count
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


@_fastapi.post("/subscribe")
async def public_subscribe(
    request: Request,
    email: str = Form(...),
    city: str = Form(...),
    topics: list[str] = Form(default=[]),
):
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
            resend.Emails.send({
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
    community_name: str = Form(""),
    city: str = Form(""),
    topic: str = Form(""),
    page_url: str = Form(""),
    message: str = Form(""),
    user_email: str = Form(""),
):
    if _FEEDBACK_EMAIL and message and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            safe_user_email = html.escape(user_email)
            safe_page_url = html.escape(page_url, quote=True)
            safe_message = html.escape(message).replace("\n", "<br>")
            reply_line = f"<b>Reply-to:</b> {safe_user_email}<br>" if user_email else ""
            resend.Emails.send({
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
    community_id: str = Form(""),
    community_name: str = Form(""),
    city: str = Form(""),
    page_url: str = Form(""),
    claimant_email: str = Form(""),
):
    if not community_name or not claimant_email:
        return JSONResponse({"ok": False, "error": "missing_fields"})
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
            resend.Emails.send({
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
    community_id: str = Form(""),
    community_name: str = Form(""),
    city: str = Form(""),
    topic: str = Form(""),
    source_url: str = Form(""),
    page_url: str = Form(""),
):
    if not community_name or not app_state.db_path:
        return JSONResponse({"ok": False})
    save_not_community_report(
        _db(), community_id, community_name, city, topic, source_url, page_url
    )
    log.info("not_community_reported", name=community_name, city=city)
    if _FEEDBACK_EMAIL and _RESEND_API_KEY:
        try:
            import resend
            resend.api_key = _RESEND_API_KEY
            safe_page = html.escape(page_url, quote=True)
            resend.Emails.send({
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
            resend.Emails.send({
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
    result = {}
    for t in (app_state.topics or []):
        result[t.name] = len(_load_communities(city, t.name))
    return JSONResponse(result)


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
    if city_name.strip() and app_state.db_path:
        save_city_request(app_state.db_path, city_name, email)
    return RedirectResponse("/varosok?requested=" + city_name.strip(), status_code=303)


@_fastapi.post("/cities/request")
async def request_city_en(request: Request, city_name: str = Form(""), email: str = Form("")):
    return RedirectResponse("/varosok", status_code=301)


@_fastapi.get("/admin", response_class=HTMLResponse)
async def admin_root_redirect():
    return RedirectResponse("/admin/", status_code=301)


@_fastapi.get("/rolunk", response_class=HTMLResponse)
async def public_about(request: Request):
    from .i18n import _detect_site
    site = _detect_site(request)
    site_cities = _site_cities(request)
    site_city_names = {c.name for c in site_cities}
    site_topic_counts = _hu_topic_counts() if site == "kozossegek" else _global_topic_counts()
    city_totals = dict(get_city_totals(_db())) if app_state.db_path else {}
    venue_counts = {k: v for k, v in (get_venue_counts(_db()) if app_state.db_path else {}).items() if k in site_city_names}
    person_counts = {k: v for k, v in (get_person_counts(_db()) if app_state.db_path else {}).items() if k in site_city_names}
    all_site_cities = sorted(
        [{"name": c.name, "slug": _slugify(c.name), "count": city_totals.get(c.name, 0)}
         for c in site_cities],
        key=lambda c: _hu_sort_key(c["name"]),
    )
    country_city_groups: dict = {}
    if site == "meetapedia":
        for c in site_cities:
            country = c.country or "Other"
            entry = {"name": c.name, "slug": _slugify(c.name), "count": city_totals.get(c.name, 0)}
            country_city_groups.setdefault(country, []).append(entry)
        for grp in country_city_groups.values():
            grp.sort(key=lambda c: c["name"])
        country_city_groups = dict(sorted(country_city_groups.items()))
    return templates.TemplateResponse(request, "public_about.html", {
        "city_count": len(site_city_names),
        "topic_count": len(app_state.topics or []),
        "total_records": sum(site_topic_counts.values()),
        "total_venues": sum(venue_counts.values()),
        "total_persons": sum(person_counts.values()),
        "topics": app_state.topics or [],
        "topic_icons": TOPIC_ICONS,
        "topic_labels": TOPIC_LABELS,
        "topic_counts": site_topic_counts,
        "all_hu_cities": all_site_cities,
        "country_city_groups": country_city_groups,
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
    init_db(_db())
    save_community_submission(
        _db(), name.strip(), city.strip(), topic.strip(),
        source_url.strip(), submitter_email.strip() or None,
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
        f"Sitemap: {site_url}/sitemap.xml\n"
    )


#: Rendered sitemaps per site, with the time they were built. The document is
#: pure SQL + string assembly over the whole corpus; Google refetches on its own
#: schedule and does not need it fresher than this.
_SITEMAP_CACHE: dict[str, tuple[float, str]] = {}
_SITEMAP_TTL = 3600.0


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


@_fastapi.get("/sitemap.xml")
async def sitemap(request: Request):
    # Local import: other functions in this module already bind `_time`
    # locally, and a module-level name would be shadowed inside them.
    import time as _time

    from fastapi.responses import Response as _Response
    ctx = lang_context(request)
    cache_key = ctx.get("site") or "kozossegek"
    cached = _SITEMAP_CACHE.get(cache_key)
    if cached and (_time.monotonic() - cached[0]) < _SITEMAP_TTL:
        return _Response(cached[1], media_type="application/xml")
    # Built in a worker thread: every query below is blocking sqlite, and on the
    # event loop it stalls unrelated requests for as long as it runs.
    xml = await asyncio.to_thread(_build_sitemap, ctx)
    _SITEMAP_CACHE[cache_key] = (_time.monotonic(), xml)
    return _Response(xml, media_type="application/xml")


def _build_sitemap(ctx: dict) -> str:
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
        for p in get_all_persons(app_state.db_path):
            if p.get("city", "") not in site_city_names:
                continue
            city_sl = _slugify(p.get("city", ""))
            name_sl = _slugify(p.get("name", ""))
            if city_sl and name_sl and (city_sl, name_sl) not in seen_persons:
                seen_persons.add((city_sl, name_sl))
                locs.append(f"{base}/{city_sl}/ember/{name_sl}")

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for loc in dict.fromkeys(locs):  # deduplicate while preserving order
        lastmod = f"<lastmod>{lastmods[loc]}</lastmod>" if loc in lastmods else ""
        lines.append(
            f"  <url><loc>{loc}</loc>{lastmod}<changefreq>weekly</changefreq></url>"
        )
    lines.append("</urlset>")
    return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# ADMIN ROUTES  (prefix: /admin, protected by _BasicAuth)
# ═══════════════════════════════════════════════════════════════════════════════

def _get_run_scopes() -> dict:
    """Compute expected search/fetch/AI call counts for each Run Now preset."""
    from ..extract import _prompt_hash, get_prompt as _ep
    cfg = app_state.pipeline_cfg
    if not cfg or not app_state.db_path:
        return {}
    # Best currently active model (same priority as pipeline extractor selection)
    if cfg.deepseek_api_key:
        model = cfg.deepseek_model or "deepseek-chat"
    else:
        return {}
    try:
        extract_fp = _prompt_hash(_ep("extraction_system") + model)
        venue_fp   = _prompt_hash(_ep("venue_system") + model)
        person_fp  = _prompt_hash(_ep("person_system") + model)
        stats = get_scope_stats(app_state.db_path, extract_fp, venue_fp, person_fp)
        hu_names = list(_hu_city_names())
        stats_hu = get_scope_stats(app_state.db_path, extract_fp, venue_fp, person_fp, cities=hu_names) if hu_names else {"with_text": 0, "extract_match": 0, "venue_match": 0, "person_match": 0}
    except Exception:
        return {}
    n              = stats["with_text"]
    extract_needed = n - stats["extract_match"]
    venue_needed   = n - stats["venue_match"]
    person_needed  = n - stats["person_match"]
    n_hu              = stats_hu["with_text"]
    extract_needed_hu = n_hu - stats_hu["extract_match"]
    venue_needed_hu   = n_hu - stats_hu["venue_match"]
    person_needed_hu  = n_hu - stats_hu["person_match"]
    city_count     = len(app_state.cities or [])
    topic_count    = len(app_state.topics or [])
    hu_city_count  = len(hu_names)
    search_pairs   = city_count * topic_count
    search_pairs_hu = hu_city_count * topic_count
    return {
        "smart": {
            "search":    search_pairs,
            "search_hu": search_pairs_hu,
            "fetch":     None,
            "fetch_hu":  None,
            "ai":        extract_needed + venue_needed + person_needed,
            "ai_hu":     extract_needed_hu + venue_needed_hu + person_needed_hu,
        },
        "rebuild": {
            "search":    search_pairs,
            "search_hu": search_pairs_hu,
            "fetch":     n,
            "fetch_hu":  n_hu,
            "ai":        n + venue_needed + person_needed,
            "ai_hu":     n_hu + venue_needed_hu + person_needed_hu,
        },
        "reai": {
            "search":    0,
            "search_hu": 0,
            "fetch":     0,
            "fetch_hu":  0,
            "ai":        n + venue_needed + person_needed,
            "ai_hu":     n_hu + venue_needed_hu + person_needed_hu,
        },
    }


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
        "upgrade_min_gain": catalogue.router.upgrade_min_gain,
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


@admin.post("/prompts/nc-accept")
async def prompts_nc_accept(rule_text: str = Form(...)):
    """Save an AI-generated extraction rule into the prompt."""
    if not app_state.db_path or not rule_text.strip():
        return JSONResponse({"ok": False})
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
async def prompts_nc_rule_remove(name: str = Form(...)):
    """Remove an AI-generated extraction rule."""
    if not app_state.db_path:
        return JSONResponse({"ok": False})
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


@admin.post("/config/cities")
async def save_cities(request: Request, cities_yaml: str = Form(...)):
    try:
        _validate_candidate_config(cities_yaml=cities_yaml)
        (CONFIG_DIR / "cities.yaml").write_text(cities_yaml, encoding="utf-8")
        _reload_runtime_config()
        return RedirectResponse("/admin/config?saved=cities", status_code=302)
    except Exception as exc:
        return _config_error_redirect(exc)


@admin.post("/config/topics")
async def save_topics(request: Request, topics_yaml: str = Form(...)):
    try:
        _validate_candidate_config(topics_yaml=topics_yaml)
        (CONFIG_DIR / "topics.yaml").write_text(topics_yaml, encoding="utf-8")
        _reload_runtime_config()
        return RedirectResponse("/admin/config?saved=topics", status_code=302)
    except Exception as exc:
        return _config_error_redirect(exc)


@admin.post("/config/settings")
async def save_settings(request: Request, settings_yaml: str = Form(...)):
    """Refused on purpose. settings.yaml is version-controlled.

    This form used to write the file and reload the config, which looked like it
    worked — until the next deploy replaced the container and the edit was gone.
    On 2026-08-18 the concurrency settings were lost exactly that way, and the
    only evidence was the pipeline quietly running the old values.

    A form that silently loses its input is worse than no form. cities.yaml and
    topics.yaml keep their editors: those are content, curated by hand and read
    back from disk. Settings are code.
    """
    del settings_yaml
    return _config_error_redirect(Exception(
        "settings.yaml is version-controlled — edit it in the repository and "
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

    cities = app_state.cities or []
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


@admin.get("/api/cache-entries")
async def api_cache_entries():
    """Return fresh cache entries as JSON for live table refresh."""
    entries = []
    if app_state.cache_manager:
        entries = app_state.cache_manager.get_index()
    return JSONResponse(entries)


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
        entries = app_state.cache_manager.get_index()
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
            e for e in app_state.cache_manager.get_index()
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


@admin.post("/progress/clear-all")
async def cache_clear_all():
    if app_state.cache_manager:
        app_state.cache_manager.clear_all()
    deleted = delete_all_communities(_db())
    log.info("clear_all_data", deleted_communities=deleted)
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
    all_persons = await asyncio.to_thread(get_all_persons, app_state.db_path)
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
        results = search_all(app_state.db_path, q)
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
    city_name = _city_from_slug(city_slug)
    if city_name:
        venues = get_venues(app_state.db_path, city_name)
    else:
        # Cities not yet loaded (e.g. test env) — scan all venues and match by slug
        all_venues = get_all_venues(app_state.db_path)
        venues = [v for v in all_venues if _slugify(v.get("city", "")) == city_slug]
        if venues:
            city_name = venues[0].get("city", city_slug)
    venue = next((v for v in venues if _slugify(v.get("name", "")) == venue_slug), None)
    if not venue or not city_name:
        return RedirectResponse("/helyszinek", status_code=302)
    if (redirect := _hu_redirect(request, city_name)):
        return redirect
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
        "related": related_communities(_city_records, exclude_key="", topic=None,
                                       locale="hu"),
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
    if city_name:
        all_persons = get_persons(app_state.db_path, city_name)
    else:
        # Cities not yet loaded (e.g. test env) — scan all persons and match by city slug
        all_persons_all = get_all_persons(app_state.db_path)
        all_persons = [p for p in all_persons_all if _slugify(p.get("city", "")) == city_slug]
        if all_persons:
            city_name = all_persons[0].get("city", city_slug)
    if not city_name:
        return RedirectResponse("/emberek", status_code=302)
    if (redirect := _hu_redirect(request, city_name)):
        return redirect
    merged = [p for p in all_persons if _slugify(p.get("name", "")) == name_slug]
    if not merged:
        return RedirectResponse("/emberek", status_code=302)
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
        "related": related_communities(_city_records, exclude_key="", topic=None,
                                       locale="hu"),
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
    record = _find_community_by_slug(city_name, segment)
    if record:
        _page_lang = lang_context(request)
        schema_json = records_to_jsonld(
            [record],
            f"{_canonical_base(request, city_name)}/{city_slug}"
            f"/{_slugify(record.get('name', ''))}")
        history = get_community_history(app_state.db_path, record.get("community_id", ""))
        rec_topic = record.get("topic", "")
        city_locale = _city_locale(city_name)
        public_topic = rec_topic if rec_topic in topic_names else None
        topic_url_slugs = {t.name: _topic_url_slug(t.name, city_locale) for t in (app_state.topics or [])}
        community_venue = get_venue_for_community(
            app_state.db_path, record.get("community_id", ""), city_name
        ) if app_state.db_path else None
        community_persons = get_persons_for_community(
            app_state.db_path, record["name"], city_name
        ) if app_state.db_path else []
        # One query for the whole city, sliced two ways. Off the loop because a
        # large city is a real read and the loop also serves everyone else.
        _city_records = await asyncio.to_thread(
            get_communities_for_city, app_state.db_path, city_name)
        related = related_communities(
            _city_records,
            exclude_key=_community_record_key(record["name"], city_name, rec_topic),
            topic=public_topic, locale=city_locale)
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
