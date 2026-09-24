"""Undecoded Brotli bodies were cached as page text (2026-09-24: ~28% of pages).

httpx decodes Brotli only when the `brotli` package is installed; it was not,
yet the fetcher offered `br`. html2text then accepted the bytes as text.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

from scraper.cache import CacheManager
from scraper.db import init_db, save_search_cache
from scraper.fetch import _HEADERS, _extract_text, looks_undecoded

NOISE = "�\x07k��Z\x13�" * 200
PAGE = "<html><body><p>" + "A Pécsi Futókör minden kedden fut a Mecsekben. " * 10 + "</p></body></html>"


def test_the_fetcher_does_not_offer_an_encoding_it_cannot_decode():
    offered = _HEADERS["Accept-Encoding"].replace(" ", "").split(",")
    if importlib.util.find_spec("brotli") is None and importlib.util.find_spec("brotlicffi") is None:
        assert "br" not in offered


def test_binary_noise_is_refused_before_either_extractor_sees_it():
    assert looks_undecoded(NOISE)
    assert not looks_undecoded(PAGE)
    assert _extract_text(NOISE) is None
    assert _extract_text(PAGE)


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "repair_undecoded_pages.py"
    spec = importlib.util.spec_from_file_location("repair_undecoded_pages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_repair_forgets_noise_pages_and_reopens_their_pairs(tmp_path):
    db = tmp_path / "repair.db"
    init_db(db)
    cache = CacheManager(db)
    cache.save_scraped("https://noise.test/", NOISE, "Pécs", "running")
    cache.save_scraped("https://good.test/", PAGE, "Pécs", "running")
    save_search_cache(db, "Pécs", "running", ["https://noise.test/", "https://good.test/"], ["q"])
    save_search_cache(db, "Győr", "running", ["https://good.test/"], ["q"])
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE search_cache SET collected_at='2026-09-01'")

    repair = _load_script()
    conn = repair._connect(db)
    pages = repair.find_undecoded(conn)
    assert list(pages.values()) == ["https://noise.test/"]
    pairs = repair.affected_pairs(conn, set(pages.values()))
    assert pairs == [("Pécs", "running")]
    repair.apply(conn, pages, pairs)

    assert cache.get_scraped("https://noise.test/") is None
    assert cache.get_scraped("https://good.test/") == PAGE
    rows = dict(conn.execute("SELECT city, collected_at FROM search_cache"))
    assert rows == {"Pécs": None, "Győr": "2026-09-01"}
    blob = json.loads(conn.execute(
        "SELECT data FROM cache_pages WHERE url='https://noise.test/'").fetchone()[0])
    assert "raw_text" not in blob and "records" not in blob
