import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import structlog

from .db import (
    bump_extract_failure,
    clear_extract_failure,
    clear_person_cache,
    delete_cache_page,
    get_cache_index,
    get_extract_failure_counts,
    get_scraped_cache_for_search_pair,
    load_cache_page,
    update_cache_page,
)
from .models import CommunityRecord

log = structlog.get_logger()


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc
    except Exception:
        return ""


class CacheManager:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    # ── Scrape ──────────────────────────────────────────────────────────────

    def get_scraped(self, url: str) -> str | None:
        entry = load_cache_page(self.db_path, _url_hash(url))
        return entry.get("raw_text") if entry else None

    def save_scraped(self, url: str, text: str, city: str, topic: str,
                     duration_s: float | None = None,
                     source_queries: list[str] | None = None) -> None:
        h = _url_hash(url)
        updates = {
            "url": url,
            "url_hash": h,
            "domain": _domain(url),
            "city": city,
            "topic": topic,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "raw_text": text,
        }
        if duration_s is not None:
            updates["scrape_duration_s"] = round(duration_s, 2)
        if source_queries is not None:
            updates["source_queries"] = source_queries
        update_cache_page(self.db_path, h, updates, create={})
        log.debug("cache_saved_scrape", url=url)

    # ── Extract ─────────────────────────────────────────────────────────────

    def get_extracted(self, url: str,
                      fingerprint: str | None = None) -> list[CommunityRecord] | None:
        entry = load_cache_page(self.db_path, _url_hash(url))
        if not entry or not entry.get("extracted_at") or entry.get("records") is None:
            return None
        if fingerprint and entry.get("extract_fingerprint") != fingerprint:
            log.debug("cache_fingerprint_mismatch", url=url,
                      stored=entry.get("extract_fingerprint"), current=fingerprint)
            return None
        try:
            return [CommunityRecord.model_validate(r) for r in entry["records"]]
        except Exception:
            return None

    def save_extracted(self, url: str, records: list[CommunityRecord],
                       duration_s: float | None = None,
                       fingerprint: str | None = None,
                       model: str | None = None,
                       quality: int | None = None) -> None:
        """`quality` is the router's 0-100 score for the model that produced
        this result. It records *how good* the cached extraction is so a later
        run can decide whether a better free model is worth re-spending a
        request on. Deliberately not part of the fingerprint — the cache key
        must stay identical across models or routing would invalidate
        everything."""
        h = _url_hash(url)
        updates = {
            "extracted_at": datetime.now(timezone.utc).isoformat(),
            "extract_fingerprint": fingerprint,
            "extract_model": model,
            "extract_quality": quality,
            "records": [r.model_dump() for r in records],
            "enrich_scraped_at": None,
            "enrich_scrape_duration_s": None,
            "enrich_extracted_at": None,
            "enrich_extract_duration_s": None,
            "enrich_count": None,
            "enrich_log": None,
        }
        if duration_s is not None:
            updates["extract_duration_s"] = round(duration_s, 2)
        update_cache_page(self.db_path, h, updates,
                          create={"url": url, "domain": _domain(url)})
        log.debug("cache_saved_extract", url=url, records=len(records),
                  fingerprint=fingerprint, model=model)

    def save_enriched_records(self, url: str, records: list[CommunityRecord]) -> None:
        update_cache_page(self.db_path, _url_hash(url),
                          {"records": [r.model_dump() for r in records]})

    # ── Venue extraction cache ───────────────────────────────────────────────

    def get_venue_extracted(self, url: str, fingerprint: str | None = None) -> list[dict] | None:
        entry = load_cache_page(self.db_path, _url_hash(url))
        if not entry or not entry.get("venue_extracted_at"):
            return None
        if fingerprint and entry.get("venue_fingerprint") != fingerprint:
            return None
        return entry.get("venues_data") or []

    def save_venue_extracted(self, url: str, venues: list[dict],
                              fingerprint: str | None = None, model: str | None = None) -> None:
        update_cache_page(self.db_path, _url_hash(url), {
            "venue_extracted_at": datetime.now(timezone.utc).isoformat(),
            "venue_fingerprint": fingerprint,
            "venue_model": model,
            "venues_data": venues,
        }, create={"url": url, "domain": _domain(url)})

    # ── Person extraction cache ──────────────────────────────────────────────

    def get_person_extracted(self, url: str, city: str, topic: str,
                              fingerprint: str | None = None) -> list[dict] | None:
        entry = load_cache_page(self.db_path, _url_hash(url))
        if not entry or not entry.get("person_extracted_at"):
            return None
        if fingerprint and entry.get("person_fingerprint") != fingerprint:
            return None
        persons_data = entry.get("persons_data") or {}
        key = f"{city}/{topic}"
        if key not in persons_data:
            return None
        return persons_data[key]

    def save_person_extracted(self, url: str, city: str, topic: str, persons: list[dict],
                               fingerprint: str | None = None, model: str | None = None) -> None:
        def _merge(entry: dict) -> dict:
            persons_data = entry.get("persons_data") or {}
            persons_data[f"{city}/{topic}"] = persons
            entry["persons_data"] = persons_data
            return entry

        update_cache_page(self.db_path, _url_hash(url), {
            "person_extracted_at": datetime.now(timezone.utc).isoformat(),
            "person_fingerprint": fingerprint,
            "person_model": model,
        }, create={"url": url, "domain": _domain(url)}, mutate=_merge)

    # ── Enrich timing markers ────────────────────────────────────────────────

    def mark_enrich_scraped(self, url: str, duration_s: float) -> None:
        update_cache_page(self.db_path, _url_hash(url), {
            "enrich_scraped_at": datetime.now(timezone.utc).isoformat(),
            "enrich_scrape_duration_s": round(duration_s, 2),
        })

    def save_enrich_log(self, url: str, enrich_log: list[dict]) -> None:
        update_cache_page(self.db_path, _url_hash(url), {"enrich_log": enrich_log})

    def mark_enrich_extracted(self, url: str, count: int, duration_s: float,
                              model: str | None = None) -> None:
        updates = {
            "enrich_extracted_at": datetime.now(timezone.utc).isoformat(),
            "enrich_extract_duration_s": round(duration_s, 2),
            "enrich_count": count,
        }
        if model is not None:
            updates["enrich_model"] = model
        update_cache_page(self.db_path, _url_hash(url), updates)

    # ── Bulk read ────────────────────────────────────────────────────────────

    def get_scraped_for_pair(self, city: str, topic: str) -> list[tuple[str, str]]:
        """Return one pair's scraped pages without loading the global raw cache."""
        return get_scraped_cache_for_search_pair(self.db_path, city, topic)

    def get_index(self) -> list[dict]:
        return get_cache_index(self.db_path)

    # ── Delete ───────────────────────────────────────────────────────────────

    def delete_scraped(self, url_hash: str) -> bool:
        return update_cache_page(
            self.db_path, url_hash,
            drop=["raw_text", "scraped_at", "scrape_duration_s"]) is not None

    def delete_extracted(self, url_hash: str) -> bool:
        return update_cache_page(
            self.db_path, url_hash,
            drop=["records", "extracted_at", "extract_duration_s",
                  "extract_fingerprint", "extract_model",
                  "enrich_scraped_at", "enrich_scrape_duration_s",
                  "enrich_extracted_at", "enrich_extract_duration_s", "enrich_count",
                  "enrich_model", "enrich_log"]) is not None

    # ── Extraction quarantine ────────────────────────────────────────────────
    #
    # A failed extraction is never cached (that would record "0 communities"
    # permanently), so without a memory of failures a page that fails the same
    # way every time is re-attempted by every run against every provider. These
    # three methods are that memory; see `db.bump_extract_failure`.

    def note_extract_failure(self, url: str, fingerprint: str,
                             error: str | None = None) -> int:
        return bump_extract_failure(self.db_path, _url_hash(url), fingerprint,
                                    url=url, error=error)

    def clear_extract_failure(self, url: str, fingerprint: str | None = None) -> None:
        clear_extract_failure(self.db_path, _url_hash(url), fingerprint)

    def extract_failure_counts(self, fingerprint: str) -> dict[str, int]:
        return get_extract_failure_counts(self.db_path, fingerprint)

    @staticmethod
    def url_hash(url: str) -> str:
        """The key `quarantined_hashes` returns, for callers holding urls."""
        return _url_hash(url)

    def get_entry(self, url_hash: str) -> dict | None:
        return load_cache_page(self.db_path, url_hash)

    def delete_entry(self, url_hash: str) -> bool:
        return delete_cache_page(self.db_path, url_hash)

    def clear_person_extracted(self) -> int:
        count = clear_person_cache(self.db_path)
        log.info("person_cache_cleared", updated=count)
        return count
