"""Deeper search where the city is large, and no fetch slots for dead domains.

Every city got 36 topics × five fetched pages: London 62 communities, Lagos 31
(2026-10-23). London's top fetched domains were visitlondon.com, Wikipedia and
timeout.com — none ever yielded a community.
"""
import json
from pathlib import Path

from scraper.db import (get_fully_processed_pairs, get_low_yield_domains,
                        get_search_cache_queries, init_db, save_search_cache)
from scraper.fetch import _is_blocked
from scraper.pipeline import CityConfig, PipelineConfig, _pages_for, district_queries


def _cfg(db: Path) -> PipelineConfig:
    from scraper.config import load_config
    return load_config(db)[2]


def test_tourism_portals_match_by_pattern_not_by_name():
    pats = ["re:^(www\\.)?visit[a-z-]*\\.", "timeout.com"]
    assert _is_blocked("https://www.visitlondon.com/things", pats)
    assert _is_blocked("https://visitberlin.de/x", pats)
    assert _is_blocked("https://www.timeout.com/london", pats)
    assert not _is_blocked("https://revisited.org/", pats)
    assert not _is_blocked("https://choir.example/", pats)


def test_low_yield_domains_are_measured_from_the_cache(tmp_path):
    import sqlite3
    db = tmp_path / "t.db"
    init_db(db)
    with sqlite3.connect(db) as c:
        for i in range(16):
            c.execute("INSERT INTO cache_pages(url_hash,url,city,topic,domain,extract_fingerprint,"
                      "records_count,data) VALUES(?,?,?,?,?,?,?,'{}')",
                      (f"d{i}", f"https://dead.example/{i}", "X", "choir", "dead.example", "fp", 0))
            c.execute("INSERT INTO cache_pages(url_hash,url,city,topic,domain,extract_fingerprint,"
                      "records_count,data) VALUES(?,?,?,?,?,?,?,'{}')",
                      (f"l{i}", f"https://live.example/{i}", "X", "choir", "live.example", "fp",
                       1 if i == 0 else 0))
    assert get_low_yield_domains(db, 15) == {"dead.example"}


def test_districts_add_one_query_each_and_more_fetch_slots(tmp_path):
    london = CityConfig(name="London", locale="en", search_variants=["London"],
                        search_districts=["Hackney", "Camden"])
    plain = CityConfig(name="Bath", locale="en", search_variants=["Bath"])
    cfg = _cfg(tmp_path / "t.db")
    assert district_queries(london, ["choir", "chorus"]) == [
        "choir Hackney London", "choir Camden London"]
    assert district_queries(plain, ["choir"]) == []
    assert _pages_for(plain, cfg) == cfg.search_max_pages
    assert _pages_for(london, cfg) == cfg.search_max_pages + 4


def test_a_deep_pair_is_not_done_on_its_first_five_pages(tmp_path):
    """Judged on five URLs a deepened pair read as done while the rest of its
    fetched pages waited unextracted."""
    import hashlib
    import sqlite3
    db = tmp_path / "t.db"
    init_db(db)
    urls = [f"https://c{i}.example/" for i in range(8)]
    save_search_cache(db, "London", "choir", urls, ["choir London", "choir Hackney London"])
    with sqlite3.connect(db) as c:
        for i, u in enumerate(urls):
            h = hashlib.sha256(u.encode()).hexdigest()[:16]
            c.execute("INSERT INTO cache_pages(url_hash,url,city,topic,scraped_at,"
                      "extract_fingerprint,records_count,data) VALUES(?,?,?,?,?,?,?,?)",
                      (h, u, "London", "choir", "2026-10-23", "fp" if i < 5 else None,
                       1 if i < 5 else None, json.dumps({"records": []})))
    assert ("London", "choir") in get_fully_processed_pairs(db, "fp", max_pages=5)
    assert ("London", "choir") not in get_fully_processed_pairs(
        db, "fp", max_pages=5, max_pages_by_city={"London": 8})
    assert "choir Hackney London" in get_search_cache_queries(db, "London", "choir")


def test_the_pilot_cities_ship_with_districts():
    from scraper.config import load_config
    cities = {c.name: c for c in load_config(Path("/nonexistent.db"))[0]}
    for name in ("London", "New York", "Lagos", "Beijing"):
        assert cities[name].search_districts
