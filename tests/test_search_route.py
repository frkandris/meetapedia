from pathlib import Path
from scraper.db import init_db
from scraper.store import save_results
from scraper.models import CommunityRecord
from scraper.pipeline import CityConfig
from scraper.web import app as web_app
from scraper.web.state import app_state
from fastapi.testclient import TestClient


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def test_search_route_empty_query(tmp_path):
    db = _db(tmp_path)
    old_db = app_state.db_path
    try:
        app_state.db_path = db
        resp = TestClient(web_app.app).get("/kereses")
        assert resp.status_code == 200
        assert 'action="/kereses"' in resp.text
    finally:
        app_state.db_path = old_db


def test_search_route_returns_community(tmp_path):
    db = _db(tmp_path)
    r = CommunityRecord(
        name="Budapest Futók", topic="running", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
    )
    save_results("Budapest", "running", [r], db)
    save_results("Berlin", "running", [CommunityRecord(
        name="Berlin Futók", topic="running", city="Berlin", locale="de",
        source_url="https://b.test", extracted_at="2026-01-01T00:00:00+00:00")], db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [
            CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
            CityConfig(name="Berlin", country="Germany", locale="de", search_variants=[])]
        resp = TestClient(web_app.app).get("/kereses?q=Fut%C3%B3k",
                                           headers={"host": "kozossegek.com"})
        assert resp.status_code == 200
        assert "Budapest Futók" in resp.text
        # kozossegek.com serves Hungarian cities only; a Berlin result would
        # link to a page that bounces to the home page.
        assert "Berlin Futók" not in resp.text
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities
