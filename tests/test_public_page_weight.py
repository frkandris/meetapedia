"""Public listing pages must not render the whole database.

Measured live on 2026-08-21: `/helyszinek` was **15.5 MB and took 34 seconds**,
rendering all 7,676 venues as cards, with the venue query and the render both
on the event loop — so one request to it, from a visitor or from Googlebot,
stalled every other request on the site for half a minute. The site losing
minutes several times a day had been read as a deploy problem all week.

`/emberek` already had the answer in its own docstring ("Empty by default — the
person list appears only after a city is picked (avoids dumping every
city/person)"). The venues page was the one place the lesson was not applied.
"""
import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scraper.db import init_db, upsert_venues
from scraper.pipeline import CityConfig
from scraper.web import app as web_app
from scraper.web.state import app_state

KOZ = {"host": "kozossegek.com"}
_CITIES = 40
_PER_CITY = 25


@pytest.fixture()
def venue_client(tmp_path: Path):
    db = tmp_path / "scraper.db"
    init_db(db)
    cities = [f"Varos{i:03d}" for i in range(_CITIES)]
    upsert_venues(db, [
        {"name": f"{ci} Helyszin {n}", "city": ci,
         "address": f"{ci} utca {n}.", "welcomed_topics": ["music"]}
        for ci in cities for n in range(_PER_CITY)
    ])
    old_db, old_cities = app_state.db_path, app_state.cities
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name=ci, country="Hungary", locale="hu", search_variants=[ci])
        for ci in cities
    ]
    try:
        yield TestClient(web_app.app)
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities


def test_the_venue_index_lists_cities_not_every_venue(venue_client):
    r = venue_client.get("/helyszinek", headers=KOZ)
    assert r.status_code == 200
    # Every city is reachable...
    assert "Varos000" in r.text
    assert "Varos039" in r.text
    # ...and not one of the 1,000 venue cards is rendered.
    assert "Helyszin 7" not in r.text


def test_a_city_still_shows_its_venues(venue_client):
    """The cards did not disappear — they moved one click away."""
    r = venue_client.get("/helyszinek?city=Varos003", headers=KOZ)
    assert r.status_code == 200
    assert "Varos003 Helyszin 7" in r.text
    assert "Varos004 Helyszin 7" not in r.text


def test_the_index_links_to_each_city_list(venue_client):
    r = venue_client.get("/helyszinek", headers=KOZ)
    assert "/helyszinek?city=Varos003" in r.text


def test_the_index_stays_small_as_venues_grow(venue_client):
    """1,000 venues here; production has 7,676 and was shipping 15.5 MB."""
    r = venue_client.get("/helyszinek", headers=KOZ)
    assert len(r.text) < 200_000


