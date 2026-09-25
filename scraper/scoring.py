"""Measure each routed model on our own extraction task.

The `quality:` numbers in config/providers.yaml start as a prior read off public
leaderboards, and 2026-08-16 showed how weak that prior is: every shipped model
name was stale within hours, and a leaderboard rank says nothing about JSON
extraction on Hungarian club pages. LLMStructBench (arXiv:2602.14743) found the
prompting strategy outweighs model size for exactly this task.

So score the fleet on pages whose answer we already hold.

**What this measures, precisely**: agreement with the *incumbent* extraction, not
ground truth. The "expected" names come from whichever model processed the page
before. That is the right yardstick for "can this free model replace DeepSeek
here" and the wrong one for "is this model correct" — a model that finds a real
club the incumbent missed is scored down for it.

Shared by `scripts/score_providers.py` (CLI) and `POST /v1/score` (remote), so
the two cannot drift.
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from pathlib import Path

import structlog

from .db import _connect
from .identity import normalized_match_key

log = structlog.get_logger()

#: Per page: 20 for answering at all, 50 × recall, 30 × precision. Answering is
#: worth little on its own — a model that reliably returns `{}` is parseable and
#: useless, and weighting responsiveness highly would rank it near one that
#: actually reads the page.
_ANSWER_POINTS = 20.0
_RECALL_POINTS = 50.0
_PRECISION_POINTS = 30.0


def _key(name: str) -> str:
    """Exact-identity key — the same collapse the database uses."""
    return normalized_match_key(name)


def _fold(name: str) -> str:
    """Accent-folded, lowercase, punctuation-free form."""
    return "".join(
        c for c in unicodedata.normalize("NFD", name.lower())
        if unicodedata.category(c) != "Mn"
    )


def _dedupe_key(name: str) -> str:
    """Key for collapsing spellings of one name within a single list.

    Not `normalized_match_key`: that keeps accents (it is the database identity
    key, where "Futóklub" and "Futoklub" are legitimately different records).
    For scoring they are one club written two ways, and counting both inflates a
    model's own precision denominator.
    """
    return re.sub(r"[^a-z0-9]+", "", _fold(name))


def _tokens(name: str) -> set[str]:
    """Word tokens for fuzzy comparison.

    Deliberately not `normalized_match_key`, which strips spaces entirely
    ("Szentendrei Futóklub" -> "szentendreifutóklub") and therefore makes token
    overlap impossible. Filler words that appear in half the club names in the
    corpus carry no identifying information and are dropped.
    """
    folded = _fold(name)
    # Tokens of one or two characters are club-type markers, not names — SV, IF,
    # FC, SE. Dropping them here means "SV Musterstadt" and "Sportverein
    # Musterstadt" both reduce to the town, which is the point.
    # Genericness filtering is the caller's job: it depends on the corpus.
    return {t for t in re.split(r"[^a-z0-9]+", folded) if len(t) > 2}


#: A token is "generic" when it appears in at least this share of the corpus's
#: names. Measured, not listed: a hand-written stopword list cannot keep up with
#: three languages, and length is not a proxy for genericness — Hungarian
#: generics are short ("klub", "SE") while German ones are long compounds
#: ("Schachverein", "Idrottssällskap"). Both must be caught by the same rule.
_GENERIC_DF = 0.10

#: Endings that make a word a club-type rather than a name. Compound languages
#: build these productively — Schachverein, Schwimmverein, Turnverein,
#: Simklubb, Idrottsförening — so no finite list of whole words keeps up, but
#: the suffix does. "Futóklub" also matches, which is correct: it is the club
#: type, and "Szentendrei" is what identifies the club.
_GENERIC_SUFFIXES = (
    "verein", "vereinigung", "gemeinschaft",
    "klubb", "forening", "sallskap", "idrottsforening",
    "egyesulet", "klub", "club",
)

#: Club-type abbreviations. Too short to survive tokenisation, but their
#: presence is what separates a club name from a bare place name: "SV
#: Musterstadt" is a club, "Szentendrei" is an adjective.
_CLUB_ABBREV = frozenset({
    "sv", "se", "sc", "if", "fc", "tv", "ac", "bc", "vfb", "fsv", "tsv",
    "mtk", "dvtk", "gik", "ik", "bk", "ff",
})

#: Fallback for the tiny-corpus case, where document frequency is meaningless.
_SEED_GENERIC = frozenset({
    "egyesulet", "klub", "kor", "csoport", "sport", "sportegyesulet", "se", "sc",
    "verein", "sportverein", "gruppe", "ev", "forening", "klubb", "club",
    "society", "association", "group",
})


def _generic_tokens(names: list[str], threshold: float = _GENERIC_DF) -> frozenset[str]:
    """Tokens too widespread in this corpus to identify a club.

    Derived from the names themselves, so it adapts to whichever market the
    sample is drawn from without anyone maintaining a list per language.
    """
    from collections import Counter

    if len(names) < 20:
        return frozenset(_SEED_GENERIC)
    df: Counter = Counter()
    for n in names:
        df.update(_tokens(n))
    cutoff = max(2, int(len(names) * threshold))
    return frozenset(_SEED_GENERIC | {t for t, c in df.items() if c >= cutoff})


def _matches(want: str, got: list[str], generic: frozenset[str] = frozenset(),
             places: frozenset[str] = frozenset()) -> bool:
    """Did the model return this name, under any reasonable reading?

    Lenient about *phrasing*, strict about *identity*. MINEA (arXiv:2404.04068)
    measured the same extractions at 59.4% with exact matching and 88.4% once
    containment was allowed, so strictness understates every model — but a loose
    rule lets one answer sweep a page, and `--apply` writes the result straight
    into the routing order.

    The rule compares **full** token sets and asks what the *difference*
    contains:

        equal sets                        -> same club
        one is a subset, and everything
          the larger side adds is generic -> same club, spelled out
        anything else                     -> different clubs

    Removing generic tokens *before* comparing was the earlier mistake: it
    collapsed "Szentendrei Futóklub" and "Szentendrei Kajak Klub" both to
    {szentendrei}, so every club in a town matched every other — and golden
    pages are single city×topic pages, where every name shares a town. The club
    type is exactly what distinguishes them. It is only ignorable when it is the
    sole difference, which is what looking at the difference (rather than
    deleting it up front) expresses.
    """
    generic = generic or _SEED_GENERIC

    def is_generic(token: str) -> bool:
        # A place name is not a club name, and Hungarian inflects it
        # ("Szentendre" -> "Szentendrei"), so compare by stem.
        return (token in generic
                or token.endswith(_GENERIC_SUFFIXES)
                or any(token.startswith(p) for p in places if len(p) > 3))

    def has_club_marker(name: str) -> bool:
        """Does the raw name announce itself as a club?

        The deciding signal between two structurally identical cases:
        {musterstadt} from "SV Musterstadt" is a club, {szentendrei} from a bare
        "Szentendrei" is not — and their token sets are indistinguishable
        because the marker is too short to survive tokenisation.
        """
        toks = [t for t in re.split(r"[^a-z0-9]+", _fold(name)) if t]
        markers = [t for t in toks
                   if t in _CLUB_ABBREV or t.endswith(_GENERIC_SUFFIXES)]
        # A marker only helps when something else stands beside it. "SV
        # Musterstadt" names a club; bare "Futóklub" or "Schachverein" is the
        # marker and nothing more, and matches every club of that type.
        return bool(markers) and len(markers) < len(toks)

    def carries_identity(tokens: set[str], name: str) -> bool:
        """Is this name specific enough to stand for a club on its own?"""
        return (len(tokens) >= 2
                or any(not is_generic(t) for t in tokens)
                or has_club_marker(name))

    wk = _key(want)
    if wk in [_key(g) for g in got]:
        return True

    wt = _tokens(want)
    if not wt:
        return False
    for g in got:
        gt = _tokens(g)
        if not gt:
            continue
        if wt == gt:
            return True
        if wt < gt:
            smaller, smaller_name, larger = wt, want, gt
        elif gt < wt:
            smaller, smaller_name, larger = gt, g, wt
        else:
            continue  # each side has something the other lacks: different clubs
        if all(is_generic(t) for t in larger - smaller) and \
                carries_identity(smaller, smaller_name):
            return True
    return False


def _pair_up(left: list[str], right: list[str],
             generic: frozenset[str] = frozenset(),
             places: frozenset[str] = frozenset()) -> int:
    """Maximum one-to-one matching between two name lists.

    Greedy pairing consumes a candidate a later item needed, so the same answer
    set scored differently depending on the order the model happened to list
    clubs in — which reintroduces exactly the run-to-run variance the
    deterministic golden set removed. Augmenting paths (Kuhn's algorithm) give
    the true maximum; n is a handful of names per page.
    """
    adj = [[j for j, cand in enumerate(right) if _matches(item, [cand], generic, places)]
           for item in left]
    match_r: dict[int, int] = {}

    def _try(i: int, seen: set[int]) -> bool:
        for j in adj[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in match_r or _try(match_r[j], seen):
                match_r[j] = i
                return True
        return False

    return sum(1 for i in range(len(left)) if _try(i, set()))


def score_page(expected, got, generic: frozenset[str] = frozenset(),
               places: frozenset[str] = frozenset()) -> float:
    """Score one page. Both arguments are raw names, not identity keys —
    matching needs the words, and the key form has no spaces.

    Both sides are deduplicated by identity key: a cached extraction can hold
    the same club twice, and with one-to-one pairing that caps recall below 1.0
    for a model returning the correct *distinct* set — scoring it below one that
    repeats itself. `got` is deduplicated the same way, or a model returning two
    spellings of one club inflates its own precision denominator.
    """
    def _dedupe(names):
        seen: dict[str, str] = {}
        for n in names:
            seen.setdefault(_dedupe_key(n), n)
        return list(seen.values())

    expected, got = _dedupe(expected), _dedupe(got)
    if not expected:
        return 0.0
    # _matches is symmetric and maximum bipartite matching is transpose-
    # invariant, so one matching serves both ratios.
    pairs = _pair_up(expected, got, generic, places)
    recall = pairs / len(expected)
    precision = (pairs / len(got)) if got else 0.0
    return (_ANSWER_POINTS + _RECALL_POINTS * recall
            + _PRECISION_POINTS * min(1.0, precision))


def corpus_names(db_path: Path, limit: int = 40_000) -> tuple[list[str], set[str]]:
    """(community names, city-name stems) for deciding which tokens are generic.

    The full corpus, not the golden set: genericness is a property of the domain
    ("sakk" appears in every chess club's name) and a dozen sample pages cannot
    show that. Ordered by record_key, because `replace_communities_for_topic`
    deletes and reinserts rows on every run — an unordered LIMIT would sample a
    different window each time and quietly change the scores.

    City stems are returned separately: a place name is not a club name, but
    Hungarian inflects it ("Szentendre" -> "Szentendrei"), so matching needs the
    stem rather than the exact token.
    """
    if not Path(db_path).exists():
        return [], set()
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT json_extract(data, '$.name'), city FROM communities"
            " WHERE hidden=0 ORDER BY record_key LIMIT ?", (limit,)).fetchall()
    names = [r[0] for r in rows if r[0]]
    stems = {t for r in rows if r[1] for t in _tokens(r[1])}
    return names, stems


def golden_set(db_path: Path, limit: int = 12,
               locale: str | None = None) -> list[dict]:
    """Cached pages plus the community names we believe they contain.

    Only pages that yielded at least one community are used: a page with zero
    expected results cannot separate a careful model from one that always
    answers "nothing here".

    The sample is **deterministic** (ordered by url_hash). A sample that moves
    between runs makes scores incomparable, and silently so — the numbers still
    look like numbers.

    `locale` restricts it to one market, and measuring without it is how a
    ranking ends up describing the wrong workload. The corpus is roughly 30%
    Hungarian and 70% international, so an unfiltered sample is mostly English
    pages — while Hungarian is the primary market. A sibling project found the
    same trap the expensive way: a model that scored better on its synthetic
    English prompt dropped into English mid-answer on the real Hungarian task
    and lost half the required fields.
    """
    if not Path(db_path).exists():
        raise FileNotFoundError(f"no database at {db_path}")
    with _connect(db_path) as conn:
        # ORDER BY url_hash, NOT extracted_at: the pipeline rewrites
        # extracted_at continuously, so a "most recently extracted" sample is a
        # different set of pages on every run. Two measurements an hour apart
        # then differ for reasons that have nothing to do with the models —
        # which is exactly what happened on 2026-08-16, where mistral-small
        # appeared to fall 80 -> 55 between runs. url_hash is stable, so the
        # sample only drifts as the corpus itself grows.
        # Filtered in SQL and read until `limit` qualify. It used to take the
        # first `limit * 8` rows and filter afterwards; with ~44% of pages
        # yielding no community and ~70% not Hungarian, 16 requested pages
        # became 7 (2026-09-25) — too few to separate models a few points apart.
        rows = conn.execute(
            """
            SELECT url, city, topic, data
              FROM cache_pages
             WHERE extracted_at IS NOT NULL AND (records_count > 0 OR records_count IS NULL)
             ORDER BY url_hash
            """,
        )
        rows = _collect(rows, limit, locale)
    return rows


def _collect(rows, limit: int, locale: str | None) -> list[dict]:
    out: list[dict] = []
    for url, city, topic, blob in rows:
        try:
            entry = json.loads(blob)
        except (TypeError, ValueError):
            continue
        text = entry.get("raw_text")
        records = entry.get("records") or []
        if locale:
            # The locale lives on the extracted records, not on cache_pages —
            # its city/topic columns are overwritten last-write-wins and cannot
            # be trusted for a join (see get_fully_processed_pairs).
            if not any((r.get("locale") or "") == locale for r in records):
                continue
        expected = [r["name"] for r in records if r.get("name")]
        if not text or not expected:
            continue
        out.append({"url": url, "city": city or "", "topic": topic or "",
                    "text": text, "expected": expected})
        if len(out) >= limit:
            break
    return out


def _charge_ledger(router, extractor, exc: Exception | None = None) -> None:
    """Attribute one scoring call to the provider's daily budget.

    Scoring calls a provider exactly as extraction does, and the provider
    charges them the same way — but `score_model` drives an `_ApiExtractor`
    directly rather than through `FallbackExtractor`, so until 2026-09-19
    nothing recorded them. A 20-page run over three models is 60 real requests
    the router never learned about, and it then planned the day against an
    allowance that much too large. That is the same under-counting the 2026-09-12
    enrichment fix removed, in the one place still doing it.

    Never raises: a ledger problem must not take down a measurement, which is
    the same rule `FallbackExtractor._note_router` follows.
    """
    if router is None:
        return
    kwargs: dict = {"reserved": True}
    if exc is None:
        kwargs |= {"ok": True, "tokens": int(getattr(extractor, "last_tokens", 0) or 0)}
    else:
        wait = getattr(exc, "wait_seconds", None)
        kwargs |= {"ok": False, "error": f"scoring: {type(exc).__name__}"}
        if wait is not None:
            kwargs |= {"rate_limited": True, "retry_after": wait}
    try:
        router.note(extractor, **kwargs)
    except Exception as note_exc:      # noqa: BLE001 - never fail a scoring run
        log.warning("scoring_ledger_write_failed", error=str(note_exc))


async def score_model(extractor, pages: list[dict],
                      generic: frozenset[str] = frozenset(),
                      places: frozenset[str] = frozenset(),
                      router=None) -> dict:
    """Run one model over the golden set.

    The score is the mean over pages the model *answered*, with `coverage`
    reporting how many that was. Averaging over all pages instead would fold
    reliability into quality, and at free-tier rate limits that mostly measures
    how recently the fleet ran, not how well the model reads a page.
    """
    total, answered, failed, errors = 0.0, 0, 0, []
    for page in pages:
        # Claimed before the call, released by `_charge_ledger` after it — the
        # same order `FallbackExtractor` uses, so concurrent work sees the slot
        # taken while the request is in flight rather than after it returns.
        if router is not None:
            try:
                router.reserve(extractor)
            except Exception as exc:   # noqa: BLE001 - see _charge_ledger
                log.warning("scoring_ledger_reserve_failed", error=str(exc))
        try:
            records = await extractor.extract(
                text=page["text"], city=page["city"], topic=page["topic"],
                locale="hu", source_url=page["url"],
            )
        except Exception as exc:
            _charge_ledger(router, extractor, exc)
            failed += 1
            if len(errors) < 3:
                errors.append(f"{type(exc).__name__}: {str(exc)[:120]}")
            continue
        _charge_ledger(router, extractor)
        got = [r.name for r in records if r.name]
        total += score_page(page["expected"], got, generic, places)
        answered += 1
    # A model that never answered is UNMEASURED, not bad. Reporting 0 conflates
    # "rate limited for 20 minutes" with "produced garbage", and writing that 0
    # into providers.yaml would drop a good model to the bottom of the routing
    # order — seen on the very first live run, where two of three Groq models
    # scored 0 purely because of a rate limit and an HTTP 400.
    measured = answered > 0
    return {
        "provider": getattr(extractor, "provider", "?"),
        "model": getattr(extractor, "model", "?"),
        "prior": getattr(extractor, "quality", 0),
        "score": round(total / answered) if measured else None,
        "coverage": round(answered / len(pages), 2) if pages else 0.0,
        "answered": answered,
        "failed": failed,
        "measured": measured,
        "errors": errors,
    }


async def score_fleet(db_path: Path, extractors: list, pages: int = 8,
                      locale: str | None = None,
                      golden: "list[dict] | None" = None,
                      router=None) -> dict:
    """Score every extractor over one shared golden set.

    Pass `locale` to measure a single market. A fleet ranked without it is
    ranked on whatever the corpus happens to contain most of, which is not the
    same question as "which model should serve our primary market".
    """
    # Off the event loop. Both of these scan large tables, and the loop also
    # serves the public site: on 2026-08-20 a scoring request held it long
    # enough for the container's liveness probe to fail, Traefik dropped the
    # route and every visitor got a 404 — the same chain as the pipeline's
    # writes, in a place I had not looked.
    # `golden` lets a caller settle "is there anything to measure?" before
    # committing to minutes of LLM calls — the API answers 422 on an empty
    # sample rather than starting a background job that has nothing to do.
    gs = golden if golden is not None else await asyncio.to_thread(
        golden_set, db_path, pages, locale)
    if not gs:
        return {"error": "no usable golden pages (need cached pages with records)"
                         + (f" for locale {locale!r}" if locale else ""),
                "pages": 0, "locale": locale, "results": []}
    # Genericness is measured from the WHOLE corpus, not the sample. A golden
    # set is a dozen pages and a few dozen names — far too small for document
    # frequency to mean anything, and the topic word ("sakk", "futás") would
    # never look common enough to discount. The communities table has tens of
    # thousands of names and answers the question properly.
    names, places = await asyncio.to_thread(corpus_names, db_path)
    generic = _generic_tokens(names)
    places = frozenset(places)
    log.info("scoring_start", pages=len(gs), models=len(extractors),
             generic_tokens=len(generic))
    results = []
    for ex in extractors:
        r = await score_model(ex, gs, generic, places, router=router)
        log.info("scoring_model_done", **{k: r[k] for k in
                                          ("provider", "model", "score", "answered", "failed")})
        results.append(r)
    # Measured first (best score first), then the unmeasured — which are last
    # because they are unknown, not because they are bad.
    results.sort(key=lambda r: (0 if r["measured"] else 1, -(r["score"] or 0)))
    unmeasured = [f"{r['provider']}:{r['model']}" for r in results if not r["measured"]]
    import hashlib
    # Identifies the sample. Two runs with different fingerprints measured
    # different pages and their scores are not comparable.
    # Covers the generic set as well as the pages: two runs that scored the
    # same pages with different genericness are not comparable either.
    sample_fp = hashlib.sha256(
        ("|".join(sorted(p["url"] for p in gs))
         + "#" + str(len(generic)) + "," + str(len(places))
         + "@" + (locale or "mixed")).encode()).hexdigest()[:12]
    return {
        "pages": len(gs),
        # Part of the sample's identity: a score measured on Hungarian pages
        # and one measured on English pages answer different questions, and
        # comparing them is the mistake this parameter exists to prevent.
        "locale": locale or "mixed",
        "sample": sample_fp,
        # Deduplicated: a cached extraction can hold the same name twice, and
        # counting it twice overstates how much the sample actually covers.
        "expected_communities": sum(len({_key(n) for n in p["expected"]}) for p in gs),
        "results": results,
        "unmeasured": unmeasured,
        "note": ("Scores measure agreement with the incumbent extraction, not "
                 "ground truth: expected names come from whichever model "
                 "processed each page before. score=null means the model could "
                 "not be measured (rate limit, error) — NOT that it scored "
                 "zero; never write a null into providers.yaml."),
    }
