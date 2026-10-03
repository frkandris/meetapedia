from pathlib import Path

from fastapi.testclient import TestClient

from tests.people_seed import seed_groups

from scraper.db import init_db, upsert_persons, upsert_venues
from scraper.models import PersonRecord, VenueRecord
from scraper.pipeline import CityConfig, TopicConfig
from scraper.web import app as web_app
from scraper.web.state import app_state


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def test_city_page_omits_venue_and_person_collections(tmp_path):
    db = _db(tmp_path)
    v = VenueRecord(
        name="Müpa Budapest", city="Budapest", locale="hu",
        source_url="https://mupa.hu", extracted_at="2026-01-01T00:00:00+00:00",
        welcomed_topics=["music"],
    )
    upsert_venues(db, [v.model_dump()])
    p = PersonRecord(
        name="Kovács János", role="leader", city="Budapest", topic="running",
        community_name="Futók", source_url="https://a.test",
        extracted_at="2026-01-01T00:00:00+00:00",
    )
    upsert_persons(db, [p.model_dump()])
    seed_groups(db)

    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
        resp = TestClient(web_app.app).get("/budapest")
        assert resp.status_code == 200
        assert "/budapest/helyszin/mupa-budapest" not in resp.text
        assert "/budapest/ember/kovacs-janos" not in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_city_topic_page_shows_relevant_venue(tmp_path):
    db = _db(tmp_path)
    v = VenueRecord(
        name="Müpa Budapest", city="Budapest", locale="hu",
        source_url="https://mupa.hu", extracted_at="2026-01-01T00:00:00+00:00",
        welcomed_topics=["music"],
    )
    upsert_venues(db, [v.model_dump()])

    old_db, old_cities, old_topics = app_state.db_path, app_state.cities, app_state.topics
    try:
        app_state.db_path = db
        app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
        app_state.topics = [TopicConfig(name="music", search_terms={"hu": ["zene"]})]
        resp = TestClient(web_app.app).get("/budapest/zene")
        assert resp.status_code == 200
        assert "/budapest/helyszin/mupa-budapest" in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities
        app_state.topics = old_topics


def test_people_listing_deduplicates_persons(tmp_path):
    db = _db(tmp_path)
    for community in ["Futók", "Kerékpárosok"]:
        p = PersonRecord(
            name="Kovács János", role="leader", city="Budapest", topic="running",
            community_name=community, source_url="https://a.test",
            extracted_at="2026-01-01T00:00:00+00:00",
        )
        upsert_persons(db, [p.model_dump()])
        seed_groups(db)

    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
        resp = TestClient(web_app.app).get("/emberek?city=Budapest")
        assert resp.status_code == 200
        assert resp.text.count("/budapest/ember/kovacs-janos") == 1
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities
