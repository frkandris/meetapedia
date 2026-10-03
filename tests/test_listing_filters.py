from pathlib import Path
from scraper.db import init_db, upsert_persons, upsert_venues
from scraper.models import PersonRecord, VenueRecord
from scraper.pipeline import CityConfig
from scraper.web import app as web_app
from scraper.web.state import app_state
from fastapi.testclient import TestClient

from tests.people_seed import seed_groups


def _db(tmp_path: Path) -> Path:
    p = tmp_path / "scraper.db"
    init_db(p)
    return p


def _cities():
    return [
        CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
        CityConfig(name="Debrecen", country="Hungary", locale="hu", search_variants=[]),
    ]


def _setup_persons(db):
    upsert_persons(db, [
        PersonRecord(name="Kovács János", role="leader", city="Budapest", topic="running",
                     community_name="Futók", source_url="https://a.test",
                     extracted_at="2026-01-01T00:00:00+00:00").model_dump(),
        PersonRecord(name="Nagy Éva", role="organizer", city="Budapest", topic="cycling",
                     community_name="Kerékpárosok", source_url="https://a.test",
                     extracted_at="2026-01-01T00:00:00+00:00").model_dump(),
        PersonRecord(name="Szabó Péter", role="coach", city="Debrecen", topic="running",
                     community_name="Futók DE", source_url="https://a.test",
                     extracted_at="2026-01-01T00:00:00+00:00").model_dump(),
    ])
    seed_groups(db)


