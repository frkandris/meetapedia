"""Duplicate detection for communities, venues, and persons."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

import structlog

from .db import (
    _community_record_key,
    _venue_record_key,
    _person_record_key,
    get_all_communities,
    get_all_venues,
    get_all_persons,
    get_community_by_record_key,
    get_duplicate_candidates,
    insert_duplicate_candidate,
    resolve_duplicate_candidate,
)

log = structlog.get_logger()


def _strip_articles(name: str) -> str:
    return re.sub(r"^(a |az |the |die |le |la |el |los |las )", "", name.lower().strip())


def _strip_city(name: str, city: str) -> str:
    """Remove city name (and Hungarian adjective +i form) before similarity comparison.
    Prevents 'Budapest Futók' vs 'Budapest Focisták' inflating similarity via shared prefix."""
    city_l = city.lower()
    # Match exact city OR adjective form (e.g. Pécs → Pécsi, Budapest → Budapesti)
    result = re.sub(r"\b" + re.escape(city_l) + r"i?\b", "", name.lower()).strip(" –-/")
    return result if len(result) > 2 else name.lower()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _strip_articles(a), _strip_articles(b)).ratio()


def _name_similarity(a: str, b: str, city: str) -> float:
    """Similarity after stripping city; also catches substring containment (e.g. 'Futók' ⊂ 'Futók Kör').
    Returns 0.0 if the shorter stripped name is too short or a single generic word."""
    na = _strip_articles(_strip_city(a, city))
    nb = _strip_articles(_strip_city(b, city))
    shorter = na if len(na) <= len(nb) else nb
    # Skip: single generic word or too short to be meaningful
    if len(shorter) < 5 or shorter.strip() in _GENERIC_WORDS:
        return 0.0
    if len(na) > 4 and len(nb) > 4 and (na in nb or nb in na):
        return 0.90
    return SequenceMatcher(None, na, nb).ratio()


def _richness(d: dict) -> int:
    return sum(1 for f in ["description", "meeting_schedule", "location", "contact", "website"]
               if d.get(f)) + len(d.get("social_links") or [])


# Generic community words that alone don't constitute a meaningful duplicate signal
_GENERIC_WORDS: frozenset[str] = frozenset({
    "csoport", "klub", "kör", "egyesület", "közösség", "közösségi", "közössége", "társaság", "társulat",
    "csapat", "team", "szakkör", "műhely", "szervezet", "alapítvány",
    "group", "club", "community", "circle", "association", "society",
    # single-word suffixes that cause false substring matches
    "színpad", "táncstúdió", "stúdió", "studio",
    # multi-word generic phrases (matched as whole shorter name)
    "idősek klubja", "nyugdíjasklub", "ifjúsági kör",
})


_INVALID_URLS: frozenset[str] = frozenset({
    "n/a", "http://n/a", "https://n/a",
    "empty", "http://empty", "https://empty",
    "none", "null", "#", "/", "",
})


def _norm_url(url: str | None) -> str:
    u = (url or "").rstrip("/").lower().strip()
    return "" if u in _INVALID_URLS else u


def detect_community_candidates(db_path: Path, city: str | None = None) -> int:
    """
    Scan communities for duplicates within the same city across different topics.
    Returns the number of new candidates inserted.
    """
    # `city` goes to SQL, not to a list comprehension: the rows carry whole
    # JSON blobs, and this runs once per processed pair (store.save_results).
    all_records = get_all_communities(db_path, city=city or None)

    # Group by city
    by_city: dict[str, list[dict]] = {}
    for r in all_records:
        by_city.setdefault(r["city"], []).append(r)

    inserted = 0
    for city_name, records in by_city.items():
        for i, a in enumerate(records):
            for b in records[i + 1:]:
                # Same topic → already deduplicated by store.py
                if a.get("topic") == b.get("topic"):
                    continue

                signal: str | None = None
                similarity = 0.0

                # URL match → definite duplicate
                url_a = _norm_url(a.get("website"))
                url_b = _norm_url(b.get("website"))
                if url_a and url_b and url_a == url_b:
                    signal = "url_match"
                    similarity = 1.0

                if signal is None:
                    similarity = _name_similarity(a["name"], b["name"], city_name)
                    if similarity >= 0.85:
                        signal = "fuzzy_name"

                if signal is None:
                    continue

                # Winner = richer record. Never swap the loop variables (a
                # mutated `a` used to leak into later inner-loop comparisons)
                # and never reorder the keys — winner_key is what the merge
                # keeps, so lexicographic sorting silently kept the poorer
                # record. Pair idempotency is handled inside
                # insert_duplicate_candidate (both key orders checked).
                winner, loser = (a, b) if _richness(a) >= _richness(b) else (b, a)
                winner_key = _community_record_key(winner["name"], winner["city"], winner["topic"])
                loser_key = _community_record_key(loser["name"], loser["city"], loser["topic"])

                if insert_duplicate_candidate(
                    db_path, "community",
                    winner.get("community_id", ""), loser.get("community_id", ""),
                    winner_key, loser_key,
                    round(similarity, 4), signal,
                ):
                    inserted += 1
                    log.info("duplicate_candidate_found", entity="community",
                             winner=winner["name"], loser=loser["name"], city=city_name,
                             signal=signal, similarity=round(similarity, 3))

    return inserted


def detect_venue_candidates(db_path: Path) -> int:
    """Detect duplicate venues within the same city."""
    all_venues = get_all_venues(db_path)

    by_city: dict[str, list[dict]] = {}
    for v in all_venues:
        by_city.setdefault(v["city"], []).append(v)

    inserted = 0
    for city_name, venues in by_city.items():
        for i, a in enumerate(venues):
            for b in venues[i + 1:]:
                signal: str | None = None
                similarity = 0.0

                url_a = _norm_url(a.get("website"))
                url_b = _norm_url(b.get("website"))
                if url_a and url_b and url_a == url_b:
                    signal = "url_match"
                    similarity = 1.0

                if signal is None:
                    similarity = _similarity(a["name"], b["name"])
                    if similarity >= 0.85:
                        signal = "fuzzy_name"

                if signal is None:
                    continue

                winner, loser = (a, b) if _richness(a) >= _richness(b) else (b, a)
                winner_key = _venue_record_key(winner["name"], winner["city"])
                loser_key = _venue_record_key(loser["name"], loser["city"])

                if insert_duplicate_candidate(
                    db_path, "venue",
                    winner.get("venue_id", ""), loser.get("venue_id", ""),
                    winner_key, loser_key,
                    round(similarity, 4), signal,
                ):
                    inserted += 1

    return inserted


def detect_person_candidates(db_path: Path) -> int:
    """Detect duplicate persons within the same city."""
    all_persons = get_all_persons(db_path)

    by_city: dict[str, list[dict]] = {}
    for p in all_persons:
        by_city.setdefault(p["city"], []).append(p)

    inserted = 0
    for city_name, persons in by_city.items():
        for i, a in enumerate(persons):
            for b in persons[i + 1:]:
                similarity = _similarity(a["name"], b["name"])
                if similarity < 0.90:
                    continue
                # Same person leading different communities is not a duplicate person record
                if _similarity(a.get("community_name", ""), b.get("community_name", "")) < 0.70:
                    continue

                winner, loser = (a, b) if _richness(a) >= _richness(b) else (b, a)
                winner_key = _person_record_key(winner["name"], winner["city"], winner.get("role", ""), winner.get("community_name", ""))
                loser_key = _person_record_key(loser["name"], loser["city"], loser.get("role", ""), loser.get("community_name", ""))

                if insert_duplicate_candidate(
                    db_path, "person",
                    winner.get("person_id", ""), loser.get("person_id", ""),
                    winner_key, loser_key,
                    round(similarity, 4), "fuzzy_name",
                ):
                    inserted += 1

    return inserted


def cleanup_stale_community_candidates(db_path: Path) -> int:
    """Auto-dismiss pending community candidates that no longer meet current detection criteria.
    Returns number of candidates dismissed."""
    dismissed = 0
    for c in get_duplicate_candidates(db_path, entity_type="community", resolved=False):
        winner = get_community_by_record_key(db_path, c["winner_key"])
        loser = get_community_by_record_key(db_path, c["loser_key"])
        if not winner or not loser:
            resolve_duplicate_candidate(db_path, c["id"], "auto_dismissed")
            dismissed += 1
            continue
        if c["signal"] == "manual":
            # An admin asserted this pair (or confirmed an auto candidate) —
            # automatic criteria drift must not dismiss their judgement.
            continue
        city = winner.get("city", "")
        still_valid = False
        if c["signal"] == "url_match":
            still_valid = (_norm_url(winner.get("website")) == _norm_url(loser.get("website"))
                           and _norm_url(winner.get("website")))
        else:
            still_valid = _name_similarity(winner["name"], loser["name"], city) >= 0.85
        if not still_valid:
            resolve_duplicate_candidate(db_path, c["id"], "auto_dismissed")
            dismissed += 1
            log.info("stale_candidate_dismissed", winner=winner["name"], loser=loser["name"])
    return dismissed


def detect_all(db_path: Path, communities: bool = True) -> int:
    """Run detection for all entity types. Returns total candidates inserted.

    `communities=False` skips the community pass — the expensive one (1.5M name
    pairs through SequenceMatcher at production scale), and redundant after a
    pipeline run because `save_results` already scans each city it saves.
    """
    cleanup_stale_community_candidates(db_path)
    c = detect_community_candidates(db_path) if communities else 0
    v = detect_venue_candidates(db_path)
    p = detect_person_candidates(db_path)
    total = c + v + p
    log.info("duplicate_scan_complete", communities=c, venues=v, persons=p)
    return total
