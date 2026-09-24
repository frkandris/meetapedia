"""Sitemap <lastmod> is a *trustworthy* freshness signal.

The community upsert only advances `updated_at` on a real content change, so a
re-extraction that produces identical data does not churn every page's lastmod
(the 2026-06 corpus-instability lesson). The sitemap emits that date per
community URL.
"""
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from scraper.db import init_db
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig
from scraper.store import save_results
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}


def _rec(desc, extracted_at="2026-01-01T00:00:00+00:00"):
    return CommunityRecord(
        name="Zenei Kör", topic="music", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at=extracted_at,
        description=desc,
    )


def _updated_at(db: Path) -> str:
    return sqlite3.connect(db).execute("SELECT updated_at FROM communities").fetchone()[0]


def test_updated_at_stable_on_unchanged_content(tmp_path: Path):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [_rec("Aktív zenei közösség Budapesten.")], db)
    first = _updated_at(db)
    # Re-extract: identical content but a FRESH extracted_at — updated_at must NOT
    # advance (extracted_at is excluded from the content fingerprint).
    save_results("Budapest", "music",
                 [_rec("Aktív zenei közösség Budapesten.", extracted_at="2026-07-27T10:00:00+00:00")], db)
    assert _updated_at(db) == first
    # Real change — updated_at advances.
    save_results("Budapest", "music", [_rec("Új leírás, más tartalom.")], db)
    assert _updated_at(db) != first


def test_sitemap_emits_lastmod_for_community(tmp_path: Path):
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [_rec("Aktív zenei közösség Budapesten.")], db)
    old_db, old_cities = app_state.db_path, app_state.cities
    app_state.db_path = db
    app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
    try:
        xml = TestClient(web_app.app).get("/sitemap.xml", headers=KOZ).text
        assert "/budapest/zenei-kor</loc><lastmod>" in xml
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities


def test_sitemap_is_cached_and_built_off_the_event_loop(tmp_path):
    """The sitemap must not be rebuilt per request on the event loop.

    It used to call get_communities() once per city×topic pair. At 3.8K cities
    that measured >30s through the CDN and stalled every other request behind
    it — the site "worked" but crawled. Now: one query, a worker thread, and an
    hour of caching.
    """
    import scraper.web.app as web_app

    src = (Path(web_app.__file__).read_text(encoding="utf-8"))
    assert "asyncio.to_thread(_build_sitemap" in src
    assert "_SITEMAP_CACHE" in src
    # The per-pair query must be gone from the builder.
    builder = src[src.index("def _build_sitemap"):src.index("</urlset>")]
    assert "get_communities(" not in builder
    assert "get_sitemap_communities(" in builder


def test_sitemap_thin_pages_stay_out(tmp_path):
    """Thin pages are noindexed, so listing them would contradict the policy."""
    from scraper.db import get_sitemap_communities, init_db
    from scraper.models import CommunityRecord
    from scraper.store import save_results

    db = tmp_path / "s.db"
    init_db(db)
    save_results("Budapest", "running", [
        CommunityRecord(name="Leírt Klub", topic="running", city="Budapest",
                        locale="hu", source_url="https://a.test",
                        extracted_at="2026-01-01T00:00:00+00:00",
                        description="Heti futás a Duna-parton."),
        CommunityRecord(name="Néma Klub", topic="running", city="Budapest",
                        locale="hu", source_url="https://b.test",
                        extracted_at="2026-01-01T00:00:00+00:00"),
    ], db)
    rows = get_sitemap_communities(db)[("Budapest", "running")]
    by_name = {r["name"]: r["thin"] for r in rows}
    assert by_name["Leírt Klub"] is False
    assert by_name["Néma Klub"] is True


def test_a_sitemap_over_the_limit_becomes_an_index_of_parts(tmp_path, monkeypatch):
    """The protocol caps a file at 50,000 URLs and a file over it is rejected
    whole; meetapedia.com served 64,666 in one `<urlset>` (2026-09-24).
    """
    db = tmp_path / "scraper.db"
    init_db(db)
    save_results("Budapest", "music", [_rec("Aktív zenei közösség Budapesten.")], db)
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "cities", [
        CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])])
    monkeypatch.setattr(web_app, "_SITEMAP_MAX_URLS", 5)
    client = TestClient(web_app.app)

    index = client.get("/sitemap.xml", headers=KOZ).text
    assert "<sitemapindex" in index
    parts = [loc.split("</loc>")[0] for loc in index.split("<loc>")[1:]]
    assert parts[0] == "https://kozossegek.com/sitemap-1.xml"

    urls = []
    for n in range(1, len(parts) + 1):
        response = client.get(f"/sitemap-{n}.xml", headers=KOZ)
        assert response.status_code == 200 and "<urlset" in response.text
        chunk = response.text.count("<url>")
        assert chunk <= 5
        urls.append(chunk)
    assert sum(urls) > 5
    assert "/budapest/zenei-kor</loc>" in "".join(
        client.get(f"/sitemap-{n}.xml", headers=KOZ).text for n in range(1, len(parts) + 1))
    assert client.get(f"/sitemap-{len(parts) + 1}.xml", headers=KOZ).status_code == 404