def test_people_city_filter(tmp_path):
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        resp = TestClient(web_app.app).get("/emberek?city=Budapest")
        assert resp.status_code == 200
        assert "Kovács János" in resp.text
        assert "Szabó Péter" not in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_people_empty_until_city_selected(tmp_path):
    # No city → no person list (avoids dumping every city/person); city options present.
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        resp = TestClient(web_app.app).get("/emberek")
        assert resp.status_code == 200
        assert "Kovács János" not in resp.text      # not listed until a city is picked
        assert "Budapest" in resp.text and "Debrecen" in resp.text  # selector options
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_people_filter_options_from_full_set(tmp_path):
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        resp = TestClient(web_app.app).get("/emberek?city=Budapest")
        # Debrecen must still appear as a selector option even when Budapest is chosen
        assert "Debrecen" in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_people_page_search_structure(tmp_path):
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        resp = TestClient(web_app.app).get("/emberek?city=Budapest")
        # The bespoke input was replaced by the shared A-Z + free-text bar
        # (templates/_listing_filter.html), auto-wired by /static/js/listing.js.
        assert "data-mp-filter" in resp.text
        assert 'data-target="#people-list"' in resp.text
        assert "data-mp-filter-az" in resp.text
        assert "data-name=" in resp.text
        assert "data-city-section" in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_people_no_role_label_on_cards(tmp_path):
    # Role filter/label removed — only 'leader' exists, so it adds no signal.
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        # Szabó Péter is a "coach" → "edző", a token that appears nowhere in the
        # page chrome (title/og_desc), so its absence proves the role badge is gone.
        resp = TestClient(web_app.app).get("/emberek?city=Debrecen")
        assert "Szabó Péter" in resp.text
        assert "edző" not in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_people_url_localized_per_site(tmp_path):
    db = _db(tmp_path)
    _setup_persons(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        c = TestClient(web_app.app)
        # meetapedia: /emberek → 301 /people
        r = c.get("/emberek?city=Budapest", headers={"host": "meetapedia.com"}, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/people?city=Budapest"
        # kozossegek: /people → 301 /emberek
        r = c.get("/people", headers={"host": "kozossegek.com"}, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == "/emberek"
        # meetapedia serves /people directly
        r = c.get("/people?city=Budapest", headers={"host": "meetapedia.com"}, follow_redirects=False)
        assert r.status_code == 200 and "Kovács János" in r.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_map_url_localized_per_site():
    c = TestClient(web_app.app)
    r = c.get("/terkep", headers={"host": "meetapedia.com"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/map"
    r = c.get("/map", headers={"host": "kozossegek.com"}, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/terkep"
    r = c.get("/map", headers={"host": "meetapedia.com"}, follow_redirects=False)
    assert r.status_code == 200
    r = c.get("/terkep", headers={"host": "kozossegek.com"}, follow_redirects=False)
    assert r.status_code == 200


def test_venues_page_has_name_search_structure(tmp_path):
    db = _db(tmp_path)
    upsert_venues(db, [
        VenueRecord(name="Müpa Budapest", city="Budapest", locale="hu",
                    source_url="https://mupa.hu", extracted_at="2026-01-01T00:00:00+00:00",
                    welcomed_topics=["music"]).model_dump(),
    ])
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[])]
        resp = TestClient(web_app.app).get("/helyszinek")
        assert resp.status_code == 200
        assert "data-mp-filter" in resp.text
        assert 'data-target="#venue-list"' in resp.text
        assert "data-mp-filter-az" in resp.text
        assert "data-name=" in resp.text
        # No group selector any more: the unfiltered page is a flat city index.
        # listing.js hides a group holding no visible child, so an element that
        # is both the group and the item hides itself on the first keystroke.
        assert "data-group" not in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_hub_pages_have_unique_intro_copy(tmp_path):
    from scraper.pipeline import TopicConfig
    db = _db(tmp_path)
    _setup_persons(db)  # not needed, but keeps db warm
    from scraper.models import CommunityRecord
    from scraper.store import save_results
    save_results("Budapest", "running", [CommunityRecord(
        name="Budapesti Futók", topic="running", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
        description="Heti közösségi futás.")], db)
    old_db, old_cities, old_topics = app_state.db_path, app_state.cities, app_state.topics
    try:
        app_state.db_path = db
        app_state.cities = _cities()
        app_state.topics = [TopicConfig(name="running", search_terms={"hu": ["futás"]})]
        c = TestClient(web_app.app)
        # city+topic hub: unique intro sentence
        assert "Böngéssz" in c.get("/budapest/running").text
        # city hub: unique intro sentence
        assert "Fedezd fel" in c.get("/budapest").text
    finally:
        app_state.db_path, app_state.cities, app_state.topics = old_db, old_cities, old_topics


def test_cities_page_splits_linked_and_pending(tmp_path):
    """Cities with content are server-rendered as links; empty ones ship as JSON.

    The page carries thousands of cities and the Tailwind CDN JIT scans the whole
    initial DOM before first paint, so server-rendering every empty city cost
    ~1.3 MB of markup. Empty cities are non-links either way (see the SEO
    thin-page rules), so nothing crawlable is lost.
    """
    import json
    import re

    from scraper.models import CommunityRecord
    from scraper.store import save_results

    db = _db(tmp_path)
    save_results("Budapest", "running", [CommunityRecord(
        name="Futóklub", topic="running", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
        description="Heti futás a Duna-parton.",
    )], db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [
            CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
            CityConfig(name="Leányfalu", country="Hungary", locale="hu", search_variants=[]),
            CityConfig(name="Szentendre", country="Hungary", locale="hu", search_variants=[]),
        ]
        resp = TestClient(web_app.app).get("/varosok")
        assert resp.status_code == 200

        # Budapest has a community → a real <a> in the server HTML.
        assert 'data-name="Budapest"' in resp.text
        assert '<a href="/budapest" data-name="Budapest"' in resp.text

        # The empty ones travel as JSON, not markup.
        payload = json.loads(
            re.search(r'id="pending-cities">(.*?)</script>', resp.text, re.S).group(1))
        assert {c["n"] for c in payload} == {"Leányfalu", "Szentendre"}
        assert 'data-name="Leányfalu"' not in resp.text
        # Non-ASCII survives the compact JSON encoding.
        assert "Leányfalu" in resp.text

        # The client-side pass must tell the filter to re-read the DOM.
        assert "MpListFilter.rescan()" in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities


def test_cities_page_omits_pending_block_when_everything_has_content(tmp_path):
    from scraper.models import CommunityRecord
    from scraper.store import save_results

    db = _db(tmp_path)
    save_results("Budapest", "running", [CommunityRecord(
        name="Futóklub", topic="running", city="Budapest", locale="hu",
        source_url="https://a.test", extracted_at="2026-01-01T00:00:00+00:00",
        description="Heti futás a Duna-parton.",
    )], db)
    old_db, old_cities = app_state.db_path, app_state.cities
    try:
        app_state.db_path = db
        app_state.cities = [
            CityConfig(name="Budapest", country="Hungary", locale="hu", search_variants=[]),
        ]
        resp = TestClient(web_app.app).get("/varosok")
        assert "pending-cities" not in resp.text
    finally:
        app_state.db_path = old_db
        app_state.cities = old_cities