def test_the_listing_routes_read_the_database_off_the_event_loop():
    """A table scan on the loop is an outage for every concurrent request."""
    src = Path("scraper/web/app.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    scans = {"get_all_venues", "get_all_persons"}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name not in ("public_venues", "_render_people"):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            fn = call.func
            if isinstance(fn, ast.Name) and fn.id in scans:
                pytest.fail(f"{node.name} calls {fn.id} directly; use asyncio.to_thread")


def test_the_submit_form_offers_only_this_site_s_cities(tmp_path: Path):
    """A Swedish community submitted to the HU site lands where nobody sees it."""
    db = tmp_path / "scraper.db"
    init_db(db)
    old_db, old_cities = app_state.db_path, app_state.cities
    app_state.db_path = db
    app_state.cities = [
        CityConfig(name="Budapest", country="Hungary", locale="hu",
                   search_variants=["Budapest"]),
        CityConfig(name="Stockholm", country="Sweden", locale="sv",
                   search_variants=["Stockholm"]),
    ]
    try:
        client = TestClient(web_app.app)
        hu = client.get("/kozosseg-bekuldes", headers=KOZ).text
        assert 'value="Budapest"' in hu
        assert 'value="Stockholm"' not in hu
        intl = client.get("/submit-community", headers={"host": "meetapedia.com"}).text
        assert 'value="Stockholm"' in intl
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities


def test_the_filter_has_no_self_grouping(venue_client):
    """listing.js hides a group holding no visible child.

    An element that is both the group and the item finds nothing inside itself
    and hides on the first keystroke, emptying the page.
    """
    html = venue_client.get("/helyszinek", headers=KOZ).text
    assert "data-group" not in html
    assert 'data-name="Varos003"' in html


def test_the_city_link_keeps_the_topic_filter(venue_client):
    """From /helyszinek?topic=…, picking a city must not widen the result."""
    from scraper.db import upsert_venues

    upsert_venues(app_state.db_path, [{
        "name": "Varos003 Jazzklub", "city": "Varos003",
        "address": "Fo ter 1.", "welcomed_topics": ["jazz"],
    }])
    html = venue_client.get("/helyszinek?topic=jazz", headers=KOZ).text
    assert "/helyszinek?city=Varos003&amp;topic=jazz" in html or \
           "/helyszinek?city=Varos003&topic=jazz" in html
    # Unfiltered, the link carries no topic at all.
    plain = venue_client.get("/helyszinek", headers=KOZ).text
    assert "topic=" not in plain.split('href="/helyszinek?city=Varos003')[1][:40]


def test_a_site_wide_topic_page_samples_cities_instead_of_rendering_all(tmp_path):
    """`/felfedezes/vallas` was 4.9 MB (2026-09-24): every record of every city
    in the visitor's country and three more, one query per city.
    """
    from scraper.models import CommunityRecord
    from scraper.store import save_results

    db = tmp_path / "scraper.db"
    init_db(db)
    cities = [f"Varos{i:03d}" for i in range(_CITIES)]
    for ci in cities:
        save_results(ci, "music", [CommunityRecord(
            name=f"{ci} Kórus {n}", city=ci, topic="music", locale="hu",
            description="Heti próbák, új tagokat várunk.",
            source_url=f"https://{ci}.test/{n}", extracted_at="2026-09-24T00:00:00Z")
            for n in range(_PER_CITY)], db)
    old_db, old_cities = app_state.db_path, app_state.cities
    app_state.db_path = db
    app_state.cities = [CityConfig(name=ci, country="Hungary", locale="hu",
                                   search_variants=[ci]) for ci in cities]
    try:
        from scraper.web.app import _topic_url_slug
        r = TestClient(web_app.app).get(f"/felfedezes/{_topic_url_slug('music', 'hu')}",
                                        headers={**KOZ, "CF-IPCountry": "HU"})
    finally:
        app_state.db_path, app_state.cities = old_db, old_cities
    assert r.status_code == 200
    rendered = sum(1 for ci in cities for n in range(_PER_CITY) if f"{ci} Kórus {n}<" in r.text)
    assert 0 < rendered <= web_app._EXPLORE_TOPIC_CITIES * 10


def test_city_topic_counts_come_from_one_query_and_skip_hidden(tmp_path, monkeypatch):
    from scraper.db import _community_record_key, set_community_hidden
    from scraper.models import CommunityRecord
    from scraper.pipeline import TopicConfig
    from scraper.store import save_results

    db = tmp_path / "scraper.db"
    init_db(db)
    rec = lambda n: CommunityRecord(name=n, city="Pécs", topic="music", locale="hu",  # noqa: E731
                                    source_url=f"https://x.test/{n}",
                                    extracted_at="2026-09-24T00:00:00Z")
    save_results("Pécs", "music", [rec("Kórus A"), rec("Kórus B")], db)
    set_community_hidden(db, _community_record_key("Kórus B", "Pécs", "music"), True)
    monkeypatch.setattr(app_state, "db_path", db)
    monkeypatch.setattr(app_state, "topics", [TopicConfig("music", {}), TopicConfig("running", {})])
    monkeypatch.setattr(web_app, "_load_communities",
                        lambda *a, **k: pytest.fail("must not load records to count them"))
    r = TestClient(web_app.app).get("/api/city-topics?city=Pécs", headers=KOZ)
    assert r.json() == {"music": 1, "running": 0}
